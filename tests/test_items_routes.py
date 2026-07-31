"""Tests for /api/items* routes (blueprints/items.py): CRUD, status
advancement chains (Sold and Return), activity log, price history, and
batch updates.
"""

import sqlite3
from datetime import date

import db as db_module
from blueprints.items import ACTIVITY_LOG_CAP
from helpers import expect


def test_create_item_defaults(client):
    resp = client.post("/api/v1/items", json={"name": "Test Widget"})
    expect("status code", 201, resp.status_code)
    body = resp.get_json()
    item = body["item"]
    expect("name", "Test Widget", item["name"])
    expect("default primary_status", "List", item["primary_status"])
    expect("default sub_status", "Draft", item["sub_status"])
    expect("no duplicate warning for a unique name", None, body["warning"])


def test_create_item_warns_on_duplicate_name(client):
    client.post("/api/v1/items", json={"name": "Same Name"})
    resp = client.post("/api/v1/items", json={"name": "same name"})  # case-insensitive match
    expect("status code", 201, resp.status_code)
    expect("duplicate warning present", True, resp.get_json()["warning"] is not None)


def test_create_item_logs_initial_price_and_activity(client):
    resp = client.post("/api/v1/items", json={"name": "Priced Item", "sale_price": 25})
    item_id = resp.get_json()["item"]["id"]

    prices = client.get(f"/api/v1/items/{item_id}/prices").get_json()
    expect("one price history entry on creation", 1, len(prices))
    expect("initial price recorded", 25, prices[0]["price"])

    log = client.get(f"/api/v1/items/{item_id}/log").get_json()
    expect("activity log has a creation entry", True, any("created" in entry["note"].lower() for entry in log))


def test_get_item_not_found(client):
    resp = client.get("/api/v1/items/999999")
    expect("status code", 404, resp.status_code)


def test_update_item_records_price_history_on_change(client):
    created = client.post("/api/v1/items", json={"name": "Item", "sale_price": 50}).get_json()["item"]
    client.put(f"/api/v1/items/{created['id']}", json={**created, "sale_price": 40})

    prices = client.get(f"/api/v1/items/{created['id']}/prices").get_json()
    expect("two price history entries after a change", 2, len(prices))


def test_update_item_records_activity_log_on_status_change(client):
    created = client.post("/api/v1/items", json={"name": "Item"}).get_json()["item"]
    client.put(
        f"/api/v1/items/{created['id']}",
        json={**created, "primary_status": "List", "sub_status": "Active"},
    )

    log = client.get(f"/api/v1/items/{created['id']}/log").get_json()
    expect("status change logged", True, any("List / Active" in entry["note"] for entry in log))


def test_update_item_stamps_pending_payment_date_exactly_once(client):
    created = client.post(
        "/api/v1/items", json={"name": "Item", "primary_status": "Sold", "sub_status": "Shipped"}
    ).get_json()["item"]

    first = client.put(
        f"/api/v1/items/{created['id']}", json={**created, "sub_status": "Pending payment", "status_date": "2026-07-10"}
    ).get_json()["item"]
    expect("pending_payment_date set on first transition to Pending payment", True, bool(first["pending_payment_date"]))

    second = client.put(
        f"/api/v1/items/{created['id']}", json={**first, "notes": "unrelated edit"}
    ).get_json()["item"]
    expect(
        "pending_payment_date unchanged by a later, unrelated save",
        first["pending_payment_date"],
        second["pending_payment_date"],
    )


def test_update_item_stamps_reduced_at_exactly_once(client):
    created = client.post(
        "/api/v1/items", json={"name": "Item", "primary_status": "List", "sub_status": "Active"}
    ).get_json()["item"]

    first = client.put(
        f"/api/v1/items/{created['id']}", json={**created, "sub_status": "Reduced"}
    ).get_json()["item"]
    expect("reduced_at set on first transition to Reduced", True, bool(first["reduced_at"]))

    second = client.put(
        f"/api/v1/items/{created['id']}", json={**first, "notes": "unrelated edit"}
    ).get_json()["item"]
    expect(
        "reduced_at unchanged by a later, unrelated save",
        first["reduced_at"],
        second["reduced_at"],
    )


