"""Tests for db.py's core behavior: schema seeding, default settings, active
database tracking, and row-to-dict conversion. Mostly exercised through the
API (the same interface every other part of the app uses), with a couple of
direct calls into db.py where that's clearer.
"""

import db
from helpers import expect


def test_init_database_seeds_default_settings(client):
    resp = client.get("/api/v1/settings")
    expect("status code", 200, resp.status_code)
    settings = resp.get_json()
    expect("Vinted fee percent defaults to 0", 0, settings["platform_fees"]["Vinted"]["percent"])
    expect("eBay fee percent default", 12.8, settings["platform_fees"]["eBay"]["percent"])
    expect("deadstock_days default", 14, settings["deadstock_days"])


def test_get_active_db_name_matches_created_db(client):
    resp = client.get("/api/v1/databases")
    data = resp.get_json()
    expect("active db is in the database list", True, data["active"] in data["databases"])
    expect(
        "db.get_active_db_name() agrees with the API",
        data["active"],
        db.get_active_db_name(client.user_id),
    )


def test_user_db_dir_is_scoped_per_user(client):
    resp = client.get("/api/v1/databases")
    active = resp.get_json()["active"]
    user_dir = db.user_db_dir(client.user_id)
    expect(
        "active db file exists inside this user's own directory",
        True,
        (user_dir / f"{active}.db").exists(),
    )
    expect("user directory is named after the user's id", str(client.user_id), user_dir.name)


def test_row_to_dict_parses_listed_on_as_a_list(client):
    listed_on = [
        {"platform": "Vinted", "url": "https://vinted.example/1", "date": "2026-01-01"},
        {"platform": "eBay", "url": "", "date": ""},
    ]
    resp = client.post("/api/v1/items", json={"name": "Widget", "listed_on": listed_on})
    expect("status code", 201, resp.status_code)
    item = resp.get_json()["item"]
    expect("listed_on is a list, not a JSON string", True, isinstance(item["listed_on"], list))
    expect("listed_on contents", listed_on, item["listed_on"])


def test_row_to_dict_defaults_listed_on_to_empty_list(client):
    resp = client.post("/api/v1/items", json={"name": "Widget with no platforms"})
    item = resp.get_json()["item"]
    expect("listed_on defaults to []", [], item["listed_on"])


def test_default_settings_shape():
    defaults = db.default_settings()
    expect("has platform_fees key", True, "platform_fees" in defaults)
    expect("has shipping_rates key", True, "shipping_rates" in defaults)
    expect("has deadstock_days key", True, "deadstock_days" in defaults)
    expect(
        "Vinted's note explains the seller pays no fee",
        True,
        "100%" in defaults["platform_fees"]["Vinted"]["note"],
    )


def test_settings_version_defaults_to_one_then_increments(tmp_path):
    import sqlite3
    import db as db_module

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()

    expect("settings version defaults to 1", 1, db_module.get_settings_version(conn))

    db_module.set_settings_version(conn, 2)
    conn.commit()
    expect("settings version increments to 2", 2, db_module.get_settings_version(conn))

    conn.close()
