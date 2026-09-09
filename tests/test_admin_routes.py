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


def _client_with_database(db_name=None):
    """A plain-role client that owns one database, so the storage and
    backup routes have something real to find. Returns
    (client, user_id, database_name)."""
    plain = _plain_client()
    name = db_name or f"admindb_{uuid.uuid4().hex[:8]}"
    resp = plain.post("/api/v1/databases", json={"name": name})
    assert resp.status_code == 200, f"database setup failed: {resp.get_json()}"
    return plain, _user_id_of(plain), name


def _password_of(test_client):
    """The password _plain_client registers its accounts with - needed by
    tests that log in again as that account after an admin action."""
    return "plain-test-password-1"


# ---------------------------------------------------------------------
# Sessions on the users list
# ---------------------------------------------------------------------


def test_user_list_reports_each_account_active_session_count():
    admin = _admin_client()
    plain = _plain_client()  # registering issued exactly one refresh token
    user_id = _user_id_of(plain)

    rows = admin.get("/api/v1/admin/users").get_json()
    row = next(r for r in rows if r["id"] == user_id)
    expect("a freshly registered account holds one session", 1, row["sessions"])


def test_revoking_sessions_ends_them_without_changing_the_password():
    admin = _admin_client()
    plain = app_module.app.test_client()
    username = f"revoke_{uuid.uuid4().hex[:12]}"
    password = "revoke-test-password-1"
    reg = plain.post("/api/v1/auth/register", json={"username": username, "password": password})
    refresh_token = reg.get_json()["refresh_token"]
    user_id = auth_middleware.decode_access_token(reg.get_json()["access_token"])["user_id"]

    resp = admin.delete(f"/api/v1/admin/users/{user_id}/sessions")
    expect("revoke succeeds", 200, resp.status_code)
    expect("reports how many sessions it ended", 1, resp.get_json()["sessions_revoked"])

    refreshed = plain.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    expect("the revoked refresh token no longer works", 401, refreshed.status_code)

    again = plain.post("/api/v1/auth/login", json={"username": username, "password": password})
    expect("the same password still signs in - this is a sign-out, not a lock-out", 200, again.status_code)


def test_revoke_sessions_404s_for_an_unknown_account():
    admin = _admin_client()
    expect("unknown account", 404, admin.delete("/api/v1/admin/users/999999999/sessions").status_code)


# ---------------------------------------------------------------------
# Force password reset
# ---------------------------------------------------------------------


def test_admin_can_reset_another_accounts_password():
    admin = _admin_client()
    plain = _plain_client()
    user_id = _user_id_of(plain)
    username = auth_middleware.decode_access_token(
        plain.environ_base["HTTP_AUTHORIZATION"][len("Bearer "):]
    )["username"]
    new_password = "a-brand-new-password-1"

    resp = admin.post(f"/api/v1/admin/users/{user_id}/password", json={"new_password": new_password})
    expect("reset succeeds", 200, resp.status_code)
    expect("names the account it acted on", username, resp.get_json()["username"])

    fresh = app_module.app.test_client()
    expect(
        "the old password no longer works",
        401,
        fresh.post("/api/v1/auth/login", json={"username": username, "password": _password_of(plain)}).status_code,
    )
    expect(
        "the new password does",
        200,
        fresh.post("/api/v1/auth/login", json={"username": username, "password": new_password}).status_code,
    )


def test_password_reset_also_clears_a_lockout_on_that_account():
    # The whole point of the route is recovering a locked-out user, so a
    # reset that leaves them still locked out has done nothing useful.
    admin = _admin_client()
    username = f"lockedreset_{uuid.uuid4().hex[:12]}"
    plain = app_module.app.test_client()
    reg = plain.post("/api/v1/auth/register", json={"username": username, "password": "original-password-1"})
    user_id = auth_middleware.decode_access_token(reg.get_json()["access_token"])["user_id"]
    for _ in range(6):
        plain.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})
    expect(
        "account is locked out before the reset",
        429,
        plain.post("/api/v1/auth/login", json={"username": username, "password": "original-password-1"}).status_code,
    )

    admin.post(f"/api/v1/admin/users/{user_id}/password", json={"new_password": "recovered-password-1"})
    expect(
        "the new password works immediately, lockout and all",
        200,
        plain.post("/api/v1/auth/login", json={"username": username, "password": "recovered-password-1"}).status_code,
    )


