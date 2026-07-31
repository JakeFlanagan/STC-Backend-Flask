"""JWT access tokens, refresh-token hashing/expiry, and the auth gate
app.py's global before_request hook calls on every non-public request.

Uses `import config` (not `from config import ...`) deliberately: the
module attribute lookup happens at call time, not import time, which is
what lets tests/test_auth_middleware.py monkeypatch
`auth_middleware.config.JWT_EXPIRY_MINUTES` to produce an already-expired
token on demand.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from flask import Response, g, jsonify, request

import auth_db
import config

ALGORITHM = "HS256"


def generate_access_token(user_id: int, username: str, role: str) -> str:
    """A short-lived JWT proving who the caller is (and what role they
    hold - see blueprints/admin.py's _require_admin), signed with
    config.JWT_SECRET and expiring after config.JWT_EXPIRY_MINUTES.
    Carries "iat" (issued-at) so check_auth can reject a token minted
    before a cascade revoke or account-disable (see
    auth_db.get_tokens_revoked_at) even though the token itself hasn't
    expired yet."""
    payload: dict[str, Any] = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=config.JWT_EXPIRY_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Return the token's payload if it's valid and unexpired, else None -
    callers don't need to distinguish garbage/expired/tampered, they all
    mean "not authenticated"."""
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex digest of a refresh token's plaintext - refresh tokens
    are stored (auth_db.py) and looked up only as this hash, never as
    plaintext, same reasoning as password hashing."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_refresh_token() -> tuple[str, str]:
    """Return (plaintext, hash) for a brand-new refresh token. The
    plaintext is returned to the client once and never stored; only the
    hash is persisted (auth_db.store_refresh_token)."""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_refresh_token(plaintext)


def refresh_expiry_timestamp() -> str:
    """config.REFRESH_EXPIRY_DAYS from now, formatted to match SQLite's
    own datetime('now') output ('YYYY-MM-DD HH:MM:SS' UTC) so
    auth_db.get_valid_refresh_token's SQL string comparison against
    datetime('now') just works, no conversion needed on either side."""
    expiry = datetime.now(timezone.utc) + timedelta(days=config.REFRESH_EXPIRY_DAYS)
    return expiry.strftime("%Y-%m-%d %H:%M:%S")


def hash_password_reset_token(token: str) -> str:
    """SHA-256 hex digest of a password-reset token's plaintext - same
    hashing as hash_refresh_token (a distinct name, not a reuse, so each
    token kind reads clearly at its own call sites), stored
    (auth_db.py) and looked up only as this hash, never as plaintext."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_password_reset_token() -> tuple[str, str]:
    """Return (plaintext, hash) for a brand-new password-reset token. The
    plaintext is emailed once (mailer.py) and never stored; only the hash
    is persisted (auth_db.create_password_reset_token)."""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_password_reset_token(plaintext)


def password_reset_expiry_timestamp() -> str:
    """config.PASSWORD_RESET_EXPIRY_MINUTES from now, formatted to match
    SQLite's own datetime('now') output - same reasoning as
    refresh_expiry_timestamp."""
    expiry = datetime.now(timezone.utc) + timedelta(minutes=config.PASSWORD_RESET_EXPIRY_MINUTES)
    return expiry.strftime("%Y-%m-%d %H:%M:%S")


def check_auth() -> tuple[Response, int] | None:
    """Read the Authorization header off the current request, verify it,
    and set flask.g.user_id/g.username/g.role on success. Returns None to
    let the request proceed, or a (response, 401) pair for app.py's
    before_request hook to return directly (Flask short-circuits a
    before_request that returns non-None).

    Also rejects a signature-valid, unexpired token if it was issued at or
    before the account's tokens_revoked_at (see
    auth_db.revoke_all_refresh_tokens_for_user / set_user_disabled) - the
    "iat" check is skipped for a token with no "iat" claim at all (issued
    before this feature shipped), same graceful-degradation as the "role"
    default below, rather than rejecting an otherwise-still-valid token
    during the rollout window. Deliberately <=, not <: both "iat" (PyJWT
    truncates to whole seconds) and tokens_revoked_at (SQLite's
    datetime('now')) only have one-second resolution, so a token minted
    the same wall-clock second as the revoke - the common case in tests,
    and possible in production too - must still be rejected; ambiguous
    ordering within that second should fail closed, not leave a
    just-revoked token live."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return jsonify({"error": "Missing Authorization header"}), 401
    token = header[len("Bearer "):]
    payload = decode_access_token(token)
    if payload is None:
        return jsonify({"error": "Invalid or expired token"}), 401
    db = auth_db.get_auth_db()
    revoked_at = auth_db.get_tokens_revoked_at(db, payload["user_id"])
    if revoked_at is not None and "iat" in payload:
        issued_at = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
        revoked_at_dt = datetime.strptime(revoked_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if issued_at <= revoked_at_dt:
            return jsonify({"error": "Invalid or expired token"}), 401
    g.user_id = payload["user_id"]
    g.username = payload["username"]
    # .get(..., "user"), not a hard key lookup: an access token issued
    # before this feature shipped won't have a "role" key at all, and
    # must degrade to the safe default rather than raise KeyError on an
    # otherwise-still-valid, unexpired token during the rollout window.
    g.role = payload.get("role", "user")
    return None
