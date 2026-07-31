"""Tests for migrations.py. Each migration is idempotent and only writes
when the *old* shape is actually present, so these tests seed a raw sqlite3
database file directly (bypassing db.py's init_database(), which already
seeds the current, correct shape), run the migration, and verify the fix.

Each migrate_*() function globs *every* .db file in DB_DIR - so unlike the
route tests (which just create their own uniquely-named database and never
touch anyone else's), these tests need their own private, empty directory.
Without that, a deliberately old/broken schema seeded by one test would
still be sitting in a shared directory when a *later* test's migration call
globs "every .db file", and blow up on a schema it wasn't expecting. The
`isolated_db_dir` fixture below monkeypatches migrations.DB_DIR (the name
migrate_*() actually reads, since `from config import DB_DIR` bound its own
copy) to a fresh pytest `tmp_path` for the duration of each test only.
"""

import sqlite3
import json

import pytest

import migrations
from db import SCHEMA, default_settings
from helpers import expect


@pytest.fixture
def isolated_db_dir(tmp_path, monkeypatch):
    # migrations.DB_DIR is monkeypatched to tmp_path (simulating the
    # per-user layout's top-level databases/ folder); the fixture returns
    # a fake "user 1" subdirectory one level under it, matching where a
    # real per-user database file lives (databases/<user_id>/name.db) -
    # see migrations.py's _run_over_all_dbs/run_migrations, which now glob
    # "*/*.db" instead of "*.db". Every existing test in this file writes
    # its database via _make_old_db(isolated_db_dir, ...) and reads it
    # back the same way, so no other test code needs to change - only
    # where this fixture points.
    user_dir = tmp_path / "1"
    user_dir.mkdir()
    monkeypatch.setattr(migrations, "DB_DIR", tmp_path)
    return user_dir


def _make_old_db(db_dir, name, schema_sql, seed_fn):
    """Create a database file directly in `db_dir` with whatever old
    schema/data a test wants."""
    path = db_dir / f"{name}.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(schema_sql)
    seed_fn(conn)
    conn.commit()
    conn.close()
    return path


def test_migrate_vinted_fees_fixes_old_rate(isolated_db_dir):
    def seed(conn):
        old_fees = {
            "Vinted": {"percent": 5, "fixed": 0.7, "note": "old, wrong"},
            "eBay": {"percent": 12.8, "fixed": 0.30, "note": "unaffected"},
        }
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('platform_fees', ?)",
            (json.dumps(old_fees),),
        )

    _make_old_db(isolated_db_dir, "old_vinted_fees", "", seed)
    migrations.migrate_vinted_fees()

    conn = sqlite3.connect(str(isolated_db_dir / "old_vinted_fees.db"))
    row = conn.execute("SELECT value FROM settings WHERE key='platform_fees'").fetchone()
    conn.close()
    fees = json.loads(row[0])
    expect("Vinted percent corrected to 0", 0, fees["Vinted"]["percent"])
    expect("Vinted fixed corrected to 0", 0, fees["Vinted"]["fixed"])
    expect("eBay fee left untouched", 12.8, fees["eBay"]["percent"])


def test_migrate_vinted_fees_leaves_already_correct_rate_alone(isolated_db_dir):
    def seed(conn):
        correct_fees = {"Vinted": {"percent": 0, "fixed": 0, "note": "already correct - do not touch"}}
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('platform_fees', ?)",
            (json.dumps(correct_fees),),
        )

    _make_old_db(isolated_db_dir, "already_correct_fees", "", seed)
    migrations.migrate_vinted_fees()

    conn = sqlite3.connect(str(isolated_db_dir / "already_correct_fees.db"))
    row = conn.execute("SELECT value FROM settings WHERE key='platform_fees'").fetchone()
    conn.close()
    fees = json.loads(row[0])
    expect(
        "note left untouched (proves it wasn't unnecessarily rewritten)",
        "already correct - do not touch",
        fees["Vinted"]["note"],
    )


