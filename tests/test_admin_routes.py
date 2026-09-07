"""Tests for blueprints/admin.py's admin-only user-management endpoints,
and the role-based access control (g.role, set from a JWT's "role" claim)
that gates them. Builds its own admin-role test client directly via
auth_db (there's no self-service way to become admin - see this file's
_admin_client helper and blueprints/admin.py's module docstring), separate
from the ordinary `client` fixture in tests/conftest.py, which is always a
plain "user"-role account.
"""

import uuid

import app as app_module
import auth_db
import auth_middleware
from helpers import expect


def _admin_client():
    """A test client authenticated as a brand-new admin-role account,
    created directly via auth_db (bypassing the self-service /register
    route, which always creates a "user"-role account)."""
    username = f"admin_{uuid.uuid4().hex[:12]}"
    password = "admin-test-password-1"
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        auth_db.create_user(db, username, password, role="admin")

    test_client = app_module.app.test_client()
    login_resp = test_client.post("/api/v1/auth/login", json={"username": username, "password": password})
    test_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {login_resp.get_json()['access_token']}"
    return test_client


def _plain_client():
    """A test client authenticated as a brand-new ordinary "user"-role
    account, via the normal self-service /register route."""
    username = f"plain_{uuid.uuid4().hex[:12]}"
    password = "plain-test-password-1"
    test_client = app_module.app.test_client()
    reg_resp = test_client.post("/api/v1/auth/register", json={"username": username, "password": password})
    test_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {reg_resp.get_json()['access_token']}"
    return test_client


def _user_id_of(test_client):
    """Decode the user id out of a client's stored Authorization header -
    convenience for tests that need to target a specific account by id."""
    token = test_client.environ_base["HTTP_AUTHORIZATION"][len("Bearer "):]
    payload = auth_middleware.decode_access_token(token)
    assert payload is not None
    return payload["user_id"]


def test_non_admin_gets_403_on_every_admin_route():
    plain = _plain_client()
    expect("list users forbidden", 403, plain.get("/api/v1/admin/users").status_code)
    expect("disable forbidden", 403, plain.post("/api/v1/admin/users/1/disable").status_code)
    expect("enable forbidden", 403, plain.post("/api/v1/admin/users/1/enable").status_code)
    expect("delete forbidden", 403, plain.delete("/api/v1/admin/users/1").status_code)


def test_admin_can_list_current_lockouts():
    admin = _admin_client()
    locked_username = f"lockout_{uuid.uuid4().hex[:12]}"
    password = "wrong-password-1"
    plain = app_module.app.test_client()
    plain.post("/api/v1/auth/register", json={"username": locked_username, "password": "correct-password-1"})

    for _ in range(6):
        plain.post("/api/v1/auth/login", json={"username": locked_username, "password": password})

    lockouts = admin.get("/api/v1/admin/lockouts").get_json()
    matching = [row for row in lockouts if row["username"] == locked_username]
    expect("locked-out username appears in the admin audit list", 1, len(matching))


def test_non_admin_gets_403_on_lockouts_route():
    plain = _plain_client()
    expect("lockouts list forbidden to non-admins", 403, plain.get("/api/v1/admin/lockouts").status_code)


def test_reenabling_clears_tokens_revoked_at_so_new_login_is_never_rejected():
    # A same-wall-clock-second disable-then-re-enable cycle is an easy
    # edge case to miss, and it could spuriously reject a
    # brand-new login (check_auth's tokens_revoked_at check is <=, not <
    # - see its docstring). Clearing tokens_revoked_at on re-enable closes
    # this: a token issued after re-enabling has nothing to compare
    # against at all, regardless of timing.
    admin = _admin_client()
    username = f"cycle_{uuid.uuid4().hex[:12]}"
    password = "cycle-password-1"
    plain = app_module.app.test_client()
    reg_resp = plain.post("/api/v1/auth/register", json={"username": username, "password": password})
    target_id = auth_middleware.decode_access_token(reg_resp.get_json()["access_token"])["user_id"]

    admin.post(f"/api/v1/admin/users/{target_id}/disable")
    admin.post(f"/api/v1/admin/users/{target_id}/enable")

    login_resp = plain.post("/api/v1/auth/login", json={"username": username, "password": password})
    expect("login succeeds right after re-enabling", 200, login_resp.status_code)
    new_access_token = login_resp.get_json()["access_token"]
    protected = plain.get(
        "/api/v1/databases", headers={"Authorization": f"Bearer {new_access_token}"}
    )
    expect("freshly issued token works immediately", 200, protected.status_code)


def test_admin_can_list_users():
    admin = _admin_client()
    target = _plain_client()
    target_id = _user_id_of(target)

    users = admin.get("/api/v1/admin/users").get_json()
    matching = [u for u in users if u["id"] == target_id]
    expect("target user appears in the admin's list", 1, len(matching))
    expect("target user's role is 'user'", "user", matching[0]["role"])
    expect("target user is not disabled", False, matching[0]["disabled"])


