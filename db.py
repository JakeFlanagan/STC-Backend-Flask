"""Database connection lifecycle, schema, and default settings.

Each "database" the user creates from the top bar is its own standalone
SQLite file under databases/ (see config.py). Exactly one of them is
"active" at a time (tracked in .active_db); every request reads/writes
whichever one is currently active via get_db(). This module owns:

  - the SQL schema used to create a brand new database file (SCHEMA)
  - the default settings seeded into a brand new database (default_settings)
  - the per-request connection lifecycle (get_db / close_db / init_app)
  - which database is "active" (get_active_db_name / set_active_db)
  - row -> dict conversion for JSON responses (row_to_dict)
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, g

from config import DB_DIR

# Schema for a brand-new database file. Run once via executescript() inside
# init_database(). Existing databases that predate a column/table added here
# are patched up separately by migrations.py - this string only affects
# databases created from now on.
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    -- primary_status / sub_status together drive the whole status pipeline
    -- (List -> Sold -> Paid, or Return / Refund branches). Valid
    -- combinations are enforced in the frontend (SUB_STATUSES in
    -- static/js/state.js), not by the database.
    primary_status TEXT NOT NULL DEFAULT 'List',
    sub_status TEXT NOT NULL DEFAULT 'Draft',
    listed_on TEXT NOT NULL DEFAULT '[]',  -- JSON array of {platform, url, date} objects, e.g. [{"platform":"Vinted","url":"...","date":"2026-01-01"}] - one entry per platform an item is (dual-)listed on, each with its own listing link and the date it went up on that platform
    sold_on TEXT NOT NULL DEFAULT '',
    buyer_name TEXT NOT NULL DEFAULT '',
    cost_price REAL NOT NULL DEFAULT 0,
    sale_price REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    shipping_cost REAL NOT NULL DEFAULT 0,
    shipping_method TEXT NOT NULL DEFAULT '',
    packed INTEGER NOT NULL DEFAULT 0,      -- boolean (0/1): item is boxed and ready to ship
    has_photos INTEGER NOT NULL DEFAULT 0,  -- boolean (0/1): listing photos have been taken
    tracking_ref TEXT NOT NULL DEFAULT '',
    return_tracking_ref TEXT NOT NULL DEFAULT '',  -- separate tracking number for the item coming back on a Return, so it doesn't have to overwrite the original sale's tracking_ref
    return_shipping_method TEXT NOT NULL DEFAULT '',  -- courier/service the buyer is returning it with - the Return-side counterpart to shipping_method
    return_shipping_cost REAL NOT NULL DEFAULT 0,  -- cost of the return label when the seller pays for it - the Return-side counterpart to shipping_cost
    source TEXT NOT NULL DEFAULT '',        -- where the item was acquired from
    -- status_date is reused for two different meanings depending on sub_status:
    -- "Pending payment" -> the due date funds should clear by;
    -- Return "Label sent"/"In transit" -> the date the buyer must return it by.
    status_date TEXT NOT NULL DEFAULT '',
    paid_date TEXT NOT NULL DEFAULT '',       -- set when sub_status becomes "Paid"
    pending_payment_date TEXT NOT NULL DEFAULT '',  -- set when sub_status becomes "Pending payment" (informational - the dispute-window countdown is based on the due date, status_date, not this)
    reduced_at TEXT NOT NULL DEFAULT '',  -- set when sub_status first becomes "Reduced" (see update_item/advance_item) - gives the Dashboard's staleness clock (get_stats' listing_anchor_date) a fresh start once you've actually done something about a slow listing, instead of forever counting from created_at
    reimbursed INTEGER NOT NULL DEFAULT 0,        -- boolean (0/1): platform/courier paid out on a lost/damaged claim
    reimbursed_amount REAL NOT NULL DEFAULT 0,    -- how much the platform/courier paid you back
    refund_amount REAL NOT NULL DEFAULT 0,        -- how much you refunded the buyer (Partial refund sub-status)
    listing_title TEXT NOT NULL DEFAULT '',       -- the actual listing headline - a permanent record, not just a Draft scratchpad
    listing_description TEXT NOT NULL DEFAULT '', -- the actual listing description, same purpose
    archived INTEGER NOT NULL DEFAULT 0,          -- boolean (0/1): hidden from Inventory unless "Show Archived" is on
    notes TEXT NOT NULL DEFAULT '',
    -- auto-incremented every time primary_status moves from 'Sold' back to
    -- 'List' (see update_item in blueprints/items.py) - a genuine relist
    -- after a sale fell through. This is the source of truth for the
    -- editor's "Relisted: N" counter, not something derived from
    -- activity_log (which only keeps the latest 50 rows per item).
    -- Manually resettable to 0 from the editor if a UI mistake inflates it.
    relisted_count INTEGER NOT NULL DEFAULT 0,
    -- soft-delete: set instead of actually removing the row, so DELETE
    -- /api/items/<id> (blueprints/items.py) can be undone within the
    -- frontend's brief "Undo" toast window without losing price_history/
    -- activity_log (a real DELETE cascades those away). Every query that
    -- lists/looks up items filters this back out - see undelete_item for
    -- the one exception that intentionally ignores it.
    deleted_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    version INTEGER NOT NULL DEFAULT 1
);

-- Every time an item's sale_price changes, a row is appended here so the
-- editor modal can show a price history trail (see loadPriceHistory in
-- static/js/editor.js).
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    price REAL NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

-- Free-text timeline of what happened to an item (status changes, manual
-- notes). Surfaced as the collapsible "Activity log" in the editor modal.
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

-- A snapshot of one completed sale cycle for an item, taken the moment it
-- moves from Sold/Return/Refund back to List (see update_item,
-- blueprints/items.py) - i.e. right before those fields on the `items` row
-- itself get cleared for the next cycle (a refund/return being relisted, or
-- a sale falling through). Without this, relisting the same physical item
-- and selling it again would just silently overwrite the previous buyer/
-- price/tracking info with no trace it ever happened. Surfaced as the
-- collapsible "Past sales" section in the editor modal.
CREATE TABLE IF NOT EXISTS sale_history (
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
    return_shipping_cost REAL NOT NULL DEFAULT 0,
    ended_status TEXT NOT NULL DEFAULT '',  -- "<primary_status> / <sub_status>" the item was in right before this cycle was archived, e.g. "Return / Returned"
    ended_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

-- Simple key/value store, one row per settings key (platform_fees,
-- shipping_rates, deadstock_days). Each value is a JSON-encoded string;
-- see default_settings() for the shape of each key.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Reusable listing description snippets, grouped by category (e.g. "Retro
-- Gaming"), copy-pasted into the item editor via the "Copy text" button.
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    version INTEGER NOT NULL DEFAULT 1
);

-- Business costs that aren't tied to any one item - packaging materials,
-- postage supplies, mileage, storage, subscriptions, etc. - so the Tax
-- summary's net profit can reflect them alongside per-item cost_price/
-- fees/shipping_cost (see get_tax, blueprints/tax.py). expense_date (not
-- created_at) is what the Tax tab's date-range filter and totals key off,
-- same as items key off paid_date rather than created_at.
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    expense_date TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    version INTEGER NOT NULL DEFAULT 1
);

-- price_history/activity_log are looked up by item_id constantly (every
-- item editor open, every platform-comparison query); items is filtered by
-- these status/sold_on/paid_date columns on nearly every Inventory/
-- Dashboard/Tax request. Harmless without indexes at single-user data
-- volumes, but cheap to have from the start for anyone else's database that
-- grows larger. Existing databases get these via migrations.py's
-- _migrate_add_indexes_one instead, since SCHEMA only runs on brand-new ones.
CREATE INDEX IF NOT EXISTS idx_price_history_item_id ON price_history(item_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_item_id ON activity_log(item_id);
CREATE INDEX IF NOT EXISTS idx_sale_history_item_id ON sale_history(item_id);
CREATE INDEX IF NOT EXISTS idx_items_primary_status ON items(primary_status);
CREATE INDEX IF NOT EXISTS idx_items_sub_status ON items(sub_status);
CREATE INDEX IF NOT EXISTS idx_items_sold_on ON items(sold_on);
CREATE INDEX IF NOT EXISTS idx_items_paid_date ON items(paid_date);
CREATE INDEX IF NOT EXISTS idx_expenses_expense_date ON expenses(expense_date);
"""