def test_migrate_platform_notes_refreshes_old_vinted_wording(isolated_db_dir):
    def seed(conn):
        old_fees = {
            "Vinted": {"percent": 0, "fixed": 0, "note": "Seller keeps 100%. Buyer pays ~5% + £0.70 protection fee separately."},
            "eBay": {"percent": 12.8, "fixed": 0.30, "note": "unaffected"},
        }
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('platform_fees', ?)",
            (json.dumps(old_fees),),
        )

    _make_old_db(isolated_db_dir, "old_vinted_note", "", seed)
    migrations.migrate_platform_notes()

    conn = sqlite3.connect(str(isolated_db_dir / "old_vinted_note.db"))
    row = conn.execute("SELECT value FROM settings WHERE key='platform_fees'").fetchone()
    conn.close()
    fees = json.loads(row[0])
    expect("Vinted note refreshed to current wording", default_settings()["platform_fees"]["Vinted"]["note"], fees["Vinted"]["note"])
    expect("eBay note left untouched", "unaffected", fees["eBay"]["note"])


def test_migrate_platform_notes_leaves_customized_note_alone(isolated_db_dir):
    def seed(conn):
        custom_fees = {"Vinted": {"percent": 0, "fixed": 0, "note": "my own custom note - do not touch"}}
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('platform_fees', ?)",
            (json.dumps(custom_fees),),
        )

    _make_old_db(isolated_db_dir, "custom_vinted_note", "", seed)
    migrations.migrate_platform_notes()

    conn = sqlite3.connect(str(isolated_db_dir / "custom_vinted_note.db"))
    row = conn.execute("SELECT value FROM settings WHERE key='platform_fees'").fetchone()
    conn.close()
    fees = json.loads(row[0])
    expect("customized note left untouched", "my own custom note - do not touch", fees["Vinted"]["note"])


