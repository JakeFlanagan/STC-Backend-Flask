"""Admin-only account management: list/disable/enable/delete other users.

Every route here requires the caller's JWT to carry role == "admin" (set
at token-issue time - see auth_middleware.generate_access_token). This is
authorization on top of the existing authentication gate (app.py's
_require_auth already guarantees g.user_id/g.role are set before any
route here runs) - _require_admin below is this blueprint's own
before_request hook, checked in addition to, not instead of, the global
one.
"""

from flask import Blueprint, Response, g, jsonify, request

import auth_db

admin_bp = Blueprint("admin", __name__, url_prefix="/api/v1/admin")


@admin_bp.before_request
def _require_admin() -> tuple[Response, int] | None:
    if request.method == "OPTIONS":
        return None
    if g.role != "admin":
        return jsonify({"error": "Admin access required"}), 403
    return None


@admin_bp.route("/users", methods=["GET"])
def list_users() -> Response:
    """List every account (id/username/role/disabled/created_at)."""
    db = auth_db.get_auth_db()
    return jsonify(auth_db.list_users(db))


@admin_bp.route("/lockouts", methods=["GET"])
def list_lockouts() -> Response:
    """List usernames currently locked out of login (see
    auth_db.record_login_failure's per-username lockout escalation)."""
    db = auth_db.get_auth_db()
    return jsonify(auth_db.list_lockouts(db))


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
    auto-deleted - see auth_db.delete_user's docstring."""
    if user_id == g.user_id:
        return jsonify({"error": "Cannot delete your own account"}), 400
    db = auth_db.get_auth_db()
    if not auth_db.delete_user(db, user_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})