def test_admin_cannot_reset_their_own_password_through_the_admin_route():
    admin = _admin_client()
    resp = admin.post(
        f"/api/v1/admin/users/{_user_id_of(admin)}/password",
        json={"new_password": "self-service-please-1"},
    )
    expect("refused, pointing at the self-service route", 400, resp.status_code)


def test_password_reset_rejects_a_short_password_and_an_unknown_account():
    admin = _admin_client()
    plain = _plain_client()
    expect(
        "too short",
        400,
        admin.post(f"/api/v1/admin/users/{_user_id_of(plain)}/password", json={"new_password": "short"}).status_code,
    )
    expect(
        "missing entirely",
        400,
        admin.post(f"/api/v1/admin/users/{_user_id_of(plain)}/password", json={}).status_code,
    )
    expect(
        "unknown account",
        404,
        admin.post("/api/v1/admin/users/999999999/password", json={"new_password": "long-enough-1"}).status_code,
    )


# ---------------------------------------------------------------------
# Lockout clearing
# ---------------------------------------------------------------------


def test_admin_can_clear_a_lockout_by_hand():
    admin = _admin_client()
    username = f"clearlock_{uuid.uuid4().hex[:12]}"
    password = "correct-password-1"
    plain = app_module.app.test_client()
    plain.post("/api/v1/auth/register", json={"username": username, "password": password})
    for _ in range(6):
        plain.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})
    expect(
        "locked out",
        429,
        plain.post("/api/v1/auth/login", json={"username": username, "password": password}).status_code,
    )

    cleared = admin.delete(f"/api/v1/admin/lockouts/{username}")
    expect("clear succeeds", 200, cleared.status_code)
    expect(
        "the account can log in again straight away",
        200,
        plain.post("/api/v1/auth/login", json={"username": username, "password": password}).status_code,
    )
    remaining = [row for row in admin.get("/api/v1/admin/lockouts").get_json() if row["username"] == username]
    expect("and is gone from the lockout list", 0, len(remaining))


def test_clearing_a_lockout_that_does_not_exist_is_a_404_not_a_cheerful_200():
    admin = _admin_client()
    resp = admin.delete(f"/api/v1/admin/lockouts/nobody_{uuid.uuid4().hex[:8]}")
    expect("no record to clear", 404, resp.status_code)


# ---------------------------------------------------------------------
# System snapshot
# ---------------------------------------------------------------------


def test_system_endpoint_reports_runtime_disk_config_and_account_figures():
    admin = _admin_client()
    body = admin.get("/api/v1/admin/system").get_json()
    expect("has a runtime section", True, "runtime" in body)
    expect("has a disk section", True, "disk" in body)
    expect("has a config section", True, "config" in body)
    expect("has an accounts section", True, "accounts" in body)
    expect("counts at least the admin account that just called it", True, body["accounts"]["users"] >= 1)
    expect("counts at least one admin", True, body["accounts"]["admins"] >= 1)
    expect("reports a python version", True, body["runtime"]["python_version"].startswith("3."))
    expect("reports free disk space", True, body["disk"]["filesystem_free_bytes"] > 0)
    expect("reports SMTP as a boolean, not a host", bool, type(body["config"]["smtp_configured"]))


def test_system_endpoint_never_returns_a_secret():
    # config.py keeps JWT_SECRET and DB_ENCRYPTION_KEY in the same
    # settings.cfg as the log level and the token lifetimes, so this is
    # the test that stops a future "just include the whole config" edit.
    import json

    import config

    admin = _admin_client()
    body = json.dumps(admin.get("/api/v1/admin/system").get_json())
    expect("JWT_SECRET does not appear anywhere in the response", False, config.JWT_SECRET in body)
    expect("DB_ENCRYPTION_KEY does not appear either", False, config.DB_ENCRYPTION_KEY in body)


