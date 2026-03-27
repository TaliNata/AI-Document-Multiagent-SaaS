"""
PII (Personally Identifiable Information) masking.

Replaces sensitive data with placeholders before sending text to LLM APIs.
After receiving the response, restores placeholders back to real values.

Masked entities:
- ИНН (10 or 12 digits)
- КПП (9 digits)
- ОГРН/ОГРНИП (13 or 15 digits)
- Паспорт (серия + номер)
- Телефон (+7 / 8 formats)
- Email
- Банковский счёт (20 digits)
- СНИЛС

The mask format is deterministic: same input always produces same placeholder.
This way, if "ИНН 7707083893" appears multiple times, it consistently
becomes "[ИНН_1]" everywhere.
"""

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MaskResult:
    """Result of masking — masked text + mapping for restoration."""
    masked_text: str
    mapping: dict[str, str] = field(default_factory=dict)
    count: int = 0


# ─── Patterns ────────────────────────────────────────────────
PATTERNS = [
    # ИНН (10 or 12 digits, often preceded by "ИНН")
    (r"\bИНН\s*[:№]?\s*(\d{10,12})\b", "ИНН", r"\1"),
    (r"\b(\d{10})\b(?=.*(?:ИНН|инн|контрагент))", "ИНН", r"\1"),

    # КПП (9 digits, preceded by "КПП")
    (r"\bКПП\s*[:№]?\s*(\d{9})\b", "КПП", r"\1"),

    # ОГРН (13 digits) / ОГРНИП (15 digits)
    (r"\bОГРН(?:ИП)?\s*[:№]?\s*(\d{13,15})\b", "ОГРН", r"\1"),

    # Паспорт (серия 4 digits + номер 6 digits)
    (r"\b(\d{2}\s?\d{2})\s+(\d{6})\b", "ПАСПОРТ", None),

    # Банковский счёт (20 digits)
    (r"\b(\d{20})\b", "СЧЁТ", r"\1"),

    # СНИЛС (XXX-XXX-XXX XX)
    (r"\b(\d{3}-\d{3}-\d{3}\s?\d{2})\b", "СНИЛС", r"\1"),

    # Телефон (+7 or 8, various formats)
    (r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", "ТЕЛЕФОН", None),

    # Email
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "EMAIL", None),
]

# Standalone digit patterns (more aggressive, use with caution)
INN_STANDALONE = re.compile(r"\b(\d{10}|\d{12})\b")


def mask_pii(text: str) -> MaskResult:
    """
    Replace PII in text with numbered placeholders.

    Returns MaskResult with masked text and a mapping dict
    that can be used to restore the original values.

    Example:
        "ИНН 7707083893" → "[ИНН_1]"
        mapping: {"[ИНН_1]": "7707083893"}
    """
    masked = text
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}

    for pattern, label, group_ref in PATTERNS:
        matches = list(re.finditer(pattern, masked))
        for match in reversed(matches):  # reverse to preserve positions
            original = match.group(0)

            # Skip if already masked
            if original.startswith("[") and original.endswith("]"):
                continue

            # Generate placeholder
            if label not in counters:
                counters[label] = 0
            counters[label] += 1
            placeholder = f"[{label}_{counters[label]}]"

            # Store mapping
            mapping[placeholder] = original

            # Replace in text
            masked = masked[: match.start()] + placeholder + masked[match.end() :]

    count = len(mapping)
    if count:
        logger.info(f"Masked {count} PII entities")

    return MaskResult(masked_text=masked, mapping=mapping, count=count)


def unmask_pii(text: str, mapping: dict[str, str]) -> str:
    """
    Restore masked placeholders back to original values.

    Used after receiving LLM response — the model may reference
    placeholders in its answer, and we need to put real data back.
    """
    result = text
    for placeholder, original in mapping.items():
        result = result.replace(placeholder, original)
    return result
