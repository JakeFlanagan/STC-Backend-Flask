"""Tests for blueprints/auth.py's register/login/refresh/logout endpoints,
and for the global auth gate (app.py's _require_auth before_request) that
protects every other route. Uses a raw, unauthenticated test client (not
the `client` fixture, which - after tests/conftest.py's Task 4 change -
already carries a valid Authorization header) so these tests have full
control over what credentials, if any, each request presents.
"""

import re
import uuid

import app as app_module
import auth_db
from helpers import expect


def _unique_username() -> str:
    return f"routeuser_{uuid.uuid4().hex[:12]}"


def _unique_email() -> str:
    return f"{uuid.uuid4().hex[:12]}@example.com"


def test_register_returns_token_pair():
    c = app_module.app.test_client()
    resp = c.post("/api/v1/auth/register", json={"username": _unique_username(), "password": "password123"})
    expect("status code", 201, resp.status_code)
    body = resp.get_json()
    expect("access_token present", True, "access_token" in body)
    expect("refresh_token present", True, "refresh_token" in body)


def test_register_duplicate_username_returns_409():
    c = app_module.app.test_client()
    username = _unique_username()
    c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    resp = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    expect("status code", 409, resp.status_code)


def test_register_short_password_returns_400():
    c = app_module.app.test_client()
    resp = c.post("/api/v1/auth/register", json={"username": _unique_username(), "password": "short"})
    expect("status code", 400, resp.status_code)


def test_login_success():
    c = app_module.app.test_client()
    username = _unique_username()
    c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    resp = c.post("/api/v1/auth/login", json={"username": username, "password": "password123"})
    expect("status code", 200, resp.status_code)
    expect("access_token present", True, "access_token" in resp.get_json())


def test_login_wrong_password_returns_401():
    c = app_module.app.test_client()
    username = _unique_username()
    c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    resp = c.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})
    expect("status code", 401, resp.status_code)


def test_login_nonexistent_user_returns_401_same_message_as_wrong_password():
    c = app_module.app.test_client()
    wrong_pw_resp = c.post("/api/v1/auth/login", json={"username": _unique_username(), "password": "whatever1"})
    username = _unique_username()
    c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    bad_pw_resp = c.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})
    expect("status code matches", bad_pw_resp.status_code, wrong_pw_resp.status_code)
    expect("error message matches", bad_pw_resp.get_json()["error"], wrong_pw_resp.get_json()["error"])


def test_refresh_rotates_token_and_old_one_stops_working():
    c = app_module.app.test_client()
    username = _unique_username()
    reg = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    old_refresh = reg.get_json()["refresh_token"]

    resp = c.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    expect("status code", 200, resp.status_code)
    new_refresh = resp.get_json()["refresh_token"]
    expect("new refresh token differs from old", True, new_refresh != old_refresh)

    reuse_resp = c.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    expect("old refresh token rejected after rotation", 401, reuse_resp.status_code)


def test_refresh_reuse_cascades_to_revoke_other_sessions():
    """Reusing a rotated-out refresh token is a token-theft signal - the
    user's other active session (a second refresh token pair, e.g. from a
    second device) must stop working too, not just the reused one."""
    c = app_module.app.test_client()
    username = _unique_username()
    reg = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    first_session_refresh = reg.get_json()["refresh_token"]

    login_resp = c.post("/api/v1/auth/login", json={"username": username, "password": "password123"})
    second_session_refresh = login_resp.get_json()["refresh_token"]

    rotate_resp = c.post("/api/v1/auth/refresh", json={"refresh_token": first_session_refresh})
    expect("rotation succeeds", 200, rotate_resp.status_code)
    rotated_refresh = rotate_resp.get_json()["refresh_token"]

    reuse_resp = c.post("/api/v1/auth/refresh", json={"refresh_token": first_session_refresh})
    expect("reused old refresh token rejected", 401, reuse_resp.status_code)

    cascaded_resp = c.post("/api/v1/auth/refresh", json={"refresh_token": rotated_refresh})
    expect("token issued by the reused rotation is also revoked", 401, cascaded_resp.status_code)

    other_session_resp = c.post("/api/v1/auth/refresh", json={"refresh_token": second_session_refresh})
    expect("unrelated second session is also revoked", 401, other_session_resp.status_code)


