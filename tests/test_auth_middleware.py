"""Tests for auth_middleware.py: JWT access-token encode/decode,
refresh-token hashing/expiry, and check_auth() - the function app.py's
global before_request hook calls to gate every non-public route."""

from datetime import datetime, timedelta, timezone

import app as app_module
import auth_middleware
import jwt
from flask import g
from helpers import expect


def test_generate_and_decode_access_token_roundtrip():
    token = auth_middleware.generate_access_token(42, "alice", "user")
    payload = auth_middleware.decode_access_token(token)
    expect("payload present", True, payload is not None)
    assert payload is not None
    expect("user_id in payload", 42, payload["user_id"])
    expect("username in payload", "alice", payload["username"])
    expect("role in payload", "user", payload["role"])


def test_decode_rejects_garbage_token():
    payload = auth_middleware.decode_access_token("not-a-real-token")
    expect("garbage token rejected", None, payload)


def test_decode_rejects_expired_token(monkeypatch):
    # auth_middleware does `import config` (not `from config import ...`)
    # specifically so this attribute is monkeypatchable per-test.
    monkeypatch.setattr(auth_middleware.config, "JWT_EXPIRY_MINUTES", -1)
    token = auth_middleware.generate_access_token(1, "bob", "user")
    payload = auth_middleware.decode_access_token(token)
    expect("expired token rejected", None, payload)


def test_hash_refresh_token_is_deterministic():
    h1 = auth_middleware.hash_refresh_token("some-plaintext-token")
    h2 = auth_middleware.hash_refresh_token("some-plaintext-token")
    expect("same input hashes the same", h1, h2)


def test_generate_refresh_token_returns_plaintext_and_matching_hash():
    plaintext, digest = auth_middleware.generate_refresh_token()
    expect("hash matches plaintext", auth_middleware.hash_refresh_token(plaintext), digest)


def test_refresh_expiry_timestamp_format():
    ts = auth_middleware.refresh_expiry_timestamp()
    expect("timestamp is 'YYYY-MM-DD HH:MM:SS' (19 chars)", 19, len(ts))


def test_check_auth_rejects_missing_header():
    with app_module.app.test_request_context("/api/v1/items"):
        result = auth_middleware.check_auth()
        expect("missing header rejected with 401", 401, result[1] if result else None)


def test_check_auth_rejects_invalid_token():
    with app_module.app.test_request_context("/api/v1/items", headers={"Authorization": "Bearer garbage"}):
        result = auth_middleware.check_auth()
        expect("invalid token rejected with 401", 401, result[1] if result else None)


def test_check_auth_accepts_valid_token_and_sets_g():
    token = auth_middleware.generate_access_token(7, "carol", "admin")
    with app_module.app.test_request_context("/api/v1/items", headers={"Authorization": f"Bearer {token}"}):
        result = auth_middleware.check_auth()
        expect("valid token accepted (no error response)", None, result)
        expect("g.user_id set", 7, g.user_id)
        expect("g.username set", "carol", g.username)
        expect("g.role set", "admin", g.role)


def test_check_auth_defaults_missing_role_to_user():
    # Simulates a token issued before the role field existed - a payload
    # with no "role" key at all must still authenticate, defaulting to
    # "user" rather than raising KeyError.
    payload = {
        "user_id": 99,
        "username": "legacy_token_user",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    token = jwt.encode(payload, auth_middleware.config.JWT_SECRET, algorithm=auth_middleware.ALGORITHM)
    with app_module.app.test_request_context("/api/v1/items", headers={"Authorization": f"Bearer {token}"}):
        result = auth_middleware.check_auth()
        expect("valid legacy-shaped token accepted", None, result)
        expect("g.role defaults to 'user'", "user", g.role)
