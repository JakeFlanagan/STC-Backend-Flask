"""Admin-only management of accounts, sessions, lockouts and stored data.

Every route here requires the caller's JWT to carry role == "admin" (set
at token-issue time - see auth_middleware.generate_access_token). This is
authorization on top of the existing authentication gate (app.py's
_require_auth already guarantees g.user_id/g.role are set before any
route here runs) - _require_admin below is this blueprint's own
before_request hook, checked in addition to, not instead of, the global
one.

Three groups of routes, in this file's order:

  accounts   list, disable, enable, delete, force-reset a password, and
             revoke every session an account holds.
  lockouts   list the usernames currently locked out of login, and clear
             one by hand.
  storage    what is on disk, per account, plus the two things nothing
             else on this backend can reach: an on-demand encrypted
             backup of any account's database, and deletion of the
             directories auth_db.delete_user deliberately orphans.

Several of these act on data belonging to someone other than the caller,
so each destructive one names what it acted on in its response rather
than returning a bare {"ok": true} - an admin panel showing "deleted 3
databases for user 7 (sam)" is auditable in a way "ok" is not.

Routes that act on the caller's OWN account are refused, not special-cased.
An admin resetting their own password or deleting their own account
through here would be doing it without the current-password check
blueprints/auth.py insists on, and the self-service routes for both
already exist.
"""

import shutil
from datetime import datetime

from flask import Blueprint, Response, g, jsonify, request

import auth_db
import backup_crypto
import config
import system_info
from blueprints.auth import MIN_PASSWORD_LENGTH
from blueprints.databases import db_backup_dir
from db import is_valid_db_name

admin_bp = Blueprint("admin", __name__, url_prefix="/api/v1/admin")


@admin_bp.before_request
def _require_admin() -> tuple[Response, int] | None:
    if request.method == "OPTIONS":
        return None
    if g.role != "admin":
        return jsonify({"error": "Admin access required"}), 403
    return None


# ---------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------


@admin_bp.route("/users", methods=["GET"])
def list_users() -> Response:
    """List every account (id/username/role/disabled/created_at/email),
    each annotated with how many live sessions it currently holds - one
    unrevoked, unexpired refresh token being one signed-in client. The
    count comes from a single grouped query rather than one per user (see
    auth_db.active_session_counts)."""
    db = auth_db.get_auth_db()
    sessions = auth_db.active_session_counts(db)
    users = auth_db.list_users(db)
    return jsonify([{**u, "sessions": sessions.get(u["id"], 0)} for u in users])


@admin_bp.route("/users/<int:user_id>/disable", methods=["POST"])
def disable_user(user_id: int) -> Response | tuple[Response, int]:
    """Disable an account - blocks its future login/refresh. An
    already-issued access token keeps working until it naturally expires
    (see auth_db.set_user_disabled's docstring)."""
    if user_id == g.user_id:
        return jsonify({"error": "Cannot disable your own account"}), 400
    db = auth_db.get_auth_db()
    if not auth_db.set_user_disabled(db, user_id, True):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@admin_bp.route("/users/<int:user_id>/enable", methods=["POST"])
def enable_user(user_id: int) -> Response | tuple[Response, int]:
    """Re-enable a previously-disabled account."""
    db = auth_db.get_auth_db()
    if not auth_db.set_user_disabled(db, user_id, False):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id: int) -> Response | tuple[Response, int]:
    """Permanently delete an account (and its refresh tokens). Their
    database files are deliberately left orphaned on disk, never
    auto-deleted - see auth_db.delete_user's docstring. GET /storage
    below is what makes those orphans visible afterwards, and
    DELETE /storage/orphans/<user_id> is the deliberate second step that
    removes them."""
    if user_id == g.user_id:
        return jsonify({"error": "Cannot delete your own account"}), 400
    db = auth_db.get_auth_db()
    user = auth_db.get_user(db, user_id)
    if user is None or not auth_db.delete_user(db, user_id):
        return jsonify({"error": "Not found"}), 404
    # What the account left behind, so the panel can offer to clean it
    # up in the same breath rather than leaving the operator to discover
    # it on the storage screen weeks later.
    left_behind = system_info.orphan_summary(user_id)
    return jsonify(
        {
            "ok": True,
            "username": user["username"],
            "orphaned_bytes": left_behind["total_bytes"],
            "orphaned_databases": left_behind["database_count"],
        }
    )


