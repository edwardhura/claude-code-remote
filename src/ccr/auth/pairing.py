"""Pairing storage and owner-model operations.

Every public function takes an :class:`AsyncSession` as its first argument and
owns no global state — transaction lifecycle is the caller's responsibility,
except for :func:`approve` and :func:`revoke` which commit the row changes
they make atomically (the database-level partial unique index on
``is_owner=true`` enforces the at-most-one-owner invariant).

Owner-only enforcement for :func:`invite` lives at the CLI/console/bot
layer; these pure functions trust their callers.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from ccr.db.models import PairedUser, PairingCode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ccr.config import Settings


_CODE_COLLISION_RETRIES = 5


class PairingError(Exception):
    """Raised when a pairing operation cannot be completed."""


class CannotRevokeOwnerError(PairingError):
    """Raised when a caller tries to :func:`revoke` the owner."""


def _utcnow() -> datetime:
    """Return ``datetime.now`` in UTC. Indirected so tests can pin the value."""
    return datetime.now(UTC)


def _generate_code() -> str:
    """Return an 8-char uppercase hex pairing code."""
    return secrets.token_hex(4).upper()


async def create_code(  # noqa: PLR0913 — keyword-only knobs, callers pass ≤3 positional
    db: AsyncSession,
    tg_user_id: int,
    tg_username: str | None,
    *,
    settings: Settings | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> PairingCode:
    """Insert and return a fresh :class:`PairingCode`.

    ``ttl_seconds`` overrides ``settings.pairing_code_ttl_seconds``; if neither
    is supplied the function falls back to ``900`` seconds (15 min) to keep the
    function callable from contexts (tests) that have no ``Settings`` handy.

    The 8-char hex code is retried up to 5x on uniqueness collision against
    existing rows in ``pairing_codes`` before giving up with
    :class:`PairingError`.
    """
    if ttl_seconds is None:
        ttl_seconds = settings.pairing_code_ttl_seconds if settings is not None else 900

    issued_at = now or _utcnow()
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    last_error: Exception | None = None
    for _ in range(_CODE_COLLISION_RETRIES):
        candidate = _generate_code()
        existing = await db.scalar(select(PairingCode).where(PairingCode.code == candidate))
        if existing is not None:
            continue
        pc = PairingCode(
            code=candidate,
            tg_user_id=tg_user_id,
            tg_username=tg_username,
            created_at=issued_at,
            expires_at=expires_at,
        )
        db.add(pc)
        try:
            await db.flush()
        except Exception as exc:  # noqa: BLE001 — surface as PairingError after retries
            last_error = exc
            await db.rollback()
            continue
        return pc

    message = "Failed to allocate a unique pairing code after 5 attempts."
    if last_error is not None:
        raise PairingError(message) from last_error
    raise PairingError(message)


async def list_pending(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[PairingCode]:
    """Return all unused, unexpired pairing codes ordered by creation time."""
    cutoff = now or _utcnow()
    stmt = (
        select(PairingCode)
        .where(PairingCode.used_at.is_(None), PairingCode.expires_at > cutoff)
        .order_by(PairingCode.created_at)
    )
    result = await db.execute(stmt)
    return list(result.scalars())


async def list_paired(db: AsyncSession) -> list[PairedUser]:
    """Return every :class:`PairedUser` row, regardless of revocation status.

    Callers that need only the active set can filter on
    ``revoked_at is None`` themselves.
    """
    stmt = select(PairedUser).order_by(PairedUser.approved_at)
    result = await db.execute(stmt)
    return list(result.scalars())


async def get_owner(db: AsyncSession) -> PairedUser | None:
    """Return the active owner, if any.

    Filters on ``revoked_at IS NULL`` so a (hypothetically) revoked owner —
    which the partial unique index would have to allow before normal flow
    re-promotes someone — does not block re-promotion. The CLI cannot
    actually revoke the owner; this guard is defensive.
    """
    stmt = select(PairedUser).where(
        PairedUser.is_owner.is_(True),
        PairedUser.revoked_at.is_(None),
    )
    result: PairedUser | None = await db.scalar(stmt)
    return result


async def approve(
    db: AsyncSession,
    code: str,
    *,
    now: datetime | None = None,
) -> PairedUser:
    """Approve ``code`` and return the resulting :class:`PairedUser`.

    Single transaction:

    1. Look up an unused, unexpired pairing code (else
       :class:`PairingError`).
    2. If no owner exists yet, the new user is auto-promoted
       (``is_owner=True``, ``approved_by_tg_user_id=None``).
    3. Otherwise the new user is a regular paired user
       (``is_owner=False``, ``approved_by_tg_user_id=<owner.tg_user_id>``).
    4. Mark the code used and upsert the paired_user row (clearing
       ``revoked_at`` on a re-pair).
    """
    cutoff = now or _utcnow()

    pc = await db.scalar(
        select(PairingCode).where(
            PairingCode.code == code,
            PairingCode.used_at.is_(None),
            PairingCode.expires_at > cutoff,
        )
    )
    if pc is None:
        message = "Code invalid or expired."
        raise PairingError(message)

    pc.used_at = cutoff
    owner = await get_owner(db)
    is_first = owner is None

    existing = await db.scalar(select(PairedUser).where(PairedUser.tg_user_id == pc.tg_user_id))
    if existing is None:
        user = PairedUser(
            tg_user_id=pc.tg_user_id,
            tg_username=pc.tg_username,
            is_owner=is_first,
            approved_by_tg_user_id=None if is_first else owner.tg_user_id,  # type: ignore[union-attr]
            approved_at=cutoff,
        )
        db.add(user)
    else:
        existing.tg_username = pc.tg_username or existing.tg_username
        existing.is_owner = existing.is_owner or is_first
        if is_first and existing.approved_by_tg_user_id is not None:
            existing.approved_by_tg_user_id = None
        elif not is_first and not existing.is_owner:
            existing.approved_by_tg_user_id = owner.tg_user_id  # type: ignore[union-attr]
        existing.approved_at = cutoff
        existing.revoked_at = None
        user = existing

    await db.commit()
    await db.refresh(user)
    return user


async def revoke(
    db: AsyncSession,
    tg_user_id: int,
    *,
    now: datetime | None = None,
) -> None:
    """Soft-revoke a paired user.

    Refuses to revoke the owner: raises :class:`CannotRevokeOwnerError`. The
    function commits the change inline so callers don't have to.
    """
    user = await db.scalar(select(PairedUser).where(PairedUser.tg_user_id == tg_user_id))
    if user is None:
        message = f"No paired user with tg_user_id={tg_user_id}."
        raise PairingError(message)
    if user.is_owner:
        message = "Cannot revoke owner."
        raise CannotRevokeOwnerError(message)
    user.revoked_at = now or _utcnow()
    await db.commit()


async def invite(
    db: AsyncSession,
    tg_user_id: int,
    label: str | None,
    *,
    now: datetime | None = None,
) -> PairedUser:
    """Pre-approve a Telegram user without an outstanding pairing code.

    Owner-only enforcement is the caller's job (CLI/console/bot layer); this
    pure function trusts its caller. If the target already exists the row is
    refreshed (``revoked_at`` cleared, ``label`` updated when supplied).
    """
    cutoff = now or _utcnow()
    owner = await get_owner(db)
    is_first = owner is None

    existing = await db.scalar(select(PairedUser).where(PairedUser.tg_user_id == tg_user_id))
    if existing is None:
        user = PairedUser(
            tg_user_id=tg_user_id,
            label=label,
            is_owner=is_first,
            approved_by_tg_user_id=None if is_first else owner.tg_user_id,  # type: ignore[union-attr]
            approved_at=cutoff,
        )
        db.add(user)
    else:
        if label is not None:
            existing.label = label
        existing.revoked_at = None
        existing.approved_at = cutoff
        if is_first:
            existing.is_owner = True
            existing.approved_by_tg_user_id = None
        elif not existing.is_owner:
            existing.approved_by_tg_user_id = owner.tg_user_id  # type: ignore[union-attr]
        user = existing

    await db.commit()
    await db.refresh(user)
    return user


__all__ = [
    "CannotRevokeOwnerError",
    "PairingError",
    "approve",
    "create_code",
    "get_owner",
    "invite",
    "list_paired",
    "list_pending",
    "revoke",
]