def test_update_item_rejects_stale_version(client):
    created = client.post("/api/v1/items", json={"name": "Contested"}).get_json()["item"]
    expect("initial version", 1, created["version"])

    first_put = client.put(f"/api/v1/items/{created['id']}", json={**created, "notes": "edit A"})
    expect("first update succeeds", 200, first_put.status_code)
    expect("version bumped to 2", 2, first_put.get_json()["item"]["version"])

    # A second client still holding the stale version-1 copy tries to save
    stale_put = client.put(f"/api/v1/items/{created['id']}", json={**created, "notes": "edit B"})
    expect("stale write rejected", 409, stale_put.status_code)
    body = stale_put.get_json()
    expect("error code", "version_conflict", body["error"])
    expect("current version returned", 2, body["current"]["version"])
    expect("current reflects the winning edit", "edit A", body["current"]["notes"])


def test_update_item_requires_version(client):
    created = client.post("/api/v1/items", json={"name": "No Version Sent"}).get_json()["item"]
    body = {k: v for k, v in created.items() if k != "version"}
    resp = client.put(f"/api/v1/items/{created['id']}", json=body)
    expect("status code", 400, resp.status_code)
    expect("error", "version is required", resp.get_json()["error"])


def test_relisting_a_sold_item_archives_sale_and_clears_current_fields(client):
    created = client.post(
        "/api/v1/items",
        json={
            "name": "Tablet", "primary_status": "Sold", "sub_status": "Paid",
            "sold_on": "eBay", "buyer_name": "buyer1", "sale_price": 100,
            "fees": 12, "shipping_cost": 3, "shipping_method": "Royal Mail",
            "tracking_ref": "TRK123", "paid_date": "2026-06-01",
        },
    ).get_json()["item"]

    relisted = client.put(
        f"/api/v1/items/{created['id']}",
        json={**created, "primary_status": "List", "sub_status": "Relisted"},
    ).get_json()["item"]

    expect("sold_on cleared", "", relisted["sold_on"])
    expect("buyer_name cleared", "", relisted["buyer_name"])
    expect("sale_price cleared", 0, relisted["sale_price"])
    expect("tracking_ref cleared", "", relisted["tracking_ref"])
    expect("paid_date cleared", "", relisted["paid_date"])
    expect("relisted_count bumped", 1, relisted["relisted_count"])

    history = client.get(f"/api/v1/items/{created['id']}/sales").get_json()
    expect("one sale archived", 1, len(history))
    expect("archived sold_on", "eBay", history[0]["sold_on"])
    expect("archived buyer_name", "buyer1", history[0]["buyer_name"])
    expect("archived sale_price", 100, history[0]["sale_price"])
    expect("archived ended_status", "Sold / Paid", history[0]["ended_status"])

    log = client.get(f"/api/v1/items/{created['id']}/log").get_json()
    expect("archive noted in activity log", True, any("archived" in entry["note"].lower() for entry in log))


def test_relisting_a_returned_item_archives_and_bumps_relisted_count(client):
    created = client.post(
        "/api/v1/items",
        json={
            "name": "Returned Widget", "primary_status": "Return", "sub_status": "Returned",
            "sold_on": "Vinted", "buyer_name": "buyer2", "sale_price": 40,
        },
    ).get_json()["item"]

    relisted = client.put(
        f"/api/v1/items/{created['id']}",
        json={**created, "primary_status": "List", "sub_status": "Relisted"},
    ).get_json()["item"]

    expect("relisted_count bumped for a Return->List relist", 1, relisted["relisted_count"])
    history = client.get(f"/api/v1/items/{created['id']}/sales").get_json()
    expect("one sale archived from the Return cycle", 1, len(history))
    expect("archived ended_status reflects the Return cycle", "Return / Returned", history[0]["ended_status"])


def test_create_item_round_trips_return_shipping_cost(client):
    item = client.post("/api/v1/items", json={"name": "Item", "return_shipping_cost": 4.5}).get_json()["item"]
    expect("return_shipping_cost persisted on create", 4.5, item["return_shipping_cost"])