def test_refresh_invalid_token_returns_401():
    c = app_module.app.test_client()
    resp = c.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    expect("status code", 401, resp.status_code)


def test_logout_revokes_token():
    c = app_module.app.test_client()
    username = _unique_username()
    reg = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    refresh_token = reg.get_json()["refresh_token"]

    resp = c.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    expect("status code", 204, resp.status_code)

    reuse_resp = c.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    expect("logged-out refresh token can't be used to refresh", 401, reuse_resp.status_code)


def test_logout_already_invalid_token_returns_401():
    c = app_module.app.test_client()
    resp = c.post("/api/v1/auth/logout", json={"refresh_token": "not-a-real-token"})
    expect("status code", 401, resp.status_code)


def test_protected_route_without_token_returns_401():
    c = app_module.app.test_client()
    resp = c.get("/api/v1/databases")
    expect("status code", 401, resp.status_code)


def test_protected_route_with_garbage_token_returns_401():
    c = app_module.app.test_client()
    resp = c.get("/api/v1/databases", headers={"Authorization": "Bearer garbage"})
    expect("status code", 401, resp.status_code)


def test_protected_route_with_valid_token_succeeds():
    c = app_module.app.test_client()
    username = _unique_username()
    reg = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    token = reg.get_json()["access_token"]
    resp = c.get("/api/v1/databases", headers={"Authorization": f"Bearer {token}"})
    expect("status code", 200, resp.status_code)


def test_health_route_works_without_token():
    c = app_module.app.test_client()
    resp = c.get("/api/v1/health")
    expect("status code", 200, resp.status_code)


def test_cors_preflight_on_protected_route_bypasses_auth_gate():
    c = app_module.app.test_client()
    resp = c.options(
        "/api/v1/items",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    expect("status code is not 401", True, resp.status_code != 401)
    expect("CORS allow-origin header present", True, "Access-Control-Allow-Origin" in resp.headers)


def test_login_locked_out_after_repeated_failures_returns_429():
    c = app_module.app.test_client()
    username = _unique_username()
    c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})

    last_status = None
    for _ in range(6):
        resp = c.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})
        last_status = resp.status_code
    expect("6th failed login within the lockout window is locked out", 429, last_status)


def test_login_success_clears_lockout_counter():
    c = app_module.app.test_client()
    username = _unique_username()
    c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})

    for _ in range(4):
        c.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})

    resp = c.post("/api/v1/auth/login", json={"username": username, "password": "password123"})
    expect("login succeeds before the lockout threshold is reached", 200, resp.status_code)

    # A successful login clears the failure counter, so the next wrong
    # attempt is failure #1 again, not #5 - must not be locked out.
    resp2 = c.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})
    expect("counter reset - single failure after success is not locked out", 401, resp2.status_code)


def test_refresh_reuse_cascade_also_revokes_live_access_token():
    """Cascade revocation (see test_refresh_reuse_cascades_to_revoke_other_sessions)
    must reach an access token minted before the theft was detected too -
    JWTs are stateless, so this only works if check_auth cross-checks
    tokens_revoked_at (see auth_db.get_tokens_revoked_at)."""
    c = app_module.app.test_client()
    username = _unique_username()
    reg = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    first_session_refresh = reg.get_json()["refresh_token"]
    live_access_token = reg.get_json()["access_token"]

    rotate_resp = c.post("/api/v1/auth/refresh", json={"refresh_token": first_session_refresh})
    expect("rotation succeeds", 200, rotate_resp.status_code)

    # Reusing the now-rotated-out token is the theft signal that triggers
    # revoke_all_refresh_tokens_for_user - see refresh()'s reuse handling.
    c.post("/api/v1/auth/refresh", json={"refresh_token": first_session_refresh})

    protected_resp = c.get("/api/v1/databases", headers={"Authorization": f"Bearer {live_access_token}"})
    expect("access token minted before the cascade revoke is now rejected", 401, protected_resp.status_code)


