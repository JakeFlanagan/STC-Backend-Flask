"""Reusable listing description templates.

Templates are just saved snippets of text grouped by category (e.g. "Retro
Gaming"), copy-pasted into a real Vinted/eBay listing via the "Copy text"
button in the item editor - this app doesn't create listings on any
platform itself. Named `templates_bp` (not `templates`) to avoid clashing
with Flask's own "templates" folder convention for Jinja HTML.
"""

from flask import Blueprint, Response, jsonify, request

from db import get_db

templates_bp = Blueprint("templates_bp", __name__, url_prefix="/api/v1/templates")


@templates_bp.route("", methods=["GET"])
def list_templates() -> Response:
    """List every saved template, grouped by category."""
    db = get_db()
    rows = db.execute("SELECT * FROM templates ORDER BY category, id").fetchall()
    return jsonify([dict(r) for r in rows])


@templates_bp.route("", methods=["POST"])
def create_template() -> Response | tuple[Response, int]:
    """Save a new template. Both category and text are required."""
    d = request.get_json(silent=True) or {}
    category = d.get("category", "").strip()
    text = d.get("text", "").strip()
    if not category or not text:
        return jsonify({"error": "Category and text are required"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO templates (category, text) VALUES (?, ?)", (category, text)
    )
    db.commit()
    row = db.execute("SELECT * FROM templates WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@templates_bp.route("/<int:template_id>", methods=["PUT"])
def update_template(template_id: int) -> Response | tuple[Response, int]:
    """Overwrite an existing template's category/text. Both are required,
    same as create_template - previously the only way to change either was
    delete + recreate (losing its position in category/id order)."""
    db = get_db()
    row = db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = request.get_json(silent=True) or {}

    client_version = d.get("version")
    if client_version is None:
        return jsonify({"error": "version is required"}), 400
    if client_version != row["version"]:
        return jsonify({"error": "version_conflict", "current": dict(row)}), 409

    category = d.get("category", "").strip()
    text = d.get("text", "").strip()
    if not category or not text:
        return jsonify({"error": "Category and text are required"}), 400
    db.execute(
        "UPDATE templates SET category = ?, text = ?, version = ? WHERE id = ?",
        (category, text, row["version"] + 1, template_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return jsonify(dict(row))


@templates_bp.route("/<int:template_id>", methods=["DELETE"])
def delete_template(template_id: int) -> Response:
    """Permanently delete a template."""
    db = get_db()
    db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    db.commit()
    return jsonify({"ok": True})
