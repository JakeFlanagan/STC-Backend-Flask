"""Item CRUD, status advancement, activity log, price history, and batch updates.

This is the biggest blueprint because items are the core object of the app -
every row in the `items` table represents one thing being tracked through
its lifecycle (List -> Sold -> Paid, or one of the Return / Refund
branches). Two side-effect tables get written alongside `items`
whenever something changes: `price_history` (every sale_price change) and
`activity_log` (every status change, plus manual notes) - both feed the
"Price history" and "Activity log" sections of the item editor modal.
"""

import csv
import io
import json
import sqlite3
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, jsonify, request

from db import get_db, row_to_dict

items_bp = Blueprint("items", __name__, url_prefix="/api/v1/items")

# Every `items` column that's safe to write from a client-supplied key, i.e.
# everything except the primary key and the two timestamps managed by the
# backend itself. batch_update() below builds its UPDATE's SET clause from
# request-body dict keys, so this whitelist is what stands between that and
# arbitrary SQL being spliced into the query.
EDITABLE_ITEM_COLUMNS = {
    "name", "primary_status", "sub_status", "listed_on", "sold_on",
    "buyer_name", "cost_price", "sale_price", "fees", "shipping_cost",
    "shipping_method", "packed", "has_photos", "tracking_ref",
    "return_tracking_ref", "return_shipping_method", "source", "status_date",
    "paid_date", "pending_payment_date", "reduced_at", "reimbursed", "reimbursed_amount",
    "refund_amount", "return_shipping_cost", "listing_title", "listing_description", "archived",
    "notes", "relisted_count",
}

# Fields that must already be filled in before an item can move to a given
# (primary_status, sub_status). Checked by update_item (the full editor's
# Save) and advance_item (the Inventory quick-advance button) - not by
# create_item, since that route is also how items get seeded directly into
# an arbitrary state (tests, imports) without going through the UI's
# fill-in-the-gaps flow. "type" tells the frontend how to render the input
# for a missing field (plain text, a checkbox, a date+optional-time pair, or
# a bare date with no time - "date-only" - for paid_date, which never has one).
REQUIRED_FIELDS = {
    ("List", "Ready to list"): [
        {"field": "listing_title", "type": "text", "label": "Listing title"},
        {"field": "listing_description", "type": "text", "label": "Listing description"},
        {"field": "has_photos", "type": "boolean", "label": "Has photos"},
    ],
    ("Sold", "Shipped"): [
        {"field": "tracking_ref", "type": "text", "label": "Tracking reference"},
    ],
    ("Sold", "Pending payment"): [
        {"field": "status_date", "type": "date", "label": "Payment due date"},
    ],
    ("Sold", "Paid"): [
        {"field": "paid_date", "type": "date-only", "label": "Payment date"},
    ],
    ("Return", "Label sent"): [
        {"field": "status_date", "type": "date", "label": "Return due date"},
        {"field": "return_shipping_cost", "type": "number", "label": "Return label cost"},
    ],
    ("Return", "In transit"): [
        {"field": "status_date", "type": "date", "label": "Return due date"},
        {"field": "return_tracking_ref", "type": "text", "label": "Return tracking reference"},
    ],
}


NUMERIC_FIELDS = (
    "cost_price", "sale_price", "fees", "shipping_cost",
    "reimbursed_amount", "refund_amount", "return_shipping_cost",
)


def validate_numeric_fields(d: dict[str, Any]) -> str | None:
    """Coerce every price/amount field present in `d` to a float, in place.

    SQLite's type affinity happily stores a string in a REAL column, which
    then breaks sum()/arithmetic in dashboard.py/tax.py the next time stats
    are computed - so this is the one place that boundary is enforced.
    Fields absent from `d` are left untouched (callers apply their own
    defaults, e.g. `d.get(field, 0)`, afterwards). Returns an error message
    naming the first invalid field, or None if everything present is valid.
    """
    for field in NUMERIC_FIELDS:
        if field in d and d[field] is not None and d[field] != "":
            try:
                d[field] = float(d[field])
            except (TypeError, ValueError):
                return f"{field} must be a number"
    return None