# The header a client uses to name which of its own databases a request
# should run against, in place of relying on server-side "active database"
# state. See resolve_request_database below.
DATABASE_HEADER = "X-STC-Database"


def is_valid_db_name(name: str) -> bool:
    """Whether `name` is acceptable as one of a user's database names.

    This is a security boundary, not a tidiness rule. Database names become
    filenames directly (user_db_dir(user_id) / f"{name}.db"), so rejecting
    anything but alphanumerics and underscores is what stops a name like
    "../../etc/passwd" or "..%2Fother_user/secrets" escaping the user's own
    directory. Keep it a deny-everything-else allowlist rather than
    blacklisting the sequences that look dangerous today.

    Was previously inline in four endpoints (create, switch, delete,
    import). One copy, because a fifth caller now exists and the failure
    mode of a copy drifting is silent.
    """
    return bool(name) and name.replace("_", "").isalnum()


def user_has_database(user_id: int, name: str) -> bool:
    """Whether this user owns a database called `name`.

    The caller must have validated the name first - this builds a path from
    it. Checking existence inside the user's OWN directory is what makes it
    safe to let a client choose the database: the request names a database,
    but it can only ever name one of its own, because nobody else's file is
    in that directory to be found.
    """
    return (user_db_dir(user_id) / f"{name}.db").exists()