def test_migrate_schema_adds_missing_columns_and_templates_table(isolated_db_dir):
    # A realistic "old" items table: everything migrate_returns() and normal
    # operation need (primary_status/sub_status), just missing the two
    # columns and the table this migration is responsible for adding.
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        primary_status TEXT NOT NULL DEFAULT 'Not listed',
        sub_status TEXT NOT NULL DEFAULT 'Draft'
    );
    """
    _make_old_db(isolated_db_dir, "old_schema", old_items_schema, lambda conn: None)
    migrations.migrate_schema()

    conn = sqlite3.connect(str(isolated_db_dir / "old_schema.db"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    conn.close()
    expect("pending_payment_date column added", True, "pending_payment_date" in cols)
    expect("refund_amount column added", True, "refund_amount" in cols)
    expect("listing_title column added", True, "listing_title" in cols)
    expect("listing_description column added", True, "listing_description" in cols)
    expect("archived column added", True, "archived" in cols)
    expect("templates table created", True, "templates" in tables)


def test_migrate_schema_renames_delivered_date_to_pending_payment_date(isolated_db_dir):
    # A database that already has the old "delivered_date" column (from
    # before dispute-window tracking moved to the "Pending payment"
    # sub-status) with a real stamped value in it - the migration should
    # rename the column in place, not drop and recreate it, so that
    # existing timestamps survive.
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        primary_status TEXT NOT NULL DEFAULT 'List',
        sub_status TEXT NOT NULL DEFAULT 'Draft',
        delivered_date TEXT NOT NULL DEFAULT ''
    );
    """
    _make_old_db(
        isolated_db_dir,
        "old_delivered",
        old_items_schema,
        lambda conn: conn.execute(
            "INSERT INTO items (name, delivered_date) VALUES ('Item', '2026-01-01 12:00:00')"
        ),
    )
    migrations.migrate_schema()

    conn = sqlite3.connect(str(isolated_db_dir / "old_delivered.db"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    value = conn.execute("SELECT pending_payment_date FROM items WHERE name='Item'").fetchone()[0]
    conn.close()
    expect("delivered_date column renamed away", False, "delivered_date" in cols)
    expect("pending_payment_date column present", True, "pending_payment_date" in cols)
    expect("existing timestamp preserved through the rename", "2026-01-01 12:00:00", value)


def test_migrate_returns_moves_buyer_returned_to_return_status(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        primary_status TEXT NOT NULL,
        sub_status TEXT NOT NULL
    );
    """

    def seed(conn):
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Old Returned Item", "Refunded", "Buyer returned"),
        )
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Unrelated Item", "Refunded", "Item damaged"),
        )

    _make_old_db(isolated_db_dir, "old_returns", old_items_schema, seed)
    migrations.migrate_returns()

    conn = sqlite3.connect(str(isolated_db_dir / "old_returns.db"))
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM items").fetchall()}
    conn.close()

    expect("migrated item's primary_status", "Return", rows["Old Returned Item"]["primary_status"])
    expect("migrated item's sub_status", "Returned", rows["Old Returned Item"]["sub_status"])
    expect("unrelated item's primary_status untouched", "Refunded", rows["Unrelated Item"]["primary_status"])
    expect("unrelated item's sub_status untouched", "Item damaged", rows["Unrelated Item"]["sub_status"])


def test_migrate_merge_list_status_combines_not_listed_and_listed(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        primary_status TEXT NOT NULL,
        sub_status TEXT NOT NULL
    );
    """

    def seed(conn):
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Draft Item", "Not listed", "Draft"),
        )
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Reduced Item", "Listed", "Price reduced"),
        )
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Unrelated Item", "Sold", "Paid"),
        )

    _make_old_db(isolated_db_dir, "old_list_status", old_items_schema, seed)
    migrations.migrate_merge_list_status()

    conn = sqlite3.connect(str(isolated_db_dir / "old_list_status.db"))
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM items").fetchall()}
    conn.close()

    expect("Not listed merged into List", "List", rows["Draft Item"]["primary_status"])
    expect("Listed merged into List", "List", rows["Reduced Item"]["primary_status"])
    expect("Price reduced renamed to Reduced", "Reduced", rows["Reduced Item"]["sub_status"])
    expect("unrelated item's primary_status untouched", "Sold", rows["Unrelated Item"]["primary_status"])
    expect("unrelated item's sub_status untouched", "Paid", rows["Unrelated Item"]["sub_status"])


def test_migrate_rename_refund_status(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        primary_status TEXT NOT NULL,
        sub_status TEXT NOT NULL
    );
    """

    def seed(conn):
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Damaged Item", "Refunded", "Item damaged"),
        )
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Unrelated Item", "Sold", "Paid"),
        )

    _make_old_db(isolated_db_dir, "old_refund_status", old_items_schema, seed)
    migrations.migrate_rename_refund_status()

    conn = sqlite3.connect(str(isolated_db_dir / "old_refund_status.db"))
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM items").fetchall()}
    conn.close()

    expect("Refunded renamed to Refund", "Refund", rows["Damaged Item"]["primary_status"])
    expect("unrelated item's primary_status untouched", "Sold", rows["Unrelated Item"]["primary_status"])


def test_migrate_rename_ready_to_ship(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        primary_status TEXT NOT NULL,
        sub_status TEXT NOT NULL
    );
    """

    def seed(conn):
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Awaiting Shipment Item", "Sold", "Ready to ship"),
        )
        conn.execute(
            "INSERT INTO items (name, primary_status, sub_status) VALUES (?,?,?)",
            ("Unrelated Item", "Sold", "Shipped"),
        )

    _make_old_db(isolated_db_dir, "old_ready_to_ship", old_items_schema, seed)
    migrations.migrate_rename_ready_to_ship()

    conn = sqlite3.connect(str(isolated_db_dir / "old_ready_to_ship.db"))
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM items").fetchall()}
    conn.close()

    expect("Ready to ship renamed to Needs Shipping", "Needs Shipping", rows["Awaiting Shipment Item"]["sub_status"])
    expect("unrelated item's sub_status untouched", "Shipped", rows["Unrelated Item"]["sub_status"])