def test_update_item_requires_return_shipping_cost_before_label_sent(client):
    item = client.post(
        "/api/v1/items", json={"name": "Returning Item", "primary_status": "Return", "sub_status": "Returned"}
    ).get_json()["item"]

    blocked = client.put(
        f"/api/v1/items/{item['id']}",
        json={**item, "sub_status": "Label sent", "status_date": "2026-07-10"},
    )
    expect("blocked without a return label cost", 400, blocked.status_code)
    expect("error code", "missing_fields", blocked.get_json()["error"])

    allowed = client.put(
        f"/api/v1/items/{item['id']}",
        json={**item, "sub_status": "Label sent", "status_date": "2026-07-10", "return_shipping_cost": 3.99},
    )
    expect("succeeds once a return label cost is supplied", 200, allowed.status_code)
    expect("return_shipping_cost persisted", 3.99, allowed.get_json()["item"]["return_shipping_cost"])


def test_advance_return_in_transit_requires_tracking_ref_and_persists_it(client):
    item = client.post(
        "/api/v1/items", json={
            "name": "Returning Item", "primary_status": "Return", "sub_status": "Label sent",
            "status_date": "2026-07-10", "return_shipping_cost": 3.99,
        }
    ).get_json()["item"]

    blocked = client.post(f"/api/v1/items/{item['id']}/advance")
    expect("blocked without a return tracking ref", 409, blocked.status_code)
    missing = {f["field"] for f in blocked.get_json()["missing_fields"]}
    expect("reports the missing tracking ref", {"return_tracking_ref"}, missing)

    resp = client.post(f"/api/v1/items/{item['id']}/advance", json={"return_tracking_ref": "RTRK1"})
    expect("advances to In transit", "In transit", resp.get_json()["item"]["sub_status"])
    expect("return_tracking_ref persisted by the quick-advance", "RTRK1", resp.get_json()["item"]["return_tracking_ref"])
    expect("return_shipping_cost untouched by the advance", 3.99, resp.get_json()["item"]["return_shipping_cost"])


def test_relisting_archives_return_shipping_cost_and_resets_it(client):
    created = client.post(
        "/api/v1/items",
        json={
            "name": "Returned Widget", "primary_status": "Return", "sub_status": "Returned",
            "sold_on": "Vinted", "buyer_name": "buyer2", "sale_price": 40, "return_shipping_cost": 6.5,
        },
    ).get_json()["item"]

    relisted = client.put(
        f"/api/v1/items/{created['id']}",
        json={**created, "primary_status": "List", "sub_status": "Relisted"},
    ).get_json()["item"]

    expect("return_shipping_cost reset to 0 on relist", 0, relisted["return_shipping_cost"])
    history = client.get(f"/api/v1/items/{created['id']}/sales").get_json()
    expect("archived return_shipping_cost", 6.5, history[0]["return_shipping_cost"])


def test_sale_history_empty_for_item_never_relisted(client):
    created = client.post("/api/v1/items", json={"name": "Untouched"}).get_json()["item"]
    history = client.get(f"/api/v1/items/{created['id']}/sales").get_json()
    expect("no sale history yet", 0, len(history))


def test_relist_platforms_refreshes_dates_and_clears_reduced_at(client):
    old_date = "2026-01-01"
    created = client.post(
        "/api/v1/items",
        json={
            "name": "Item", "primary_status": "List", "sub_status": "Active",
            "listed_on": [{"platform": "Vinted", "url": "", "date": old_date}, {"platform": "eBay", "url": "", "date": old_date}],
        },
    ).get_json()["item"]
    client.put(f"/api/v1/items/{created['id']}", json={**created, "sub_status": "Reduced"})

    resp = client.post(f"/api/v1/items/{created['id']}/relist-platforms")
    expect("status code", 200, resp.status_code)
    item = resp.get_json()["item"]
    today = date.today().isoformat()
    expect("every platform date refreshed to today", [today, today], [p["date"] for p in item["listed_on"]])
    expect("reduced_at cleared", "", item["reduced_at"])


def test_relist_platforms_requires_at_least_one_platform(client):
    created = client.post("/api/v1/items", json={"name": "Never Listed Anywhere"}).get_json()["item"]
    resp = client.post(f"/api/v1/items/{created['id']}/relist-platforms")
    expect("status code", 400, resp.status_code)


