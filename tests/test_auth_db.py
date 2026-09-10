"""Tests for auth_db.py's account storage: schema, connection lifecycle,
and the user/refresh-token CRUD helpers. Called directly (not via HTTP) so
these test the storage layer in isolation from blueprints/auth.py's route
logic (covered separately in tests/test_auth_routes.py).
"""

import sqlite3
import uuid

import app as app_module
import auth_db
from helpers import expect

# Initialize auth_db teardown callback on the app to prevent connection leaks in tests.
# This is idempotent and safe; the real app.py also calls this in Task 3.
auth_db.init_app(app_module.app)


def _unique_username() -> str:
    return f"user_{uuid.uuid4().hex[:12]}"


def test_create_and_verify_user():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()

        user = auth_db.create_user(db, username, "correct-password")
        expect("created user has username", username, user["username"])

        verified = auth_db.verify_user(db, username, "correct-password")
        expect("verify with correct password succeeds", username, verified["username"] if verified else None)

        wrong = auth_db.verify_user(db, username, "wrong-password")
        expect("verify with wrong password fails", None, wrong)


def test_create_user_duplicate_username_raises():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()
        auth_db.create_user(db, username, "password123")

        raised = False
        try:
            auth_db.create_user(db, username, "different-password")
        except ValueError:
            raised = True
        expect("duplicate username raises ValueError", True, raised)


def test_refresh_token_lifecycle():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")

        auth_db.store_refresh_token(db, user["id"], "some-token-hash", "2099-01-01 00:00:00")
        valid = auth_db.get_valid_refresh_token(db, "some-token-hash")
        expect("stored token is valid", user["id"], valid["user_id"] if valid else None)

        auth_db.revoke_refresh_token(db, "some-token-hash")
        revoked = auth_db.get_valid_refresh_token(db, "some-token-hash")
        expect("revoked token is no longer valid", None, revoked)


def test_expired_refresh_token_is_invalid():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")

        auth_db.store_refresh_token(db, user["id"], "expired-token-hash", "2000-01-01 00:00:00")
        result = auth_db.get_valid_refresh_token(db, "expired-token-hash")
        expect("expired token is invalid", None, result)


def test_unknown_refresh_token_is_invalid():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        result = auth_db.get_valid_refresh_token(db, "no-such-hash")
        expect("unknown token is invalid", None, result)


def test_verify_user_uses_dummy_hash_for_nonexistent_user(monkeypatch):
    calls = []
    original = auth_db.check_password_hash

    def spy(pw_hash, password):
        calls.append(pw_hash)
        return original(pw_hash, password)

    monkeypatch.setattr(auth_db, "check_password_hash", spy)

    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        result = auth_db.verify_user(db, _unique_username(), "whatever-password")

    expect("verify returns None for nonexistent user", None, result)
    expect("check_password_hash was still called once", 1, len(calls))
    expect("dummy hash was used, not skipped", auth_db._DUMMY_PASSWORD_HASH, calls[0])


def test_store_refresh_token_prunes_expired_but_keeps_revoked_unexpired_rows():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")

        auth_db.store_refresh_token(db, user["id"], "expired-hash", "2000-01-01 00:00:00")
        auth_db.store_refresh_token(db, user["id"], "revoked-hash", "2099-01-01 00:00:00")
        auth_db.revoke_refresh_token(db, "revoked-hash")

        # Storing a new token prunes the expired row above, but must leave
        # the revoked-but-unexpired row alone - it has to survive until its
        # natural expiry so get_reused_refresh_token_owner can still detect
        # a replay of it (see store_refresh_token's docstring).
        auth_db.store_refresh_token(db, user["id"], "new-hash", "2099-01-01 00:00:00")

        remaining = {
            r["token_hash"]
            for r in db.execute(
                "SELECT token_hash FROM refresh_tokens WHERE user_id = ?", (user["id"],)
            ).fetchall()
        }
        expect("expired row pruned", False, "expired-hash" in remaining)
        expect("revoked-but-unexpired row kept", True, "revoked-hash" in remaining)
        expect("new row present", True, "new-hash" in remaining)