@admin_bp.route("/users/<int:user_id>/password", methods=["POST"])
def reset_user_password(user_id: int) -> Response | tuple[Response, int]:
    """Set another account's password, without knowing the old one.

    This is the operator's answer to "I am locked out and email reset does
    not work here" - SMTP is optional in this backend (config.SMTP_HOST
    can be blank), and when it is unset the entire password-reset flow in
    blueprints/auth.py is a no-op, leaving no way to recover an account
    short of editing auth.db by hand.

    Refused for the caller's own account: /api/v1/auth/password already
    does that, and does it properly, by requiring the current password.
    Bypassing that check on yourself turns a stolen admin token into
    permanent ownership of the admin account.

    Every session on the target account ends (auth_db.set_user_password),
    including any access token already issued to it - which is the point
    when the reason for the reset is a suspected compromise.
    """
    d = request.get_json(silent=True) or {}
    new_password = d.get("new_password", "")
    if user_id == g.user_id:
        return jsonify({"error": "Use /api/v1/auth/password to change your own password"}), 400
    if not new_password:
        return jsonify({"error": "new_password is required"}), 400
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    db = auth_db.get_auth_db()
    user = auth_db.get_user(db, user_id)
    if user is None:
        return jsonify({"error": "Not found"}), 404
    auth_db.set_user_password(db, user_id, new_password)
    # A locked-out account stays locked after a reset unless this is
    # cleared: the lockout counts failures per username, and knows nothing
    # about the password having changed underneath it. Clearing it here is
    # what makes the new password immediately usable, which is the whole
    # point of the route.
    auth_db.clear_login_failures(db, user["username"])
    return jsonify({"ok": True, "username": user["username"], "sessions_revoked": True})


@admin_bp.route("/users/<int:user_id>/sessions", methods=["DELETE"])
def revoke_user_sessions(user_id: int) -> Response | tuple[Response, int]:
    """End every session an account holds, without changing its password.

    Revokes its refresh tokens and stamps tokens_revoked_at, so an access
    token already in someone's hands is rejected on its very next use
    rather than riding out its remaining TTL (see
    auth_db.revoke_all_refresh_tokens_for_user). The account can sign
    straight back in with the same password - this is "sign that laptop
    out", not "lock them out".

    Allowed on the caller's own account, unlike the password route: the
    worst case is signing yourself out, which is recoverable by logging
    back in, and an admin who wants to drop their own stale sessions
    should not have to delete their account to do it.
    """
    db = auth_db.get_auth_db()
    user = auth_db.get_user(db, user_id)
    if user is None:
        return jsonify({"error": "Not found"}), 404
    revoked = auth_db.active_session_counts(db).get(user_id, 0)
    auth_db.revoke_all_refresh_tokens_for_user(db, user_id)
    return jsonify({"ok": True, "username": user["username"], "sessions_revoked": revoked})


# ---------------------------------------------------------------------
# Lockouts
# ---------------------------------------------------------------------


@admin_bp.route("/lockouts", methods=["GET"])
def list_lockouts() -> Response:
    """List usernames currently locked out of login (see
    auth_db.record_login_failure's per-username lockout escalation)."""
    db = auth_db.get_auth_db()
    return jsonify(auth_db.list_lockouts(db))


@admin_bp.route("/lockouts/<username>", methods=["DELETE"])
def clear_lockout(username: str) -> Response | tuple[Response, int]:
    """Clear one username's lockout and its failure count, letting them
    try again immediately.

    404 when there is no failure record for that username at all, rather
    than a cheerful 200: the usual cause is a typo in the username, and an
    operator who has just "cleared" a lockout that is in fact still in
    force will spend the next ten minutes debugging the wrong thing.

    Note this takes a username, not a user id - lockouts are recorded
    against whatever username was typed at the login form, which need not
    be a real account (see auth_db.record_login_failure). Clearing a
    lockout for a username nobody owns is meaningful and allowed.
    """
    db = auth_db.get_auth_db()
    if not auth_db.clear_lockout(db, username):
        return jsonify({"error": "No lockout record for that username"}), 404
    return jsonify({"ok": True, "username": username})


