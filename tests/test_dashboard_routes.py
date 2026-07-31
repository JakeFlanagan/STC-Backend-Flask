"""Tests for /api/stats and /api/platforms (blueprints/dashboard.py).

Each "needs attention" list in /api/stats is seeded into existence
deliberately: the deadstock_days threshold is set to 0 via /api/settings so
a just-created item already qualifies, and due dates (status_date) are set
in the past so an item is already overdue/past its dispute window - rather
than trying to backdate created_at directly - this keeps every test going
through the same API surface a real user would.
"""

from datetime import date, datetime, timedelta

from helpers import expect


def test_stats_empty_db_has_zero_counts(client):
    resp = client.get("/api/v1/stats")
    expect("status code", 200, resp.status_code)
    s = resp.get_json()
    expect("total_items", 0, s["total_items"])
    expect("no stale items", [], s["stale_items"])
    expect("no overdue payments", [], s["overdue_payments"])
    expect("no overdue returns", [], s["overdue_returns"])
    expect("no unreimbursed losses", [], s["unreimbursed_losses"])
    expect("no dispute windows", [], s["dispute_windows"])


def test_stats_excludes_soft_deleted_items(client):
    item = client.post("/api/v1/items", json={"name": "To Delete", "primary_status": "List", "sub_status": "Active", "sale_price": 20}).get_json()["item"]
    client.post("/api/v1/items", json={"name": "Kept", "primary_status": "List", "sub_status": "Active", "sale_price": 30})
    client.delete(f"/api/v1/items/{item['id']}")

    s = client.get("/api/v1/stats").get_json()
    expect("total_items excludes the deleted one", 1, s["total_items"])
    expect("listed_total excludes the deleted item's price", 30, s["listed_total"])


def test_stats_counts_by_status(client):
    client.post("/api/v1/items", json={"name": "A", "primary_status": "List", "sub_status": "Draft"})
    client.post("/api/v1/items", json={"name": "B", "primary_status": "List", "sub_status": "Active"})
    client.post("/api/v1/items", json={"name": "C", "primary_status": "Sold", "sub_status": "Paid", "sale_price": 100})

    s = client.get("/api/v1/stats").get_json()
    expect("total_items", 3, s["total_items"])
    expect("not_listed_count", 1, s["not_listed_count"])
    expect("listed_count", 1, s["listed_count"])
    expect("paid_count", 1, s["paid_count"])
    expect("paid_total", 100, s["paid_total"])


def test_stats_flags_stale_listings(client):
    client.put("/api/v1/settings", json={"deadstock_days": 0, "version": 1})
    item = client.post(
        "/api/v1/items", json={"name": "Old Listing", "primary_status": "List", "sub_status": "Active", "sale_price": 20}
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    stale_ids = [i["id"] for i in s["stale_items"]]
    expect("stale listing flagged", True, item["id"] in stale_ids)


def test_stats_stale_uses_earliest_listed_on_date_over_created_at(client):
    # created_at is "now" (0 days old - not stale on its own), but the item
    # carries a listed_on date from well before the deadstock threshold -
    # get_stats should measure staleness from that platform date, not from
    # when the record itself was created.
    old_date = (date.today() - timedelta(days=40)).isoformat()
    client.put("/api/v1/settings", json={"deadstock_days": 5, "version": 1})
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Long-Listed Elsewhere", "primary_status": "List", "sub_status": "Active",
            "sale_price": 20, "listed_on": [{"platform": "Vinted", "url": "", "date": old_date}],
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    stale_ids = [i["id"] for i in s["stale_items"]]
    expect("flagged stale based on the old listed_on date, not created_at", True, item["id"] in stale_ids)


def test_stats_reduced_at_resets_staleness_clock_even_with_an_old_listed_on_date(client):
    # Reducing the price is a deliberate "I did something about this" action
    # - it should give the item a fresh staleness clock, overriding an old
    # listed_on date that would otherwise still flag it as stale.
    old_date = (date.today() - timedelta(days=40)).isoformat()
    client.put("/api/v1/settings", json={"deadstock_days": 5, "version": 1})
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Just Reduced", "primary_status": "List", "sub_status": "Active",
            "sale_price": 20, "listed_on": [{"platform": "Vinted", "url": "", "date": old_date}],
        },
    ).get_json()["item"]

    client.put(f"/api/v1/items/{item['id']}", json={**item, "sub_status": "Reduced"})

    s = client.get("/api/v1/stats").get_json()
    stale_ids = [i["id"] for i in s["stale_items"]]
    expect("no longer stale - reduced_at (today) wins over the old listed_on date", False, item["id"] in stale_ids)


