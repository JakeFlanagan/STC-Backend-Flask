"""Database management: create/switch/delete/backup/restore/export.

Routes here operate on the collection of .db files under databases/<user_id>/
(scoped to g.user_id - see db.py's user_db_dir) and their backups under
databases/<user_id>/backups/ - not on the rows inside any one database.
Item-level routes live in blueprints/items.py instead.
"""

import io
import os
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, Response, g, jsonify, request, send_file

import backup_crypto
from config import DB_ENCRYPTION_KEY, MAX_BACKUPS_PER_DB
from db import get_active_db_name, init_database, is_valid_db_name, set_active_db, user_db_dir

databases_bp = Blueprint("databases", __name__, url_prefix="/api/v1/databases")

MIN_EXPORT_PASSWORD_LENGTH = 8


def db_backup_dir(user_id: int, name: str) -> Path:
    """The backup subfolder for one of user_id's databases (e.g.
    databases/<user_id>/backups/clearout/), created on first use. Every
    database's backups live in their own subfolder, so filenames within it
    never need a name prefix to avoid colliding with another database's
    backups - the folder is the boundary."""
    d = user_db_dir(user_id) / "backups" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def auto_backup(user_id: int) -> None:
    """Take one encrypted backup of user_id's active database per
    calendar day, and prune old ones.

    Called from app.py's _daily_auto_backup before_request hook on every
    authenticated request - cheap because it no-ops instantly once today's
    backup already exists. Keeps at most MAX_BACKUPS_PER_DB backups per
    database, oldest first. Encrypted with config.DB_ENCRYPTION_KEY (see
    backup_crypto.py) - there's no human present in this code path to ask
    for a password. Note: pre-existing plaintext .db backup files from
    before this encryption feature shipped are left on disk untouched but
    are no longer visible through list_backups or restorable via /restore,
    which only recognize encrypted *.db.enc files - not deleted, just
    orphaned, following the same precedent as subsystem 2b's per-user-isolation.
    """
    name = get_active_db_name(user_id)
    if not name:
        return
    src = user_db_dir(user_id) / f"{name}.db"
    if not src.exists():
        return
    backup_dir = db_backup_dir(user_id, name)
    today = date.today().isoformat()
    dst = backup_dir / f"{today}.db.enc"
    if dst.exists():
        return
    dst.write_bytes(backup_crypto.encrypt_with_secret(src.read_bytes(), DB_ENCRYPTION_KEY))
    # prune old backups for this db
    backups = sorted(backup_dir.glob("*.db.enc"))
    while len(backups) > MAX_BACKUPS_PER_DB:
        os.remove(backups.pop(0))


@databases_bp.route("", methods=["GET"])
def list_databases() -> Response:
    """List every database file (by name, no extension) belonging to the
    current user, and which one is active."""
    dbs = [p.stem for p in sorted(user_db_dir(g.user_id).glob("*.db"))]
    active = get_active_db_name(g.user_id)
    return jsonify({"databases": dbs, "active": active})


@databases_bp.route("", methods=["POST"])
def create_database() -> Response | tuple[Response, int]:
    """Create a new, empty database for the current user and make it the
    active one.

    Names are normalized to lowercase-with-underscores and restricted to
    alphanumerics, so they're always safe to use directly as a filename.
    """
    name = (request.get_json(silent=True) or {}).get("name", "").strip().lower().replace(" ", "_")
    if not is_valid_db_name(name):
        return jsonify({"error": "Invalid name"}), 400
    if (user_db_dir(g.user_id) / f"{name}.db").exists():
        return jsonify({"error": "Already exists"}), 409
    init_database(g.user_id, name)
    return jsonify({"ok": True, "name": name})


@databases_bp.route("/switch", methods=["POST"])
def switch_database() -> Response | tuple[Response, int]:
    """Make an existing database (belonging to the current user) the
    active one for all their future requests."""
    name = (request.get_json(silent=True) or {}).get("name", "")
    if not is_valid_db_name(name):
        return jsonify({"error": "Invalid name"}), 400
    if not (user_db_dir(g.user_id) / f"{name}.db").exists():
        return jsonify({"error": "Not found"}), 404
    set_active_db(g.user_id, name)
    # force new connection - otherwise this request (and any already-open
    # connection cached on g) would keep talking to the old database
    db = g.pop("db", None)
    if db:
        db.close()
    return jsonify({"ok": True, "active": name})


@databases_bp.route("/<name>", methods=["DELETE"])
def delete_database(name: str) -> Response | tuple[Response, int]:
    """Permanently delete one of the current user's database files.
    Refuses to delete the active one (switch to a different database
    first) to avoid leaving the app with no active database mid-request."""
    if not is_valid_db_name(name):
        return jsonify({"error": "Invalid name"}), 400
    path = user_db_dir(g.user_id) / f"{name}.db"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    active = get_active_db_name(g.user_id)
    if name == active:
        return jsonify({"error": "Cannot delete active database"}), 400
    path.unlink()
    return jsonify({"ok": True})


@databases_bp.route("/backup", methods=["POST"])
def manual_backup() -> Response | tuple[Response, int]:
    """Take an on-demand, timestamped, encrypted backup of the current
    user's active database (the "Backup" button in the top bar),
    independent of the once-a-day auto_backup."""
    name = get_active_db_name(g.user_id)
    if not name:
        return jsonify({"error": "No active database"}), 400
    src = user_db_dir(g.user_id) / f"{name}.db"
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = db_backup_dir(g.user_id, name) / f"{ts}.db.enc"
    dst.write_bytes(backup_crypto.encrypt_with_secret(src.read_bytes(), DB_ENCRYPTION_KEY))
    return jsonify({"ok": True, "file": dst.name})


