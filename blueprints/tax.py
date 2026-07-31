"""Tax summary and CSV export for paid items.

Both routes only ever consider items that are Sold / Paid with a
paid_date set - i.e. money that's actually landed, not still-pending
sales - since that's the date HMRC cares about (see the "Tax Notes" the
app's UI shows: record the platform balance date, not the bank
withdrawal date). `_paid_items_query` builds that same filter (plus an
optional date range) for both routes so they can never drift apart.
"""

import io
from datetime import date, datetime
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from db import get_db, row_to_dict

tax_bp = Blueprint("tax", __name__, url_prefix="/api/v1/tax")


def _uk_tax_quarter(date_str: str) -> tuple[str, str] | None:
    """Return (tax_year, quarter) for a "YYYY-MM-DD..." date string, e.g.
    ("2026/27", "Q2") - using the UK tax year (6 April - 5 April) split into
    four ~3-month quarters starting on the 6th of Apr/Jul/Oct/Jan.
    Approximate - HMRC's own accounting-period quarters can vary - this is a
    running-total view for the seller, not an official filing boundary (see
    the "not tax advice" note already on this tab). Returns None for an
    empty/unparseable date.
    """
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    tax_year_start = d.year if (d.month, d.day) >= (4, 6) else d.year - 1
    tax_year = f"{tax_year_start}/{str(tax_year_start + 1)[-2:]}"
    quarter = "Q1"
    for bound_date, label in (
        (date(tax_year_start, 4, 6), "Q1"),
        (date(tax_year_start, 7, 6), "Q2"),
        (date(tax_year_start, 10, 6), "Q3"),
        (date(tax_year_start + 1, 1, 6), "Q4"),
    ):
        if d >= bound_date:
            quarter = label
    return tax_year, quarter


def _paid_items_query(from_date: str, to_date: str) -> tuple[str, list[str]]:
    """Build the shared SQL query + params for "paid items in this date
    range", reused by both get_tax() and export_tax_csv() so their
    definition of "in range" can never disagree."""
    query = "SELECT * FROM items WHERE deleted_at = '' AND primary_status='Sold' AND sub_status='Paid' AND paid_date != ''"
    params = []
    if from_date:
        query += " AND paid_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND paid_date <= ?"
        params.append(to_date)
    query += " ORDER BY paid_date ASC"
    return query, params


def _expenses_query(from_date: str, to_date: str) -> tuple[str, list[str]]:
    """Same "in range" shape as _paid_items_query, but for the expenses
    ledger (blueprints/expenses.py) - keyed off expense_date rather than
    paid_date."""
    query = "SELECT * FROM expenses WHERE 1=1"
    params = []
    if from_date:
        query += " AND expense_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND expense_date <= ?"
        params.append(to_date)
    query += " ORDER BY expense_date ASC"
    return query, params


def _return_shipping_query(
    from_date: str, to_date: str
) -> tuple[tuple[str, list[str]], tuple[str, list[str]]]:
    """Build the two queries for "return shipping cost incurred in this date
    range" - current Return-status items (keyed off status_date, their
    Return due date) plus archived sale_history rows from a since-relisted
    item (keyed off ended_at, when that cycle was archived). Neither row set
    passes _paid_items_query's Sold/Paid filter, so this is summed
    separately into return_shipping_total, the same way _expenses_query's
    result is."""
    items_query = "SELECT return_shipping_cost, status_date FROM items WHERE deleted_at = '' AND return_shipping_cost > 0"
    items_params = []
    if from_date:
        items_query += " AND status_date >= ?"
        items_params.append(from_date)
    if to_date:
        items_query += " AND status_date <= ?"
        items_params.append(to_date)

    history_query = (
        "SELECT sh.return_shipping_cost, sh.ended_at FROM sale_history sh "
        "JOIN items i ON sh.item_id = i.id "
        "WHERE i.deleted_at = '' AND sh.return_shipping_cost > 0"
    )
    history_params = []
    if from_date:
        history_query += " AND sh.ended_at >= ?"
        history_params.append(from_date)
    if to_date:
        history_query += " AND sh.ended_at <= ?"
        history_params.append(to_date)

    return (items_query, items_params), (history_query, history_params)


