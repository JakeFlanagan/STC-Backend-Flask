"""Per-database settings: platform fees, shipping rates, thresholds.

Settings are stored one row per key in the `settings` table (see SCHEMA in
db.py), each value JSON-encoded - see default_settings() in db.py for what
keys exist and their shape. Named `settings_bp` for the same reason as
templates_bp: avoids any ambiguity with Flask's own naming conventions.
"""

import json

from flask import Blueprint, Response, jsonify, request

from db import default_settings, get_all_settings, get_db, get_settings_version, set_settings_version

settings_bp = Blueprint("settings_bp", __name__, url_prefix="/api/v1/settings")


@settings_bp.route("", methods=["GET"])
def get_settings() -> Response:
    """Return all settings for the active database, filling in any key
    that's missing (e.g. a new setting added after this database was
    created) with its current default rather than requiring a migration."""
    db = get_db()
    result = get_all_settings(db)
    result.pop("_version", None)
    for k, v in default_settings().items():
        if k not in result:
            result[k] = v
    result["version"] = get_settings_version(db)
    return jsonify(result)


@settings_bp.route("", methods=["PUT"])
def update_settings() -> Response | tuple[Response, int]:
    """Overwrite one or more settings keys. Only the keys present in the
    request body are touched; anything not sent is left as-is. Requires
    the caller's last-known version, so two concurrent editors can't
    silently overwrite each other's changes (see
    stc-frontend-flutter's sync engine, which is what this exists for)."""
    d = request.get_json(silent=True) or {}
    db = get_db()

    client_version = d.pop("version", None)
    if client_version is None:
        return jsonify({"error": "version is required"}), 400
    # `_version` is the reserved internal key the version counter itself is
    # stored under (see get_settings_version/set_settings_version, db.py) -
    # strip it from the body so a client can't overwrite it as if it were a
    # regular setting, regardless of write ordering below.
    d.pop("_version", None)

    current_version = get_settings_version(db)
    if client_version != current_version:
        result = get_all_settings(db)
        result.pop("_version", None)
        for k, v in default_settings().items():
            if k not in result:
                result[k] = v
        result["version"] = current_version
        return jsonify({"error": "version_conflict", "current": result}), 409

    for k, v in d.items():
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (k, json.dumps(v)),
        )
    new_version = current_version + 1
    set_settings_version(db, new_version)
    db.commit()
    return jsonify({"ok": True, "version": new_version})