def test_import_items_creates_rows_from_csv(client):
    csv_content = (
        "name,cost_price,sale_price,source\n"
        "Vintage Jacket,10,25,Charity shop\n"
        "Retro Lamp,5,15,Car boot sale\n"
    )
    resp = client.post("/api/v1/items/import", json={"content": csv_content})
    expect("status code", 200, resp.status_code)
    body = resp.get_json()
    expect("both rows created", 2, body["created"])
    expect("nothing skipped", [], body["skipped"])

    items = {i["name"]: i for i in client.get("/api/v1/items").get_json()}
    expect("Vintage Jacket imported", True, "Vintage Jacket" in items)
    expect("imported cost_price parsed as a number", 10.0, items["Vintage Jacket"]["cost_price"])
    expect("imported source column mapped", "Car boot sale", items["Retro Lamp"]["source"])


def test_import_items_is_case_insensitive_on_headers(client):
    csv_content = "NAME,Sale_Price\nWidget,20\n"
    resp = client.post("/api/v1/items/import", json={"content": csv_content})
    expect("status code", 200, resp.status_code)
    expect("created despite mixed-case headers", 1, resp.get_json()["created"])


def test_import_items_skips_rows_missing_a_name_but_keeps_going(client):
    csv_content = "name,sale_price\n,20\nGood Row,30\n"
    resp = client.post("/api/v1/items/import", json={"content": csv_content})
    body = resp.get_json()
    expect("one row created", 1, body["created"])
    expect("one row skipped", 1, len(body["skipped"]))
    expect("skipped row reason", "name is required", body["skipped"][0]["reason"])
    expect("skipped row references its own CSV line", 2, body["skipped"][0]["row"])


def test_import_items_skips_rows_with_non_numeric_price_but_keeps_going(client):
    csv_content = "name,sale_price\nBad Price,not-a-number\nGood Row,30\n"
    resp = client.post("/api/v1/items/import", json={"content": csv_content})
    body = resp.get_json()
    expect("only the good row created", 1, body["created"])
    expect("bad-price row skipped", 1, len(body["skipped"]))

    items = [i["name"] for i in client.get("/api/v1/items").get_json()]
    expect("only Good Row made it in", ["Good Row"], items)


def test_import_items_rejects_empty_content(client):
    resp = client.post("/api/v1/items/import", json={"content": ""})
    expect("status code", 400, resp.status_code)


def test_import_items_seeds_price_history_when_sale_price_given(client):
    client.post("/api/v1/items/import", json={"content": "name,sale_price\nPriced Item,42\n"})
    item = next(i for i in client.get("/api/v1/items").get_json() if i["name"] == "Priced Item")
    prices = client.get(f"/api/v1/items/{item['id']}/prices").get_json()
    expect("price history seeded from the imported sale_price", 1, len(prices))
    expect("seeded price matches", 42, prices[0]["price"])


def test_delete_item(client):
    created = client.post("/api/v1/items", json={"name": "To Delete"}).get_json()["item"]
    resp = client.delete(f"/api/v1/items/{created['id']}")
    expect("status code", 200, resp.status_code)

    gone = client.get(f"/api/v1/items/{created['id']}")
    expect("item is gone", 404, gone.status_code)


def test_deleted_item_is_a_soft_delete_hidden_from_list_and_lookup(client):
    created = client.post("/api/v1/items", json={"name": "Soft Deleted", "sale_price": 25}).get_json()["item"]
    client.delete(f"/api/v1/items/{created['id']}")

    expect("deleted item excluded from the list", [], client.get("/api/v1/items").get_json())
    expect("deleted item's own GET 404s", 404, client.get(f"/api/v1/items/{created['id']}").status_code)
    expect(
        "deleted item's PUT 404s (can't be edited while deleted)",
        404,
        client.put(f"/api/v1/items/{created['id']}", json={**created, "sale_price": 50}).status_code,
    )


def test_undelete_item_restores_it_with_history_intact(client):
    created = client.post("/api/v1/items", json={"name": "Recoverable", "sale_price": 25}).get_json()["item"]
    client.delete(f"/api/v1/items/{created['id']}")

    resp = client.post(f"/api/v1/items/{created['id']}/undelete")
    expect("status code", 200, resp.status_code)
    restored = resp.get_json()["item"]
    expect("restored item keeps its original id", created["id"], restored["id"])
    expect("restored item is visible again in the list", [created["id"]], [i["id"] for i in client.get("/api/v1/items").get_json()])

    prices = client.get(f"/api/v1/items/{created['id']}/prices").get_json()
    expect("price history survived the soft-delete/undelete round trip", 1, len(prices))
    log = client.get(f"/api/v1/items/{created['id']}/log").get_json()
    expect("activity log survived the soft-delete/undelete round trip", True, any("created" in e["note"].lower() for e in log))


