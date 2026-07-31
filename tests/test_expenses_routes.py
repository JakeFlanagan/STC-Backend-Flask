"""Tests for /api/expenses* routes (blueprints/expenses.py): CRUD for the
expense ledger, and its integration into /api/tax's net profit
(blueprints/tax.py).
"""

from helpers import expect


def test_create_expense_defaults(client):
    resp = client.post("/api/v1/expenses", json={"description": "Packaging tape", "amount": 5.5, "expense_date": "2026-01-10"})
    expect("status code", 201, resp.status_code)
    expense = resp.get_json()["expense"]
    expect("description", "Packaging tape", expense["description"])
    expect("amount", 5.5, expense["amount"])
    expect("expense_date", "2026-01-10", expense["expense_date"])
    expect("category defaults to empty", "", expense["category"])


def test_create_expense_rejects_non_numeric_amount(client):
    resp = client.post("/api/v1/expenses", json={"description": "Bad", "amount": "not-a-number"})
    expect("status code", 400, resp.status_code)


def test_list_expenses_newest_first(client):
    client.post("/api/v1/expenses", json={"description": "Older", "amount": 1, "expense_date": "2026-01-01"})
    client.post("/api/v1/expenses", json={"description": "Newer", "amount": 2, "expense_date": "2026-02-01"})

    resp = client.get("/api/v1/expenses")
    expect("status code", 200, resp.status_code)
    names = [e["description"] for e in resp.get_json()]
    expect("newest expense_date first", ["Newer", "Older"], names)


def test_update_expense(client):
    created = client.post("/api/v1/expenses", json={"description": "Mileage", "amount": 10, "expense_date": "2026-01-01"}).get_json()["expense"]
    resp = client.put(f"/api/v1/expenses/{created['id']}", json={"amount": 15, "category": "Travel", "version": created["version"]})
    expect("status code", 200, resp.status_code)
    updated = resp.get_json()["expense"]
    expect("amount updated", 15, updated["amount"])
    expect("category updated", "Travel", updated["category"])
    expect("description left untouched", "Mileage", updated["description"])


def test_update_expense_rejects_stale_version(client):
    created = client.post(
        "/api/v1/expenses", json={"description": "Boxes", "amount": 5, "expense_date": "2026-01-01"}
    ).get_json()["expense"]
    expect("initial version", 1, created["version"])

    first_put = client.put(f"/api/v1/expenses/{created['id']}", json={"amount": 6, "version": 1})
    expect("first update succeeds", 200, first_put.status_code)
    expect("version bumped to 2", 2, first_put.get_json()["expense"]["version"])

    stale_put = client.put(f"/api/v1/expenses/{created['id']}", json={"amount": 7, "version": 1})
    expect("stale write rejected", 409, stale_put.status_code)
    body = stale_put.get_json()
    expect("error code", "version_conflict", body["error"])
    expect("current amount reflects the winning edit", 6, body["current"]["amount"])


def test_update_expense_requires_version(client):
    created = client.post(
        "/api/v1/expenses", json={"description": "Tape", "amount": 3, "expense_date": "2026-01-01"}
    ).get_json()["expense"]
    resp = client.put(f"/api/v1/expenses/{created['id']}", json={"amount": 4})
    expect("status code", 400, resp.status_code)
    expect("error", "version is required", resp.get_json()["error"])


def test_update_expense_not_found(client):
    resp = client.put("/api/v1/expenses/999999", json={"amount": 1})
    expect("status code", 404, resp.status_code)


def test_delete_expense(client):
    created = client.post("/api/v1/expenses", json={"description": "Storage", "amount": 20, "expense_date": "2026-01-01"}).get_json()["expense"]
    resp = client.delete(f"/api/v1/expenses/{created['id']}")
    expect("status code", 200, resp.status_code)
    expect("expense list now empty", [], client.get("/api/v1/expenses").get_json())


def test_tax_summary_subtracts_expenses_in_range_from_profit(client):
    client.post(
        "/api/v1/items",
        json={"name": "Sold Item", "primary_status": "Sold", "sub_status": "Paid", "sale_price": 100, "paid_date": "2026-01-15"},
    )
    client.post("/api/v1/expenses", json={"description": "In range", "amount": 20, "expense_date": "2026-01-10"})
    client.post("/api/v1/expenses", json={"description": "Out of range", "amount": 999, "expense_date": "2025-06-01"})

    resp = client.get("/api/v1/tax?from=2026-01-01&to=2026-01-31")
    t = resp.get_json()["totals"]
    expect("expenses_total only counts the in-range expense", 20, t["expenses_total"])
    expect("profit is revenue minus the in-range expense", 80, t["profit"])
    expect("expenses list excludes the out-of-range one", 1, len(resp.get_json()["expenses"]))
