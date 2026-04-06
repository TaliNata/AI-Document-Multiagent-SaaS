"""
Shared dependencies for all API endpoints.

The key piece is `get_db_with_tenant` — it wraps the raw DB session
and executes SET LOCAL to activate RLS for the current org.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.user import User
from app.utils.security import decode_token

bearer_scheme = HTTPBearer()


# ─── DB session with RLS ──────────────────────────────────────
async def get_db_with_tenant(request: Request):
    """
    Yields an AsyncSession with RLS activated for the current org.

    After SET LOCAL, every query on RLS-enabled tables automatically
    filters by org_id — even if the developer forgets WHERE org_id = ...
    """
    org_id: uuid.UUID | None = getattr(request.state, "org_id", None)

    async with async_session_factory() as session:
        async with session.begin():
            if org_id:
                # SET LOCAL scopes to this transaction only
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :org_id, true)"),
                    {"org_id": str(org_id)},
                )
            yield session


# Type alias for cleaner endpoint signatures
DbSession = Annotated[AsyncSession, Depends(get_db_with_tenant)]


# ─── Current user ─────────────────────────────────────────────
async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    """
    Decodes the JWT, fetches the user from DB, validates they're active.
    Returns the User ORM object.
    """
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    from sqlalchemy import select

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# ─── Role checker ─────────────────────────────────────────────
class RoleRequired:
    """
    Dependency that checks if the current user has one of the allowed roles.

    Usage:
        @router.delete("/...", dependencies=[Depends(RoleRequired("admin"))])
        @router.post("/...", dependencies=[Depends(RoleRequired("admin", "manager"))])
    """

    def __init__(self, *allowed_roles: str):
        self.allowed_roles = set(allowed_roles)

    async def __call__(self, user: CurrentUser):
        if user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not allowed. Required: {self.allowed_roles}",
            )
        return user