def test_undelete_item_not_found(client):
    resp = client.post("/api/v1/items/999999/undelete")
    expect("status code", 404, resp.status_code)


def test_recreating_a_deleted_items_name_does_not_warn_of_a_duplicate(client):
    created = client.post("/api/v1/items", json={"name": "Reused Name"}).get_json()["item"]
    client.delete(f"/api/v1/items/{created['id']}")

    resp = client.post("/api/v1/items", json={"name": "Reused Name"})
    expect("status code", 201, resp.status_code)
    expect("no duplicate warning against a soft-deleted item", None, resp.get_json()["warning"])


def test_advance_sold_chain_end_to_end(client):
    # tracking_ref and status_date are set upfront so the chain's own
    # requirements (Shipped needs a tracking reference, Pending payment
    # needs a due date - see REQUIRED_FIELDS in blueprints/items.py) are
    # already satisfied by the time /advance reaches those steps. Paid also
    # requires a payment date, but that one's supplied on the fly below
    # since it's not known in advance the way the others are.
    item = client.post(
        "/api/v1/items", json={
            "name": "Sold Item", "primary_status": "Sold", "sub_status": "Needs Shipping",
            "tracking_ref": "TRACK123", "status_date": "2026-07-10",
        }
    ).get_json()["item"]
    item_id = item["id"]

    for expected_sub_status in ["Shipped", "At collection point", "Pending payment"]:
        resp = client.post(f"/api/v1/items/{item_id}/advance")
        actual = resp.get_json()["item"]["sub_status"]
        expect(f"advances to {expected_sub_status}", expected_sub_status, actual)

    # Paid is blocked until a payment date is supplied
    resp = client.post(f"/api/v1/items/{item_id}/advance")
    expect("blocked without a payment date", "missing_fields", resp.get_json().get("error"))
    resp = client.post(f"/api/v1/items/{item_id}/advance", json={"paid_date": "2026-07-15"})
    body = resp.get_json()
    expect("advances to Paid", "Paid", body["item"]["sub_status"])
    expect("paid_date persisted", "2026-07-15", body["item"]["paid_date"])


def test_advance_stops_at_end_of_chain(client):
    item = client.post(
        "/api/v1/items", json={"name": "Paid Item", "primary_status": "Sold", "sub_status": "Paid"}
    ).get_json()["item"]
    resp = client.post(f"/api/v1/items/{item['id']}/advance")
    expect("stays at Paid (end of chain)", "Paid", resp.get_json()["item"]["sub_status"])


def test_paid_date_backfill_sets_only_that_field(client):
    item = client.post(
        "/api/v1/items",
        json={"name": "Legacy Paid Item", "primary_status": "Sold", "sub_status": "Paid", "sale_price": 100},
    ).get_json()["item"]

    resp = client.post(f"/api/v1/items/{item['id']}/paid-date", json={"paid_date": "2026-02-01"})
    expect("status code", 200, resp.status_code)
    updated = resp.get_json()["item"]
    expect("paid_date persisted", "2026-02-01", updated["paid_date"])
    expect("other fields untouched", "Legacy Paid Item", updated["name"])
    expect("sale_price untouched", 100, updated["sale_price"])


def test_paid_date_backfill_requires_a_date(client):
    item = client.post(
        "/api/v1/items",
        json={"name": "Legacy Paid Item", "primary_status": "Sold", "sub_status": "Paid"},
    ).get_json()["item"]

    resp = client.post(f"/api/v1/items/{item['id']}/paid-date", json={"paid_date": ""})
    expect("status code", 400, resp.status_code)
    expect("error is missing_fields", "missing_fields", resp.get_json()["error"])


def test_advance_return_chain(client):
    # status_date (the return due date) and return_shipping_cost are set
    # upfront - Label sent requires both (see REQUIRED_FIELDS in
    # blueprints/items.py). In transit also requires a return_tracking_ref,
    # but that's not known in advance, so it's supplied on the fly below -
    # same pattern as the Sold chain's payment date.
    item = client.post(
        "/api/v1/items", json={
            "name": "Returning Item", "primary_status": "Return", "sub_status": "Label sent",
            "status_date": "2026-07-10", "return_shipping_cost": 3.99,
        }
    ).get_json()["item"]
    item_id = item["id"]

    resp = client.post(f"/api/v1/items/{item_id}/advance", json={"return_tracking_ref": "RTRK1"})
    expect("advances to In transit", "In transit", resp.get_json()["item"]["sub_status"])

    resp = client.post(f"/api/v1/items/{item_id}/advance")
    expect("advances to Returned", "Returned", resp.get_json()["item"]["sub_status"])


