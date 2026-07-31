"""One-off data/schema migrations, run once at startup against every database file.

Each `migrate_*()` function is idempotent (safe to run repeatedly) and
self-contained: it opens its own raw sqlite3 connection per database file,
checks whether the fix/column/table is already in place, and only writes if
it isn't. That's still true and is what the test suite exercises directly
(each test calls one `migrate_*()` function against its own isolated
directory).

What run_migrations() adds on top is a per-database `schema_version` table,
so a database that's already at the latest version is skipped entirely on
startup instead of every migration re-opening it and re-running its
"is this already applied" checks forever. Each `migrate_*()` function has a
matching `_migrate_*_one(conn)` helper containing the actual per-connection
logic; the public function is just that helper looped over every database
file (kept around because the tests, and the public API, call it that way),
while run_migrations() calls the helpers directly against one already-open,
version-gated connection.

New migrations: add the per-connection logic as `_migrate_x_one(conn)`, a
`migrate_x()` wrapper that loops it over every user's databases like the
others, and append `_migrate_x_one` to MIGRATIONS below (order matters -
some migrations depend on an earlier one having already run).
"""

import json
import sqlite3
from collections.abc import Callable

from config import DB_DIR
from db import default_settings


def _run_over_all_dbs(one_fn: Callable[[sqlite3.Connection], None]) -> None:
    """Apply one `_migrate_x_one(conn)` helper to every database file
    belonging to every user, each against its own connection/transaction.
    Shared by every public `migrate_x()` wrapper below - they only differ
    in which helper they run. Databases live one directory level under
    DB_DIR now (databases/<user_id>/name.db - see db.py's user_db_dir),
    hence the "*/*.db" glob instead of "*.db". Any pre-existing flat
    databases/*.db file from before this subsystem existed is NOT matched by
    this glob and is permanently unreachable through this sweep - see
    config.py's DB_DIR comment for the operator-facing note.
    """
    for db_path in DB_DIR.glob("*/*.db"):
        conn = sqlite3.connect(str(db_path))
        try:
            one_fn(conn)
            conn.commit()
        finally:
            conn.close()


def _migrate_vinted_fees_one(conn: sqlite3.Connection) -> None:
    """Fix a database seeded before Vinted's fee was corrected to 0% + £0.00.

    Early versions of default_settings() incorrectly charged the seller a
    5% + £0.70 Vinted fee (that charge is actually paid by the buyer, not
    the seller). If this database's stored Vinted fee still matches those
    old numbers, overwrite it with the current, correct default.
    """
    correct_vinted = default_settings()["platform_fees"]["Vinted"]
    row = conn.execute("SELECT value FROM settings WHERE key='platform_fees'").fetchone()
    if not row:
        return
    fees = json.loads(row[0])
    vinted = fees.get("Vinted")
    if vinted and (vinted.get("percent") == 5 or vinted.get("fixed") == 0.7):
        fees["Vinted"] = correct_vinted
        conn.execute(
            "UPDATE settings SET value=? WHERE key='platform_fees'",
            (json.dumps(fees),),
        )


def migrate_vinted_fees() -> None:
    _run_over_all_dbs(_migrate_vinted_fees_one)


def _migrate_platform_notes_one(conn: sqlite3.Connection) -> None:
    """Refresh a platform_fees note if it still matches old, now-superseded
    wording.

    Vinted's note used to describe its buyer-side Buyer Protection Fee
    (~5% + £0.70) itself; the fee calculator now computes that fee directly
    (and eBay's equivalent tiered one alongside it), so the note only needs
    to cover the seller-fee side. Only overwrites a note that still matches
    that exact old wording, so a note a user has since customized is left
    alone - same guard as _migrate_vinted_fees_one uses for the fee amount.
    """
    old_notes = {
        "Vinted": "Seller keeps 100%. Buyer pays ~5% + £0.70 protection fee separately.",
    }
    row = conn.execute("SELECT value FROM settings WHERE key='platform_fees'").fetchone()
    if not row:
        return
    fees = json.loads(row[0])
    current = default_settings()["platform_fees"]
    changed = False
    for plat, old_note in old_notes.items():
        if plat in fees and fees[plat].get("note") == old_note:
            fees[plat]["note"] = current[plat]["note"]
            changed = True
    if changed:
        conn.execute("UPDATE settings SET value=? WHERE key='platform_fees'", (json.dumps(fees),))