def test_stats_draft_item_not_flagged_stale_or_unpacked(client):
    # A Draft item was never actually published, so it shouldn't be treated
    # like a live listing that's gone stale, and shouldn't count towards the
    # "unpacked" total - both of those only apply once something is Active/
    # Reduced/Relisted.
    client.put("/api/v1/settings", json={"deadstock_days": 0, "version": 1})
    item = client.post(
        "/api/v1/items", json={"name": "Draft Item", "primary_status": "List", "sub_status": "Draft"}
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    stale_ids = [i["id"] for i in s["stale_items"]]
    expect("Draft item not flagged stale", False, item["id"] in stale_ids)
    expect("not_listed_count includes the Draft item", 1, s["not_listed_count"])
    expect("listed_count excludes the Draft item", 0, s["listed_count"])


def test_stats_flags_overdue_payments(client):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Overdue Payment",
            "primary_status": "Sold",
            "sub_status": "Pending payment",
            "status_date": yesterday,
            "sale_price": 30,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    overdue_ids = [i["id"] for i in s["overdue_payments"]]
    expect("overdue payment flagged", True, item["id"] in overdue_ids)


def test_stats_date_only_due_date_not_overdue_until_next_day(client):
    # A due date entered with no time ("YYYY-MM-DD") means "due sometime
    # that day" - it shouldn't count as overdue until the day is over.
    today = date.today().isoformat()
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Due Today", "primary_status": "Sold", "sub_status": "Pending payment",
            "status_date": today, "sale_price": 30,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    overdue_ids = [i["id"] for i in s["overdue_payments"]]
    expect("not overdue yet - due date still counting down to end of day", False, item["id"] in overdue_ids)


def test_stats_exact_due_time_is_overdue_the_moment_it_passes(client):
    # A due date entered *with* a time is exact - no next-day grace period.
    past_moment = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Due Moments Ago", "primary_status": "Sold", "sub_status": "Pending payment",
            "status_date": past_moment, "sale_price": 30,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    overdue_ids = [i["id"] for i in s["overdue_payments"]]
    expect("overdue immediately once an exact due time passes", True, item["id"] in overdue_ids)


def test_stats_flags_overdue_returns(client):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Overdue Return",
            "primary_status": "Return",
            "sub_status": "Label sent",
            "status_date": yesterday,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    overdue_ids = [i["id"] for i in s["overdue_returns"]]
    expect("overdue return flagged", True, item["id"] in overdue_ids)


def test_stats_computes_unreimbursed_loss(client):
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Damaged, not reimbursed",
            "primary_status": "Refund",
            "sub_status": "Item damaged",
            "sale_price": 40,
            "reimbursed": False,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    losses = {i["id"]: i for i in s["unreimbursed_losses"]}
    expect("item appears in unreimbursed_losses", True, item["id"] in losses)
    expect("full sale price counted as a loss", 40, losses[item["id"]]["net_loss"])
    expect("total reflects the loss", 40, s["unreimbursed_total"])


def test_stats_partial_reimbursement_reduces_loss(client):
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Damaged, partially reimbursed",
            "primary_status": "Refund",
            "sub_status": "Item damaged",
            "sale_price": 40,
            "reimbursed": True,
            "reimbursed_amount": 15,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    losses = {i["id"]: i for i in s["unreimbursed_losses"]}
    expect("net loss is sale price minus reimbursement", 25, losses[item["id"]]["net_loss"])


def test_stats_full_reimbursement_means_no_loss(client):
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Fully reimbursed",
            "primary_status": "Refund",
            "sub_status": "Item damaged",
            "sale_price": 40,
            "reimbursed": True,
            "reimbursed_amount": 40,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    loss_ids = [i["id"] for i in s["unreimbursed_losses"]]
    expect("fully-reimbursed item not flagged as a loss", False, item["id"] in loss_ids)


def test_stats_dispute_window_open_while_due_date_in_future(client):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    item = client.post(
        "/api/v1/items", json={
            "name": "Pending Payment Item", "primary_status": "Sold", "sub_status": "Shipped",
            "status_date": tomorrow,
        }
    ).get_json()["item"]
    client.post(f"/api/v1/items/{item['id']}/advance")  # Shipped -> At collection point
    client.post(f"/api/v1/items/{item['id']}/advance")  # At collection point -> Pending payment

    s = client.get("/api/v1/stats").get_json()
    window = next(w for w in s["dispute_windows"] if w["id"] == item["id"])
    expect("dispute window open while the due date is still in the future", False, window["closed"])


def test_stats_dispute_window_closed_once_due_date_passes(client):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    item = client.post(
        "/api/v1/items", json={
            "name": "Overdue Pending Item", "primary_status": "Sold", "sub_status": "Pending payment",
            "status_date": yesterday,
        }
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    window = next(w for w in s["dispute_windows"] if w["id"] == item["id"])
    expect("dispute window closed once the due date's own deadline has passed", True, window["closed"])


def test_dispute_window_closes_at_equals_exact_due_time(client):
    # A due date entered *with* a time is used exactly as the dispute
    # window's closing moment - no next-day rounding, no extra buffer added
    # on top (closes_at *is* the due date, not the due date plus days).
    due = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Exact Due Time", "primary_status": "Sold", "sub_status": "Pending payment",
            "status_date": due,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    window = next(w for w in s["dispute_windows"] if w["id"] == item["id"])
    expect("closes_at equals the exact due time, with no buffer added", due, window["closes_at"])
    expect("window already closed since the due time is in the past", True, window["closed"])


def test_dispute_window_closes_at_is_next_day_for_date_only_due_date(client):
    today = date.today().isoformat()
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Date Only Due", "primary_status": "Sold", "sub_status": "Pending payment",
            "status_date": today,
        },
    ).get_json()["item"]

    s = client.get("/api/v1/stats").get_json()
    window = next(w for w in s["dispute_windows"] if w["id"] == item["id"])
    expected = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    expect("closes_at is the start of the next day for a date-only due date", expected, window["closes_at"])
    expect("window still open (due date hasn't rolled into the next day yet)", False, window["closed"])