def test_advance_list_chain(client):
    # listing_title/listing_description/has_photos are set upfront - Ready
    # to list requires all three (see REQUIRED_FIELDS in blueprints/items.py).
    item = client.post(
        "/api/v1/items", json={
            "name": "Draft Item", "primary_status": "List", "sub_status": "Draft",
            "listing_title": "Title", "listing_description": "Description", "has_photos": True,
        }
    ).get_json()["item"]
    item_id = item["id"]

    for expected_sub_status in ["Ready to list", "Active", "Reduced"]:
        resp = client.post(f"/api/v1/items/{item_id}/advance")
        actual = resp.get_json()["item"]["sub_status"]
        expect(f"advances to {expected_sub_status}", expected_sub_status, actual)

    reduced_at = resp.get_json()["item"]["reduced_at"]
    expect("reduced_at stamped by the quick-advance into Reduced", True, bool(reduced_at))

    # Reduced is the end of the chain - Relisted is a manually-chosen branch,
    # not reachable via quick-advance - so advancing again is a no-op.
    resp = client.post(f"/api/v1/items/{item_id}/advance")
    expect("stays at Reduced (end of chain)", "Reduced", resp.get_json()["item"]["sub_status"])
    expect("reduced_at unchanged by the no-op re-advance", reduced_at, resp.get_json()["item"]["reduced_at"])


def test_advance_refunded_is_a_no_op(client):
    item = client.post(
        "/api/v1/items", json={"name": "Refunded Item", "primary_status": "Refund", "sub_status": "Item damaged"}
    ).get_json()["item"]
    resp = client.post(f"/api/v1/items/{item['id']}/advance")
    expect("Refund has no advancement chain", "Item damaged", resp.get_json()["item"]["sub_status"])


def test_advance_refuses_without_required_field(client):
    # Created directly via the API (ungated - create_item doesn't validate),
    # so it starts one step before Shipped with no tracking reference at all.
    item = client.post(
        "/api/v1/items", json={"name": "No Tracking", "primary_status": "Sold", "sub_status": "Needs Shipping"}
    ).get_json()["item"]
    resp = client.post(f"/api/v1/items/{item['id']}/advance")
    expect("status code", 409, resp.status_code)
    body = resp.get_json()
    expect("error code", "missing_fields", body["error"])
    expect("reports the missing tracking reference", ["tracking_ref"], [f["field"] for f in body["missing_fields"]])

    unchanged = client.get(f"/api/v1/items/{item['id']}").get_json()
    expect("sub_status did not advance", "Needs Shipping", unchanged["sub_status"])


def test_advance_succeeds_when_missing_field_supplied_in_body(client):
    item = client.post(
        "/api/v1/items", json={"name": "No Tracking", "primary_status": "Sold", "sub_status": "Needs Shipping"}
    ).get_json()["item"]
    resp = client.post(f"/api/v1/items/{item['id']}/advance", json={"tracking_ref": "TRACK456"})
    expect("status code", 200, resp.status_code)
    updated = resp.get_json()["item"]
    expect("advanced to Shipped", "Shipped", updated["sub_status"])
    expect("tracking_ref persisted alongside the advance", "TRACK456", updated["tracking_ref"])


def test_advance_ready_to_list_requires_title_description_and_photos(client):
    item = client.post(
        "/api/v1/items", json={"name": "Bare Draft", "primary_status": "List", "sub_status": "Draft"}
    ).get_json()["item"]
    resp = client.post(f"/api/v1/items/{item['id']}/advance")
    expect("status code", 409, resp.status_code)
    missing = {f["field"] for f in resp.get_json()["missing_fields"]}
    expect("all three fields reported missing", {"listing_title", "listing_description", "has_photos"}, missing)


