"""Dashboard stats and cross-platform comparison.

Both routes here recompute everything from scratch on every request
(there's no caching/materialized view) - the item counts involved are
small enough for a single user's inventory that this is simpler and
fast enough, at the cost of doing a bit of repeated work per request.
"""

from datetime import datetime, timedelta
from typing import Any

from flask import Blueprint, Response, jsonify

from db import get_all_settings, get_db, row_to_dict

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/v1")


def profit(i: dict[str, Any]) -> float:
    return (i["sale_price"] or 0) - (i["cost_price"] or 0) - (i["fees"] or 0) - (i["shipping_cost"] or 0) - (i["return_shipping_cost"] or 0)


def unreimbursed_loss(items: list[dict[str, Any]], platform: str | None = None) -> list[dict[str, Any]]:
    """Items lost/damaged (Refund) or lost in transit on the way back
    (Return) where the platform hasn't covered what you refunded the buyer,
    each annotated with net_loss - what you refunded minus whatever the
    platform paid you back, only included when that's still actually a loss
    (net_loss > 0). Optionally scoped to one `sold_on` platform, so
    get_platform_comparison's per-platform value_lost lines up with
    get_stats' totalled unreimbursed_losses instead of using a second
    definition of loss.
    """
    candidates = [
        i for i in items
        if (platform is None or i["sold_on"] == platform)
        and ((i["primary_status"] == "Refund" and i["sub_status"] in ("Item lost", "Item damaged"))
             or (i["primary_status"] == "Return" and i["sub_status"] == "Lost in transit"))
    ]
    losses = []
    for i in candidates:
        covered = i["reimbursed_amount"] if i["reimbursed"] else 0
        net_loss = (i["sale_price"] or 0) - (covered or 0)
        if net_loss > 0:
            losses.append({**i, "net_loss": net_loss})
    return losses


def listing_anchor_date(item: dict[str, Any]) -> str:
    """The date a published listing's "staleness" (deadstock/stale_items and
    listed_items' days_listed, both in get_stats) is measured from.

    Reducing the price is a genuine "I did something about this" action -
    the same idea behind relisted_count resetting the Sold pipeline - so
    once an item has been reduced, its clock restarts from reduced_at
    instead of forever counting from created_at. Short of that, dual-listing
    means an item can carry several different listed_on dates (one per
    platform); the earliest one wins, since that's the most conservative
    "how long has this genuinely been up somewhere" reading. Falls back to
    created_at for an item with no reduction and no platform dates recorded
    at all.
    """
    if item["sub_status"] == "Reduced" and item["reduced_at"]:
        return item["reduced_at"]
    dates = [p["date"] for p in (item["listed_on"] or []) if p.get("date")]
    if dates:
        return min(dates)
    return item["created_at"]


