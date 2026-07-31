"""Cross-user data isolation tests for subsystem 2b: confirms two
different registered accounts never see or affect each other's databases,
despite hitting the exact same routes. Each test builds its own two
authenticated clients directly (rather than using the shared `client`
fixture in tests/conftest.py, which only gives one identity) so both
identities are under the test's explicit control.
"""

import uuid

import app as app_module
from helpers import expect


def _authenticated_client():
    """A fresh Flask test client, registered and authenticated as its own
    brand-new throwaway user. Mirrors tests/conftest.py's `client` fixture
    but without its database-creation step, so callers can control
    exactly when/whether a database gets created."""
    test_client = app_module.app.test_client()
    username = f"isoluser_{uuid.uuid4().hex[:12]}"
    resp = test_client.post(
        "/api/v1/auth/register", json={"username": username, "password": "isolation-password-1"}
    )
    assert resp.status_code == 201
    test_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {resp.get_json()['access_token']}"
    return test_client


def test_two_users_can_create_a_database_with_the_same_name():
    alice = _authenticated_client()
    bob = _authenticated_client()

    resp_a = alice.post("/api/v1/databases", json={"name": "shared_name"})
    resp_b = bob.post("/api/v1/databases", json={"name": "shared_name"})
    expect("alice's create succeeds", 200, resp_a.status_code)
    expect("bob's create succeeds too - no collision with alice's db", 200, resp_b.status_code)


def test_users_list_only_their_own_databases():
    alice = _authenticated_client()
    bob = _authenticated_client()

    alice.post("/api/v1/databases", json={"name": "alice_only_db"})
    bob.post("/api/v1/databases", json={"name": "bob_only_db"})

    alice_list = alice.get("/api/v1/databases").get_json()["databases"]
    bob_list = bob.get("/api/v1/databases").get_json()["databases"]
    expect("alice sees her own database", True, "alice_only_db" in alice_list)
    expect("alice does not see bob's database", False, "bob_only_db" in alice_list)
    expect("bob sees his own database", True, "bob_only_db" in bob_list)
    expect("bob does not see alice's database", False, "alice_only_db" in bob_list)


def test_user_cannot_switch_to_another_users_database():
    alice = _authenticated_client()
    bob = _authenticated_client()

    alice.post("/api/v1/databases", json={"name": "alices_private_db"})
    resp = bob.post("/api/v1/databases/switch", json={"name": "alices_private_db"})
    expect("bob's switch to alice's database 404s, as if it doesn't exist", 404, resp.status_code)


def test_user_cannot_delete_another_users_database():
    alice = _authenticated_client()
    bob = _authenticated_client()

    alice.post("/api/v1/databases", json={"name": "alices_db_to_protect"})
    resp = bob.delete("/api/v1/databases/alices_db_to_protect")
    expect("bob's delete of alice's database 404s", 404, resp.status_code)

    still_there = alice.get("/api/v1/databases").get_json()["databases"]
    expect("alice's database is untouched", True, "alices_db_to_protect" in still_there)


def test_export_reflects_the_caller_own_active_database_not_anothers():
    alice = _authenticated_client()
    bob = _authenticated_client()

    alice.post("/api/v1/databases", json={"name": "alices_export_target"})
    bob.post("/api/v1/databases", json={"name": "bobs_export_target"})

    bob_export = bob.post("/api/v1/databases/export", json={"password": "bobs-export-password-1"})
    expect("bob's export succeeds", 200, bob_export.status_code)
    disposition = bob_export.headers.get("Content-Disposition") or ""
    expect("bob's export is named after his own database", True, "bobs_export_target" in disposition)
    expect("bob's export is never alice's database", False, "alices_export_target" in disposition)


def test_user_cannot_see_another_users_backups():
    alice = _authenticated_client()
    bob = _authenticated_client()

    alice.post("/api/v1/databases", json={"name": "alice_backup_db"})
    alice.post("/api/v1/databases/backup")

    bob.post("/api/v1/databases", json={"name": "bob_backup_db"})
    bob_backups = bob.get("/api/v1/databases/backups").get_json()
    expect("bob has exactly his own auto-created backup, none of alice's", 1, len(bob_backups))