def migrate_platform_notes() -> None:
    _run_over_all_dbs(_migrate_platform_notes_one)


def _migrate_schema_one(conn: sqlite3.Connection) -> None:
    """Add columns/tables introduced after a database was first created.

    SCHEMA in db.py only applies to brand-new databases (via
    executescript's CREATE TABLE IF NOT EXISTS). Existing databases created
    before a column/table existed need it added explicitly here with
    ALTER TABLE / CREATE TABLE IF NOT EXISTS, one check per addition.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "pending_payment_date" not in cols:
        if "delivered_date" in cols:
            # dispute-window tracking moved from the (now-removed)
            # "Delivered" sub-status to "Pending payment" - rename
            # rather than add, so existing timestamps aren't lost.
            conn.execute("ALTER TABLE items RENAME COLUMN delivered_date TO pending_payment_date")
        else:
            conn.execute("ALTER TABLE items ADD COLUMN pending_payment_date TEXT NOT NULL DEFAULT ''")
    if "refund_amount" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN refund_amount REAL NOT NULL DEFAULT 0")
    if "listing_title" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN listing_title TEXT NOT NULL DEFAULT ''")
    if "listing_description" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN listing_description TEXT NOT NULL DEFAULT ''")
    if "archived" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "relisted_count" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN relisted_count INTEGER NOT NULL DEFAULT 0")
    if "return_tracking_ref" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN return_tracking_ref TEXT NOT NULL DEFAULT ''")
    if "return_shipping_method" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN return_shipping_method TEXT NOT NULL DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")


def migrate_schema() -> None:
    _run_over_all_dbs(_migrate_schema_one)


def _migrate_returns_one(conn: sqlite3.Connection) -> None:
    """Move the old 'Refunded / Buyer returned' combo to its own 'Return' status.

    'Buyer returned' used to be a Refunded sub-status; it's now its own
    Return primary status with its own label-sent/in-transit/returned
    pipeline. A completed return implies the item is already back, so
    existing rows map onto Return / Returned (the equivalent terminal
    state), not one of the new in-progress sub-statuses.
    """
    conn.execute(
        "UPDATE items SET primary_status='Return', sub_status='Returned' "
        "WHERE primary_status='Refunded' AND sub_status='Buyer returned'"
    )


def migrate_returns() -> None:
    _run_over_all_dbs(_migrate_returns_one)


def _migrate_merge_list_status_one(conn: sqlite3.Connection) -> None:
    """Merge 'Not listed' and 'Listed' into one 'List' status, and rename
    the 'Price reduced' sub-status to 'Reduced'.

    The Inventory tab's collapsible sections are grouped by primary_status,
    and having a listing's "not yet published" and "published" phases split
    across two separate statuses didn't earn its own section - they're one
    continuous chain now (Draft -> Ready to list -> Active -> Reduced ->
    Relisted), so the two old primary statuses collapse into one.
    """
    conn.execute(
        "UPDATE items SET primary_status='List' WHERE primary_status IN ('Not listed', 'Listed')"
    )
    conn.execute(
        "UPDATE items SET sub_status='Reduced' WHERE sub_status='Price reduced'"
    )


def migrate_merge_list_status() -> None:
    _run_over_all_dbs(_migrate_merge_list_status_one)


def _migrate_rename_refund_status_one(conn: sqlite3.Connection) -> None:
    """Rename the 'Refunded' primary status to 'Refund', for naming
    consistency with 'Return' (both are now short nouns, not one noun and
    one past-tense adjective). Must run after migrate_returns(), since that
    migration still looks for the old 'Refunded' name as its source status.
    """
    conn.execute("UPDATE items SET primary_status='Refund' WHERE primary_status='Refunded'")


def migrate_rename_refund_status() -> None:
    _run_over_all_dbs(_migrate_rename_refund_status_one)


def _migrate_rename_ready_to_ship_one(conn: sqlite3.Connection) -> None:
    """Rename the Sold sub-status 'Ready to ship' to 'Needs Shipping' -
    clearer that shipping hasn't started yet, rather than reading as an item
    already packed and waiting to go out."""
    conn.execute(
        "UPDATE items SET sub_status='Needs Shipping' "
        "WHERE primary_status='Sold' AND sub_status='Ready to ship'"
    )


def migrate_rename_ready_to_ship() -> None:
    _run_over_all_dbs(_migrate_rename_ready_to_ship_one)


def _migrate_add_indexes_one(conn: sqlite3.Connection) -> None:
    """Add indexes on the foreign keys and columns Inventory/Dashboard
    filter/sort by most - price_history.item_id and activity_log.item_id
    (looked up per-item constantly, e.g. get_platform_comparison's
    avg_days_to_sell query), and items.primary_status/sub_status/sold_on/
    paid_date (every Inventory/Dashboard/Tax query filters on these)."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_history_item_id ON price_history(item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_item_id ON activity_log(item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_primary_status ON items(primary_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_sub_status ON items(sub_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_sold_on ON items(sold_on)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_paid_date ON items(paid_date)")