def test_platforms_breakdown_computes_avg_discount_percent(client):
    item = client.post(
        "/api/v1/items",
        json={
            "name": "Reduced Before Sale", "sold_on": "Vinted", "primary_status": "Sold",
            "sub_status": "Paid", "sale_price": 100, "buyer_name": "Sam", "paid_date": "2026-01-01",
        },
    ).get_json()["item"]
    put = client.put(f"/api/v1/items/{item['id']}", json={**item, "sale_price": 80})
    expect("reduce-price PUT succeeds", 200, put.status_code)

    # Sold at full asking price - no discount, shouldn't drag the average down.
    client.post(
        "/api/v1/items",
        json={
            "name": "Sold At Asking", "sold_on": "Vinted", "primary_status": "Sold",
            "sub_status": "Paid", "sale_price": 50, "buyer_name": "Sam", "paid_date": "2026-01-01",
        },
    )

    resp = client.get("/api/v1/platforms")
    by_platform = {p["platform"]: p for p in resp.get_json()}
    expect("avg discount is just the one genuinely-discounted item's 20%", 20.0, by_platform["Vinted"]["avg_discount_percent"])


def test_platforms_breakdown_avg_discount_percent_null_with_no_reductions(client):
    client.post(
        "/api/v1/items",
        json={"name": "Sold At Asking", "sold_on": "eBay", "primary_status": "Sold", "sub_status": "Paid", "sale_price": 50, "buyer_name": "Sam"},
    )

    resp = client.get("/api/v1/platforms")
    by_platform = {p["platform"]: p for p in resp.get_json()}
    expect("no discounted sales - avg_discount_percent is null, not 0", None, by_platform["eBay"]["avg_discount_percent"])


