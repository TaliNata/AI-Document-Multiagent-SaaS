"""
Document parsing pipeline — Celery task.

This is the main background task triggered after file upload.
It orchestrates the full flow:

    MinIO (download) → Parser (text) → Chunker (pieces) → Embeddings (vectors) → Qdrant + PostgreSQL

Status transitions: uploaded → parsing → parsed (or → error)
"""

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.worker import celery_app
from app.config import get_settings
from app.services.storage import download_file
from app.services.parser import extract_text
from app.services.chunker import chunk_text

settings = get_settings()
logger = logging.getLogger(__name__)


def _get_sync_session():
    """
    Create a synchronous DB session for Celery tasks.

    Celery workers run in a separate process and can't use the async
    session from FastAPI. We use a sync engine + session instead.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = (
        f"postgresql+psycopg2://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )
    engine = create_engine(sync_url)
    return sessionmaker(bind=engine)()


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def parse_document(self, document_id: str) -> dict:
    """
    Parse a document: extract text, split into chunks, generate embeddings.

    Args:
        document_id: UUID string of the document to parse.

    Returns:
        Dict with parsing results (chunk count, character count).

    The task is idempotent — re-running it on the same document
    will delete old chunks and re-create them.
    """
    from app.models.document import Document, DocumentChunk

    db = _get_sync_session()
    doc_uuid = uuid.UUID(document_id)

    try:
        # ── Step 1: Load document metadata ────────────────
        doc = db.execute(
            select(Document).where(Document.id == doc_uuid)
        ).scalar_one_or_none()

        if not doc:
            logger.error(f"Document {document_id} not found")
            return {"error": "Document not found"}

        # Update status → parsing
        doc.status = "parsing"
        db.commit()

        logger.info(f"Parsing document {doc.title} ({doc.mime_type})")

        # ── Step 2: Download file from MinIO ──────────────
        file_bytes = download_file(doc.file_path)
        logger.info(f"Downloaded {len(file_bytes)} bytes from MinIO")

        # ── Step 3: Extract text ──────────────────────────
        text = extract_text(file_bytes, doc.mime_type)

        if not text.strip():
            doc.status = "error"
            doc.metadata_ = {**doc.metadata_, "parse_error": "No text extracted"}
            db.commit()
            return {"error": "No text extracted"}

        logger.info(f"Extracted {len(text)} characters")

        # ── Step 4: Split into chunks ─────────────────────
        chunks = chunk_text(
            text,
            chunk_size=800,
            chunk_overlap=200,
            doc_metadata={
                "doc_type": doc.doc_type,
                "title": doc.title,
            },
        )
        logger.info(f"Created {len(chunks)} chunks")

        # ── Step 5: Generate embeddings + store in Qdrant ─
        stored_chunks = []
        if settings.openai_api_key:
            from app.services.embeddings import store_chunks, generate_embeddings

            chunk_dicts = [
                {"index": c.index, "content": c.content, "metadata": c.metadata}
                for c in chunks
            ]
            chunk_texts = [c.content for c in chunks]

            # Embed in batches of 50 (API rate limits)
            batch_size = 50
            all_embeddings = []
            for i in range(0, len(chunk_texts), batch_size):
                batch = chunk_texts[i : i + batch_size]
                embeddings = generate_embeddings(batch)
                all_embeddings.extend(embeddings)

            stored_chunks = store_chunks(
                chunks=chunk_dicts,
                embeddings=all_embeddings,
                org_id=str(doc.org_id),
                document_id=document_id,
            )
            logger.info(f"Stored {len(stored_chunks)} vectors in Qdrant")
        else:
            logger.warning("OPENAI_API_KEY not set — skipping embeddings")

        # ── Step 6: Save chunks to PostgreSQL ─────────────
        # Delete old chunks (idempotency)
        db.execute(
            DocumentChunk.__table__.delete().where(
                DocumentChunk.document_id == doc_uuid
            )
        )

        # Build a lookup: chunk_index → qdrant_point_id
        qdrant_ids = {sc.chunk_index: sc.qdrant_point_id for sc in stored_chunks}

        for chunk in chunks:
            db_chunk = DocumentChunk(
                document_id=doc_uuid,
                chunk_index=chunk.index,
                content=chunk.content,
                qdrant_point_id=qdrant_ids.get(chunk.index),
                metadata_=chunk.metadata,
            )
            db.add(db_chunk)

        # ── Step 7: Auto-classify and extract entities ────
        import asyncio

        doc_type = "unknown"
        entities = {}

        try:
            from app.agents.analyst_agent import classify_document, extract_entities

            doc_type = asyncio.run(classify_document(text))
            doc.doc_type = doc_type
            logger.info(f"Auto-classified as: {doc_type}")

            entities = asyncio.run(extract_entities(text))
            logger.info(f"Extracted {len(entities)} entity fields")
        except Exception as e:
            logger.warning(f"Classification/extraction failed (non-fatal): {e}")

        # ── Step 8: Update document status ────────────────
        doc.status = "parsed"
        doc.metadata_ = {
            **doc.metadata_,
            "char_count": len(text),
            "chunk_count": len(chunks),
            "has_embeddings": bool(settings.openai_api_key),
            "entities": entities,
        }
        db.commit()

        logger.info(f"Document {doc.title} parsed successfully")
        return {
            "document_id": document_id,
            "status": "parsed",
            "char_count": len(text),
            "chunk_count": len(chunks),
        }

    except Exception as exc:
        db.rollback()
        logger.exception(f"Failed to parse document {document_id}")

        # Update status to error
        try:
            db.execute(
                update(Document)
                .where(Document.id == doc_uuid)
                .values(status="error", metadata_={"parse_error": str(exc)})
            )
            db.commit()
        except Exception:
            pass

        # Retry with exponential backoff
        raise self.retry(exc=exc)

    finally:
        db.close()
