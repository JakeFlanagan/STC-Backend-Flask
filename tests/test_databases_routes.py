"""Tests for /api/databases* routes (blueprints/databases.py): creating,
switching, deleting databases, and backup/restore/export.
"""

import io
import uuid

import app as app_module
import auth_middleware
import backup_crypto
from helpers import expect


def test_list_databases_includes_the_fixture_db(client):
    resp = client.get("/api/v1/databases")
    expect("status code", 200, resp.status_code)
    data = resp.get_json()
    expect("active db is in the list", True, data["active"] in data["databases"])


def test_create_database_success(client):
    resp = client.post("/api/v1/databases", json={"name": "a_new_test_db"})
    expect("status code", 200, resp.status_code)
    expect("returned name", "a_new_test_db", resp.get_json()["name"])

    listed = client.get("/api/v1/databases").get_json()["databases"]
    expect("new db appears in the list", True, "a_new_test_db" in listed)


def test_create_database_normalizes_name(client):
    resp = client.post("/api/v1/databases", json={"name": "My Cool DB"})
    expect("status code", 200, resp.status_code)
    expect("name normalized to lowercase+underscores", "my_cool_db", resp.get_json()["name"])


def test_create_database_rejects_invalid_name(client):
    resp = client.post("/api/v1/databases", json={"name": "!!!"})
    expect("status code", 400, resp.status_code)
    expect("error message present", True, "error" in resp.get_json())


def test_create_database_rejects_duplicate(client):
    client.post("/api/v1/databases", json={"name": "dupe_db"})
    resp = client.post("/api/v1/databases", json={"name": "dupe_db"})
    expect("status code", 409, resp.status_code)


def test_switch_database(client):
    client.post("/api/v1/databases", json={"name": "switch_target"})
    resp = client.post("/api/v1/databases/switch", json={"name": "switch_target"})
    expect("status code", 200, resp.status_code)
    expect("active db reported", "switch_target", resp.get_json()["active"])

    confirm = client.get("/api/v1/databases").get_json()
    expect("active db persisted", "switch_target", confirm["active"])


def test_switch_database_not_found(client):
    resp = client.post("/api/v1/databases/switch", json={"name": "does_not_exist_xyz"})
    expect("status code", 404, resp.status_code)


def test_delete_database(client):
    client.post("/api/v1/databases", json={"name": "to_delete"})
    client.post("/api/v1/databases", json={"name": "keep_me_active"})  # switches away from to_delete
    resp = client.delete("/api/v1/databases/to_delete")
    expect("status code", 200, resp.status_code)

    listed = client.get("/api/v1/databases").get_json()["databases"]
    expect("deleted db no longer listed", False, "to_delete" in listed)


def test_deleting_a_database_deletes_its_backups(client):
    client.post("/api/v1/databases", json={"name": "with_backups"})
    old_backup = client.post("/api/v1/databases/backup").get_json()["file"]
    listed = [b["file"] for b in client.get("/api/v1/databases/backups").get_json()]
    expect("backup listed", True, old_backup in listed)

    client.post("/api/v1/databases", json={"name": "elsewhere"})  # switch away
    expect("delete ok", 200, client.delete("/api/v1/databases/with_backups").status_code)

    # A new database with the same name must not inherit the old backups.
    # (It may get its own automatic daily backup on the next request.)
    client.post("/api/v1/databases", json={"name": "with_backups"})
    listed = [b["file"] for b in client.get("/api/v1/databases/backups").get_json()]
    expect("old backup gone", False, old_backup in listed)


def test_delete_active_database_fails(client):
    active = client.get("/api/v1/databases").get_json()["active"]
    resp = client.delete(f"/api/v1/databases/{active}")
    expect("status code", 400, resp.status_code)


def test_delete_database_not_found(client):
    resp = client.delete("/api/v1/databases/does_not_exist_xyz")
    expect("status code", 404, resp.status_code)


def test_manual_backup_creates_a_file(client):
    resp = client.post("/api/v1/databases/backup")
    expect("status code", 200, resp.status_code)
    data = resp.get_json()
    expect("response includes a filename", True, bool(data.get("file")))

    backups = client.get("/api/v1/databases/backups").get_json()
    names = [b["file"] for b in backups]
    expect("backup file is listed", True, data["file"] in names)


def test_list_backups_shape(client):
    client.post("/api/v1/databases/backup")
    backups = client.get("/api/v1/databases/backups").get_json()
    expect("at least one backup", True, len(backups) >= 1)
    b = backups[0]
    expect("backup entry has file key", True, "file" in b)
    expect("backup entry has size key", True, "size" in b)
    expect("backup entry has modified key", True, "modified" in b)


