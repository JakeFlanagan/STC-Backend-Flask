"""Account storage: users and refresh tokens.

Kept in a dedicated auth.db, separate from the per-"database" SQLite files
under databases/ (see db.py) - this is account data shared across every
"database" a user creates, not per-project data. Mirrors db.py's
per-request connection pattern (connection cached on Flask's `g`, closed
at the end of every request via teardown_appcontext), but unlike db.py
there's exactly one auth.db - no "active" file to switch between.
"""

import sqlite3
from typing import Any

from flask import Flask, g
from werkzeug.security import check_password_hash, generate_password_hash

from config import BASE_DIR

AUTH_DB_PATH = BASE_DIR / "auth.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    email TEXT
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_failures (
    username TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_failure_at TEXT
);

CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""


def get_auth_db() -> sqlite3.Connection:
    """Return this request's connection to auth.db, cached on `g`. The
    schema is created (IF NOT EXISTS) on every fresh connection - cheap and
    idempotent, and avoids needing a separate one-time init step since
    there's no per-name "create a new database" flow here like db.py has.
    _migrate_add_role_and_disabled_columns and
    _migrate_add_tokens_revoked_at_column patch up an existing users table
    that predates those features, since CREATE TABLE IF NOT EXISTS never
    alters an already-existing table."""
    if "auth_db" not in g:
        g.auth_db = sqlite3.connect(str(AUTH_DB_PATH))
        g.auth_db.row_factory = sqlite3.Row
        g.auth_db.execute("PRAGMA foreign_keys=ON")
        # WAL mode lets readers and a writer proceed concurrently instead
        # of blocking on SQLite's default rollback-journal exclusive
        # write lock; busy_timeout makes a writer that does hit
        # contention (multiple gunicorn workers now the default - see
        # README's "Scaling beyond one worker") retry for up to 5s instead
        # of failing immediately with SQLITE_BUSY. Set on every connection
        # open, same as the schema/migrations right below - cheap, and
        # WAL mode is a database-file-level, not connection-level,
        # property anyway, so this is idempotent across workers.
        g.auth_db.execute("PRAGMA busy_timeout = 5000")
        g.auth_db.execute("PRAGMA journal_mode = WAL")
        g.auth_db.executescript(SCHEMA)
        _migrate_add_role_and_disabled_columns(g.auth_db)
        _migrate_add_tokens_revoked_at_column(g.auth_db)
        _migrate_add_last_failure_at_column(g.auth_db)
        _migrate_add_email_column(g.auth_db)
    return g.auth_db


def _migrate_add_role_and_disabled_columns(db: sqlite3.Connection) -> None:
    """Add the role/disabled columns to a users table that predates the
    admin-role feature. SQLite has no 'ADD COLUMN IF NOT EXISTS', so this
    checks PRAGMA table_info first and only alters whichever column, if
    any, is actually missing - cheap, idempotent, safe to run on every
    connection open."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "role" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if "disabled" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
    db.commit()


def _migrate_add_tokens_revoked_at_column(db: sqlite3.Connection) -> None:
    """Add the tokens_revoked_at column to a users table that predates
    access-token cascade revocation. Same idempotent PRAGMA table_info
    check as _migrate_add_role_and_disabled_columns - safe to run on
    every connection open."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "tokens_revoked_at" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN tokens_revoked_at TEXT")
    db.commit()


def _migrate_add_last_failure_at_column(db: sqlite3.Connection) -> None:
    """Add the last_failure_at column to a login_failures table that
    predates stale-sub-threshold-row pruning. Same idempotent
    PRAGMA table_info check as the other _migrate_* functions."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(login_failures)").fetchall()}
    if "last_failure_at" not in columns:
        db.execute("ALTER TABLE login_failures ADD COLUMN last_failure_at TEXT")
    db.commit()


def _migrate_add_email_column(db: sqlite3.Connection) -> None:
    """Add the email column (and its unique index) to a users table that
    predates the password-reset feature. SQLite disallows a UNIQUE
    constraint on a column added via ALTER TABLE ADD COLUMN, so the
    uniqueness constraint lives in a separate index instead - created
    here (after the column is guaranteed to exist) rather than as an
    inline constraint in SCHEMA, since a fresh install's CREATE TABLE
    also omits it for the same reason, and this way both paths converge
    on the exact same index. SQLite's UNIQUE index treats every NULL as
    distinct from every other NULL, so any number of accounts with no
    email on file (the common case for every account that predates this
    migration) can coexist without violating it."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "email" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN email TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    db.commit()


