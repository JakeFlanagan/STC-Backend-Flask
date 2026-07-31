"""Tests for /api/tax and /api/tax/csv (blueprints/tax.py). Both only ever
consider Sold/Paid items with a paid_date set."""

from blueprints.tax import _uk_tax_quarter
from helpers import expect


def _make_paid_item(client, name, paid_date, sale_price=100, cost_price=20, fees=10, shipping_cost=5, sold_on="Vinted", return_shipping_cost=0):
    return client.post(
        "/api/v1/items",
        json={
            "name": name,
            "primary_status": "Sold",
            "sub_status": "Paid",
            "paid_date": paid_date,
            "sale_price": sale_price,
            "cost_price": cost_price,
            "fees": fees,
            "shipping_cost": shipping_cost,
            "sold_on": sold_on,
            "return_shipping_cost": return_shipping_cost,
        },
    ).get_json()["item"]


def test_tax_summary_excludes_soft_deleted_items(client):
    item = _make_paid_item(client, "Deleted Sale", "2026-01-15", sale_price=100)
    client.delete(f"/api/v1/items/{item['id']}")

    totals = client.get("/api/v1/tax").get_json()["totals"]
    expect("deleted item excluded from tax totals", 0, totals["count"])
    expect("deleted item's revenue excluded", 0, totals["revenue"])


def test_tax_summary_only_counts_paid_items(client):
    _make_paid_item(client, "Paid Item", "2026-01-15")
    client.post(
        "/api/v1/items",
        json={"name": "Not Paid Yet", "primary_status": "Sold", "sub_status": "Pending payment", "sale_price": 999},
    )

    resp = client.get("/api/v1/tax")
    expect("status code", 200, resp.status_code)
    totals = resp.get_json()["totals"]
    expect("only the paid item is counted", 1, totals["count"])
    expect("revenue matches the paid item only", 100, totals["revenue"])


def test_tax_summary_computes_profit(client):
    _make_paid_item(client, "Item", "2026-01-15", sale_price=100, cost_price=20, fees=10, shipping_cost=5)
    totals = client.get("/api/v1/tax").get_json()["totals"]
    expect("profit = revenue - costs - fees - shipping", 65, totals["profit"])


def test_tax_date_range_filters(client):
    _make_paid_item(client, "January Sale", "2026-01-15")
    _make_paid_item(client, "March Sale", "2026-03-15")

    resp = client.get("/api/v1/tax?from=2026-02-01&to=2026-04-01")
    names = [i["name"] for i in resp.get_json()["items"]]
    expect("only the March sale is in range", ["March Sale"], names)


def test_tax_csv_export_contains_paid_items(client):
    _make_paid_item(client, "CSV Item", "2026-01-15", sale_price=50)

    resp = client.get("/api/v1/tax/csv")
    expect("status code", 200, resp.status_code)
    csv_text = resp.data.decode("utf-8")
    expect("header row present", True, csv_text.startswith("Item,Platform,Sale Price"))
    expect("item name present in the CSV body", True, "CSV Item" in csv_text)


def test_uk_tax_quarter_boundaries():
    expect("6 Apr is Q1 of the tax year starting that day", ("2026/27", "Q1"), _uk_tax_quarter("2026-04-06"))
    expect("5 Apr is Q4 of the *previous* tax year", ("2025/26", "Q4"), _uk_tax_quarter("2026-04-05"))
    expect("6 Jul is Q2", ("2026/27", "Q2"), _uk_tax_quarter("2026-07-06"))
    expect("6 Oct is Q3", ("2026/27", "Q3"), _uk_tax_quarter("2026-10-06"))
    expect("6 Jan (next calendar year) is still Q4 of the same tax year", ("2026/27", "Q4"), _uk_tax_quarter("2027-01-06"))
    expect("empty date returns None", None, _uk_tax_quarter(""))


def test_tax_quarterly_trend_nets_expenses_against_revenue_in_the_same_quarter(client):
    _make_paid_item(client, "April Sale", "2026-04-10", sale_price=100, cost_price=0, fees=0, shipping_cost=0)
    client.post("/api/v1/expenses", json={"description": "Tape", "amount": 15, "expense_date": "2026-04-20"})
    # A July sale/expense lands in a different quarter - shouldn't bleed into Q1's numbers.
    _make_paid_item(client, "July Sale", "2026-07-10", sale_price=50, cost_price=0, fees=0, shipping_cost=0)

    resp = client.get("/api/v1/tax")
    trend = {(q["tax_year"], q["quarter"]): q for q in resp.get_json()["quarterly_trend"]}

    q1 = trend[("2026/27", "Q1")]
    expect("Q1 revenue is just the April sale", 100, q1["revenue"])
    expect("Q1 expenses is just the April expense", 15, q1["expenses_total"])
    expect("Q1 profit nets the expense against the revenue", 85, q1["profit"])

    q2 = trend[("2026/27", "Q2")]
    expect("Q2 revenue is just the July sale", 50, q2["revenue"])
    expect("Q2 has no expenses", 0, q2["expenses_total"])


