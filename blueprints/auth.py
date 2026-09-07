"""Account endpoints: register, login, refresh, logout, password change.

Unlike every other blueprint, these routes are exempt from app.py's global
_require_auth before_request hook (see its _PUBLIC_PATHS) - you need to be
able to register/login before you have a token to prove who you are.
"""

import sqlite3

from flask import Blueprint, Response, g, jsonify, request

import auth_db
import config
import mailer
from auth_middleware import (
    generate_access_token,
    generate_refresh_token,
    generate_password_reset_token,
    hash_password_reset_token,
    hash_refresh_token,
    password_reset_expiry_timestamp,
    refresh_expiry_timestamp,
)
from rate_limit import limiter

auth_bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")

MIN_PASSWORD_LENGTH = 8

# The generic response every password-reset-request call gets, whether or
# not the username exists or has an email on file - never let this route
# be used to enumerate valid usernames.
_RESET_REQUESTED_MESSAGE = "if an account with that username has an email on file, a reset link has been sent"


def _issue_token_pair(db: sqlite3.Connection, user_id: int, username: str, role: str) -> dict[str, str]:
    """Mint a fresh access+refresh token pair, persisting the refresh
    token's hash. Shared by register/login/refresh so all three return the
    exact same response shape. role travels in the access token (see
    auth_middleware.generate_access_token) - refresh always re-derives it
    fresh from auth_db (see get_valid_refresh_token), so a role change
    takes effect at the next refresh even though the access token itself
    is static until it expires."""
    access_token = generate_access_token(user_id, username, role)
    refresh_token, refresh_hash = generate_refresh_token()
    auth_db.store_refresh_token(db, user_id, refresh_hash, refresh_expiry_timestamp())
    return {"access_token": access_token, "refresh_token": refresh_token}


@auth_bp.route("/register", methods=["POST"])
@limiter.limit("5 per hour")
def register() -> Response | tuple[Response, int]:
    d = request.get_json(silent=True) or {}
    username = d.get("username", "")
    password = d.get("password", "")
    email = d.get("email") or None  # optional - see auth_db.create_user's docstring
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    db = auth_db.get_auth_db()
    try:
        user = auth_db.create_user(db, username, password, email=email)
    except ValueError as e:
        error = "email already in use" if "email" in str(e) else "username already taken"
        return jsonify({"error": error}), 409
    return jsonify(_issue_token_pair(db, user["id"], user["username"], user["role"])), 201


@auth_bp.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def login() -> Response | tuple[Response, int]:
    d = request.get_json(silent=True) or {}
    username = d.get("username", "")
    db = auth_db.get_auth_db()
    if auth_db.login_lockout_remaining(db, username) is not None:
        return jsonify({"error": "too many failed attempts, try again later"}), 429
    user = auth_db.verify_user(db, username, d.get("password", ""))
    if user is None:
        auth_db.record_login_failure(db, username)
        # Re-check immediately: if *this* failure is the one that just
        # tipped the count past the free-attempts threshold, report the
        # lockout (429) rather than a plain 401 - the lockout check above
        # ran before this attempt was recorded, so it can't see a lock this
        # same request just triggered.
        if auth_db.login_lockout_remaining(db, username) is not None:
            return jsonify({"error": "too many failed attempts, try again later"}), 429
        return jsonify({"error": "invalid username or password"}), 401
    auth_db.clear_login_failures(db, username)
    return jsonify(_issue_token_pair(db, user["id"], user["username"], user["role"]))


@auth_bp.route("/refresh", methods=["POST"])
def refresh() -> Response | tuple[Response, int]:
    d = request.get_json(silent=True) or {}
    token = d.get("refresh_token", "")
    db = auth_db.get_auth_db()
    token_hash = hash_refresh_token(token)
    valid = auth_db.get_valid_refresh_token(db, token_hash)
    if valid is None:
        # A reused, already-rotated-out refresh token is a signal of
        # possible token theft (this user's real client already moved past
        # it) - revoke every other active session for this user too, not
        # just reject this one request.
        reused_by = auth_db.get_reused_refresh_token_owner(db, token_hash)
        if reused_by is not None:
            auth_db.revoke_all_refresh_tokens_for_user(db, reused_by)
        return jsonify({"error": "invalid or expired refresh token"}), 401
    auth_db.revoke_refresh_token(db, token_hash)
    return jsonify(_issue_token_pair(db, valid["user_id"], valid["username"], valid["role"]))