def get_tokens_revoked_at(db: sqlite3.Connection, user_id: int) -> str | None:
    """Return the timestamp after which user_id's previously-issued access
    tokens must be rejected (see auth_middleware.check_auth), or None if
    no cascade revoke or account-disable has ever happened for this
    user."""
    row = db.execute("SELECT tokens_revoked_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["tokens_revoked_at"] if row else None


def close_auth_db(exc: BaseException | None) -> None:
    """Close the request-scoped auth.db connection, if one was opened."""
    db = g.pop("auth_db", None)
    if db:
        db.close()


def init_app(app: Flask) -> None:
    """Wire this module's connection lifecycle into a Flask app. Called
    once from app.py at startup, same pattern as db.py's init_app."""
    app.teardown_appcontext(close_auth_db)


def create_user(
    db: sqlite3.Connection, username: str, password: str, role: str = "user", email: str | None = None
) -> dict[str, Any]:
    """Create a new user with a hashed password and the given role
    (defaults to the ordinary "user" role - only ensure_admin_account
    passes role="admin"). email is optional (None by default) - it only
    exists to support the password-reset flow (blueprints/auth.py), and
    plenty of accounts (everything created before that feature shipped)
    have none on file; those accounts just can't use self-service reset
    until an email is set via set_user_email. Raises ValueError if the
    username or email is already taken (caught by blueprints/auth.py's
    register())."""
    try:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, email) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, email),
        )
    except sqlite3.IntegrityError as e:
        if "users.email" in str(e):
            raise ValueError(f"email {email!r} is already in use") from None
        raise ValueError(f"username {username!r} is already taken") from None
    db.commit()
    assert cur.lastrowid is not None  # INSERT always populates lastrowid in sqlite3
    return {"id": cur.lastrowid, "username": username, "role": role, "email": email}


# A real, fixed-cost password hash with no matching account - compared
# against on every verify_user() call where no row was found, so a
# nonexistent username takes the same time to reject as a wrong password
# for a real one. Without this, skipping check_password_hash entirely for
# a missing row would let a timing measurement distinguish "no such user"
# from "wrong password", even though both already return the identical
# response (see verify_user's docstring) - the timing side-channel is
# invisible to the response body/status but not to a clock.
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-password-for-timing-safety")


def verify_user(db: sqlite3.Connection, username: str, password: str) -> dict[str, Any] | None:
    """Return {"id", "username", "role"} if the credentials are valid and
    the account isn't disabled, else None - the caller (blueprints/auth.py's
    login()) uses the same 401 message in every failure case (wrong
    username, wrong password, or disabled account), so it doesn't matter
    to the caller which case this is. check_password_hash always runs,
    even for a nonexistent username (against _DUMMY_PASSWORD_HASH), so
    every failure case takes the same time too - see
    _DUMMY_PASSWORD_HASH's comment."""
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    hash_to_check = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
    valid = check_password_hash(hash_to_check, password)
    if row is None or not valid or row["disabled"]:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def store_refresh_token(db: sqlite3.Connection, user_id: int, token_hash: str, expires_at: str) -> None:
    """Persist a refresh token's hash, first pruning user_id's own expired
    refresh tokens so the table doesn't grow unbounded - every
    register/login/refresh call reaches this function, so an active
    account cleans up after itself every time it authenticates. expires_at
    is a 'YYYY-MM-DD HH:MM:SS' UTC string, same format SQLite's own
    datetime('now') produces, so get_valid_refresh_token's SQL comparison
    works without any format conversion.

    Deliberately does NOT also prune revoked-but-unexpired rows: a
    rotated-out refresh token's row has to survive until its natural
    expiry so get_reused_refresh_token_owner can still find it if it gets
    replayed later (that's the reuse-detection signal refresh() uses to
    cascade-revoke a user's other sessions) - pruning it immediately on the
    very next refresh would erase that signal before it could ever fire.
    """
    db.execute(
        "DELETE FROM refresh_tokens WHERE user_id = ? AND expires_at <= datetime('now')",
        (user_id,),
    )
    db.execute(
        "INSERT INTO refresh_tokens (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash, user_id, expires_at),
    )
    db.commit()