@tax_bp.route("", methods=["GET"])
def get_tax() -> Response:
    """Return paid items in the given date range (?from=&to=, both
    optional) plus revenue/costs/fees/shipping/profit totals across them.
    Powers the Tax summary tab.

    Also returns `unaccounted`: Sold/Paid items with no paid_date at all
    (pre-dating that field becoming required, or created directly via the
    API/import). They're excluded from every total above - the whole point
    of the field is "money that's landed on this date" - so they're
    surfaced separately for the UI to nag about instead of silently
    dropping that income from the tax report. Not date-range filtered,
    since they have no date to filter by.

    `expenses` is the expense ledger (blueprints/expenses.py) in the same
    date range, keyed off expense_date - `totals.profit` is already net of
    `expenses_total`, so callers don't need their own copy of that
    subtraction.
    """
    db = get_db()
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")

    query, params = _paid_items_query(from_date, to_date)
    rows = db.execute(query, params).fetchall()
    items = [row_to_dict(r) for r in rows]

    unaccounted_rows = db.execute(
        "SELECT * FROM items WHERE deleted_at = '' AND primary_status='Sold' AND sub_status='Paid' "
        "AND (paid_date IS NULL OR paid_date = '') ORDER BY id DESC"
    ).fetchall()

    expense_query, expense_params = _expenses_query(from_date, to_date)
    expenses = [dict(r) for r in db.execute(expense_query, expense_params).fetchall()]
    expenses_total = sum(e["amount"] or 0 for e in expenses)

    (rs_items_query, rs_items_params), (rs_history_query, rs_history_params) = _return_shipping_query(from_date, to_date)
    return_shipping_current = [dict(r) for r in db.execute(rs_items_query, rs_items_params).fetchall()]
    return_shipping_archived = [dict(r) for r in db.execute(rs_history_query, rs_history_params).fetchall()]
    return_shipping_total = (
        sum(r["return_shipping_cost"] or 0 for r in return_shipping_current)
        + sum(r["return_shipping_cost"] or 0 for r in return_shipping_archived)
    )

    revenue = sum(i["sale_price"] or 0 for i in items)
    costs = sum(i["cost_price"] or 0 for i in items)
    fees = sum(i["fees"] or 0 for i in items)
    shipping = sum(i["shipping_cost"] or 0 for i in items)
    profit = revenue - costs - fees - shipping - expenses_total - return_shipping_total

    # UK tax-year quarterly breakdown (see _uk_tax_quarter above) - paid
    # items and expenses are bucketed into the same (tax_year, quarter) key
    # so each quarter's profit is net of the expenses actually incurred that
    # quarter, not just revenue-side totals.
    quarterly: dict[tuple[str, str], dict[str, Any]] = {}

    def _quarter_bucket(tax_year: str, quarter: str) -> dict[str, Any]:
        return quarterly.setdefault(
            (tax_year, quarter),
            {"tax_year": tax_year, "quarter": quarter, "revenue": 0, "costs": 0, "fees": 0, "shipping": 0,
             "expenses_total": 0, "return_shipping_total": 0, "count": 0},
        )

    for i in items:
        key = _uk_tax_quarter(i["paid_date"])
        if not key:
            continue
        bucket = _quarter_bucket(*key)
        bucket["revenue"] += i["sale_price"] or 0
        bucket["costs"] += i["cost_price"] or 0
        bucket["fees"] += i["fees"] or 0
        bucket["shipping"] += i["shipping_cost"] or 0
        bucket["count"] += 1
    for e in expenses:
        key = _uk_tax_quarter(e["expense_date"])
        if not key:
            continue
        bucket = _quarter_bucket(*key)
        bucket["expenses_total"] += e["amount"] or 0
    for r in return_shipping_current:
        key = _uk_tax_quarter(r["status_date"])
        if not key:
            continue
        bucket = _quarter_bucket(*key)
        bucket["return_shipping_total"] += r["return_shipping_cost"] or 0
    for r in return_shipping_archived:
        key = _uk_tax_quarter(r["ended_at"])
        if not key:
            continue
        bucket = _quarter_bucket(*key)
        bucket["return_shipping_total"] += r["return_shipping_cost"] or 0

    quarterly_trend = sorted(quarterly.values(), key=lambda b: (b["tax_year"], b["quarter"]))
    for b in quarterly_trend:
        b["profit"] = b["revenue"] - b["costs"] - b["fees"] - b["shipping"] - b["expenses_total"] - b["return_shipping_total"]

    return jsonify({
        "items": items,
        "expenses": expenses,
        "unaccounted": [row_to_dict(r) for r in unaccounted_rows],
        "quarterly_trend": quarterly_trend,
        "totals": {
            "revenue": revenue,
            "costs": costs,
            "fees": fees,
            "shipping": shipping,
            "expenses_total": expenses_total,
            "return_shipping_total": return_shipping_total,
            "profit": profit,
            "count": len(items),
        },
    })


@tax_bp.route("/csv", methods=["GET"])
def export_tax_csv() -> Response:
    """Export the same paid-items-in-range data as get_tax(), as a
    downloadable CSV (the Tax summary tab's "Export CSV" button)."""
    db = get_db()
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")

    query, params = _paid_items_query(from_date, to_date)
    rows = db.execute(query, params).fetchall()

    lines = ["Item,Platform,Sale Price,Cost Price,Fees,Shipping,Return Shipping,Profit,Paid Date"]
    for r in rows:
        profit = (r["sale_price"] or 0) - (r["cost_price"] or 0) - (r["fees"] or 0) - (r["shipping_cost"] or 0) - (r["return_shipping_cost"] or 0)
        name = r["name"].replace('"', '""')  # escape embedded quotes for valid CSV
        lines.append(
            f'"{name}","{r["sold_on"]}",{r["sale_price"]:.2f},{r["cost_price"]:.2f},'
            f'{r["fees"]:.2f},{r["shipping_cost"]:.2f},{r["return_shipping_cost"]:.2f},{profit:.2f},"{r["paid_date"]}"'
        )

    csv_content = "\n".join(lines)
    # built in-memory rather than a temp file on disk, so there's nothing
    # left behind on disk after send_file streams it
    buf = io.BytesIO(csv_content.encode("utf-8"))
    return send_file(buf, as_attachment=True, download_name="tax_export.csv", mimetype="text/csv")