def test_tax_lists_paid_items_missing_a_payment_date_as_unaccounted(client):
    # created directly via /api/items (which skips REQUIRED_FIELDS, unlike
    # the editor/advance paths), so this is a Paid item with no paid_date -
    # exactly the state the "unaccounted" list exists to surface.
    unaccounted_item = client.post(
        "/api/v1/items",
        json={"name": "Legacy Paid Item", "primary_status": "Sold", "sub_status": "Paid", "sale_price": 100},
    ).get_json()["item"]
    _make_paid_item(client, "Properly Paid Item", "2026-01-15")

    resp = client.get("/api/v1/tax")
    totals = resp.get_json()["totals"]
    unaccounted = resp.get_json()["unaccounted"]

    expect("unaccounted item excluded from totals", 1, totals["count"])
    expect("one item flagged as unaccounted", 1, len(unaccounted))
    expect("unaccounted item id matches", unaccounted_item["id"], unaccounted[0]["id"])


def test_tax_summary_computes_profit_deducting_return_shipping_cost(client):
    _make_paid_item(client, "Item", "2026-01-15", sale_price=100, cost_price=20, fees=10, shipping_cost=5, return_shipping_cost=8)
    totals = client.get("/api/v1/tax").get_json()["totals"]
    expect("profit = revenue - costs - fees - shipping - return_shipping_cost", 57, totals["profit"])


def test_tax_csv_export_includes_return_shipping_column_and_deducts_it_from_profit(client):
    _make_paid_item(client, "CSV Item", "2026-01-15", sale_price=100, cost_price=20, fees=10, shipping_cost=5, return_shipping_cost=8)
    resp = client.get("/api/v1/tax/csv")
    csv_text = resp.data.decode("utf-8")
    expect("header includes Return Shipping column", True, "Return Shipping" in csv_text.splitlines()[0])
    expect("row profit deducts return_shipping_cost (100-20-10-5-8=57)", True, "57.00" in csv_text)


def test_tax_return_shipping_total_sums_current_return_items_in_range(client):
    client.post("/api/v1/items", json={
        "name": "Mid-return Item", "primary_status": "Return", "sub_status": "Label sent",
        "status_date": "2026-01-20", "return_shipping_cost": 12,
    })
    # outside the queried range - must not be counted
    client.post("/api/v1/items", json={
        "name": "Out of range Item", "primary_status": "Return", "sub_status": "Label sent",
        "status_date": "2026-06-01", "return_shipping_cost": 99,
    })

    resp = client.get("/api/v1/tax?from=2026-01-01&to=2026-01-31")
    totals = resp.get_json()["totals"]
    expect("return_shipping_total counts only the in-range item", 12, totals["return_shipping_total"])


def test_tax_return_shipping_total_includes_archived_sale_history_rows(client):
    returned = client.post(
        "/api/v1/items",
        json={
            "name": "Returned Widget", "primary_status": "Return", "sub_status": "Returned",
            "sold_on": "Vinted", "buyer_name": "buyer2", "sale_price": 40, "return_shipping_cost": 7,
        },
    ).get_json()["item"]
    client.put(
        f"/api/v1/items/{returned['id']}",
        json={**returned, "primary_status": "List", "sub_status": "Relisted"},
    )

    totals = client.get("/api/v1/tax").get_json()["totals"]
    expect("return_shipping_total includes the archived sale_history row", 7, totals["return_shipping_total"])


def test_tax_return_shipping_total_excludes_soft_deleted_items_archived_history(client):
    returned = client.post(
        "/api/v1/items",
        json={
            "name": "Returned Then Deleted", "primary_status": "Return", "sub_status": "Returned",
            "sold_on": "Vinted", "buyer_name": "buyer3", "sale_price": 40, "return_shipping_cost": 7,
        },
    ).get_json()["item"]
    client.put(
        f"/api/v1/items/{returned['id']}",
        json={**returned, "primary_status": "List", "sub_status": "Relisted"},
    )
    client.delete(f"/api/v1/items/{returned['id']}")

    totals = client.get("/api/v1/tax").get_json()["totals"]
    expect("archived return_shipping_cost excluded once the parent item is soft-deleted", 0, totals["return_shipping_total"])


def test_tax_quarterly_trend_nets_return_shipping_against_profit(client):
    _make_paid_item(client, "April Sale", "2026-04-10", sale_price=100, cost_price=0, fees=0, shipping_cost=0)
    client.post("/api/v1/items", json={
        "name": "April Return", "primary_status": "Return", "sub_status": "Label sent",
        "status_date": "2026-04-15", "return_shipping_cost": 10,
    })

    resp = client.get("/api/v1/tax")
    q1 = {(q["tax_year"], q["quarter"]): q for q in resp.get_json()["quarterly_trend"]}[("2026/27", "Q1")]
    expect("Q1 return_shipping_total", 10, q1["return_shipping_total"])
    expect("Q1 profit nets the return shipping cost against revenue", 90, q1["profit"])