ACTIVITY_LOG_CAP = 200


def _log_activity(db: sqlite3.Connection, item_id: int, note: str, cap: int = ACTIVITY_LOG_CAP) -> None:
    """Insert one activity_log row for `item_id`, then prune down to `cap`
    rows if that pushed it over - keeping the newest `cap` rows, plus two
    that are always protected regardless of age: the earliest "Item
    created" row (the item's origin story) and the earliest "Status:
    Sold ..." row (get_platform_comparison's avg_days_to_sell,
    blueprints/dashboard.py, depends on that one surviving). get_item_log's
    50-row read limit means this rarely matters in practice, but nothing
    was otherwise capping how many rows a long-lived, frequently-edited item
    could accumulate.
    """
    db.execute("INSERT INTO activity_log (item_id, note) VALUES (?, ?)", (item_id, note))
    count = db.execute(
        "SELECT COUNT(*) AS n FROM activity_log WHERE item_id = ?", (item_id,)
    ).fetchone()["n"]
    if count <= cap:
        return

    protected_ids = set()
    for pattern in ("Item created", "Status: Sold%"):
        row = db.execute(
            "SELECT id FROM activity_log WHERE item_id = ? AND note LIKE ? ORDER BY created_at ASC LIMIT 1",
            (item_id, pattern),
        ).fetchone()
        if row:
            protected_ids.add(row["id"])

    keep_ids = {
        r["id"] for r in db.execute(
            "SELECT id FROM activity_log WHERE item_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (item_id, cap),
        ).fetchall()
    } | protected_ids

    all_ids = [r["id"] for r in db.execute("SELECT id FROM activity_log WHERE item_id = ?", (item_id,)).fetchall()]
    prune_ids = [i for i in all_ids if i not in keep_ids]
    if prune_ids:
        placeholders = ",".join("?" * len(prune_ids))
        db.execute(f"DELETE FROM activity_log WHERE id IN ({placeholders})", prune_ids)


def missing_required_fields(
    values: dict[str, Any], ps: str, ss: str, entering_sold: bool = False
) -> list[dict[str, str]]:
    """Which of the fields required for `ps`/`ss` are empty in `values` (a
    dict of item field values). `entering_sold` additionally requires a
    buyer name - checked only at the moment an item's primary_status first
    becomes "Sold" (not on every later Sold sub-status change), since that's
    the one point a buyer is actually known to exist."""
    specs = list(REQUIRED_FIELDS.get((ps, ss), []))
    if entering_sold:
        specs = [{"field": "buyer_name", "type": "text", "label": "Buyer username"}] + specs
    missing = []
    for spec in specs:
        val = values.get(spec["field"])
        empty = not val if spec["type"] == "boolean" else not str(val or "").strip()
        if empty:
            missing.append(spec)
    return missing


@items_bp.route("", methods=["GET"])
def list_items() -> Response:
    """Return every non-deleted item, newest first. Powers the Inventory table."""
    db = get_db()
    rows = db.execute("SELECT * FROM items WHERE deleted_at = '' ORDER BY id DESC").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@items_bp.route("/<int:item_id>", methods=["GET"])
def get_item(item_id: int) -> Response | tuple[Response, int]:
    """Return a single non-deleted item by id. Used when the caller doesn't
    already have the full item list loaded (e.g. Dashboard quick actions)."""
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at = ''", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row_to_dict(row))