def test_update_item_refuses_pending_payment_without_due_date(client):
    item = client.post(
        "/api/v1/items", json={"name": "Item", "primary_status": "Sold", "sub_status": "Shipped"}
    ).get_json()["item"]
    resp = client.put(f"/api/v1/items/{item['id']}", json={**item, "sub_status": "Pending payment"})
    expect("status code", 400, resp.status_code)
    expect("error code", "missing_fields", resp.get_json()["error"])

    unchanged = client.get(f"/api/v1/items/{item['id']}").get_json()
    expect("sub_status was not changed", "Shipped", unchanged["sub_status"])


def test_update_item_entering_sold_requires_buyer_name(client):
    item = client.post(
        "/api/v1/items", json={"name": "Draft Item", "primary_status": "List", "sub_status": "Draft"}
    ).get_json()["item"]

    blocked = client.put(
        f"/api/v1/items/{item['id']}", json={**item, "primary_status": "Sold", "sub_status": "Needs Shipping"}
    )
    expect("blocked without a buyer name", 400, blocked.status_code)

    allowed = client.put(
        f"/api/v1/items/{item['id']}",
        json={**item, "primary_status": "Sold", "sub_status": "Needs Shipping", "buyer_name": "buyer1"},
    )
    expect("succeeds once a buyer name is supplied", 200, allowed.status_code)
    expect("primary_status is now Sold", "Sold", allowed.get_json()["item"]["primary_status"])


def test_update_item_does_not_require_buyer_name_on_later_sold_edits(client):
    # buyer_name is only required at the moment of *entering* Sold, not on
    # every subsequent save while already Sold.
    item = client.post(
        "/api/v1/items",
        json={"name": "Item", "primary_status": "Sold", "sub_status": "Needs Shipping", "buyer_name": "buyer1"},
    ).get_json()["item"]
    resp = client.put(f"/api/v1/items/{item['id']}", json={**item, "buyer_name": "", "notes": "typo fix"})
    expect("status code", 200, resp.status_code)


def test_add_item_log_note(client):
    item = client.post("/api/v1/items", json={"name": "Item"}).get_json()["item"]
    resp = client.post(f"/api/v1/items/{item['id']}/log", json={"note": "Manual note"})
    expect("status code", 200, resp.status_code)

    log = client.get(f"/api/v1/items/{item['id']}/log").get_json()
    expect("manual note recorded", True, any(entry["note"] == "Manual note" for entry in log))


def test_add_item_log_rejects_empty_note(client):
    item = client.post("/api/v1/items", json={"name": "Item"}).get_json()["item"]
    resp = client.post(f"/api/v1/items/{item['id']}/log", json={"note": "   "})
    expect("status code", 400, resp.status_code)


def test_batch_update_marks_shipped(client):
    a = client.post("/api/v1/items", json={"name": "A"}).get_json()["item"]
    b = client.post("/api/v1/items", json={"name": "B"}).get_json()["item"]

    resp = client.post(
        "/api/v1/items/batch",
        json={"ids": [a["id"], b["id"]], "updates": {"primary_status": "Sold", "sub_status": "Shipped"}},
    )
    expect("status code", 200, resp.status_code)
    expect("count updated", 2, resp.get_json()["count"])

    for item_id in (a["id"], b["id"]):
        item = client.get(f"/api/v1/items/{item_id}").get_json()
        expect(f"item {item_id} primary_status", "Sold", item["primary_status"])
        expect(f"item {item_id} sub_status", "Shipped", item["sub_status"])


def test_batch_update_ignores_non_whitelisted_columns(client):
    """Negative-path test for EDITABLE_ITEM_COLUMNS - the whitelist that
    stands between batch_update()'s request-body-driven SET clause and
    arbitrary SQL (see its own comment in blueprints/items.py). A key not on
    the whitelist should be silently skipped, not applied."""
    item = client.post("/api/v1/items", json={"name": "Original Name", "notes": "original"}).get_json()["item"]

    resp = client.post(
        "/api/v1/items/batch",
        json={"ids": [item["id"]], "updates": {"id": 99999, "not_a_real_column": "hacked"}},
    )
    expect("status code", 200, resp.status_code)

    unchanged = client.get(f"/api/v1/items/{item['id']}").get_json()
    expect("id untouched", item["id"], unchanged["id"])
    expect("name untouched", "Original Name", unchanged["name"])
    expect("notes untouched", "original", unchanged["notes"])