def test_restore_backup_round_trip(client):
    create = client.post("/api/v1/items", json={"name": "Restorable Item", "sale_price": 10})
    item_id = create.get_json()["item"]["id"]

    backup_file = client.post("/api/v1/databases/backup").get_json()["file"]

    client.delete(f"/api/v1/items/{item_id}")
    gone = client.get(f"/api/v1/items/{item_id}")
    expect("item deleted before restore", 404, gone.status_code)

    restore_resp = client.post("/api/v1/databases/restore", json={"file": backup_file})
    expect("restore status code", 200, restore_resp.status_code)

    restored = client.get(f"/api/v1/items/{item_id}")
    expect("item restored", 200, restored.status_code)
    expect("restored item name", "Restorable Item", restored.get_json()["name"])


def test_restore_backup_takes_a_pre_restore_snapshot(client):
    backup_file = client.post("/api/v1/databases/backup").get_json()["file"]

    before_count = len(client.get("/api/v1/databases/backups").get_json())
    client.post("/api/v1/databases/restore", json={"file": backup_file})
    after_count = len(client.get("/api/v1/databases/backups").get_json())

    expect("a new pre-restore snapshot was created", before_count + 1, after_count)


def test_restore_backup_rejects_path_traversal(client):
    resp = client.post("/api/v1/databases/restore", json={"file": "../../etc/passwd"})
    expect("status code", 404, resp.status_code)


def test_restore_backup_not_found(client):
    resp = client.post("/api/v1/databases/restore", json={"file": "nonexistent.db"})
    expect("status code", 404, resp.status_code)


def test_delete_backup(client):
    backup_file = client.post("/api/v1/databases/backup").get_json()["file"]

    resp = client.delete(f"/api/v1/databases/backups/{backup_file}")
    expect("status code", 200, resp.status_code)

    remaining = [b["file"] for b in client.get("/api/v1/databases/backups").get_json()]
    expect("backup no longer listed", False, backup_file in remaining)


def test_delete_backup_rejects_path_traversal(client):
    resp = client.delete("/api/v1/databases/backups/..%2F..%2Fapp.py")
    expect("status code", 404, resp.status_code)


def test_backups_are_isolated_per_database(client):
    # back up whatever database the fixture created...
    client.post("/api/v1/databases/backup")

    # ...then switch to a brand-new database - it should have one auto-backup
    # created by the before_request hook on the GET request below, and should
    # not inherit any from the previous active database. (Backup filenames are
    # just timestamps now with no name prefix, so this checks actual folder
    # isolation rather than filename uniqueness, which two backups taken in
    # the same second could coincidentally share.)
    client.post("/api/v1/databases", json={"name": "other_db_for_isolation_test"})
    other_db_backups = client.get("/api/v1/databases/backups").get_json()
    expect("a brand-new database has exactly one auto-backup from the GET request", 1, len(other_db_backups))


def test_export_database_requires_password(client):
    resp = client.post("/api/v1/databases/export", json={"password": "short"})
    expect("status code", 400, resp.status_code)


def test_export_database_returns_encrypted_data(client):
    resp = client.post("/api/v1/databases/export", json={"password": "export-password-1"})
    expect("status code", 200, resp.status_code)
    expect(
        "exported data is not a raw, readable sqlite file",
        False,
        resp.data.startswith(b"SQLite format 3"),
    )


def test_export_import_round_trip(client):
    export_resp = client.post("/api/v1/databases/export", json={"password": "roundtrip-password-1"})
    exported_bytes = export_resp.data

    import_resp = client.post(
        "/api/v1/databases/import",
        data={
            "password": "roundtrip-password-1",
            "name": "imported_copy",
            "file": (io.BytesIO(exported_bytes), "export.db.enc"),
        },
        content_type="multipart/form-data",
    )
    expect("import status code", 201, import_resp.status_code)
    expect("imported name", "imported_copy", import_resp.get_json()["name"])

    listed = client.get("/api/v1/databases").get_json()["databases"]
    expect("imported db appears in the list", True, "imported_copy" in listed)


def test_import_wrong_password_fails(client):
    export_resp = client.post("/api/v1/databases/export", json={"password": "right-password-1"})
    import_resp = client.post(
        "/api/v1/databases/import",
        data={
            "password": "wrong-password-1",
            "name": "should_not_import",
            "file": (io.BytesIO(export_resp.data), "export.db.enc"),
        },
        content_type="multipart/form-data",
    )
    expect("status code", 400, import_resp.status_code)