@auth_bp.route("/logout", methods=["POST"])
def logout() -> Response | tuple[Response, int] | tuple[str, int]:
    d = request.get_json(silent=True) or {}
    token = d.get("refresh_token", "")
    db = auth_db.get_auth_db()
    token_hash = hash_refresh_token(token)
    valid = auth_db.get_valid_refresh_token(db, token_hash)
    if valid is None:
        return jsonify({"error": "invalid or expired refresh token"}), 401
    auth_db.revoke_refresh_token(db, token_hash)
    return "", 204


@auth_bp.route("/email", methods=["POST"])
def set_email() -> Response | tuple[Response, int]:
    """Self-service set/update the caller's own email - the only way an
    account created before this feature shipped (with none on file) can
    opt into password-reset/request below. Authenticated (not in app.py's
    _PUBLIC_PATHS): g.user_id is set by the global auth gate."""
    d = request.get_json(silent=True) or {}
    email = d.get("email", "")
    if not email:
        return jsonify({"error": "email is required"}), 400
    db = auth_db.get_auth_db()
    if not auth_db.set_user_email(db, g.user_id, email):
        return jsonify({"error": "email already in use"}), 409
    return jsonify({"ok": True})


@auth_bp.route("/password", methods=["POST"])
@limiter.limit("10 per hour")
def change_password() -> Response | tuple[Response, int]:
    """Change the caller's own password, given their current one.

    Authenticated (not in app.py's _PUBLIC_PATHS), so g.user_id and
    g.username are already set by the global auth gate. The current
    password is still required on top of that: an access token can be
    stolen, and without this check a stolen token could be traded for
    permanent ownership of the account.

    Rate limited because it is an oracle. Without a limit, anyone holding a
    token could guess the current password here as fast as they liked,
    which would quietly undo the per-username lockout that login() is
    protected by.

    Every session is ended, including the caller's own, so a successful
    change is followed by a 200 and then a re-login rather than by new
    tokens. Handing back a fresh pair here would be friendlier and also
    wrong: check_auth compares a token's "iat" against tokens_revoked_at
    with <=, and both have one-second resolution, so a token minted in the
    same wall-clock second as the revoke is rejected anyway. Better an
    honest re-login than a token that works or not depending on timing."""
    d = request.get_json(silent=True) or {}
    current_password = d.get("current_password", "")
    new_password = d.get("new_password", "")
    if not current_password or not new_password:
        return jsonify({"error": "current_password and new_password are required"}), 400
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    db = auth_db.get_auth_db()
    if auth_db.verify_user(db, g.username, current_password) is None:
        return jsonify({"error": "current password is incorrect"}), 401
    auth_db.set_user_password(db, g.user_id, new_password)
    return jsonify({"ok": True, "sessions_revoked": True})


@auth_bp.route("/password-reset/request", methods=["POST"])
@limiter.limit("5 per hour")
def request_password_reset() -> Response:
    """Always returns the same 200 response, whether or not username
    exists or has an email on file - see _RESET_REQUESTED_MESSAGE. Only
    when both are true does a reset link actually get generated/emailed;
    every other case is silently a no-op, indistinguishable from the
    outside."""
    d = request.get_json(silent=True) or {}
    username = d.get("username", "")
    db = auth_db.get_auth_db()
    user = auth_db.get_user_by_username(db, username)
    if user is not None and user["email"] and not user["disabled"]:
        plaintext, token_hash = generate_password_reset_token()
        auth_db.create_password_reset_token(db, user["id"], token_hash, password_reset_expiry_timestamp())
        reset_link = f"{config.APP_BASE_URL}/reset-password?token={plaintext}"
        mailer.send_password_reset_email(user["email"], reset_link)
    return jsonify({"message": _RESET_REQUESTED_MESSAGE})


@auth_bp.route("/password-reset/confirm", methods=["POST"])
@limiter.limit("10 per hour")
def confirm_password_reset() -> Response | tuple[Response, int]:
    """Consume a password-reset token: sets the new password and
    cascade-revokes every existing session for the account (see
    auth_db.consume_password_reset) - a password reset assumes the old
    password may be compromised, same reasoning as reused-refresh-token
    detection."""
    d = request.get_json(silent=True) or {}
    token = d.get("token", "")
    new_password = d.get("new_password", "")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    db = auth_db.get_auth_db()
    token_hash = hash_password_reset_token(token)
    if not auth_db.consume_password_reset(db, token_hash, new_password):
        return jsonify({"error": "invalid or expired reset token"}), 401
    return jsonify({"ok": True})
