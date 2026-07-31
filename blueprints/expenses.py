"""Expense ledger CRUD.

Business costs that aren't tied to any one item - packaging materials,
postage supplies, mileage, storage, subscriptions, etc. - tracked here so
the Tax summary's net profit (blueprints/tax.py) can reflect them alongside
the per-item cost_price/fees/shipping_cost that's already deducted there.
"""

from typing import Any

from flask import Blueprint, Response, jsonify, request

from db import get_db

expenses_bp = Blueprint("expenses", __name__, url_prefix="/api/v1/expenses")


def _validate_amount(d: dict[str, Any]) -> str | None:
    if "amount" in d and d["amount"] is not None and d["amount"] != "":
        try:
            d["amount"] = float(d["amount"])
        except (TypeError, ValueError):
            return "amount must be a number"
    return None


@expenses_bp.route("", methods=["GET"])
def list_expenses() -> Response:
    """Return every expense, newest expense_date first. Powers the Tax
    summary tab's Expenses panel."""
    db = get_db()
    rows = db.execute("SELECT * FROM expenses ORDER BY expense_date DESC, id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@expenses_bp.route("", methods=["POST"])
def create_expense() -> tuple[Response, int]:
    d = request.get_json(silent=True) or {}
    error = _validate_amount(d)
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO expenses (description, category, amount, expense_date, notes) VALUES (?,?,?,?,?)",
        (d.get("description", ""), d.get("category", ""), d.get("amount", 0), d.get("expense_date", ""), d.get("notes", "")),
    )
    db.commit()
    row = db.execute("SELECT * FROM expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"expense": dict(row)}), 201


@expenses_bp.route("/<int:expense_id>", methods=["PUT"])
def update_expense(expense_id: int) -> Response | tuple[Response, int]:
    d = request.get_json(silent=True) or {}
    error = _validate_amount(d)
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    row = db.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    client_version = d.get("version")
    if client_version is None:
        return jsonify({"error": "version is required"}), 400
    if client_version != row["version"]:
        return jsonify({"error": "version_conflict", "current": dict(row)}), 409

    db.execute(
        """UPDATE expenses SET description=?, category=?, amount=?, expense_date=?, notes=?,
           updated_at=datetime('now'), version=? WHERE id=?""",
        (
            d.get("description", row["description"]),
            d.get("category", row["category"]),
            d.get("amount", row["amount"]),
            d.get("expense_date", row["expense_date"]),
            d.get("notes", row["notes"]),
            row["version"] + 1,
            expense_id,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    return jsonify({"expense": dict(row)})


@expenses_bp.route("/<int:expense_id>", methods=["DELETE"])
def delete_expense(expense_id: int) -> Response:
    db = get_db()
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    return jsonify({"ok": True})