def test_get_reused_refresh_token_owner():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")

        auth_db.store_refresh_token(db, user["id"], "live-hash", "2099-01-01 00:00:00")
        expect(
            "unrevoked token has no reuse owner",
            None,
            auth_db.get_reused_refresh_token_owner(db, "live-hash"),
        )

        auth_db.revoke_refresh_token(db, "live-hash")
        expect(
            "revoked token's owner is returned",
            user["id"],
            auth_db.get_reused_refresh_token_owner(db, "live-hash"),
        )

        expect(
            "unknown hash has no reuse owner",
            None,
            auth_db.get_reused_refresh_token_owner(db, "never-issued-hash"),
        )


def test_migration_adds_role_and_disabled_columns_to_old_schema(tmp_path):
    # Isolated raw connection/file (not the shared session auth.db), same
    # reasoning as tests/test_migrations.py's isolated_db_dir fixture -
    # avoids touching the real users table other tests in this session
    # rely on.
    path = tmp_path / "old_shape_auth.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.commit()

    auth_db._migrate_add_role_and_disabled_columns(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    expect("role column added", True, "role" in columns)
    expect("disabled column added", True, "disabled" in columns)
    conn.close()


def test_create_user_defaults_to_user_role():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")
        expect("default role is 'user'", "user", user["role"])


def test_create_user_accepts_explicit_role():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123", role="admin")
        expect("explicit role honored", "admin", user["role"])


def test_verify_user_returns_role():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()
        auth_db.create_user(db, username, "password123", role="admin")
        verified = auth_db.verify_user(db, username, "password123")
        expect("verify_user returns the account's role", "admin", verified["role"] if verified else None)


def test_verify_user_rejects_disabled_account():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()
        user = auth_db.create_user(db, username, "password123")
        auth_db.set_user_disabled(db, user["id"], True)
        result = auth_db.verify_user(db, username, "password123")
        expect("disabled account cannot verify even with correct password", None, result)


def test_get_valid_refresh_token_rejects_disabled_account():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")
        auth_db.store_refresh_token(db, user["id"], "disabled-user-hash", "2099-01-01 00:00:00")

        auth_db.set_user_disabled(db, user["id"], True)
        result = auth_db.get_valid_refresh_token(db, "disabled-user-hash")
        expect("disabled account's refresh token is rejected", None, result)


def test_get_valid_refresh_token_includes_role():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123", role="admin")
        auth_db.store_refresh_token(db, user["id"], "role-check-hash", "2099-01-01 00:00:00")
        valid = auth_db.get_valid_refresh_token(db, "role-check-hash")
        expect("refresh token lookup includes role", "admin", valid["role"] if valid else None)


def test_ensure_admin_account_creates_once_and_is_idempotent():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()
        auth_db.ensure_admin_account(db, username, "admin-password-123")
        user = auth_db.verify_user(db, username, "admin-password-123")
        expect("admin account created with admin role", "admin", user["role"] if user else None)

        # A second call with a different password must be a no-op - it
        # never resets an existing account's password.
        auth_db.ensure_admin_account(db, username, "a-totally-different-password")
        still_original = auth_db.verify_user(db, username, "admin-password-123")
        expect(
            "second call is a no-op - original password still works",
            username,
            still_original["username"] if still_original else None,
        )


def test_list_users_includes_created_accounts():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()
        user = auth_db.create_user(db, username, "password123")
        users = auth_db.list_users(db)
        matching = [u for u in users if u["id"] == user["id"]]
        expect("created user appears in list_users", 1, len(matching))
        expect(
            "listed user has expected shape",
            {"id", "username", "role", "disabled", "created_at", "email"},
            set(matching[0].keys()),
        )


def test_set_user_disabled_toggles_flag_and_reports_not_found():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")

        expect("disabling an existing user returns True", True, auth_db.set_user_disabled(db, user["id"], True))
        users = {u["id"]: u for u in auth_db.list_users(db)}
        expect("user is now disabled", True, users[user["id"]]["disabled"])

        expect("re-enabling returns True", True, auth_db.set_user_disabled(db, user["id"], False))
        users = {u["id"]: u for u in auth_db.list_users(db)}
        expect("user is now enabled again", False, users[user["id"]]["disabled"])

        expect("disabling a nonexistent id returns False", False, auth_db.set_user_disabled(db, -1, True))


def test_delete_user_removes_account_and_refresh_tokens():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")
        auth_db.store_refresh_token(db, user["id"], "to-be-deleted-hash", "2099-01-01 00:00:00")

        expect("deleting an existing user returns True", True, auth_db.delete_user(db, user["id"]))

        remaining_users = [u for u in auth_db.list_users(db) if u["id"] == user["id"]]
        expect("user no longer listed", 0, len(remaining_users))
        remaining_tokens = db.execute(
            "SELECT 1 FROM refresh_tokens WHERE user_id = ?", (user["id"],)
        ).fetchall()
        expect("user's refresh tokens also deleted", 0, len(remaining_tokens))

        expect("deleting a nonexistent id returns False", False, auth_db.delete_user(db, -1))


def test_delete_user_removes_outstanding_password_reset_tokens():
    """password_resets has a REFERENCES users(id) and no ON DELETE
    CASCADE, and get_auth_db() turns foreign keys on, so leaving a reset
    token behind is not an untidy row - the DELETE itself raises
    IntegrityError and the admin route returns a 500. Any account that
    ever used the reset flow would be undeletable."""
    from auth_middleware import password_reset_expiry_timestamp

    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")
        auth_db.create_password_reset_token(db, user["id"], "outstanding-hash", password_reset_expiry_timestamp())

        expect("delete succeeds with a reset token outstanding", True, auth_db.delete_user(db, user["id"]))

        remaining = db.execute("SELECT 1 FROM password_resets WHERE user_id = ?", (user["id"],)).fetchall()
        expect("user's reset tokens also deleted", 0, len(remaining))


def test_record_login_failure_locks_after_free_attempts():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()

        for _ in range(5):
            auth_db.record_login_failure(db, username)
        expect(
            "still unlocked after 5 free attempts",
            None,
            auth_db.login_lockout_remaining(db, username),
        )

        auth_db.record_login_failure(db, username)
        expect(
            "locked after 6th failure",
            True,
            auth_db.login_lockout_remaining(db, username) is not None,
        )


def test_clear_login_failures_removes_lockout():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()

        for _ in range(6):
            auth_db.record_login_failure(db, username)
        expect(
            "locked before clear",
            True,
            auth_db.login_lockout_remaining(db, username) is not None,
        )

        auth_db.clear_login_failures(db, username)
        expect("unlocked after clear", None, auth_db.login_lockout_remaining(db, username))


def test_login_lockout_is_per_username():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        locked_username = _unique_username()
        other_username = _unique_username()

        for _ in range(6):
            auth_db.record_login_failure(db, locked_username)

        expect(
            "locked username is locked",
            True,
            auth_db.login_lockout_remaining(db, locked_username) is not None,
        )
        expect(
            "unrelated username is unaffected",
            None,
            auth_db.login_lockout_remaining(db, other_username),
        )


def test_set_user_disabled_false_clears_tokens_revoked_at():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123")

        auth_db.set_user_disabled(db, user["id"], True)
        expect(
            "tokens_revoked_at set after disabling",
            True,
            auth_db.get_tokens_revoked_at(db, user["id"]) is not None,
        )

        auth_db.set_user_disabled(db, user["id"], False)
        expect(
            "tokens_revoked_at cleared after re-enabling",
            None,
            auth_db.get_tokens_revoked_at(db, user["id"]),
        )


def test_migration_adds_tokens_revoked_at_column_to_old_schema(tmp_path):
    path = tmp_path / "old_shape_auth_2.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            disabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.commit()

    auth_db._migrate_add_tokens_revoked_at_column(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    expect("tokens_revoked_at column added", True, "tokens_revoked_at" in columns)
    conn.close()


def test_get_auth_db_sets_busy_timeout_and_wal_mode():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        busy_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        expect("busy_timeout is set to 5 seconds", 5000, busy_timeout)
        expect("journal_mode is WAL", "wal", journal_mode)


def test_migration_adds_last_failure_at_column_to_old_schema(tmp_path):
    path = tmp_path / "old_shape_login_failures.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE login_failures (
            username TEXT PRIMARY KEY,
            failure_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT
        )"""
    )
    conn.commit()

    auth_db._migrate_add_last_failure_at_column(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(login_failures)").fetchall()}
    expect("last_failure_at column added", True, "last_failure_at" in columns)
    conn.close()


def test_record_login_failure_increments_failure_count_atomically():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()

        for _ in range(3):
            auth_db.record_login_failure(db, username)

        row = db.execute(
            "SELECT failure_count, last_failure_at FROM login_failures WHERE username = ?",
            (username,),
        ).fetchone()
        expect("failure_count reflects every recorded failure", 3, row["failure_count"])
        expect("last_failure_at is stamped", True, row["last_failure_at"] is not None)


def test_record_login_failure_prunes_stale_sub_threshold_rows_for_other_usernames():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        stale_username = _unique_username()
        active_username = _unique_username()

        auth_db.record_login_failure(db, stale_username)  # never locked (1 < 5 free attempts)
        db.execute(
            "UPDATE login_failures SET last_failure_at = datetime('now', '-2 days') WHERE username = ?",
            (stale_username,),
        )
        db.commit()

        auth_db.record_login_failure(db, active_username)  # triggers the prune pass

        remaining = db.execute(
            "SELECT 1 FROM login_failures WHERE username = ?", (stale_username,)
        ).fetchone()
        expect("stale sub-threshold row was pruned", None, remaining)


def test_record_login_failure_lockout_escalates_and_caps():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()

        for _ in range(11):  # 5 free attempts + 6 escalating failures
            auth_db.record_login_failure(db, username)
        row = db.execute(
            "SELECT failure_count, locked_until FROM login_failures WHERE username = ?",
            (username,),
        ).fetchone()
        expect("failure_count reflects all 11 recorded failures", 11, row["failure_count"])
        remaining_seconds = db.execute(
            "SELECT (julianday(?) - julianday('now')) * 86400", (row["locked_until"],)
        ).fetchone()[0]
        # 11th failure: lockout_seconds = min(30 * 2**(11-5), 3600) = min(1920, 3600) = 1920
        expect("lockout window matches the doubling formula, not yet capped", True, 1900 <= remaining_seconds <= 1920)

        for _ in range(4):  # push well past where doubling would exceed the cap
            auth_db.record_login_failure(db, username)
        row = db.execute(
            "SELECT locked_until FROM login_failures WHERE username = ?", (username,)
        ).fetchone()
        remaining_seconds = db.execute(
            "SELECT (julianday(?) - julianday('now')) * 86400", (row["locked_until"],)
        ).fetchone()[0]
        # 15th failure: lockout_seconds = min(30 * 2**10, 3600) = min(30720, 3600) = 3600
        expect("lockout window capped at _LOGIN_LOCKOUT_CAP_SECONDS", True, 3590 <= remaining_seconds <= 3600)


def test_list_lockouts_returns_only_currently_locked_usernames():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        locked_username = _unique_username()
        unlocked_username = _unique_username()

        for _ in range(6):
            auth_db.record_login_failure(db, locked_username)
        auth_db.record_login_failure(db, unlocked_username)  # only 1 failure, never locked

        lockouts = {row["username"]: row for row in auth_db.list_lockouts(db)}
        expect("locked username is present", True, locked_username in lockouts)
        expect("unlocked username is absent", False, unlocked_username in lockouts)
        expect(
            "listed lockout has expected shape",
            {"username", "locked_until"},
            set(lockouts[locked_username].keys()),
        )


def _unique_email() -> str:
    return f"{uuid.uuid4().hex[:12]}@example.com"


def test_migration_adds_email_column_to_old_schema(tmp_path):
    path = tmp_path / "old_shape_email.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            disabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.commit()

    auth_db._migrate_add_email_column(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    expect("email column added", True, "email" in columns)
    # Two NULL emails must coexist (accounts that predate this feature) -
    # proves the uniqueness index doesn't collide NULL with NULL.
    conn.execute("INSERT INTO users (username, password_hash) VALUES ('a', 'x')")
    conn.execute("INSERT INTO users (username, password_hash) VALUES ('b', 'x')")
    conn.commit()
    conn.close()


def test_create_user_with_email_then_duplicate_email_is_rejected():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        email = _unique_email()
        user = auth_db.create_user(db, _unique_username(), "password123", email=email)
        expect("email stored", email, user["email"])
        try:
            auth_db.create_user(db, _unique_username(), "password123", email=email)
            raised = False
        except ValueError as e:
            raised = True
            expect("error message names email", True, "email" in str(e))
        expect("duplicate email raises ValueError", True, raised)


def test_get_user_by_username_returns_none_for_unknown_username():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        expect("unknown username returns None", None, auth_db.get_user_by_username(db, _unique_username()))


def test_get_user_by_username_returns_account_shape():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        username = _unique_username()
        email = _unique_email()
        created = auth_db.create_user(db, username, "password123", email=email)
        found = auth_db.get_user_by_username(db, username)
        expect("id matches", created["id"], found["id"] if found else None)
        expect("email matches", email, found["email"] if found else None)
        expect("not disabled", False, found["disabled"] if found else None)


def test_set_user_email_updates_and_rejects_duplicate():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user_a = auth_db.create_user(db, _unique_username(), "password123")
        user_b = auth_db.create_user(db, _unique_username(), "password123")
        email = _unique_email()

        expect("setting a fresh email succeeds", True, auth_db.set_user_email(db, user_a["id"], email))
        found = auth_db.get_user_by_username(db, user_a["username"])
        expect("email persisted", email, found["email"] if found else None)

        expect(
            "setting the same email on a different account fails",
            False,
            auth_db.set_user_email(db, user_b["id"], email),
        )
        expect("setting email on a nonexistent id returns False", False, auth_db.set_user_email(db, -1, "x@example.com"))


def test_password_reset_token_lifecycle():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "old-password-1", email=_unique_email())

        from auth_middleware import generate_password_reset_token, password_reset_expiry_timestamp

        plaintext, token_hash = generate_password_reset_token()
        auth_db.create_password_reset_token(db, user["id"], token_hash, password_reset_expiry_timestamp())

        expect(
            "valid token resolves to the right user_id",
            user["id"],
            auth_db.get_valid_password_reset(db, token_hash),
        )

        auth_db.store_refresh_token(db, user["id"], "reset-lifecycle-hash", "2099-01-01 00:00:00")
        expect("password reset succeeds", True, auth_db.consume_password_reset(db, token_hash, "new-password-1"))

        verified_old = auth_db.verify_user(db, user["username"], "old-password-1")
        expect("old password no longer works", None, verified_old)
        verified_new = auth_db.verify_user(db, user["username"], "new-password-1")
        expect("new password works", user["username"], verified_new["username"] if verified_new else None)

        expect(
            "existing refresh token revoked by the reset",
            None,
            auth_db.get_valid_refresh_token(db, "reset-lifecycle-hash"),
        )
        expect(
            "tokens_revoked_at stamped by the reset",
            True,
            auth_db.get_tokens_revoked_at(db, user["id"]) is not None,
        )

        expect("the same token cannot be used twice", False, auth_db.consume_password_reset(db, token_hash, "third-password-1"))


def test_consume_password_reset_rejects_unknown_token():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        expect(
            "unknown token hash is rejected",
            False,
            auth_db.consume_password_reset(db, "never-issued-hash", "some-password-1"),
        )


def test_create_password_reset_token_invalidates_previous_unused_token_for_same_user():
    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        user = auth_db.create_user(db, _unique_username(), "password123", email=_unique_email())

        from auth_middleware import password_reset_expiry_timestamp

        auth_db.create_password_reset_token(db, user["id"], "first-reset-hash", password_reset_expiry_timestamp())
        auth_db.create_password_reset_token(db, user["id"], "second-reset-hash", password_reset_expiry_timestamp())

        expect("older token invalidated", None, auth_db.get_valid_password_reset(db, "first-reset-hash"))
        expect("newer token still valid", user["id"], auth_db.get_valid_password_reset(db, "second-reset-hash"))
