"""Tests for /api/settings routes (blueprints/settings_bp.py)."""

from helpers import expect


def test_get_settings_returns_defaults_for_a_new_db(client):
    resp = client.get("/api/v1/settings")
    expect("status code", 200, resp.status_code)
    settings = resp.get_json()
    expect("Vinted percent default", 0, settings["platform_fees"]["Vinted"]["percent"])
    expect("deadstock_days default", 14, settings["deadstock_days"])
    expect("currency_symbol default", "£", settings["currency_symbol"])


def test_update_settings_currency_symbol(client):
    resp = client.put("/api/v1/settings", json={"currency_symbol": "$", "version": 1})
    expect("status code", 200, resp.status_code)

    confirm = client.get("/api/v1/settings").get_json()
    expect("currency_symbol updated", "$", confirm["currency_symbol"])


def test_update_settings_persists(client):
    resp = client.put("/api/v1/settings", json={"deadstock_days": 30, "version": 1})
    expect("status code", 200, resp.status_code)

    confirm = client.get("/api/v1/settings").get_json()
    expect("deadstock_days updated", 30, confirm["deadstock_days"])


def test_update_settings_only_touches_sent_keys(client):
    before = client.get("/api/v1/settings").get_json()
    client.put("/api/v1/settings", json={"deadstock_days": 21, "version": before["version"]})
    after = client.get("/api/v1/settings").get_json()

    expect("deadstock_days changed", 21, after["deadstock_days"])
    expect("platform_fees left untouched", before["platform_fees"], after["platform_fees"])


def test_get_settings_includes_version(client):
    settings = client.get("/api/v1/settings").get_json()
    expect("initial version", 1, settings["version"])


def test_update_settings_rejects_stale_version(client):
    first_put = client.put("/api/v1/settings", json={"deadstock_days": 10, "version": 1})
    expect("first update succeeds", 200, first_put.status_code)
    expect("version bumped to 2", 2, first_put.get_json()["version"])

    stale_put = client.put("/api/v1/settings", json={"deadstock_days": 99, "version": 1})
    expect("stale write rejected", 409, stale_put.status_code)
    body = stale_put.get_json()
    expect("error code", "version_conflict", body["error"])
    expect("current reflects the winning edit", 10, body["current"]["deadstock_days"])
    expect("current version", 2, body["current"]["version"])


def test_update_settings_requires_version(client):
    resp = client.put("/api/v1/settings", json={"deadstock_days": 10})
    expect("status code", 400, resp.status_code)
    expect("error", "version is required", resp.get_json()["error"])


def test_update_settings_ignores_client_supplied_underscore_version(client):
    # "_version" is the reserved internal key the version counter is stored
    # under - a client sending it as if it were a regular setting must not
    # be able to corrupt or bypass the real counter.
    resp = client.put("/api/v1/settings", json={"_version": 999, "deadstock_days": 5, "version": 1})
    expect("status code", 200, resp.status_code)
    expect("real version unaffected by client-supplied _version", 2, resp.get_json()["version"])

    confirm = client.get("/api/v1/settings").get_json()
    expect("version still the real counter, not 999", 2, confirm["version"])
    expect("_version never surfaces as a public key", False, "_version" in confirm)