def test_migrate_add_indexes_creates_expected_indexes(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        primary_status TEXT NOT NULL,
        sub_status TEXT NOT NULL,
        sold_on TEXT NOT NULL DEFAULT '',
        paid_date TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE price_history (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL);
    CREATE TABLE activity_log (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL);
    """
    _make_old_db(isolated_db_dir, "old_no_indexes", old_items_schema, lambda conn: None)
    migrations.migrate_add_indexes()

    conn = sqlite3.connect(str(isolated_db_dir / "old_no_indexes.db"))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    for expected in (
        "idx_price_history_item_id", "idx_activity_log_item_id",
        "idx_items_primary_status", "idx_items_sub_status",
        "idx_items_sold_on", "idx_items_paid_date",
    ):
        expect(f"{expected} created", True, expected in names)


def test_migrate_listed_on_platform_details_converts_old_shape_and_folds_url(isolated_db_dir):
    # A realistic "old" items table: listed_on is still a plain list of
    # platform name strings, and there's a single item-level `url` column
    # (removed by this migration once its value has been folded into each
    # listed_on entry).
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        listed_on TEXT NOT NULL DEFAULT '[]',
        url TEXT NOT NULL DEFAULT ''
    );
    """

    def seed(conn):
        conn.execute(
            "INSERT INTO items (name, listed_on, url) VALUES (?,?,?)",
            ("Dual-listed Item", json.dumps(["Vinted", "eBay"]), "https://vinted.example/1"),
        )
        conn.execute(
            "INSERT INTO items (name, listed_on, url) VALUES (?,?,?)",
            ("Not yet listed Item", json.dumps([]), ""),
        )

    _make_old_db(isolated_db_dir, "old_listed_on", old_items_schema, seed)
    migrations.migrate_listed_on_platform_details()

    conn = sqlite3.connect(str(isolated_db_dir / "old_listed_on.db"))
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM items").fetchall()}
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    conn.close()

    expect("url column dropped", False, "url" in cols)
    dual_listed_on = json.loads(rows["Dual-listed Item"]["listed_on"])
    expect(
        "each platform gets its own entry carrying the old url",
        [
            {"platform": "Vinted", "url": "https://vinted.example/1", "date": ""},
            {"platform": "eBay", "url": "https://vinted.example/1", "date": ""},
        ],
        dual_listed_on,
    )
    expect("empty listed_on left as an empty list", [], json.loads(rows["Not yet listed Item"]["listed_on"]))


def test_migrate_reduced_at_adds_missing_column(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sub_status TEXT NOT NULL DEFAULT 'Draft'
    );
    """
    _make_old_db(isolated_db_dir, "old_reduced_at", old_items_schema, lambda conn: None)
    migrations.migrate_reduced_at()

    conn = sqlite3.connect(str(isolated_db_dir / "old_reduced_at.db"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    conn.close()
    expect("reduced_at column added", True, "reduced_at" in cols)


def test_migrate_expenses_table_creates_table_and_index(isolated_db_dir):
    _make_old_db(isolated_db_dir, "old_no_expenses", "", lambda conn: None)
    migrations.migrate_expenses_table()

    conn = sqlite3.connect(str(isolated_db_dir / "old_no_expenses.db"))
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    indexes = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
    conn.close()
    expect("expenses table created", True, "expenses" in tables)
    expect("expenses expense_date index created", True, "idx_expenses_expense_date" in indexes)


def test_migrate_deleted_at_adds_missing_column(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );
    """
    _make_old_db(isolated_db_dir, "old_deleted_at", old_items_schema, lambda conn: None)
    migrations.migrate_deleted_at()

    conn = sqlite3.connect(str(isolated_db_dir / "old_deleted_at.db"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    conn.close()
    expect("deleted_at column added", True, "deleted_at" in cols)


def test_migrate_sale_history_table_creates_table_and_index(isolated_db_dir):
    _make_old_db(isolated_db_dir, "old_no_sale_history", "", lambda conn: None)
    migrations.migrate_sale_history_table()

    conn = sqlite3.connect(str(isolated_db_dir / "old_no_sale_history.db"))
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    indexes = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
    conn.close()
    expect("sale_history table created", True, "sale_history" in tables)
    expect("sale_history item_id index created", True, "idx_sale_history_item_id" in indexes)


def test_migrate_return_shipping_cost_adds_columns_to_items_and_sale_history(isolated_db_dir):
    old_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );
    CREATE TABLE sale_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL
    );
    """
    _make_old_db(isolated_db_dir, "old_return_shipping_cost", old_schema, lambda conn: None)
    migrations.migrate_return_shipping_cost()

    conn = sqlite3.connect(str(isolated_db_dir / "old_return_shipping_cost.db"))
    items_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    sale_history_cols = [r[1] for r in conn.execute("PRAGMA table_info(sale_history)").fetchall()]
    conn.close()
    expect("items.return_shipping_cost added", True, "return_shipping_cost" in items_cols)
    expect("sale_history.return_shipping_cost added", True, "return_shipping_cost" in sale_history_cols)


def test_migrate_version_fields_adds_version_columns(isolated_db_dir):
    old_items_schema = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """

    def seed(conn):
        conn.execute("INSERT INTO items (name) VALUES ('Existing Item')")

    _make_old_db(isolated_db_dir, "old_version_fields", old_items_schema, seed)
    migrations._migrate_version_fields_one(sqlite3.connect(str(isolated_db_dir / "old_version_fields.db")))

    conn = sqlite3.connect(str(isolated_db_dir / "old_version_fields.db"))
    item_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    expense_cols = [r[1] for r in conn.execute("PRAGMA table_info(expenses)").fetchall()]
    template_cols = [r[1] for r in conn.execute("PRAGMA table_info(templates)").fetchall()]
    expect("version column added to items", True, "version" in item_cols)
    expect("version column added to expenses", True, "version" in expense_cols)
    expect("version column added to templates", True, "version" in template_cols)

    row = conn.execute("SELECT version FROM items WHERE name = 'Existing Item'").fetchone()
    expect("existing rows backfilled to version 1", 1, row[0])

    # idempotent - running twice must not error or reset an already-bumped version
    conn.execute("UPDATE items SET version = 3 WHERE name = 'Existing Item'")
    conn.commit()
    migrations._migrate_version_fields_one(conn)
    conn.commit()
    row = conn.execute("SELECT version FROM items WHERE name = 'Existing Item'").fetchone()
    expect("re-running migration doesn't clobber existing version", 3, row[0])

    conn.close()


def test_run_migrations_stamps_schema_version_and_skips_on_rerun(isolated_db_dir):
    # A fresh, already-current-shape database (as db.init_database() would
    # create) still needs run_migrations() to stamp its schema_version - a
    # database with no version row is treated as version 0, so the first
    # run must bring it fully up to date, and a second run must be a no-op.
    def seed(conn):
        for k, v in default_settings().items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))

    _make_old_db(isolated_db_dir, "fresh", SCHEMA, seed)

    migrations.run_migrations()
    conn = sqlite3.connect(str(isolated_db_dir / "fresh.db"))
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    expect("schema_version reaches CURRENT_SCHEMA_VERSION after first run",
           migrations.CURRENT_SCHEMA_VERSION, version)

    # bump it "ahead" so a second run touching it would be visible, then
    # confirm run_migrations() doesn't reset or otherwise touch it
    conn = sqlite3.connect(str(isolated_db_dir / "fresh.db"))
    conn.execute("UPDATE schema_version SET version=?", (migrations.CURRENT_SCHEMA_VERSION,))
    conn.commit()
    conn.close()

    migrations.run_migrations()
    conn = sqlite3.connect(str(isolated_db_dir / "fresh.db"))
    version_after = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    expect("schema_version unchanged by a no-op second run",
           migrations.CURRENT_SCHEMA_VERSION, version_after)
