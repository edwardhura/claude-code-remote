"""Tests for :mod:`ccr.auth.tokens` — JWT mint/verify and verify_kind."""

from __future__ import annotations

import jwt
import pytest

from ccr.auth import tokens as tokens_module
from ccr.auth.tokens import (
    TokenError,
    TokenKind,
    VerifiedToken,
    mint,
    verify,
    verify_kind,
)
from ccr.config import Settings

VALID_SECRET = "x" * 64


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "PUBLIC_URL": "https://example.com",
            "JWT_SECRET": VALID_SECRET,
            "TOKEN_TTL_SECONDS": 1800,
        },
    )


def test_mint_then_verify_roundtrip_viewer(settings: Settings) -> None:
    token = mint(TokenKind.VIEWER, tg_user_id=42, payload={"foo": "bar"}, settings=settings)

    verified = verify(token, settings)

    assert isinstance(verified, VerifiedToken)
    assert verified.kind == TokenKind.VIEWER
    assert verified.tg_user_id == 42
    assert verified.payload == {"foo": "bar"}
    assert verified.jti
    assert isinstance(verified.jti, str)


def test_minted_jwt_includes_all_six_claims(settings: Settings) -> None:
    token = mint(TokenKind.VIEWER, tg_user_id=7, payload={"a": 1}, settings=settings, _now=1000)

    raw = jwt.decode(token, options={"verify_signature": False})

    assert set(raw.keys()) >= {"sub", "kind", "iat", "exp", "jti", "payload"}
    assert raw["sub"] == "7"
    assert raw["kind"] == "viewer"
    assert raw["iat"] == 1000
    assert raw["exp"] == 1000 + 1800
    assert raw["jti"]


def test_expired_token_rejected(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = mint(TokenKind.VIEWER, tg_user_id=42, payload={}, settings=settings, _now=0)

    monkeypatch.setattr(tokens_module.time, "time", lambda: 1801.0)

    with pytest.raises(TokenError):
        verify(token, settings)


def test_tampered_signature_rejected(settings: Settings) -> None:
    token = mint(TokenKind.VIEWER, tg_user_id=42, payload={"foo": "bar"}, settings=settings)

    header, body, signature = token.split(".")
    flipped = "A" if signature[0] != "A" else "B"
    tampered = f"{header}.{body}.{flipped}{signature[1:]}"

    with pytest.raises(TokenError):
        verify(tampered, settings)


def test_verify_kind_rejects_wrong_kind(settings: Settings) -> None:
    token = mint(TokenKind.VIEWER, tg_user_id=42, payload={}, settings=settings)

    with pytest.raises(TokenError) as excinfo:
        verify_kind(token, settings, expected=TokenKind.PREVIEW)

    message = str(excinfo.value)
    assert "viewer" in message
    assert "preview" in message


def test_verify_kind_accepts_matching_kind(settings: Settings) -> None:
    token = mint(TokenKind.PREVIEW, tg_user_id=99, payload={"port": 3000}, settings=settings)

    verified = verify_kind(token, settings, expected=TokenKind.PREVIEW)

    assert verified.kind == TokenKind.PREVIEW
    assert verified.tg_user_id == 99
    assert verified.payload == {"port": 3000}


def test_preview_payload_roundtrip(settings: Settings) -> None:
    token = mint(TokenKind.PREVIEW, tg_user_id=1, payload={"port": 3000}, settings=settings)

    verified = verify(token, settings)

    assert verified.kind == TokenKind.PREVIEW
    assert verified.payload["port"] == 3000


def test_none_algorithm_token_rejected(settings: Settings) -> None:
    """Defence in depth: a token forged with `alg: "none"` must not verify."""
    forged = jwt.encode(
        {
            "sub": "42",
            "kind": "viewer",
            "payload": "{}",
            "iat": 0,
            "exp": 9_999_999_999,
            "jti": "deadbeef",
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(TokenError):
        verify(forged, settings)


def _signed_with_claims(claims: dict[str, object], settings: Settings) -> str:
    """Encode arbitrary claims with the project's HS256 secret."""
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def test_missing_claim_rejected(settings: Settings) -> None:
    forged = _signed_with_claims(
        {
            "sub": "1",
            "kind": "viewer",
            "payload": "{}",
            "iat": 0,
            "exp": 9_999_999_999,
            # jti missing
        },
        settings,
    )

    with pytest.raises(TokenError, match="missing required claim"):
        verify(forged, settings)


def test_non_integer_sub_rejected(settings: Settings) -> None:
    forged = _signed_with_claims(
        {
            "sub": "not-a-number",
            "kind": "viewer",
            "payload": "{}",
            "iat": 0,
            "exp": 9_999_999_999,
            "jti": "abc",
        },
        settings,
    )

    with pytest.raises(TokenError, match="'sub'"):
        verify(forged, settings)


def test_unknown_kind_rejected(settings: Settings) -> None:
    forged = _signed_with_claims(
        {
            "sub": "1",
            "kind": "admin",
            "payload": "{}",
            "iat": 0,
            "exp": 9_999_999_999,
            "jti": "abc",
        },
        settings,
    )

    with pytest.raises(TokenError, match="'kind'"):
        verify(forged, settings)


def test_invalid_payload_json_rejected(settings: Settings) -> None:
    forged = _signed_with_claims(
        {
            "sub": "1",
            "kind": "viewer",
            "payload": "{not json",
            "iat": 0,
            "exp": 9_999_999_999,
            "jti": "abc",
        },
        settings,
    )

    with pytest.raises(TokenError, match="'payload'"):
        verify(forged, settings)


def test_payload_not_object_rejected(settings: Settings) -> None:
    forged = _signed_with_claims(
        {
            "sub": "1",
            "kind": "viewer",
            "payload": "[1, 2, 3]",
            "iat": 0,
            "exp": 9_999_999_999,
            "jti": "abc",
        },
        settings,
    )

    with pytest.raises(TokenError, match="JSON object"):
        verify(forged, settings)


def test_empty_jti_rejected(settings: Settings) -> None:
    forged = _signed_with_claims(
        {
            "sub": "1",
            "kind": "viewer",
            "payload": "{}",
            "iat": 0,
            "exp": 9_999_999_999,
            "jti": "",
        },
        settings,
    )

    with pytest.raises(TokenError, match="'jti'"):
        verify(forged, settings)