def test_platforms_breakdown_computes_sell_through_rate(client):
    client.post(
        "/api/v1/items",
        json={"name": "Paid Item", "sold_on": "Vinted", "primary_status": "Sold", "sub_status": "Paid", "sale_price": 50, "buyer_name": "Sam"},
    )
    client.post(
        "/api/v1/items",
        json={
            "name": "Still Listed Item", "primary_status": "List", "sub_status": "Active",
            "listed_on": [{"platform": "Vinted", "url": "", "date": ""}],
        },
    )

    resp = client.get("/api/v1/platforms")
    by_platform = {p["platform"]: p for p in resp.get_json()}
    expect("1 paid of 2 total (1 paid + 1 still listed) = 50%", 50.0, by_platform["Vinted"]["sell_through_rate"])


def test_platforms_breakdown_aggregates_by_platform(client):
    client.post("/api/v1/items", json={"name": "Vinted Sale", "sold_on": "Vinted", "sub_status": "Paid", "primary_status": "Sold", "sale_price": 50})
    client.post("/api/v1/items", json={"name": "eBay Sale", "sold_on": "eBay", "sub_status": "Paid", "primary_status": "Sold", "sale_price": 30})
    client.post("/api/v1/items", json={"name": "Vinted Refund", "sold_on": "Vinted", "primary_status": "Refund", "sub_status": "Item damaged"})

    resp = client.get("/api/v1/platforms")
    expect("status code", 200, resp.status_code)
    by_platform = {p["platform"]: p for p in resp.get_json()}

    expect("Vinted revenue", 50, by_platform["Vinted"]["revenue"])
    expect("Vinted refund count", 1, by_platform["Vinted"]["refund_count"])
    expect("eBay revenue", 30, by_platform["eBay"]["revenue"])
    expect("eBay fee percent from settings", 12.8, by_platform["eBay"]["fee_percent"])


def test_stats_monthly_trend_buckets_paid_items_by_paid_date_month(client):
    client.post("/api/v1/items", json={
        "name": "Jan item", "primary_status": "Sold", "sub_status": "Paid",
        "sale_price": 100, "cost_price": 20, "paid_date": "2026-01-15",
    })
    client.post("/api/v1/items", json={
        "name": "Jan item 2", "primary_status": "Sold", "sub_status": "Paid",
        "sale_price": 50, "cost_price": 10, "paid_date": "2026-01-20",
    })
    client.post("/api/v1/items", json={
        "name": "Feb item", "primary_status": "Sold", "sub_status": "Paid",
        "sale_price": 30, "cost_price": 5, "paid_date": "2026-02-01",
    })
    # Paid with no payment date recorded - excluded from the trend, same as
    # it's excluded from the Tax tab's own monthly chart.
    client.post("/api/v1/items", json={
        "name": "Unaccounted", "primary_status": "Sold", "sub_status": "Paid", "sale_price": 999,
    })

    s = client.get("/api/v1/stats").get_json()
    trend = {m["month"]: m for m in s["monthly_trend"]}

    expect("two months present", ["2026-01", "2026-02"], sorted(trend.keys()))
    expect("Jan revenue is both Jan items summed", 150, trend["2026-01"]["revenue"])
    expect("Jan profit is both Jan items' profit summed", 120, trend["2026-01"]["profit"])
    expect("Jan count", 2, trend["2026-01"]["count"])
    expect("Feb revenue", 30, trend["2026-02"]["revenue"])
    expect("Feb count", 1, trend["2026-02"]["count"])


def test_paid_profit_deducts_return_shipping_cost(client):
    client.post("/api/v1/items", json={
        "name": "Item", "primary_status": "Sold", "sub_status": "Paid",
        "sale_price": 100, "cost_price": 20, "fees": 10, "shipping_cost": 5,
        "return_shipping_cost": 8, "paid_date": "2026-01-15",
    })
    s = client.get("/api/v1/stats").get_json()
    expect("paid_profit nets return_shipping_cost", 57, s["paid_profit"])