def migrate_add_indexes() -> None:
    _run_over_all_dbs(_migrate_add_indexes_one)


def _migrate_listed_on_platform_details_one(conn: sqlite3.Connection) -> None:
    """Give each platform in listed_on its own URL and listed date, folding
    the old item-level `url` column into them.

    listed_on used to be a plain list of platform names (["Vinted", "eBay"])
    and there was a single item-level `url` column for "the" listing link -
    fine for one platform, but with no way to tell which platform a link
    belonged to once an item was dual-listed, and no per-platform listed
    date at all. listed_on entries are now {platform, url, date} objects;
    the old global url (if any) is copied onto every platform already in
    listed_on as a starting point, since there's no way to know which one
    it originally belonged to - the user can split/correct them from the
    editor afterwards.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    has_url_col = "url" in cols
    rows = conn.execute(f"SELECT id, listed_on{', url' if has_url_col else ''} FROM items").fetchall()
    for row in rows:
        item_id, raw_listed_on = row[0], row[1]
        old_url = row[2] if has_url_col else ""
        try:
            listed_on = json.loads(raw_listed_on) if raw_listed_on else []
        except (json.JSONDecodeError, TypeError):
            listed_on = []
        if listed_on and all(isinstance(p, str) for p in listed_on):
            listed_on = [{"platform": p, "url": old_url or "", "date": ""} for p in listed_on]
            conn.execute("UPDATE items SET listed_on=? WHERE id=?", (json.dumps(listed_on), item_id))
    if has_url_col:
        conn.execute("ALTER TABLE items DROP COLUMN url")


def migrate_listed_on_platform_details() -> None:
    _run_over_all_dbs(_migrate_listed_on_platform_details_one)


def _migrate_reduced_at_one(conn: sqlite3.Connection) -> None:
    """Add the reduced_at column for databases created before the Dashboard's
    staleness clock started resetting on a price reduction (see
    listing_anchor_date, blueprints/dashboard.py)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "reduced_at" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN reduced_at TEXT NOT NULL DEFAULT ''")


def migrate_reduced_at() -> None:
    _run_over_all_dbs(_migrate_reduced_at_one)


def _migrate_expenses_table_one(conn: sqlite3.Connection) -> None:
    """Add the expenses table for databases created before the Tax summary
    tracked non-item business costs (see blueprints/expenses.py)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        amount REAL NOT NULL DEFAULT 0,
        expense_date TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expenses_expense_date ON expenses(expense_date)")


def migrate_expenses_table() -> None:
    _run_over_all_dbs(_migrate_expenses_table_one)


def _migrate_deleted_at_one(conn: sqlite3.Connection) -> None:
    """Add the deleted_at column for databases created before item deletion
    became a soft-delete (see delete_item/undelete_item, blueprints/items.py)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")


def migrate_deleted_at() -> None:
    _run_over_all_dbs(_migrate_deleted_at_one)