def get_valid_refresh_token(db: sqlite3.Connection, token_hash: str) -> dict[str, Any] | None:
    """Return {"user_id", "username", "role"} for a live (unrevoked,
    unexpired, not-disabled-account) refresh token, else None - covers
    "doesn't exist", "expired", "revoked", and "account disabled" all the
    same way, since callers treat every case identically. The disabled
    check here (not just at login) is what makes disabling an account
    block an already-logged-in session's next /refresh immediately,
    rather than waiting for that refresh token to expire on its own."""
    row = db.execute(
        """SELECT rt.user_id, u.username, u.role FROM refresh_tokens rt
           JOIN users u ON u.id = rt.user_id
           WHERE rt.token_hash = ? AND rt.revoked = 0 AND rt.expires_at > datetime('now')
           AND u.disabled = 0""",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    return {"user_id": row["user_id"], "username": row["username"], "role": row["role"]}


def revoke_refresh_token(db: sqlite3.Connection, token_hash: str) -> None:
    """Mark a refresh token revoked. A no-op (no error) if the hash
    doesn't exist - callers already check get_valid_refresh_token() first
    whenever that distinction matters."""
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
    db.commit()


def get_reused_refresh_token_owner(db: sqlite3.Connection, token_hash: str) -> int | None:
    """Return the user_id token_hash belongs to if it names a refresh token
    that exists and is already revoked, else None. blueprints/auth.py's
    refresh() calls this once get_valid_refresh_token has already rejected
    the token, to tell "reuse of a rotated-out token" (possible theft:
    someone replayed a token this user's own client already rotated away
    from) apart from "never issued / long expired" (unremarkable). Expiry
    doesn't factor in here: even an expired-but-revoked token still proves
    whoever holds it once had a legitimate one, which is the only thing
    that matters for this check."""
    row = db.execute(
        "SELECT user_id, revoked FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None or not row["revoked"]:
        return None
    return row["user_id"]


def revoke_all_refresh_tokens_for_user(db: sqlite3.Connection, user_id: int) -> None:
    """Revoke every refresh token belonging to user_id - called when a
    rotated-out refresh token gets reused (see was_refresh_token_revoked),
    since that's a signal the user's whole set of sessions may be
    compromised, not just the one request that got rejected. Also stamps
    tokens_revoked_at on the user's row: a refresh token revoke alone
    can't reach an already-issued access token (it's stateless, not
    stored anywhere check_auth would see it) - tokens_revoked_at is what
    makes check_auth reject one minted before this call, on its very next
    use, instead of letting it ride out its remaining TTL."""
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
    db.execute("UPDATE users SET tokens_revoked_at = datetime('now') WHERE id = ?", (user_id,))
    db.commit()


# Per-username backstop on top of rate_limit.py's per-IP throttling - a
# distributed attacker (many source IPs) isn't slowed by per-IP limits
# alone. The first _LOGIN_LOCKOUT_FREE_ATTEMPTS failures never lock (the
# per-IP limiter already covers casual single-source abuse); each failure
# past that doubles the lockout window, capped at _LOGIN_LOCKOUT_CAP_SECONDS.
_LOGIN_LOCKOUT_FREE_ATTEMPTS = 5
_LOGIN_LOCKOUT_BASE_SECONDS = 30
_LOGIN_LOCKOUT_CAP_SECONDS = 3600


def record_login_failure(db: sqlite3.Connection, username: str) -> None:
    """Record one failed login attempt for username, escalating a lockout
    once failure_count passes _LOGIN_LOCKOUT_FREE_ATTEMPTS. The
    increment itself is a single atomic UPSERT (failure_count =
    failure_count + 1 ... RETURNING), not a separate SELECT-then-INSERT -
    SQLite serializes writers, so this can't lose a concurrent increment
    the way a read-then-write pair could (reachable now that multi-worker
    gunicorn is the default - see README's "Scaling beyond one worker").

    Also prunes stale rows for other usernames: locked_until more than a
    day in the past (same as before), OR - new - a row that never reached
    lockout (locked_until IS NULL) whose last_failure_at is more than a
    day old. Without the second clause, a low-and-slow distributed
    credential-stuffing spray (many usernames, a few attempts each, never
    enough to lock) would never get pruned at all."""
    db.execute(
        "DELETE FROM login_failures WHERE username != ? AND ("
        "(locked_until IS NOT NULL AND locked_until <= datetime('now', '-1 day')) OR "
        "(locked_until IS NULL AND last_failure_at IS NOT NULL "
        "AND last_failure_at <= datetime('now', '-1 day'))"
        ")",
        (username,),
    )
    failure_count = db.execute(
        "INSERT INTO login_failures (username, failure_count, last_failure_at) "
        "VALUES (?, 1, datetime('now')) "
        "ON CONFLICT(username) DO UPDATE SET "
        "failure_count = failure_count + 1, last_failure_at = datetime('now') "
        "RETURNING failure_count",
        (username,),
    ).fetchone()[0]
    if failure_count > _LOGIN_LOCKOUT_FREE_ATTEMPTS:
        lockout_seconds = min(
            _LOGIN_LOCKOUT_BASE_SECONDS * 2 ** (failure_count - _LOGIN_LOCKOUT_FREE_ATTEMPTS),
            _LOGIN_LOCKOUT_CAP_SECONDS,
        )
        db.execute(
            "UPDATE login_failures SET locked_until = datetime('now', ?) WHERE username = ?",
            (f"+{lockout_seconds} seconds", username),
        )
    db.commit()


def login_lockout_remaining(db: sqlite3.Connection, username: str) -> str | None:
    """Return username's locked_until timestamp if it's currently locked
    out, else None."""
    row = db.execute(
        "SELECT locked_until FROM login_failures WHERE username = ? "
        "AND locked_until IS NOT NULL AND locked_until > datetime('now')",
        (username,),
    ).fetchone()
    return row["locked_until"] if row else None


def clear_login_failures(db: sqlite3.Connection, username: str) -> None:
    """Clear username's failure record - called on a successful login, so
    the next wrong attempt starts counting from zero again."""
    db.execute("DELETE FROM login_failures WHERE username = ?", (username,))
    db.commit()


def ensure_admin_account(db: sqlite3.Connection, username: str, password: str) -> None:
    """Create the admin account from settings.cfg's ADMIN_USERNAME/
    ADMIN_PASSWORD if no user with that username exists yet - idempotent,
    safe to call on every startup (see app.py's __main__ block). Does
    nothing once the account exists, even if ADMIN_PASSWORD has since
    changed in settings.cfg - this only ever creates the account, it never
    resets a password."""
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if row is not None:
        return
    try:
        create_user(db, username, password, role="admin")
    except ValueError:
        pass  # created between the SELECT and here - already exists, nothing to do


def list_users(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every account's id/username/role/disabled/created_at/email,
    oldest first - the data GET /api/v1/admin/users (blueprints/admin.py)
    surfaces."""
    rows = db.execute(
        "SELECT id, username, role, disabled, created_at, email FROM users ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "username": r["username"],
            "role": r["role"],
            "disabled": bool(r["disabled"]),
            "created_at": r["created_at"],
            "email": r["email"],
        }
        for r in rows
    ]


def list_lockouts(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every username currently locked out of login (locked_until
    in the future), soonest-expiring first - the data
    GET /api/v1/admin/lockouts (blueprints/admin.py) surfaces. Closes the
    TODO.md gap where an operator investigating "I can't log in" had to
    query auth.db directly."""
    rows = db.execute(
        "SELECT username, locked_until FROM login_failures "
        "WHERE locked_until IS NOT NULL AND locked_until > datetime('now') "
        "ORDER BY locked_until"
    ).fetchall()
    return [{"username": r["username"], "locked_until": r["locked_until"]} for r in rows]


def set_user_disabled(db: sqlite3.Connection, user_id: int, disabled: bool) -> bool:
    """Set a user's disabled flag. Returns False if no user with that id
    exists (the caller, blueprints/admin.py, turns that into a 404), True
    on success. A disabled user's future login/refresh attempts fail (see
    verify_user/get_valid_refresh_token). Disabling (not re-enabling) also
    stamps tokens_revoked_at, so an already-issued access token is
    rejected by check_auth on its very next use too, not just future
    logins/refreshes - see revoke_all_refresh_tokens_for_user's docstring
    for why that column exists. Re-enabling clears tokens_revoked_at back
    to NULL: leaving a stale value in place forever would mean any login
    issued in the very same wall-clock second as a disable-then-re-enable
    cycle could be spuriously rejected by check_auth's fail-closed <=
    comparison - clearing it removes that comparison entirely for every
    login after the account is enabled again."""
    cur = db.execute("UPDATE users SET disabled = ? WHERE id = ?", (1 if disabled else 0, user_id))
    if disabled:
        db.execute("UPDATE users SET tokens_revoked_at = datetime('now') WHERE id = ?", (user_id,))
    else:
        db.execute("UPDATE users SET tokens_revoked_at = NULL WHERE id = ?", (user_id,))
    db.commit()
    return cur.rowcount > 0


def delete_user(db: sqlite3.Connection, user_id: int) -> bool:
    """Permanently delete a user's account and refresh tokens. Their
    databases/<user_id>/ directory on disk is deliberately left untouched -
    this backend never auto-destroys data, only ever auto-orphans it (same
    precedent as subsystem 2b's legacy per-user directories and the
    backup-encryption feature's legacy plaintext backups). Returns False
    if no user with that id exists (the caller, blueprints/admin.py, turns
    that into a 404)."""
    db.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
    cur = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return cur.rowcount > 0


def get_user_by_username(db: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    """Return {"id", "username", "email", "disabled"} for username, or
    None if no such account exists. Used by the password-reset request
    route (blueprints/auth.py) to look up an account's email without
    needing its password - unlike verify_user, this never checks a
    password, so callers must not use it anywhere that stands in for
    authentication."""
    row = db.execute(
        "SELECT id, username, email, disabled FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "email": row["email"], "disabled": bool(row["disabled"])}


def set_user_email(db: sqlite3.Connection, user_id: int, email: str) -> bool:
    """Set (or change) a user's email - the only way an account created
    before this feature shipped (with no email on file) can opt into
    self-service password reset. Returns False if no user with that id
    exists, or if email is already in use by a different account (caught
    via the idx_users_email unique index - see
    _migrate_add_email_column); True on success."""
    try:
        cur = db.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
    except sqlite3.IntegrityError:
        return False
    db.commit()
    return cur.rowcount > 0


def create_password_reset_token(db: sqlite3.Connection, user_id: int, token_hash: str, expires_at: str) -> None:
    """Persist a password-reset token's hash. First prunes every
    already-expired token table-wide (self-cleanup, same pattern as
    store_refresh_token/record_login_failure) and invalidates any of this
    user's own still-unused tokens - only the most recently requested
    reset link should ever work, so an old email lying around (or
    clicking "forgot password" twice) can't leave two live links at
    once."""
    db.execute("DELETE FROM password_resets WHERE expires_at <= datetime('now')")
    db.execute("DELETE FROM password_resets WHERE user_id = ? AND used = 0", (user_id,))
    db.execute(
        "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash, user_id, expires_at),
    )
    db.commit()


def get_valid_password_reset(db: sqlite3.Connection, token_hash: str) -> int | None:
    """Return the user_id a live (unused, unexpired) password-reset token
    belongs to, else None."""
    row = db.execute(
        "SELECT user_id FROM password_resets WHERE token_hash = ? AND used = 0 AND expires_at > datetime('now')",
        (token_hash,),
    ).fetchone()
    return row["user_id"] if row else None


def consume_password_reset(db: sqlite3.Connection, token_hash: str, new_password: str) -> bool:
    """Atomically set the account's new password, mark the token used,
    and cascade-revoke every existing session (refresh tokens +
    already-issued access tokens, via tokens_revoked_at) - a password
    reset is a "assume compromise, start clean" event, the same reasoning
    revoke_all_refresh_tokens_for_user already applies to reused refresh
    tokens. Returns False if the token isn't currently valid (already
    used, expired, or never existed) - the caller (blueprints/auth.py)
    turns that into a single generic error response, without
    distinguishing which case it was."""
    user_id = get_valid_password_reset(db, token_hash)
    if user_id is None:
        return False
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user_id))
    db.execute("UPDATE password_resets SET used = 1 WHERE token_hash = ?", (token_hash,))
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
    db.execute("UPDATE users SET tokens_revoked_at = datetime('now') WHERE id = ?", (user_id,))
    db.commit()
    return True