def test_batch_update_rejects_non_numeric_price(client):
    item = client.post("/api/v1/items", json={"name": "Item"}).get_json()["item"]
    resp = client.post(
        "/api/v1/items/batch",
        json={"ids": [item["id"]], "updates": {"cost_price": "not-a-number"}},
    )
    expect("status code", 400, resp.status_code)
    expect("error names the field", True, "cost_price" in resp.get_json()["error"])

    unchanged = client.get(f"/api/v1/items/{item['id']}").get_json()
    expect("cost_price untouched", 0, unchanged["cost_price"])


def test_activity_log_prunes_old_entries_but_protects_key_rows(client):
    item = client.post("/api/v1/items", json={"name": "Prune Test"}).get_json()["item"]
    item_id = item["id"]

    # Give it a "Status: Sold ..." row to protect too, alongside "Item created".
    client.put(f"/api/v1/items/{item_id}", json={
        **item, "primary_status": "Sold", "sub_status": "Needs Shipping", "buyer_name": "Buyer",
    })

    # Comfortably past ACTIVITY_LOG_CAP so pruning has to actually run more
    # than once (a single one-row overshoot doesn't trigger a real prune -
    # see _log_activity's docstring).
    for i in range(ACTIVITY_LOG_CAP + 60):
        resp = client.post(f"/api/v1/items/{item_id}/log", json={"note": f"note {i}"})
        expect(f"log note {i} accepted", 200, resp.status_code)

    name = db_module.get_active_db_name(client.user_id)
    conn = sqlite3.connect(str(db_module.user_db_dir(client.user_id) / f"{name}.db"))
    notes = [r[0] for r in conn.execute(
        "SELECT note FROM activity_log WHERE item_id = ? ORDER BY id ASC", (item_id,)
    ).fetchall()]
    conn.close()

    expect("Item created row survives pruning", True, "Item created" in notes)
    expect("first Status: Sold row survives pruning", True, any(n.startswith("Status: Sold") for n in notes))
    expect("row count stays close to the cap (plus protected rows)", True, len(notes) <= ACTIVITY_LOG_CAP + 2)
    expect("the newest note survives", True, f"note {ACTIVITY_LOG_CAP + 59}" in notes)
    expect("an old, unprotected note was actually pruned", False, "note 0" in notes)


def test_fix_archived_sale_return_shipping_cost(client):
    created = client.post(
        "/api/v1/items",
        json={
            "name": "Returned Widget", "primary_status": "Return", "sub_status": "Returned",
            "sold_on": "Vinted", "buyer_name": "buyer2", "sale_price": 40,
        },
    ).get_json()["item"]
    client.put(
        f"/api/v1/items/{created['id']}",
        json={**created, "primary_status": "List", "sub_status": "Relisted"},
    )
    sale_id = client.get(f"/api/v1/items/{created['id']}/sales").get_json()[0]["id"]

    resp = client.put(f"/api/v1/items/{created['id']}/sales/{sale_id}", json={"return_shipping_cost": 9.5})
    expect("status code", 200, resp.status_code)
    expect("updated value returned", 9.5, resp.get_json()["sale"]["return_shipping_cost"])

    refetched = client.get(f"/api/v1/items/{created['id']}/sales").get_json()
    expect("value persisted", 9.5, refetched[0]["return_shipping_cost"])


def test_fix_archived_sale_return_shipping_cost_not_found(client):
    item = client.post("/api/v1/items", json={"name": "Item"}).get_json()["item"]
    resp = client.put(f"/api/v1/items/{item['id']}/sales/999999", json={"return_shipping_cost": 5})
    expect("status code", 404, resp.status_code)


def test_fix_archived_sale_return_shipping_cost_rejects_non_numeric(client):
    created = client.post(
        "/api/v1/items",
        json={"name": "Returned Widget", "primary_status": "Return", "sub_status": "Returned", "sale_price": 40},
    ).get_json()["item"]
    client.put(
        f"/api/v1/items/{created['id']}",
        json={**created, "primary_status": "List", "sub_status": "Relisted"},
    )
    sale_id = client.get(f"/api/v1/items/{created['id']}/sales").get_json()[0]["id"]

    resp = client.put(f"/api/v1/items/{created['id']}/sales/{sale_id}", json={"return_shipping_cost": "abc"})
    expect("status code", 400, resp.status_code)