def user_db_dir(user_id: int) -> Path:
    """The directory holding one user's databases and their backups
    (databases/<user_id>/), created on first use. Every user's data lives
    in their own subfolder under DB_DIR - the isolation boundary between
    accounts (see auth_middleware.check_auth, which sets g.user_id from a
    verified JWT before any route handler that needs a database runs)."""
    d = DB_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_settings() -> dict[str, Any]:
    """Return the settings dict seeded into every brand-new database.

    Also used by get_settings() (blueprints/settings_bp.py) to fill in any
    keys missing from an existing database, so adding a new settings key
    here doesn't require a migration - old databases just pick up the
    default the next time they're read.
    """
    return {
        # "business" or "personal" - which eBay account type this database's
        # sales run through. Settings' Platform fees panel uses this to lock
        # eBay's fee fields to the right preset (see ACCOUNT_TYPE_EBAY_FEE in
        # static/js/settings.js) rather than leaving them to drift out of
        # sync by hand: personal/private sellers pay eBay nothing (eBay
        # charges their buyer a Buyer Protection Fee instead), while business
        # sellers pay a Final Value Fee. Vinted never charges the seller
        # either way, so it isn't affected by this toggle.
        "account_type": "personal",
        "platform_fees": {
            # percent/fixed are the fee the SELLER pays; "note" is shown
            # read-only next to the fee inputs in Settings, and doubles as
            # the explanatory blurb in the fee calculator. Both Vinted and
            # eBay also charge the BUYER a separate Buyer Protection Fee on
            # top of the listing price (Vinted: ~5% + £0.70; eBay: tiered -
            # 7%/4%/2% + £0.10) - that fee is baked into the price the buyer
            # sees, not billed separately at checkout, and it never touches
            # the seller's payout, so it's computed by the fee calculator
            # itself (calculator.js's buyerProtectionFee()) rather than
            # being a "note" here.
            "Vinted": {"percent": 0, "fixed": 0, "note": "Vinted charges no seller fee - you keep 100% of the sale price."},
            "eBay": {"percent": 12.8, "fixed": 0.40, "note": "Business seller Final Value Fee, deducted from seller payout (per-order fee is £0.40 for orders over £10, £0.30 under). Private sellers pay £0 - eBay charges the buyer instead."},
            "Facebook": {"percent": 0, "fixed": 0, "note": "No seller fees on most categories."},
        },
        # Carrier -> { rate label: price }. Purely reference data shown in
        # the fee calculator's shipping dropdown and the rates table below
        # it; editable per-database from Settings.
        "shipping_rates": {
            "Royal Mail": {
                "Large Letter 0-100g 2nd": 1.55,
                "Large Letter 0-100g 1st": 3.30,
                "Large Letter 101-250g 2nd": 1.90,
                "Large Letter 101-250g 1st": 3.60,
                "Small Parcel 0-2kg 2nd": 4.25,
                "Small Parcel 0-2kg 1st": 5.45,
                "Medium Parcel 0-2kg 2nd": 6.95,
                "Medium Parcel 0-2kg 1st": 8.05,
                "Medium Parcel 2-5kg 2nd": 8.75,
                "Medium Parcel 2-5kg 1st": 10.05,
                "Tracked 48 Small Parcel": 6.05,
                "Tracked 24 Small Parcel": 7.30,
                "Tracked 48 Medium 2-10kg": 8.85,
                "Tracked 24 Medium 2-10kg": 9.85,
            },
            "Evri": {
                "Postable <1kg (ParcelShop)": 2.70,
                "0-1kg (ParcelShop)": 3.29,
                "1-2kg (ParcelShop)": 3.29,
                "2-5kg (ParcelShop)": 3.29,
                "5-10kg (ParcelShop)": 5.39,
                "10-15kg (ParcelShop)": 7.29,
                "Next Day 0-1kg": 3.99,
                "Next Day 1-2kg": 3.99,
                "Next Day 2-5kg": 5.09,
            },
            "InPost": {
                "Small (Locker to Locker)": 1.99,
                "Medium (Locker to Locker)": 2.59,
                "Large (Locker to Locker)": 3.99,
                "Small (to Home)": 2.89,
                "Medium (to Home)": 3.99,
                "Large (to Home)": 5.99,
            },
            "DHL": {
                "Small Parcel": 5.49,
                "Medium Parcel": 7.99,
                "Large Parcel": 12.99,
            },
        },
        # Symbol shown by every currency figure across the app (static/js/
        # format.js's fC()), editable from Settings. A single global symbol,
        # not a real per-item currency - this is a single-user local tool,
        # not a multi-currency ledger.
        "currency_symbol": "£",
        # Dashboard alert threshold, editable from Settings.
        "deadstock_days": 14,  # a published List item (Active/Reduced/Relisted) older than this shows up as "stale"
        "deadstock_enabled": True,  # lets the alert be switched off entirely instead of just tuned
        # Where-to-check-prices links shown from the £ icon on each Inventory
        # row (editor.js openPriceCheck), editable from Settings > Platform
        # Management. "{query}" is replaced with the item name.
        "price_check_links": [
            {"label": "Vinted", "url": "https://www.vinted.co.uk/catalog?search_text={query}"},
            {"label": "eBay sold listings", "url": "https://www.ebay.co.uk/sch/i.html?_nkw={query}&LH_Sold=1&LH_Complete=1"},
        ],
    }