# ---------------------------------------------------------------------
# System and storage
# ---------------------------------------------------------------------


@admin_bp.route("/system", methods=["GET"])
def system() -> Response:
    """Runtime, disk and account figures for the admin overview.

    Read-only, and deliberately secret-free: see system_info's module
    docstring for why the config half names its fields by hand instead of
    dumping settings.cfg.
    """
    db = auth_db.get_auth_db()
    return jsonify(
        {
            "runtime": system_info.runtime_summary(),
            "disk": system_info.disk_summary(),
            "config": system_info.config_summary(),
            "accounts": auth_db.account_stats(db),
        }
    )


@admin_bp.route("/storage", methods=["GET"])
def storage() -> Response:
    """Every database file on disk, grouped by the account that owns it,
    plus orphaned directories and legacy flat files (see
    system_info.storage_inventory)."""
    db = auth_db.get_auth_db()
    return jsonify(system_info.storage_inventory(auth_db.list_users(db)))


@admin_bp.route("/storage/<int:user_id>/<name>/backup", methods=["POST"])
def backup_user_database(user_id: int, name: str) -> Response | tuple[Response, int]:
    """Take an on-demand encrypted backup of any account's database.

    The same operation blueprints/databases.py's /backup route performs for
    the caller's own active database, aimed at someone else's - the one
    thing an operator wants before doing anything irreversible to an
    account they do not own.

    Encrypted with config.DB_ENCRYPTION_KEY, like every other backup this
    backend writes, and lands in that user's own backups directory, so it
    shows up in their own backup list and restores through their own
    /restore route. Nothing here is readable by the admin who pressed the
    button, which is the intended limit: this is a safety net, not an
    export of somebody else's books.
    """
    if not is_valid_db_name(name):
        return jsonify({"error": "Invalid database name"}), 400
    db = auth_db.get_auth_db()
    if auth_db.get_user(db, user_id) is None:
        return jsonify({"error": "Not found"}), 404
    src = system_info.database_path(user_id, name)
    if not src.exists():
        return jsonify({"error": "Not found"}), 404
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = db_backup_dir(user_id, name) / f"{ts}.db.enc"
    dst.write_bytes(backup_crypto.encrypt_with_secret(src.read_bytes(), config.DB_ENCRYPTION_KEY))
    return jsonify({"ok": True, "file": dst.name, "size_bytes": dst.stat().st_size})


@admin_bp.route("/storage/orphans/<int:user_id>", methods=["DELETE"])
def delete_orphan(user_id: int) -> Response | tuple[Response, int]:
    """Permanently delete the data directory of an account that no longer
    exists.

    This is the only route in the whole backend that destroys somebody's
    databases, and it is unrecoverable - the files are not moved aside,
    and the encrypted backups inside the directory go with them.

    Two things make it safe enough to exist. It refuses any user_id that
    still has an account, so it can only ever touch what
    auth_db.delete_user already orphaned; and the path is built from an
    integer route parameter, so there is no name here that could be made
    to point outside DB_DIR.

    Deleting the account first and the files second, as two explicit
    steps, is the deliberate design (see auth_db.delete_user's docstring):
    it means "I deleted the wrong user" is a mistake you can still walk
    back from, right up until this call.
    """
    db = auth_db.get_auth_db()
    if auth_db.get_user(db, user_id) is not None:
        return jsonify({"error": "That user still exists - delete the account first"}), 400
    target = config.DB_DIR / str(user_id)
    if not target.is_dir():
        return jsonify({"error": "Not found"}), 404
    freed = system_info.dir_size(target)
    shutil.rmtree(target)
    return jsonify({"ok": True, "user_id": user_id, "freed_bytes": freed})