def test_disabling_user_revokes_their_live_access_token():
    c = app_module.app.test_client()
    username = _unique_username()
    reg = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    live_access_token = reg.get_json()["access_token"]

    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.verify_user(db, username, "password123")
        assert user is not None
        auth_db.set_user_disabled(db, user["id"], True)

    resp = c.get("/api/v1/databases", headers={"Authorization": f"Bearer {live_access_token}"})
    expect("access token is rejected right after the account is disabled", 401, resp.status_code)


def test_register_accepts_optional_email():
    c = app_module.app.test_client()
    resp = c.post(
        "/api/v1/auth/register",
        json={"username": _unique_username(), "password": "password123", "email": _unique_email()},
    )
    expect("status code", 201, resp.status_code)


def test_register_duplicate_email_returns_409():
    c = app_module.app.test_client()
    email = _unique_email()
    c.post("/api/v1/auth/register", json={"username": _unique_username(), "password": "password123", "email": email})
    resp = c.post(
        "/api/v1/auth/register", json={"username": _unique_username(), "password": "password123", "email": email}
    )
    expect("status code", 409, resp.status_code)
    expect("error mentions email", True, "email" in resp.get_json()["error"])


def test_set_email_requires_auth():
    c = app_module.app.test_client()
    resp = c.post("/api/v1/auth/email", json={"email": _unique_email()})
    expect("unauthenticated set-email is rejected", 401, resp.status_code)


def test_set_email_success_and_duplicate():
    c = app_module.app.test_client()
    reg = c.post("/api/v1/auth/register", json={"username": _unique_username(), "password": "password123"})
    token = reg.get_json()["access_token"]

    taken_email = _unique_email()
    other_client = app_module.app.test_client()
    other_client.post(
        "/api/v1/auth/register",
        json={"username": _unique_username(), "password": "password123", "email": taken_email},
    )

    ok_resp = c.post(
        "/api/v1/auth/email", json={"email": _unique_email()}, headers={"Authorization": f"Bearer {token}"}
    )
    expect("setting a fresh email succeeds", 200, ok_resp.status_code)

    dup_resp = c.post(
        "/api/v1/auth/email", json={"email": taken_email}, headers={"Authorization": f"Bearer {token}"}
    )
    expect("setting an email already in use is rejected", 409, dup_resp.status_code)


def test_password_reset_request_always_returns_200_and_same_body():
    c = app_module.app.test_client()
    unknown_resp = c.post("/api/v1/auth/password-reset/request", json={"username": _unique_username()})
    expect("unknown username still returns 200", 200, unknown_resp.status_code)

    no_email_username = _unique_username()
    c.post("/api/v1/auth/register", json={"username": no_email_username, "password": "password123"})
    no_email_resp = c.post("/api/v1/auth/password-reset/request", json={"username": no_email_username})
    expect("account with no email on file still returns 200", 200, no_email_resp.status_code)

    expect(
        "response body identical regardless of whether the account exists",
        unknown_resp.get_json(),
        no_email_resp.get_json(),
    )


def test_password_reset_full_flow(caplog):
    c = app_module.app.test_client()
    username = _unique_username()
    email = _unique_email()
    reg = c.post(
        "/api/v1/auth/register", json={"username": username, "password": "old-password-1", "email": email}
    )
    old_access_token = reg.get_json()["access_token"]

    with caplog.at_level("INFO", logger="stc.mailer"):
        request_resp = c.post("/api/v1/auth/password-reset/request", json={"username": username})
    expect("request status code", 200, request_resp.status_code)

    reset_links = [r.reset_link for r in caplog.records if r.name == "stc.mailer"]
    expect("exactly one reset link logged (SMTP_HOST unset in tests)", 1, len(reset_links))
    match = re.search(r"token=([^&]+)", reset_links[0])
    assert match is not None
    token = match.group(1)

    confirm_resp = c.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "new-password-1"}
    )
    expect("confirm status code", 200, confirm_resp.status_code)

    old_login = c.post("/api/v1/auth/login", json={"username": username, "password": "old-password-1"})
    expect("old password rejected after reset", 401, old_login.status_code)
    new_login = c.post("/api/v1/auth/login", json={"username": username, "password": "new-password-1"})
    expect("new password works after reset", 200, new_login.status_code)

    still_live = c.get("/api/v1/databases", headers={"Authorization": f"Bearer {old_access_token}"})
    expect("pre-reset access token revoked by the reset", 401, still_live.status_code)

    reuse_resp = c.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "another-password-1"}
    )
    expect("reused reset token is rejected", 401, reuse_resp.status_code)