def _migrate_sale_history_table_one(conn: sqlite3.Connection) -> None:
    """Add the sale_history table for databases created before relisting a
    returned/refunded item archived its previous sale cycle (see
    update_item, blueprints/items.py)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS sale_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        sold_on TEXT NOT NULL DEFAULT '',
        buyer_name TEXT NOT NULL DEFAULT '',
        sale_price REAL NOT NULL DEFAULT 0,
        fees REAL NOT NULL DEFAULT 0,
        shipping_cost REAL NOT NULL DEFAULT 0,
        shipping_method TEXT NOT NULL DEFAULT '',
        tracking_ref TEXT NOT NULL DEFAULT '',
        paid_date TEXT NOT NULL DEFAULT '',
        refund_amount REAL NOT NULL DEFAULT 0,
        reimbursed INTEGER NOT NULL DEFAULT 0,
        reimbursed_amount REAL NOT NULL DEFAULT 0,
        ended_status TEXT NOT NULL DEFAULT '',
        ended_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_history_item_id ON sale_history(item_id)")


def migrate_sale_history_table() -> None:
    _run_over_all_dbs(_migrate_sale_history_table_one)


def _migrate_return_shipping_cost_one(conn: sqlite3.Connection) -> None:
    """Add the return_shipping_cost column to items and sale_history, for
    databases created before the seller's return label cost was tracked per
    item (see blueprints/items.py's REQUIRED_FIELDS and blueprints/tax.py's
    return_shipping_total)."""
    items_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "return_shipping_cost" not in items_cols:
        conn.execute("ALTER TABLE items ADD COLUMN return_shipping_cost REAL NOT NULL DEFAULT 0")
    sale_history_cols = [r[1] for r in conn.execute("PRAGMA table_info(sale_history)").fetchall()]
    if "return_shipping_cost" not in sale_history_cols:
        conn.execute("ALTER TABLE sale_history ADD COLUMN return_shipping_cost REAL NOT NULL DEFAULT 0")


def migrate_return_shipping_cost() -> None:
    _run_over_all_dbs(_migrate_return_shipping_cost_one)


def _migrate_version_fields_one(conn: sqlite3.Connection) -> None:
    """Add the version column to items/expenses/templates, for databases
    created before optimistic-concurrency conflict detection existed (see
    blueprints/items.py, blueprints/expenses.py, blueprints/templates_bp.py).
    Existing rows backfill to version 1 - the same starting point a
    brand-new row gets via the column's DEFAULT 1."""
    for table in ("items", "expenses", "templates"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "version" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN version INTEGER NOT NULL DEFAULT 1")


def migrate_version_fields() -> None:
    _run_over_all_dbs(_migrate_version_fields_one)


# Every migration's per-connection helper, in the order they must run (some
# depend on an earlier one having already applied - see each docstring).
# A database's schema_version is just an index into this list: version N
# means "everything up to MIGRATIONS[N-1] has been applied".
MIGRATIONS = [
    _migrate_vinted_fees_one,
    _migrate_schema_one,
    _migrate_returns_one,
    _migrate_merge_list_status_one,
    _migrate_rename_refund_status_one,
    _migrate_rename_ready_to_ship_one,
    _migrate_add_indexes_one,
    _migrate_platform_notes_one,
    _migrate_listed_on_platform_details_one,
    _migrate_reduced_at_one,
    _migrate_expenses_table_one,
    _migrate_deleted_at_one,
    _migrate_sale_history_table_one,
    _migrate_return_shipping_cost_one,
    _migrate_version_fields_one,
]

CURRENT_SCHEMA_VERSION = len(MIGRATIONS)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """A database with no schema_version table (i.e. older than this
    tracking existing at all) is treated as version 0 - every migration
    still needs to run against it, same as before this table existed."""
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def run_migrations() -> None:
    """Run every migration not yet applied, against every database file.

    Called once at app startup (see the __main__ block in app.py). Skips a
    database entirely once its schema_version reaches CURRENT_SCHEMA_VERSION,
    instead of every migration re-opening every database file and re-running
    its "already applied?" check on every single launch forever.
    """
    for db_path in DB_DIR.glob("*/*.db"):
        conn = sqlite3.connect(str(db_path))
        try:
            version = _get_schema_version(conn)
            if version >= CURRENT_SCHEMA_VERSION:
                continue
            for migrate_one in MIGRATIONS[version:]:
                migrate_one(conn)
            _set_schema_version(conn, CURRENT_SCHEMA_VERSION)
            conn.commit()
        finally:
            conn.close()