def test_admin_can_disable_and_enable_a_user():
    admin = _admin_client()
    username = f"toggle_{uuid.uuid4().hex[:12]}"
    password = "toggle-password-1"
    plain = app_module.app.test_client()
    reg_resp = plain.post("/api/v1/auth/register", json={"username": username, "password": password})
    target_id = auth_middleware.decode_access_token(reg_resp.get_json()["access_token"])["user_id"]

    disable_resp = admin.post(f"/api/v1/admin/users/{target_id}/disable")
    expect("disable status code", 200, disable_resp.status_code)
    blocked_login = plain.post("/api/v1/auth/login", json={"username": username, "password": password})
    expect("login blocked while disabled", 401, blocked_login.status_code)

    enable_resp = admin.post(f"/api/v1/admin/users/{target_id}/enable")
    expect("enable status code", 200, enable_resp.status_code)
    restored_login = plain.post("/api/v1/auth/login", json={"username": username, "password": password})
    expect("login works again after re-enabling", 200, restored_login.status_code)


def test_disabling_a_user_blocks_future_refresh_and_the_current_access_token():
    # Pre-Task-3 this access token rode out its remaining TTL after
    # disabling (JWTs are stateless, and check_auth trusted a
    # signature-valid, unexpired token without a database round-trip). See
    # auth_db.set_user_disabled / auth_middleware.check_auth's
    # tokens_revoked_at cross-check, which closed that gap.
    admin = _admin_client()
    username = f"disablerefresh_{uuid.uuid4().hex[:12]}"
    password = "disablerefresh-password-1"
    plain = app_module.app.test_client()
    reg_resp = plain.post("/api/v1/auth/register", json={"username": username, "password": password})
    body = reg_resp.get_json()
    access_token = body["access_token"]
    refresh_token = body["refresh_token"]
    target_id = auth_middleware.decode_access_token(access_token)["user_id"]

    admin.post(f"/api/v1/admin/users/{target_id}/disable")

    now_rejected = plain.get("/api/v1/databases", headers={"Authorization": f"Bearer {access_token}"})
    expect("existing access token is rejected right after disabling", 401, now_rejected.status_code)

    refresh_resp = plain.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    expect("refresh is blocked immediately after disabling", 401, refresh_resp.status_code)


def test_admin_cannot_disable_or_delete_own_account():
    admin = _admin_client()
    self_id = _user_id_of(admin)

    disable_resp = admin.post(f"/api/v1/admin/users/{self_id}/disable")
    expect("cannot disable own account", 400, disable_resp.status_code)

    delete_resp = admin.delete(f"/api/v1/admin/users/{self_id}")
    expect("cannot delete own account", 400, delete_resp.status_code)


def test_admin_can_delete_a_user():
    admin = _admin_client()
    target = _plain_client()
    target_id = _user_id_of(target)

    delete_resp = admin.delete(f"/api/v1/admin/users/{target_id}")
    expect("delete status code", 200, delete_resp.status_code)

    users = admin.get("/api/v1/admin/users").get_json()
    expect("deleted user no longer listed", 0, len([u for u in users if u["id"] == target_id]))


def test_admin_routes_return_404_for_nonexistent_user_id():
    admin = _admin_client()
    expect("disable nonexistent 404", 404, admin.post("/api/v1/admin/users/999999999/disable").status_code)
    expect("enable nonexistent 404", 404, admin.post("/api/v1/admin/users/999999999/enable").status_code)
    expect("delete nonexistent 404", 404, admin.delete("/api/v1/admin/users/999999999").status_code)


def test_options_preflight_on_admin_route_bypasses_the_role_gate():
    # No Authorization header at all - g.role is never set for an OPTIONS
    # request (see app.py's _require_auth, which skips check_auth() for
    # preflights). _require_admin must not crash trying to read g.role.
    test_client = app_module.app.test_client()
    resp = test_client.options(
        "/api/v1/admin/users",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    expect("status code is not 500", True, resp.status_code != 500)
    expect("status code is not 401 or 403 either - a real preflight response", True, resp.status_code not in (401, 403))
    expect("CORS allow-origin header present", True, "Access-Control-Allow-Origin" in resp.headers)


def test_unauthenticated_request_gets_401_before_the_403_role_gate():
    # No Authorization header - should fail at the global auth gate
    # (401), never reach _require_admin's role check (403), proving the
    # two gates run in the right order.
    test_client = app_module.app.test_client()
    resp = test_client.get("/api/v1/admin/users")
    expect("unauthenticated request gets 401, not 403", 401, resp.status_code)