def test_password_reset_confirm_rejects_bad_token():
    c = app_module.app.test_client()
    resp = c.post(
        "/api/v1/auth/password-reset/confirm", json={"token": "not-a-real-token", "new_password": "new-password-1"}
    )
    expect("status code", 401, resp.status_code)


def test_password_reset_confirm_rejects_short_password():
    c = app_module.app.test_client()
    resp = c.post("/api/v1/auth/password-reset/confirm", json={"token": "whatever", "new_password": "short"})
    expect("status code", 400, resp.status_code)


def _register_and_get_tokens(c, username, password="password123"):
    """Create a fresh account and return its (access, refresh) pair."""
    body = c.post("/api/v1/auth/register", json={"username": username, "password": password}).get_json()
    return body["access_token"], body["refresh_token"]


def test_change_password_lets_the_new_password_log_in():
    c = app_module.app.test_client()
    username = _unique_username()
    access, _ = _register_and_get_tokens(c, username)

    resp = c.post(
        "/api/v1/auth/password",
        json={"current_password": "password123", "new_password": "brand-new-password"},
        headers={"Authorization": f"Bearer {access}"},
    )
    expect("status code", 200, resp.status_code)

    new_login = c.post("/api/v1/auth/login", json={"username": username, "password": "brand-new-password"})
    expect("the new password logs in", 200, new_login.status_code)
    old_login = c.post("/api/v1/auth/login", json={"username": username, "password": "password123"})
    expect("the old password does not", 401, old_login.status_code)


def test_change_password_requires_the_current_one():
    """A stolen access token must not be tradeable for permanent ownership
    of the account, which is what a change endpoint that trusted the token
    alone would allow."""
    c = app_module.app.test_client()
    username = _unique_username()
    access, _ = _register_and_get_tokens(c, username)

    resp = c.post(
        "/api/v1/auth/password",
        json={"current_password": "not-the-password", "new_password": "brand-new-password"},
        headers={"Authorization": f"Bearer {access}"},
    )
    expect("status code", 401, resp.status_code)
    still_works = c.post("/api/v1/auth/login", json={"username": username, "password": "password123"})
    expect("the original password is untouched", 200, still_works.status_code)


def test_change_password_rejects_a_short_new_password():
    c = app_module.app.test_client()
    access, _ = _register_and_get_tokens(c, _unique_username())
    resp = c.post(
        "/api/v1/auth/password",
        json={"current_password": "password123", "new_password": "short"},
        headers={"Authorization": f"Bearer {access}"},
    )
    expect("status code", 400, resp.status_code)


def test_change_password_revokes_every_existing_session():
    """Both halves matter. The refresh token is stored, so revoking it is
    a row update; the access token is stateless, so only tokens_revoked_at
    can reach one already issued."""
    c = app_module.app.test_client()
    username = _unique_username()
    access, refresh = _register_and_get_tokens(c, username)

    c.post(
        "/api/v1/auth/password",
        json={"current_password": "password123", "new_password": "brand-new-password"},
        headers={"Authorization": f"Bearer {access}"},
    )

    reused = c.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    expect("the old refresh token is dead", 401, reused.status_code)

    with_old_access = c.get("/api/v1/settings", headers={"Authorization": f"Bearer {access}"})
    expect("the already-issued access token is dead too", 401, with_old_access.status_code)


def test_change_password_requires_authentication():
    c = app_module.app.test_client()
    resp = c.post(
        "/api/v1/auth/password",
        json={"current_password": "password123", "new_password": "brand-new-password"},
    )
    expect("status code", 401, resp.status_code)