@databases_bp.route("/backups", methods=["GET"])
def list_backups() -> Response:
    """List every encrypted backup file for the current user's active
    database, newest first."""
    name = get_active_db_name(g.user_id)
    if not name:
        return jsonify([])
    result = []
    for p in sorted(db_backup_dir(g.user_id, name).glob("*.db.enc"), reverse=True):
        stat = p.stat()
        result.append({
            "file": p.name,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return jsonify(result)


@databases_bp.route("/restore", methods=["POST"])
def restore_backup() -> Response | tuple[Response, int]:
    """Overwrite the current user's active database with one of its own
    (encrypted) backups.

    This is destructive (it replaces the live database file), so a
    pre-restore snapshot of the *current* state is taken first (also
    encrypted), meaning a restore can itself always be undone by restoring
    that snapshot.

    The requested filename is reduced to just its basename before being
    looked up inside this database's own backup subfolder, so this can't be
    used to read or overwrite a file outside it (or another database's
    backups) regardless of what path the caller sends.
    """
    name = get_active_db_name(g.user_id)
    if not name:
        return jsonify({"error": "No active database"}), 400
    filename = Path((request.get_json(silent=True) or {}).get("file", "")).name
    backup_dir = db_backup_dir(g.user_id, name)
    backup_path = backup_dir / filename
    if not backup_path.exists():
        return jsonify({"error": "Backup not found"}), 404

    try:
        decrypted = backup_crypto.decrypt_with_secret(backup_path.read_bytes(), DB_ENCRYPTION_KEY)
    except backup_crypto.DecryptionError:
        return jsonify({"error": "Backup could not be decrypted"}), 400

    # snapshot the current state first, in case the restore was a mistake
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = user_db_dir(g.user_id) / f"{name}.db"
    pre_restore = backup_crypto.encrypt_with_secret(dst.read_bytes(), DB_ENCRYPTION_KEY)
    (backup_dir / f"pre-restore_{ts}.db.enc").write_bytes(pre_restore)

    db = g.pop("db", None)  # close any open connection before overwriting the file
    if db:
        db.close()
    dst.write_bytes(decrypted)
    return jsonify({"ok": True})


@databases_bp.route("/backups/<path:filename>", methods=["DELETE"])
def delete_backup(filename: str) -> Response | tuple[Response, int]:
    """Delete one backup file for the current user's active database.

    Same basename-only lookup as restore_backup(), for the same reason:
    the active database's own backup subfolder is the isolation boundary,
    so a path-traversal attempt just resolves to a filename that isn't
    there and 404s.
    """
    name = get_active_db_name(g.user_id)
    if not name:
        return jsonify({"error": "Not found"}), 404
    safe_name = Path(filename).name
    path = db_backup_dir(g.user_id, name) / safe_name
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    path.unlink()
    return jsonify({"ok": True})


@databases_bp.route("/export", methods=["POST"])
def export_database() -> Response | tuple[Response, int]:
    """Download the current user's active database, encrypted with a
    caller-supplied password (used for e.g. moving it to another machine -
    see /import for bringing one back in). The password never leaves this
    request; only whoever has it can decrypt the exported file."""
    name = get_active_db_name(g.user_id)
    if not name:
        return jsonify({"error": "No active database"}), 400
    password = (request.get_json(silent=True) or {}).get("password", "")
    if len(password) < MIN_EXPORT_PASSWORD_LENGTH:
        return jsonify({"error": f"password must be at least {MIN_EXPORT_PASSWORD_LENGTH} characters"}), 400
    path = user_db_dir(g.user_id) / f"{name}.db"
    encrypted = backup_crypto.encrypt_with_password(path.read_bytes(), password)
    return send_file(
        io.BytesIO(encrypted),
        as_attachment=True,
        download_name=f"{name}.db.enc",
        mimetype="application/octet-stream",
    )


@databases_bp.route("/import", methods=["POST"])
def import_database() -> Response | tuple[Response, int]:
    """Decrypt an uploaded export (see /export) with a caller-supplied
    password and create it as a new database for the current user, made
    active - the counterpart to /export for bringing data onto a different
    machine.

    Name validation matches create_database exactly - the imported name
    becomes a real filename under this user's own directory, so the same
    alphanumeric+underscore restriction applies for the same reason.
    """
    uploaded = request.files.get("file")
    password = request.form.get("password", "")
    name = request.form.get("name", "").strip().lower().replace(" ", "_")

    if uploaded is None:
        return jsonify({"error": "file is required"}), 400
    if not is_valid_db_name(name):
        return jsonify({"error": "Invalid name"}), 400
    if len(password) < MIN_EXPORT_PASSWORD_LENGTH:
        return jsonify({"error": f"password must be at least {MIN_EXPORT_PASSWORD_LENGTH} characters"}), 400
    if (user_db_dir(g.user_id) / f"{name}.db").exists():
        return jsonify({"error": "Already exists"}), 409

    try:
        decrypted = backup_crypto.decrypt_with_password(uploaded.read(), password)
    except backup_crypto.DecryptionError:
        return jsonify({"error": "Could not decrypt file - wrong password or corrupted file"}), 400

    if not decrypted.startswith(backup_crypto.SQLITE_HEADER):
        return jsonify({"error": "Decrypted file is not a valid database"}), 400

    path = user_db_dir(g.user_id) / f"{name}.db"
    path.write_bytes(decrypted)
    set_active_db(g.user_id, name)
    return jsonify({"ok": True, "name": name}), 201
