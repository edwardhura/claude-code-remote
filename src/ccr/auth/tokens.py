"""JWT signed-URL token mint / verify (HS256, stateless, 30-min TTL by default).

Two token kinds are issued:

* :attr:`TokenKind.VIEWER` — read-only access to the multi-tab session viewer.
* :attr:`TokenKind.PREVIEW` — single-port localhost reverse-proxy access; the
  ``payload`` carries ``{"port": <int>}`` and the proxy must check it against
  the URL port.

The module is deliberately small: tokens carry their own expiry and signature,
so verification touches no database. The HMAC secret comes from
:attr:`ccr.config.Settings.jwt_secret` (which is enforced ≥ 32 chars at
``Settings`` construction). Algorithm is locked to HS256 — ``"none"`` and
asymmetric variants are rejected by passing ``algorithms=["HS256"]`` explicitly
to :func:`jwt.decode`.

Public surface:

* :class:`TokenKind` — string enum (``viewer`` / ``preview``).
* :class:`TokenError` — raised by :func:`verify` / :func:`verify_kind` on any
  failure (expired, bad signature, malformed claims, wrong kind).
* :class:`VerifiedToken` — dataclass returned by successful verification;
  exposes ``kind``, ``tg_user_id``, ``payload``, ``jti``.
* :func:`mint` — encode a token for ``(kind, tg_user_id, payload)``.
* :func:`verify` — decode + validate a token; returns :class:`VerifiedToken`.
* :func:`verify_kind` — :func:`verify` plus a kind check.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import jwt

if TYPE_CHECKING:
    from ccr.config import Settings


_ALGORITHM = "HS256"


class TokenKind(StrEnum):
    """Discriminator embedded in every minted token's ``kind`` claim."""

    VIEWER = "viewer"
    PREVIEW = "preview"


class TokenError(Exception):
    """Raised on any token verification failure (expired, tampered, malformed)."""


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """A successfully decoded token. Exactly the fields callers need."""

    kind: TokenKind
    tg_user_id: int
    payload: dict[str, Any]
    jti: str


def mint(
    kind: TokenKind,
    tg_user_id: int,
    payload: dict[str, Any],
    settings: Settings,
    *,
    _now: int | None = None,
) -> str:
    """Return an HS256-signed JWT string for ``(kind, tg_user_id, payload)``.

    The ``payload`` dict is JSON-encoded into the ``payload`` claim verbatim,
    so callers can put whatever they like in there as long as it is
    JSON-serializable. ``_now`` is a test seam — production callers leave it
    as ``None`` and get the current epoch.
    """
    iat = int(time.time()) if _now is None else int(_now)
    exp = iat + settings.token_ttl_seconds
    claims: dict[str, Any] = {
        "sub": str(tg_user_id),
        "kind": str(kind.value),
        "payload": json.dumps(payload),
        "iat": iat,
        "exp": exp,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm=_ALGORITHM,
    )


def verify(token: str, settings: Settings) -> VerifiedToken:
    """Decode ``token``, validate signature and ``exp``, return a :class:`VerifiedToken`.

    Raises :class:`TokenError` on any failure — caller should catch only that.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
        )
    except jwt.PyJWTError as exc:
        msg = f"token verification failed: {exc}"
        raise TokenError(msg) from exc

    try:
        sub = claims["sub"]
        kind_raw = claims["kind"]
        payload_raw = claims["payload"]
        jti = claims["jti"]
    except KeyError as exc:
        msg = f"token missing required claim: {exc.args[0]!r}"
        raise TokenError(msg) from exc

    try:
        tg_user_id = int(sub)
    except (TypeError, ValueError) as exc:
        msg = f"token 'sub' is not an integer-coercible string: {sub!r}"
        raise TokenError(msg) from exc

    try:
        kind = TokenKind(kind_raw)
    except ValueError as exc:
        msg = f"token 'kind' claim is not a known TokenKind: {kind_raw!r}"
        raise TokenError(msg) from exc

    try:
        payload = json.loads(payload_raw)
    except (TypeError, json.JSONDecodeError) as exc:
        msg = f"token 'payload' claim is not valid JSON: {payload_raw!r}"
        raise TokenError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"token 'payload' claim must decode to a JSON object, got {type(payload).__name__}"
        raise TokenError(msg)

    if not isinstance(jti, str) or not jti:
        msg = "token 'jti' claim must be a non-empty string"
        raise TokenError(msg)

    return VerifiedToken(kind=kind, tg_user_id=tg_user_id, payload=payload, jti=jti)


def verify_kind(token: str, settings: Settings, expected: TokenKind) -> VerifiedToken:
    """Like :func:`verify`, but additionally enforce ``verified.kind == expected``.

    Raises :class:`TokenError` (with both kinds named in the message) on mismatch.
    """
    verified = verify(token, settings)
    if verified.kind != expected:
        msg = f"expected kind {expected.value!r}, got {verified.kind.value!r}"
        raise TokenError(msg)
    return verified


__all__ = [
    "TokenError",
    "TokenKind",
    "VerifiedToken",
    "mint",
    "verify",
    "verify_kind",
]
