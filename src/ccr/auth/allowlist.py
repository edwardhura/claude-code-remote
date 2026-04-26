"""Cheap allowlist predicates used by bot/web middleware and tests.

Both predicates filter on ``revoked_at IS NULL`` so soft-revoked users no
longer pass the gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ccr.db.models import PairedUser

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def is_paired(db: AsyncSession, tg_user_id: int) -> bool:
    """Return ``True`` iff ``tg_user_id`` is in the active allowlist."""
    stmt = select(PairedUser.tg_user_id).where(
        PairedUser.tg_user_id == tg_user_id,
        PairedUser.revoked_at.is_(None),
    )
    return await db.scalar(stmt) is not None


async def is_owner(db: AsyncSession, tg_user_id: int) -> bool:
    """Return ``True`` iff ``tg_user_id`` is the active owner."""
    stmt = select(PairedUser.tg_user_id).where(
        PairedUser.tg_user_id == tg_user_id,
        PairedUser.is_owner.is_(True),
        PairedUser.revoked_at.is_(None),
    )
    return await db.scalar(stmt) is not None


__all__ = ["is_owner", "is_paired"]
