"""Pairing, allowlist and (later) JWT helpers.

Pure async functions live in :mod:`ccr.auth.pairing` and
:mod:`ccr.auth.allowlist`. They take an :class:`AsyncSession` first and own no
global state — DB lifecycle is the caller's responsibility.
"""

from __future__ import annotations

from ccr.auth.allowlist import is_owner, is_paired
from ccr.auth.pairing import (
    CannotRevokeOwnerError,
    PairingError,
    approve,
    create_code,
    get_owner,
    invite,
    list_paired,
    list_pending,
    revoke,
)
from ccr.auth.tokens import (
    TokenError,
    TokenKind,
    VerifiedToken,
    mint,
    verify,
    verify_kind,
)

__all__ = [
    "CannotRevokeOwnerError",
    "PairingError",
    "TokenError",
    "TokenKind",
    "VerifiedToken",
    "approve",
    "create_code",
    "get_owner",
    "invite",
    "is_owner",
    "is_paired",
    "list_paired",
    "list_pending",
    "mint",
    "revoke",
    "verify",
    "verify_kind",
]