def _insert_item_row(db: sqlite3.Connection, d: dict[str, Any]) -> int:
    """Insert one items row from `d` (a dict of column name -> value, same
    shape as create_item's request body) plus its initial price-history and
    activity-log entries. Shared by create_item() and import_items() so a
    bulk CSV import creates rows exactly the same way a single POST does.
    Caller is responsible for commit()."""
    cur = db.execute(
        """INSERT INTO items (name, primary_status, sub_status, listed_on, sold_on,
           buyer_name, cost_price, sale_price, fees, shipping_cost, shipping_method,
           packed, has_photos, tracking_ref, return_tracking_ref, return_shipping_method,
           return_shipping_cost, source, status_date, paid_date,
           reimbursed, reimbursed_amount, refund_amount, listing_title,
           listing_description, archived, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            d.get("name", ""),
            d.get("primary_status", "List"),
            d.get("sub_status", "Draft"),
            json.dumps(d.get("listed_on", [])),  # stored as JSON text - SQLite has no array type
            d.get("sold_on", ""),
            d.get("buyer_name", ""),
            d.get("cost_price", 0),
            d.get("sale_price", 0),
            d.get("fees", 0),
            d.get("shipping_cost", 0),
            d.get("shipping_method", ""),
            1 if d.get("packed") else 0,
            1 if d.get("has_photos") else 0,
            d.get("tracking_ref", ""),
            d.get("return_tracking_ref", ""),
            d.get("return_shipping_method", ""),
            d.get("return_shipping_cost", 0),
            d.get("source", ""),
            d.get("status_date", ""),
            d.get("paid_date", ""),
            1 if d.get("reimbursed") else 0,
            d.get("reimbursed_amount", 0),
            d.get("refund_amount", 0),
            d.get("listing_title", ""),
            d.get("listing_description", ""),
            1 if d.get("archived") else 0,
            d.get("notes", ""),
        ),
    )
    item_id = cur.lastrowid
    assert item_id is not None

    sp = d.get("sale_price", 0)
    if sp and sp > 0:
        db.execute("INSERT INTO price_history (item_id, price) VALUES (?, ?)", (item_id, sp))

    _log_activity(db, item_id, "Item created")
    return item_id


@items_bp.route("", methods=["POST"])
def create_item() -> tuple[Response, int]:
    """Create a new item, plus its initial price-history and activity-log entries.

    Also checks for an existing non-deleted item with the same name
    (case-insensitive) and returns a non-blocking `warning` in the response
    if one is found - the item is still created either way, this is just a
    heads-up for the frontend to show the user (e.g. accidental duplicate
    add). A softly-deleted item's name doesn't count, so recreating
    something you just deleted (rather than restoring it via Undo) doesn't
    trigger a false "duplicate" warning.
    """
    d = request.get_json(silent=True) or {}
    error = validate_numeric_fields(d)
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    # duplicate check
    existing = db.execute(
        "SELECT id FROM items WHERE LOWER(name) = LOWER(?) AND deleted_at = ''", (d.get("name", ""),)
    ).fetchone()
    warning = None
    if existing:
        warning = f"Similar item exists (id {existing['id']})"

    item_id = _insert_item_row(db, d)
    # one commit for all three inserts (item, price-history, activity-log),
    # so a crash between them can't leave an item without its side-effect rows
    db.commit()

    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": row_to_dict(row), "warning": warning}), 201


IMPORT_COLUMNS = (
    "name", "primary_status", "sub_status", "sold_on", "buyer_name",
    "cost_price", "sale_price", "fees", "shipping_cost", "shipping_method",
    "tracking_ref", "source", "status_date", "paid_date", "listing_title",
    "listing_description", "notes",
)


@items_bp.route("/import", methods=["POST"])
def import_items() -> Response | tuple[Response, int]:
    """Bulk-create items from a pasted/uploaded CSV (the Inventory tab's
    "Import CSV" button) - one row per item, matched by header name
    (case-insensitive) against IMPORT_COLUMNS; any other column in the file
    is ignored. Only `name` is required - everything else defaults the same
    way a bare POST /api/items would (listed_on always starts empty - a flat
    CSV cell isn't a natural fit for its {platform, url, date} shape, so
    platforms are added afterward through the editor).

    Rows are processed independently: one bad row (missing name, a
    non-numeric price, ...) is skipped and reported in `skipped`, it doesn't
    abort the rest of the file. Doesn't do create_item's duplicate-name
    check - a bulk import bringing in a whole spreadsheet at once isn't the
    place for a per-row "similar item exists" nag.
    """
    content = (request.get_json(silent=True) or {}).get("content", "")
    if not content.strip():
        return jsonify({"error": "No CSV content provided"}), 400

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return jsonify({"error": "CSV has no header row"}), 400
    # case-insensitive header lookup: lowercased header -> the real column
    # name in that file (whatever case the user actually typed/exported)
    header_map = {h.lower().strip(): h for h in reader.fieldnames}

    db = get_db()
    created = 0
    skipped = []
    for row_num, row in enumerate(reader, start=2):  # row 1 is the header
        d = {}
        for col in IMPORT_COLUMNS:
            header = header_map.get(col)
            if header is not None and (row.get(header) or "").strip() != "":
                d[col] = row[header].strip()

        name = d.get("name", "").strip()
        if not name:
            skipped.append({"row": row_num, "reason": "name is required"})
            continue
        d["name"] = name

        error = validate_numeric_fields(d)
        if error:
            skipped.append({"row": row_num, "reason": error})
            continue

        _insert_item_row(db, d)
        created += 1

    db.commit()
    return jsonify({"created": created, "skipped": skipped})


@items_bp.route("/<int:item_id>", methods=["PUT"])
def update_item(item_id: int) -> Response | tuple[Response, int]:
    """Overwrite every field on an item (the editor modal always sends the
    full item, not a partial patch), and record any side effects the change
    implies: a new price-history row if sale_price changed, a new
    activity-log entry if the status changed, and a pending_payment_date
    stamp the moment sub_status first becomes "Pending payment"."""
    d = request.get_json(silent=True) or {}
    error = validate_numeric_fields(d)
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    old = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at = ''", (item_id,)).fetchone()
    if not old:
        return jsonify({"error": "Not found"}), 404

    client_version = d.get("version")
    if client_version is None:
        return jsonify({"error": "version is required"}), 400
    if client_version != old["version"]:
        return jsonify({"error": "version_conflict", "current": row_to_dict(old)}), 409

    new_ps = d.get("primary_status", old["primary_status"])
    new_ss = d.get("sub_status", old["sub_status"])

    # refuse to save if the target status is missing a field it requires -
    # checked before any writes happen, so a rejected save has no side effects
    entering_sold = new_ps == "Sold" and old["primary_status"] != "Sold"
    missing = missing_required_fields(d, new_ps, new_ss, entering_sold=entering_sold)
    if missing:
        return jsonify({"error": "missing_fields", "missing_fields": missing}), 400

    # a fresh listing cycle starting for this same physical item - a
    # completed Sold/Return/Refund cycle moving back to List (a
    # refunded/returned item being relisted, or a sale falling through).
    # The old cycle's buyer/price/shipping/tracking fields are about to be
    # wiped below so they don't linger as "current" info for the next cycle
    # - archive them into sale_history first, then force-reset them on `d`
    # regardless of what the caller sent, so quickRelist() (item-actions.js,
    # which spreads the old item's fields unchanged apart from the status)
    # can't accidentally carry them forward into the new cycle.
    is_relist = old["primary_status"] in ("Sold", "Return", "Refund") and new_ps == "List"
    if is_relist and (old["sold_on"] or old["buyer_name"] or old["sale_price"] or old["tracking_ref"]):
        db.execute(
            """INSERT INTO sale_history (item_id, sold_on, buyer_name, sale_price, fees,
               shipping_cost, shipping_method, tracking_ref, paid_date, refund_amount,
               reimbursed, reimbursed_amount, return_shipping_cost, ended_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item_id, old["sold_on"], old["buyer_name"], old["sale_price"], old["fees"],
                old["shipping_cost"], old["shipping_method"], old["tracking_ref"], old["paid_date"],
                old["refund_amount"], old["reimbursed"], old["reimbursed_amount"], old["return_shipping_cost"],
                f"{old['primary_status']} / {old['sub_status']}",
            ),
        )
        _log_activity(
            db, item_id,
            f"Sale archived: {old['sold_on'] or 'unknown platform'} / "
            f"{old['buyer_name'] or 'unknown buyer'} / {old['sale_price']}",
        )
    if is_relist:
        for field in (
            "sold_on", "buyer_name", "shipping_method", "tracking_ref",
            "return_tracking_ref", "return_shipping_method", "paid_date", "status_date",
        ):
            d[field] = ""
        for field in ("sale_price", "fees", "shipping_cost", "refund_amount", "reimbursed_amount", "return_shipping_cost"):
            d[field] = 0
        d["reimbursed"] = False

    # track price changes
    new_price = d.get("sale_price", old["sale_price"])
    if new_price != old["sale_price"] and new_price > 0:
        db.execute(
            "INSERT INTO price_history (item_id, price) VALUES (?, ?)",
            (item_id, new_price),
        )

    # track status changes
    if new_ps != old["primary_status"] or new_ss != old["sub_status"]:
        _log_activity(db, item_id, f"Status: {new_ps} / {new_ss}")

    # bump the Relisted counter every time a completed Sold/Return/Refund
    # cycle moves back to List (a genuine relist, whether a sale fell
    # through or a returned/refunded item is going up for sale again).
    # Stored on the item itself rather than derived from activity_log, since
    # that log is capped at 50 rows per item and would silently undercount
    # old history.
    new_relisted_count = old["relisted_count"] + 1 if is_relist else old["relisted_count"]

    # record when an item first enters Pending payment (informational only -
    # the dispute-window countdown is based on the due date, status_date,
    # not this timestamp). Only on the *first* transition, so re-saving the
    # item while it's still Pending payment doesn't keep bumping it forward.
    # A relist always resets it - the new cycle hasn't reached payment yet.
    if is_relist:
        pending_payment_date = ""
    elif new_ss == "Pending payment" and old["sub_status"] != "Pending payment":
        pending_payment_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        pending_payment_date = old["pending_payment_date"]

    # record when an item first enters Reduced - gives the Dashboard's
    # staleness clock (listing_anchor_date, blueprints/dashboard.py) a fresh
    # start once you've actually cut the price, instead of forever counting
    # from created_at. Only on the *first* transition, same reasoning as
    # pending_payment_date above. A relist always resets it too - the
    # previous listing cycle's staleness clock doesn't apply to a new one.
    if is_relist:
        reduced_at = ""
    elif new_ss == "Reduced" and old["sub_status"] != "Reduced":
        reduced_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        reduced_at = old["reduced_at"]

    db.execute(
        """UPDATE items SET name=?, primary_status=?, sub_status=?, listed_on=?,
           sold_on=?, buyer_name=?, cost_price=?, sale_price=?, fees=?,
           shipping_cost=?, shipping_method=?, packed=?, has_photos=?,
           tracking_ref=?, return_tracking_ref=?, return_shipping_method=?, return_shipping_cost=?,
           source=?, status_date=?, paid_date=?, pending_payment_date=?,
           reduced_at=?, reimbursed=?, reimbursed_amount=?, refund_amount=?, listing_title=?,
           listing_description=?, archived=?, notes=?, relisted_count=?,
           updated_at=datetime('now'), version=?
           WHERE id=?""",
        (
            d.get("name", old["name"]),
            new_ps,
            new_ss,
            json.dumps(d.get("listed_on", [])),
            d.get("sold_on", ""),
            d.get("buyer_name", ""),
            d.get("cost_price", 0),
            new_price,
            d.get("fees", 0),
            d.get("shipping_cost", 0),
            d.get("shipping_method", ""),
            1 if d.get("packed") else 0,
            1 if d.get("has_photos") else 0,
            d.get("tracking_ref", ""),
            d.get("return_tracking_ref", ""),
            d.get("return_shipping_method", ""),
            d.get("return_shipping_cost", 0),
            d.get("source", ""),
            d.get("status_date", ""),
            d.get("paid_date", ""),
            pending_payment_date,
            reduced_at,
            1 if d.get("reimbursed") else 0,
            d.get("reimbursed_amount", 0),
            d.get("refund_amount", 0),
            d.get("listing_title", ""),
            d.get("listing_description", ""),
            1 if d.get("archived") else 0,
            d.get("notes", ""),
            new_relisted_count,
            old["version"] + 1,
            item_id,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": row_to_dict(row)})


@items_bp.route("/<int:item_id>", methods=["DELETE"])
def delete_item(item_id: int) -> Response:
    """Soft-delete an item: stamp deleted_at instead of actually removing
    the row, so undelete_item() below can restore it - price_history/
    activity_log stay completely intact, unlike a real DELETE (which would
    cascade them away). This is what backs the frontend's "Undo" toast
    (see deleteItem, static/js/editor.js) - the row only ever gets truly
    removed if the whole database file is deleted."""
    db = get_db()
    db.execute("UPDATE items SET deleted_at=datetime('now') WHERE id=?", (item_id,))
    db.commit()
    return jsonify({"ok": True})


@items_bp.route("/<int:item_id>/undelete", methods=["POST"])
def undelete_item(item_id: int) -> Response | tuple[Response, int]:
    """Restore a soft-deleted item - the "Undo" action behind the
    delete-item toast. Deliberately doesn't filter on deleted_at itself
    (unlike every other item lookup), since restoring is the one operation
    that needs to find a *deleted* row."""
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    db.execute("UPDATE items SET deleted_at='' WHERE id=?", (item_id,))
    db.commit()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": row_to_dict(row)})


@items_bp.route("/<int:item_id>/paid-date", methods=["POST"])
def set_paid_date(item_id: int) -> Response | tuple[Response, int]:
    """Backfill paid_date on an item that's already Sold/Paid, without
    touching any other field. This is the fix action behind the Tax view's
    "unaccounted paid items" notice - those are items that reached Paid
    before paid_date became required (or were created directly via the
    API/import), so a full editor Save isn't needed, just this one field."""
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at = ''", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    paid_date = (request.get_json(silent=True) or {}).get("paid_date", "").strip()
    if not paid_date:
        return jsonify({
            "error": "missing_fields",
            "missing_fields": [{"field": "paid_date", "type": "date-only", "label": "Payment date"}],
        }), 400
    db.execute(
        "UPDATE items SET paid_date=?, updated_at=datetime('now') WHERE id=?",
        (paid_date, item_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": row_to_dict(row)})


@items_bp.route("/<int:item_id>/relist-platforms", methods=["POST"])
def relist_platforms(item_id: int) -> Response | tuple[Response, int]:
    """Refresh every platform's listed_on date to today and clear reduced_at,
    in one click - the "Relist" quick action for an item that's still
    genuinely live (bumped/renewed on its platforms) but whose staleness
    clock (listing_anchor_date, blueprints/dashboard.py) is stale. Doesn't
    touch primary_status/sub_status/sale_price - this is purely a refresh of
    the staleness signal, not a full relist through the editor.
    """
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at = ''", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    listed_on = json.loads(row["listed_on"] or "[]")
    if not listed_on:
        return jsonify({"error": "No platforms to relist"}), 400
    today = datetime.now().strftime("%Y-%m-%d")
    for p in listed_on:
        p["date"] = today
    db.execute(
        "UPDATE items SET listed_on=?, reduced_at='', updated_at=datetime('now') WHERE id=?",
        (json.dumps(listed_on), item_id),
    )
    _log_activity(db, item_id, "Relisted (platform dates refreshed)")
    db.commit()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": row_to_dict(row)})


@items_bp.route("/<int:item_id>/reset-relisted", methods=["POST"])
def reset_relisted_count(item_id: int) -> Response | tuple[Response, int]:
    """Reset an item's Relisted counter back to 0.

    The counter increments automatically (see update_item) and isn't
    otherwise editable, so this is the escape hatch for when a UI mistake -
    e.g. briefly flipping an item to Sold and back - inflates it."""
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at = ''", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    db.execute("UPDATE items SET relisted_count=0 WHERE id=?", (item_id,))
    db.commit()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": row_to_dict(row)})


@items_bp.route("/<int:item_id>/advance", methods=["POST"])
def advance_item(item_id: int) -> Response | tuple[Response, int]:
    """Quick-advance an item to the next sub-status in its pipeline.

    `chains` defines the linear "happy path" for each primary_status - e.g.
    Sold goes Needs Shipping -> Shipped -> ... -> Paid, and List ends at
    Reduced. Branch/exception sub-statuses (Item lost, Defaulted, Relisted,
    etc.) are intentionally NOT part of any chain: they're only reachable by
    picking them explicitly in the editor (or, for Relisted, via the
    Return tab's "Relist" action), never via this one-click "next step"
    action. If the item's current sub-status isn't in its chain (e.g. it's
    already at a branch), or it's already at the end of the chain, this is
    a no-op.

    If the target sub-status has required fields (REQUIRED_FIELDS) that
    aren't already filled in, the caller can supply them in the request
    body (the frontend does this via a "fill in the missing fields" popup
    when a plain, body-less advance comes back with a 409); otherwise the
    advance is refused and nothing changes.
    """
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at = ''", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    d = request.get_json(silent=True) or {}
    error = validate_numeric_fields(d)
    if error:
        return jsonify({"error": error}), 400
    ps = row["primary_status"]
    ss = row["sub_status"]

    # define advancement chains
    chains = {
        "List": ["Draft", "Ready to list", "Active", "Reduced"],
        "Sold": [
            "Needs Shipping", "Shipped", "At collection point",
            "Pending payment", "Paid",
        ],
        "Return": ["Label sent", "In transit", "Returned"],
        "Refund": [],  # no linear path - every Refund sub-status is a manually-chosen terminal outcome
    }

    chain = chains.get(ps, [])
    if ss in chain:
        idx = chain.index(ss)
        if idx + 1 < len(chain):
            new_ss = chain[idx + 1]

            # overlay any field values submitted alongside this advance onto
            # the current row, so a value supplied to satisfy a requirement
            # is both checked and actually persisted below
            merged = dict(row)
            for f in (
                "tracking_ref", "status_date", "listing_title", "listing_description", "paid_date",
                "return_tracking_ref", "return_shipping_cost",
            ):
                if f in d:
                    merged[f] = d[f]
            if "has_photos" in d:
                merged["has_photos"] = 1 if d["has_photos"] else 0

            missing = missing_required_fields(merged, ps, new_ss)
            if missing:
                return jsonify({"error": "missing_fields", "missing_fields": missing}), 409

            # record when an item first enters Pending payment (informational
            # only, same rule as update_item's PUT path - see there for why
            # the dispute-window countdown doesn't actually use this)
            pending_payment_date = (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if new_ss == "Pending payment"
                else row["pending_payment_date"]
            )
            # record when an item first enters Reduced - see update_item's
            # PUT path for why this feeds the Dashboard's staleness clock
            # instead of status_date/created_at
            reduced_at = (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if new_ss == "Reduced" and row["sub_status"] != "Reduced"
                else row["reduced_at"]
            )
            db.execute(
                """UPDATE items SET sub_status=?, tracking_ref=?, status_date=?,
                   listing_title=?, listing_description=?, has_photos=?,
                   pending_payment_date=?, reduced_at=?, paid_date=?,
                   return_tracking_ref=?, return_shipping_cost=?, updated_at=datetime('now') WHERE id=?""",
                (
                    new_ss,
                    merged["tracking_ref"],
                    merged["status_date"],
                    merged["listing_title"],
                    merged["listing_description"],
                    merged["has_photos"],
                    pending_payment_date,
                    reduced_at,
                    merged["paid_date"],
                    merged["return_tracking_ref"],
                    merged["return_shipping_cost"],
                    item_id,
                ),
            )
            _log_activity(db, item_id, f"Advanced to {ps} / {new_ss}")
            db.commit()

    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": row_to_dict(row)})


@items_bp.route("/<int:item_id>/log", methods=["GET"])
def get_item_log(item_id: int) -> Response:
    """Return an item's most recent 50 activity-log entries, newest first."""
    db = get_db()
    logs = db.execute(
        "SELECT * FROM activity_log WHERE item_id = ? ORDER BY created_at DESC LIMIT 50",
        (item_id,),
    ).fetchall()
    return jsonify([dict(r) for r in logs])


@items_bp.route("/<int:item_id>/log", methods=["POST"])
def add_item_log(item_id: int) -> Response | tuple[Response, int]:
    """Append a manual, free-text note to an item's activity log."""
    db = get_db()
    note = (request.get_json(silent=True) or {}).get("note", "").strip()
    if not note:
        return jsonify({"error": "Empty note"}), 400
    _log_activity(db, item_id, note)
    db.commit()
    return jsonify({"ok": True})


@items_bp.route("/<int:item_id>/prices", methods=["GET"])
def get_price_history(item_id: int) -> Response:
    """Return every recorded sale_price for an item, newest first."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM price_history WHERE item_id = ? ORDER BY recorded_at DESC",
        (item_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@items_bp.route("/<int:item_id>/sales", methods=["GET"])
def get_sale_history(item_id: int) -> Response:
    """Return every archived sale cycle for an item, newest first - the
    editor modal's "Past sales" section. A row is written here by
    update_item whenever a Sold/Return/Refund cycle moves back to List (see
    there for when/why), so this is empty for an item that's never been
    sold-and-relisted."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sale_history WHERE item_id = ? ORDER BY ended_at DESC",
        (item_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@items_bp.route("/<int:item_id>/sales/<int:sale_id>", methods=["PUT"])
def update_sale_return_shipping_cost(item_id: int, sale_id: int) -> Response | tuple[Response, int]:
    """Fix return_shipping_cost on one archived sale_history row - the only
    field on a past sale cycle that's ever editable after the fact (an item
    already returned and relisted before this field existed). Single-field
    whitelist, not a general-purpose edit surface for the rest of the row -
    see the "Past sales" pencil icon in editor.js for the UI this backs."""
    d = request.get_json(silent=True) or {}
    if "return_shipping_cost" not in d:
        return jsonify({"error": "return_shipping_cost is required"}), 400
    try:
        cost = float(d["return_shipping_cost"])
    except (TypeError, ValueError):
        return jsonify({"error": "return_shipping_cost must be a number"}), 400
    db = get_db()
    row = db.execute(
        "SELECT * FROM sale_history WHERE id = ? AND item_id = ?", (sale_id, item_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    db.execute("UPDATE sale_history SET return_shipping_cost=? WHERE id=?", (cost, sale_id))
    db.commit()
    row = db.execute("SELECT * FROM sale_history WHERE id = ?", (sale_id,)).fetchone()
    return jsonify({"sale": dict(row)})


@items_bp.route("/batch", methods=["POST"])
def batch_update() -> Response | tuple[Response, int]:
    """Apply the same field updates to many items at once (e.g. the
    Inventory table's "Mark shipped" button, which sets primary_status and
    sub_status on every selected row in one call).

    `updates` is a dict of column name -> new value; unlike update_item()
    this only touches the columns actually present in `updates`, leaving
    everything else on each row untouched.
    """
    d = request.get_json(silent=True) or {}
    ids = d.get("ids", [])
    updates = d.get("updates", {})
    error = validate_numeric_fields(updates)
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    for item_id in ids:
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in updates.items():
            if k not in EDITABLE_ITEM_COLUMNS:
                continue
            if k == "listed_on":
                sets.append(f"{k}=?")
                vals.append(json.dumps(v))
            elif k in ("packed", "has_photos", "reimbursed", "archived"):
                sets.append(f"{k}=?")
                vals.append(1 if v else 0)
            else:
                sets.append(f"{k}=?")
                vals.append(v)
        if sets:
            sets.append("updated_at=datetime('now')")
            vals.append(item_id)
            db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=? AND deleted_at=''", vals)
            if "primary_status" in updates or "sub_status" in updates:
                ps = updates.get("primary_status", "")
                ss = updates.get("sub_status", "")
                _log_activity(db, item_id, f"Batch update: {ps} / {ss}")
    db.commit()
    return jsonify({"ok": True, "count": len(ids)})