def test_import_rejects_invalid_name(client):
    export_resp = client.post("/api/v1/databases/export", json={"password": "some-password-1"})
    import_resp = client.post(
        "/api/v1/databases/import",
        data={
            "password": "some-password-1",
            "name": "!!!",
            "file": (io.BytesIO(export_resp.data), "export.db.enc"),
        },
        content_type="multipart/form-data",
    )
    expect("status code", 400, import_resp.status_code)


def test_import_duplicate_name_conflict(client):
    export_resp = client.post("/api/v1/databases/export", json={"password": "dup-password-1"})
    active_name = client.get("/api/v1/databases").get_json()["active"]
    import_resp = client.post(
        "/api/v1/databases/import",
        data={
            "password": "dup-password-1",
            "name": active_name,
            "file": (io.BytesIO(export_resp.data), "export.db.enc"),
        },
        content_type="multipart/form-data",
    )
    expect("status code", 409, import_resp.status_code)


def test_import_garbage_file_fails_header_check(client):
    garbage = backup_crypto.encrypt_with_password(b"not a sqlite file", "garbage-password-1")
    import_resp = client.post(
        "/api/v1/databases/import",
        data={
            "password": "garbage-password-1",
            "name": "garbage_import",
            "file": (io.BytesIO(garbage), "garbage.enc"),
        },
        content_type="multipart/form-data",
    )
    expect("status code", 400, import_resp.status_code)


def test_manual_backup_file_is_encrypted_not_raw_sqlite(client):
    client.post("/api/v1/databases/backup")
    backup_file = client.get("/api/v1/databases/backups").get_json()[0]["file"]
    expect("backup filename has .enc extension", True, backup_file.endswith(".db.enc"))


def test_switch_database_rejects_path_traversal_name(client):
    resp = client.post("/api/v1/databases/switch", json={"name": "../1/clearout"})
    expect("status code", 400, resp.status_code)


def test_delete_database_rejects_path_traversal_name(client):
    # A multi-segment payload like "../1/clearout" never even reaches
    # delete_database(): Flask's default <name> converter matches exactly
    # one path segment, so this simply doesn't match the route pattern and
    # 404s at the routing layer. A single traversal-like segment (e.g. "..")
    # does reach the view and is now caught by the explicit validation,
    # returning 400. Either way, no traversal succeeds.
    resp = client.delete("/api/v1/databases/../1/clearout")
    expect("multi-segment traversal doesn't match the route at all", 404, resp.status_code)

    resp = client.delete("/api/v1/databases/..")
    expect("single-segment traversal-like name is rejected by validation", 400, resp.status_code)


def test_switch_database_traversal_cannot_reach_another_users_database():
    victim = app_module.app.test_client()
    victim_username = f"traversal_victim_{uuid.uuid4().hex[:12]}"
    victim_auth = victim.post(
        "/api/v1/auth/register", json={"username": victim_username, "password": "victim-password-1"}
    )
    victim.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {victim_auth.get_json()['access_token']}"
    victim.post("/api/v1/databases", json={"name": "victim_secret_db"})
    victim_payload = auth_middleware.decode_access_token(victim_auth.get_json()["access_token"])
    assert victim_payload is not None
    victim_id = victim_payload["user_id"]

    attacker = app_module.app.test_client()
    attacker_username = f"traversal_attacker_{uuid.uuid4().hex[:12]}"
    attacker_auth = attacker.post(
        "/api/v1/auth/register", json={"username": attacker_username, "password": "attacker-password-1"}
    )
    attacker.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {attacker_auth.get_json()['access_token']}"

    resp = attacker.post(
        "/api/v1/databases/switch", json={"name": f"../{victim_id}/victim_secret_db"}
    )
    expect("traversal attempt rejected, not silently accepted", True, resp.status_code in (400, 404))

    confirm = attacker.get("/api/v1/databases").get_json()
    expect("attacker's active database was not changed to the victim's", True, confirm["active"] != f"../{victim_id}/victim_secret_db")


def test_import_rejects_oversized_upload(client, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 10)
    resp = client.post(
        "/api/v1/databases/import",
        data={
            "password": "some-password-1",
            "name": "too_big_import",
            "file": (io.BytesIO(b"this payload is more than 10 bytes long"), "big.enc"),
        },
        content_type="multipart/form-data",
    )
    expect("status code", 413, resp.status_code)
    expect("JSON error contract still applies", True, "error" in (resp.get_json() or {}))