def test_non_admin_gets_403_on_system_and_storage():
    plain = _plain_client()
    expect("system forbidden", 403, plain.get("/api/v1/admin/system").status_code)
    expect("storage forbidden", 403, plain.get("/api/v1/admin/storage").status_code)
    expect("storage backup forbidden", 403, plain.post("/api/v1/admin/storage/1/whatever/backup").status_code)
    expect("orphan delete forbidden", 403, plain.delete("/api/v1/admin/storage/orphans/1").status_code)
    expect("password reset forbidden", 403, plain.post("/api/v1/admin/users/1/password", json={}).status_code)
    expect("session revoke forbidden", 403, plain.delete("/api/v1/admin/users/1/sessions").status_code)
    expect("lockout clear forbidden", 403, plain.delete("/api/v1/admin/lockouts/someone").status_code)


# ---------------------------------------------------------------------
# Storage inventory
# ---------------------------------------------------------------------


def test_storage_lists_each_accounts_databases():
    admin = _admin_client()
    _, user_id, db_name = _client_with_database()

    body = admin.get("/api/v1/admin/storage").get_json()
    account = next(a for a in body["accounts"] if a["id"] == user_id)
    names = [d["name"] for d in account["databases"]]
    expect("the account's database is listed", True, db_name in names)
    expect("with a non-zero size", True, next(d for d in account["databases"] if d["name"] == db_name)["size_bytes"] > 0)
    expect("and a total across every account", True, body["totals"]["database_count"] >= 1)


def test_deleted_account_leaves_an_orphan_the_storage_screen_can_see():
    admin = _admin_client()
    _, user_id, _ = _client_with_database()

    deleted = admin.delete(f"/api/v1/admin/users/{user_id}")
    expect("delete succeeds", 200, deleted.status_code)
    expect("and reports what it left behind", True, deleted.get_json()["orphaned_databases"] >= 1)

    body = admin.get("/api/v1/admin/storage").get_json()
    orphan = next((o for o in body["orphans"] if o["user_id"] == user_id), None)
    expect("the orphaned directory is visible", True, orphan is not None)
    expect("with the databases still in it", True, orphan["database_count"] >= 1)


def test_orphan_deletion_refuses_while_the_account_still_exists():
    admin = _admin_client()
    _, user_id, _ = _client_with_database()
    resp = admin.delete(f"/api/v1/admin/storage/orphans/{user_id}")
    expect("refused - delete the account first", 400, resp.status_code)


def test_orphan_deletion_frees_the_directory_once_the_account_is_gone():
    admin = _admin_client()
    _, user_id, _ = _client_with_database()
    admin.delete(f"/api/v1/admin/users/{user_id}")

    resp = admin.delete(f"/api/v1/admin/storage/orphans/{user_id}")
    expect("delete succeeds", 200, resp.status_code)
    expect("and reports the bytes it freed", True, resp.get_json()["freed_bytes"] > 0)

    body = admin.get("/api/v1/admin/storage").get_json()
    expect(
        "the orphan is gone from the inventory",
        None,
        next((o for o in body["orphans"] if o["user_id"] == user_id), None),
    )
    expect("deleting it twice is a 404", 404, admin.delete(f"/api/v1/admin/storage/orphans/{user_id}").status_code)


# ---------------------------------------------------------------------
# On-demand backup of someone else's database
# ---------------------------------------------------------------------


def test_admin_backup_lands_in_that_users_own_backup_list():
    admin = _admin_client()
    plain, user_id, db_name = _client_with_database()

    resp = admin.post(f"/api/v1/admin/storage/{user_id}/{db_name}/backup")
    expect("backup succeeds", 200, resp.status_code)
    expect("the file is encrypted (.db.enc)", True, resp.get_json()["file"].endswith(".db.enc"))

    # The user's own /backups route lists backups of their ACTIVE
    # database, which the fixture just created and switched to.
    listed = [b["file"] for b in plain.get("/api/v1/databases/backups").get_json()]
    expect("the admin's backup shows up as one of the user's own", True, resp.get_json()["file"] in listed)


def test_admin_backup_rejects_a_bad_name_an_unknown_user_and_an_unknown_database():
    admin = _admin_client()
    _, user_id, _ = _client_with_database()
    expect(
        "a name that is not a valid filename",
        400,
        admin.post(f"/api/v1/admin/storage/{user_id}/not..valid/backup").status_code,
    )
    expect(
        "an account that does not exist",
        404,
        admin.post("/api/v1/admin/storage/999999999/anything/backup").status_code,
    )
    expect(
        "a database that account does not have",
        404,
        admin.post(f"/api/v1/admin/storage/{user_id}/nosuchdb/backup").status_code,
    )