def due_deadline(status_date: str) -> datetime | None:
    """The exact moment a user-entered due date (`status_date`) counts as
    passed. A date-only value ("YYYY-MM-DD", no time chosen in the editor)
    means "due sometime that day" - so the deadline is the start of the
    *next* day, not midnight of the day itself (a payment due "6th July"
    isn't overdue until the 7th). A value with a time attached is exact.
    Returns None for an empty or unparseable value.
    """
    if not status_date:
        return None
    try:
        if len(status_date) <= 10:
            return datetime.strptime(status_date, "%Y-%m-%d") + timedelta(days=1)
        return datetime.strptime(status_date, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


@dashboard_bp.route("/stats", methods=["GET"])
def get_stats() -> Response:
    """Compute every number and alert list shown on the Dashboard tab.

    This single endpoint bundles together: headline totals (paid/pending/
    listed value), per-bucket counts, and several "needs your attention"
    lists (stale listings, overdue payments, overdue returns, unreimbursed
    losses, dispute windows) - each computed once here so the frontend can
    just render whatever's non-empty as an alert panel.
    """
    db = get_db()
    items = db.execute("SELECT * FROM items WHERE deleted_at = ''").fetchall()
    items = [row_to_dict(r) for r in items]

    paid = [i for i in items if i["primary_status"] == "Sold" and i["sub_status"] == "Paid"]
    pending = [i for i in items if i["primary_status"] == "Sold" and i["sub_status"] == "Pending payment"]
    # List covers both "not yet published" (Draft/Ready to list) and
    # "published" (Active/Reduced/Relisted) phases of one continuous chain -
    # these stats only ever meant the *published* half, so that's what
    # `listed` still means here; `not_listed` is the other half.
    listed = [i for i in items if i["primary_status"] == "List" and i["sub_status"] in ("Active", "Reduced", "Relisted")]
    not_listed = [i for i in items if i["primary_status"] == "List" and i["sub_status"] in ("Draft", "Ready to list")]
    sold_active = [i for i in items if i["primary_status"] == "Sold" and i["sub_status"] != "Paid"]
    refunded = [i for i in items if i["primary_status"] == "Refund"]
    returns = [i for i in items if i["primary_status"] == "Return"]
    returns_in_progress = [i for i in returns if i["sub_status"] in ("Label sent", "In transit")]

    # load settings (thresholds live here: deadstock_days)
    settings = get_all_settings(db)
    deadstock_days = settings.get("deadstock_days", 14)
    deadstock_enabled = settings.get("deadstock_enabled", True)

    # deadstock: published List items that have been sitting for too long without selling
    stale = []
    now = datetime.now()
    if deadstock_enabled:
        for i in listed:
            try:
                anchor = datetime.fromisoformat(listing_anchor_date(i))
                days = (now - anchor).days
                if days >= deadstock_days:
                    stale.append({**i, "days_listed": days})
            except ValueError:
                pass

    # overdue payments: funds due to be released (Pending payment) past their due date
    overdue = []
    for i in pending:
        due = due_deadline(i["status_date"])
        if due and now > due:
            overdue.append(i)

    # overdue returns (buyer hasn't sent the item back by the due date)
    overdue_returns = []
    for i in returns_in_progress:
        due = due_deadline(i["status_date"])
        if due and now > due:
            overdue_returns.append(i)

    # unreimbursed losses: see unreimbursed_loss() above
    unreimbursed_losses = unreimbursed_loss(items)
    unreimbursed_total = sum(x["net_loss"] for x in unreimbursed_losses)

    # monthly revenue/profit trend for the Dashboard's earnings chart -
    # bucketed by the "YYYY-MM" prefix of paid_date, same grouping the Tax
    # tab's own chart uses (blueprints/tax.py's CSV export / static/js/tax.js).
    monthly_trend: dict[str, dict[str, Any]] = {}
    for i in paid:
        if not i["paid_date"]:
            continue
        month = i["paid_date"][:7]
        bucket = monthly_trend.setdefault(month, {"month": month, "revenue": 0, "profit": 0, "count": 0})
        bucket["revenue"] += i["sale_price"] or 0
        bucket["profit"] += profit(i)
        bucket["count"] += 1
    monthly_trend = sorted(monthly_trend.values(), key=lambda x: x["month"])  # type: ignore[assignment]

    # dispute windows (items marked Pending payment, buyer still has time to
    # raise an issue). closes_at *is* the payment's due date (status_date) -
    # due_deadline() gives an exact due time as-is, or the start of the next
    # day for a date-only due date - not the due date plus an extra buffer.
    # "closed" tells the frontend whether to show a countdown or a "ready to
    # advance to Paid" prompt.
    dispute_windows = []
    for i in items:
        if i["primary_status"] == "Sold" and i["sub_status"] == "Pending payment":
            due = due_deadline(i["status_date"])
            if due:
                dispute_windows.append({
                    **i,
                    "closes_at": due.strftime("%Y-%m-%d %H:%M:%S"),
                    "closed": now > due,
                })

    # up and coming: dispute windows (Sold/Pending payment due dates) and
    # in-progress return due dates, merged into one timeline sorted
    # soonest-first - an item already overdue sorts to the very top since
    # its due_at is in the past. Replaces having to check three separate
    # alert panels to see what's most urgent.
    upcoming_deadlines = []
    for i in pending:
        due = due_deadline(i["status_date"])
        if due:
            upcoming_deadlines.append({**i, "kind": "payment", "due_at": due.strftime("%Y-%m-%d %H:%M:%S"), "overdue": now > due})
    for i in returns_in_progress:
        due = due_deadline(i["status_date"])
        if due:
            upcoming_deadlines.append({**i, "kind": "return", "due_at": due.strftime("%Y-%m-%d %H:%M:%S"), "overdue": now > due})
    upcoming_deadlines.sort(key=lambda x: x["due_at"])

    # every published listing, longest-listed first (then highest value) -
    # so the Dashboard always shows what to consider reducing/relisting,
    # not just the ones already past the deadstock threshold (is_stale
    # flags those for styling, but every listed item shows up here).
    listed_items = []
    for i in listed:
        try:
            days = (now - datetime.fromisoformat(listing_anchor_date(i))).days
        except ValueError:
            days = 0
        listed_items.append({**i, "days_listed": days, "is_stale": bool(deadstock_enabled and days >= deadstock_days)})
    listed_items.sort(key=lambda x: (-x["days_listed"], -(x["sale_price"] or 0)))

    # to-do list: small housekeeping tasks surfaced as individual items
    # rather than a single "N items need X" count, so each is clickable.
    needs_description = [i for i in items if i["primary_status"] == "List" and i["sub_status"] == "Draft" and not (i["listing_description"] or "").strip()]
    needs_photos = [i for i in not_listed + listed if not i["has_photos"]]
    needs_packing = [i for i in listed if not i["packed"]]

    return jsonify({
        "total_items": len(items),
        "paid_total": sum(i["sale_price"] or 0 for i in paid),
        "paid_profit": sum(profit(i) for i in paid),
        "paid_count": len(paid),
        "pending_total": sum(i["sale_price"] or 0 for i in pending),
        "pending_count": len(pending),
        "listed_total": sum(i["sale_price"] or 0 for i in listed),
        "listed_count": len(listed),
        "sold_active_count": len(sold_active),
        "refunded_count": len(refunded),
        "not_listed_count": len(not_listed),
        "return_count": len(returns),
        "unpacked_listed": len([i for i in listed if not i["packed"]]),
        "no_photos": len([i for i in not_listed + listed if not i["has_photos"]]),
        "stale_items": stale,
        "overdue_payments": overdue,
        "overdue_returns": overdue_returns,
        "dispute_windows": dispute_windows,
        "reimbursed_total": sum(i["reimbursed_amount"] or 0 for i in refunded if i["reimbursed"]),
        "unreimbursed_losses": unreimbursed_losses,
        "unreimbursed_total": unreimbursed_total,
        "monthly_trend": monthly_trend,
        "upcoming_deadlines": upcoming_deadlines,
        "listed_items": listed_items,
        "needs_description": needs_description,
        "needs_photos": needs_photos,
        "needs_packing": needs_packing,
    })


@dashboard_bp.route("/platforms", methods=["GET"])
def get_platform_comparison() -> Response:
    """Break down revenue/profit/speed/fees/returns/refunds by platform
    (Vinted/eBay/etc.) for the Dashboard's "Platform breakdown" table -
    answers "should I focus on one site" with actual numbers from this
    database.

    value_lost mirrors unreimbursed_losses/unreimbursed_total in get_stats
    (item lost/damaged or lost-in-transit, net of whatever the platform
    reimbursed) - scoped to this platform instead of totalled across all of
    them.

    avg_days_to_sell is measured from an item's created_at to the first
    "Status: Sold ..." activity-log entry for it (see update_item in
    blueprints/items.py, which writes that log entry) - items with no such
    entry yet (e.g. never actually transitioned to Sold through the normal
    flow) are simply excluded from that average rather than skewing it.

    avg_discount_percent is how much a paid item's price typically dropped
    between its first recorded price_history entry (the original listing
    price) and what it actually sold for - items with only one price_history
    row (never reduced) or that sold for at or above their original price
    don't count as a "discount" and are excluded rather than pulling the
    average toward zero.

    sell_through_rate is paid_count as a share of (paid_count + still-listed
    count on that platform) - "of everything you've put up on this platform,
    what fraction has actually sold" - items still sitting in List/Active
    /Reduced/Relisted with this platform in their listed_on count as the
    "still listed" side.
    """
    db = get_db()
    items = [row_to_dict(r) for r in db.execute("SELECT * FROM items WHERE deleted_at = ''").fetchall()]

    settings = get_all_settings(db)
    platform_fees = settings.get("platform_fees", {})

    # earliest "Status: Sold ..." log entry per item, in one query instead of
    # one query per sold item - avg_days_to_sell below just looks each
    # item's id up in this dict.
    sold_at_by_item = {
        r["item_id"]: r["sold_at"]
        for r in db.execute(
            """SELECT item_id, MIN(created_at) AS sold_at FROM activity_log
               WHERE note LIKE 'Status: Sold%' GROUP BY item_id"""
        ).fetchall()
    }

    # every item's recorded prices, oldest first, in one query instead of
    # one per item - avg_discount_percent below compares the first entry
    # (the original listing price) to the item's current sale_price.
    # Ordered by id (not just recorded_at) as the tiebreaker: recorded_at
    # only has second-level precision, so two price changes landing in the
    # same second would otherwise sort ambiguously - id is autoincrement,
    # so it's always true insertion order.
    price_history_by_item: dict[int, list[float]] = {}
    for r in db.execute("SELECT item_id, price FROM price_history ORDER BY item_id, recorded_at ASC, id ASC").fetchall():
        price_history_by_item.setdefault(r["item_id"], []).append(r["price"])

    # union of platforms that have actually had a sale, and platforms with
    # configured fees (so a platform shows up in Settings even with 0 sales)
    platforms = sorted({i["sold_on"] for i in items if i["sold_on"]} | set(platform_fees.keys()))
    result = []
    for plat in platforms:
        sold_items = [i for i in items if i["sold_on"] == plat]
        paid_items = [i for i in sold_items if i["sub_status"] == "Paid"]
        refunded_items = [i for i in items if i["sold_on"] == plat and i["primary_status"] == "Refund"]
        return_items = [i for i in items if i["sold_on"] == plat and i["primary_status"] == "Return"]
        still_listed_items = [
            i for i in items
            if i["primary_status"] == "List" and i["sub_status"] in ("Active", "Reduced", "Relisted")
            and any(p.get("platform") == plat for p in (i["listed_on"] or []))
        ]

        # value_lost: see unreimbursed_loss() above - same definition as
        # unreimbursed_losses in get_stats, just scoped to this platform, so
        # it lines up with the Dashboard's own "Refunds" stat tile rather
        # than introducing a second notion of loss.
        value_lost = sum(x["net_loss"] for x in unreimbursed_loss(items, platform=plat))

        days_to_sell = []
        for i in sold_items:
            sold_at_str = sold_at_by_item.get(i["id"])
            if sold_at_str:
                try:
                    sold_at = datetime.fromisoformat(sold_at_str)
                    created = datetime.fromisoformat(i["created_at"])
                    days_to_sell.append((sold_at - created).days)
                except ValueError:
                    pass

        discounts = []
        for i in paid_items:
            history = price_history_by_item.get(i["id"], [])
            if len(history) < 2:
                continue
            first_price = history[0]
            final_price = i["sale_price"] or 0
            if first_price > 0 and final_price < first_price:
                discounts.append((first_price - final_price) / first_price * 100)

        sell_through_pool = len(paid_items) + len(still_listed_items)

        result.append({
            "platform": plat,
            "revenue": sum(i["sale_price"] or 0 for i in paid_items),
            "profit": sum(profit(i) for i in paid_items),
            "sold_count": len(sold_items),
            "paid_count": len(paid_items),
            "return_count": len(return_items),
            "avg_days_to_sell": round(sum(days_to_sell) / len(days_to_sell), 1) if days_to_sell else None,
            "avg_discount_percent": round(sum(discounts) / len(discounts), 1) if discounts else None,
            "sell_through_rate": round(len(paid_items) / sell_through_pool * 100, 1) if sell_through_pool else None,
            "fee_percent": platform_fees.get(plat, {}).get("percent"),
            "refund_count": len(refunded_items),
            "value_lost": value_lost,
        })

    return jsonify(result)