def get_all_settings(db: sqlite3.Connection) -> dict[str, Any]:
    """Return every settings row as a dict, JSON-decoding each value.

    Falls back to the raw string for a value that isn't valid JSON (there
    shouldn't be any, since update_settings() always writes via
    json.dumps(), but an old hand-edited database file could have one).
    Shared by every route that needs the full settings dict (get_settings,
    dashboard's get_stats/get_platform_comparison) so the decode-loop and
    its fallback only live in one place.
    """
    settings = {}
    for r in db.execute("SELECT key, value FROM settings").fetchall():
        try:
            settings[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            settings[r["key"]] = r["value"]
    return settings


def get_settings_version(db: sqlite3.Connection) -> int:
    """The current version of the whole settings blob for the active
    database - a single counter covering every settings key, not one per
    key, since PUT /settings always writes a set of keys together as one
    logical change (see blueprints/settings_bp.py). Stored as a row in the
    same generic settings key/value table under the reserved key
    '_version', rather than a separate table, to avoid a schema migration
    just for one integer. Defaults to 1 for a database that's never had a
    settings write conflict-checked yet."""
    row = db.execute("SELECT value FROM settings WHERE key = '_version'").fetchone()
    if not row:
        return 1
    try:
        return int(json.loads(row["value"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 1


def set_settings_version(db: sqlite3.Connection, version: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('_version', ?)",
        (json.dumps(version),),
    )


def get_active_db_name(user_id: int) -> str | None:
    """Return the stem (no .db extension) of user_id's currently active
    database.

    Falls back to the first database alphabetically within this user's own
    directory if their .active_db is missing/stale, and persists that
    choice so future calls are consistent. Returns None only if this user
    has no database files at all yet.
    """
    user_dir = user_db_dir(user_id)
    state_file = user_dir / ".active_db"
    if state_file.exists():
        name = state_file.read_text().strip()
        if (user_dir / f"{name}.db").exists():
            return name
    dbs = sorted(user_dir.glob("*.db"))
    if dbs:
        name = dbs[0].stem
        set_active_db(user_id, name)
        return name
    return None


def set_active_db(user_id: int, name: str) -> None:
    """Make `name` (no .db extension) the active database for all of
    user_id's future requests."""
    (user_db_dir(user_id) / ".active_db").write_text(name)


def get_db() -> sqlite3.Connection:
    """Return a SQLite connection to the current request's user's active
    database, cached on Flask's `g` for the duration of the request.

    Reads g.user_id, set by auth_middleware.check_auth() before any route
    handler runs - every route that calls get_db() is reached only after
    the global auth gate in app.py, so g.user_id is always present here.
    If this user has no database at all yet, a fresh "clearout" database
    is created for them and made active.

    Which database, in priority order:

      1. g.db_name, set from the X-STC-Database header by app.py's
         _resolve_database hook (already validated and confirmed to be
         this user's own file).
      2. .active_db, this user's last explicit choice.

    The header wins because it is the only one of the two that can differ
    between two tabs. .active_db is per-USER state living in the user's
    directory, so every tab that user has open necessarily shares it, and
    switching database in one switches it under the others. Any
    server-held "current database" has that problem by definition; making
    the database a property of the request is the only thing that fixes
    it. .active_db stays as the fallback, which demotes it from source of
    truth to "the one this user had last".
    """
    if "db" not in g:
        user_id = g.user_id
        name = g.get("db_name") or get_active_db_name(user_id)
        if name is None:
            name = "clearout"
            init_database(user_id, name)
        path = user_db_dir(user_id) / f"{name}.db"
        g.db = sqlite3.connect(str(path))
        g.db.row_factory = sqlite3.Row  # lets us access columns by name, e.g. row["sale_price"]
        g.db.execute("PRAGMA journal_mode=WAL")   # allows concurrent readers while a write is in progress
        g.db.execute("PRAGMA foreign_keys=ON")    # enforces the ON DELETE CASCADE rules in SCHEMA
        # WAL keeps readers off the write lock, but two writers to the same
        # file still serialise, and without this the loser raises "database
        # is locked" immediately instead of waiting. One user with the app
        # open in two tabs, or a double-submitted form, is enough to hit it.
        # auth_db.py sets the same timeout on the shared auth database for
        # exactly this reason - the per-user databases, where nearly all the
        # writing happens, were the ones missing it.
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(exc: BaseException | None) -> None:
    """Close the request-scoped connection, if one was opened.

    Registered with Flask via init_app() so it runs automatically at the
    end of every request/app-context, regardless of whether the request
    succeeded or raised.
    """
    db = g.pop("db", None)
    if db:
        db.close()


def init_database(user_id: int, name: str) -> None:
    """Create a brand-new database file called `name`.db for user_id, seed
    it with the schema and default settings, and make it that user's
    active database.

    Used both for a user's very first database (see get_db()) and whenever
    they click "+ New DB" (see blueprints/databases.py's create_database).
    """
    path = user_db_dir(user_id) / f"{name}.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    # seed default settings
    defaults = default_settings()
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (k, json.dumps(v)),
        )
    conn.commit()
    conn.close()
    set_active_db(user_id, name)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row into a plain dict suitable for jsonify().

    Also JSON-decodes the `listed_on` column back into a real list, since
    it's stored in SQLite as a JSON string (SQLite has no array type).
    """
    d = dict(row)
    for k in ("listed_on",):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                d[k] = []
    return d


def init_app(app: Flask) -> None:
    """Wire this module's connection lifecycle into a Flask app.

    Called once from app.py at startup. Kept as a function (rather than
    db.py importing `app` directly) to avoid a circular import between
    app.py and db.py - the same pattern Flask-SQLAlchemy and other
    extensions use.
    """
    app.teardown_appcontext(close_db)
