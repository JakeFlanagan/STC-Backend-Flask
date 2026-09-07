"""Paths and constants shared across the app.

This module has no dependencies on the rest of the app (no Flask, no db
imports) so it can be safely imported from anywhere without risking a
circular import. It also runs its directory-creation side effects
(`mkdir`) once, at import time, so every other module can assume
`databases/` already exists. Import time also loads (or creates)
`settings.cfg` via `_load_or_create_settings()`, which can write a
freshly generated settings.cfg to disk and print a newly generated admin
password to stdout the first time it runs.
"""

import os
import re
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

# Root of the project - everything else (settings.cfg, auth.db, databases/)
# is relative to this. Normally the real project directory, but two env
# vars can override it, checked in this order:
#   - SC_TEST_DATA_DIR: tests/conftest.py points this at a throwaway temp
#     directory (before importing app/db/anything else) so a test run's
#     databases/, backups, and .active_db never touch the real ones.
#   - SC_DATA_DIR: the Docker image's production override (see Dockerfile/
#     docker-compose.yml) - lets settings.cfg/auth.db/databases/ live on a
#     directory volume separate from /app's code, so an image rebuild never
#     touches persisted state. Also sidesteps a real bug: bind-mounting
#     settings.cfg as an individual file (rather than mounting its parent
#     directory) makes python-dotenv's atomic rename-based write to it fail
#     with "Device or resource busy", since a single-file bind mount can't
#     be renamed over the way a normal file can.
IS_TEST = bool(os.environ.get("SC_TEST_DATA_DIR"))

if IS_TEST:
    BASE_DIR = Path(os.environ["SC_TEST_DATA_DIR"])
elif os.environ.get("SC_DATA_DIR"):
    BASE_DIR = Path(os.environ["SC_DATA_DIR"])
else:
    BASE_DIR = Path(__file__).parent

# Where the actual SQLite database files live (one .db file per "database"
# the user creates from the top bar, e.g. clearout.db, sole_trader.db).
# Per-user isolation (subsystem 2b) moved all databases from databases/*.db
# to databases/<user_id>/*.db - see db.py's user_db_dir. Any pre-existing flat
# databases/*.db files from before this subsystem shipped are left on disk
# untouched but are no longer reachable through the API or migrations; an
# operator who wants one back must manually move it into a specific user's
# databases/<user_id>/ subdirectory (found via auth.db's users table).
DB_DIR = BASE_DIR / "databases"

# Oldest backups beyond this count (per database name) get pruned by
# auto_backup() in blueprints/databases.py.
MAX_BACKUPS_PER_DB = 30

# Ensure the runtime directory exists before anything tries to read/write it.
DB_DIR.mkdir(exist_ok=True)

# Every key settings.cfg can hold, with defaults for the non-secret ones.
# Secret-bearing keys (JWT_SECRET, DB_ENCRYPTION_KEY, ADMIN_PASSWORD) are
# deliberately blank here - _load_or_create_settings() fills them in with a
# freshly generated value the first time it sees them blank. Keep this in
# sync with settings.cfg.example by hand; the app itself never reads that
# file, it's a human-facing template only.
_DEFAULTS: dict[str, str] = {
    "SC_HOST": "127.0.0.1",
    "SC_PORT": "5000",
    "SC_DEBUG": "0",
    "JWT_SECRET": "",
    "JWT_EXPIRY_MINUTES": "60",
    "REFRESH_EXPIRY_DAYS": "30",
    "DB_ENCRYPTION_KEY": "",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "",
    # Set to 1 once the admin account actually exists in auth.db (see
    # mark_admin_bootstrapped below). While it is 0, a blank ADMIN_PASSWORD
    # is treated as "not created yet" and gets generated.
    "ADMIN_BOOTSTRAPPED": "0",
    "ALLOWED_ORIGINS": "http://localhost:*,http://127.0.0.1:*",
    "REDIS_URL": "",
    "LOG_LEVEL": "INFO",
    "SENTRY_DSN": "",
    "SENTRY_TRACES_SAMPLE_RATE": "0.0",
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_FROM": "",
    "SMTP_USE_TLS": "1",
    "APP_BASE_URL": "http://localhost:3000",
    "TRUSTED_PROXY_HOPS": "1",
    "PASSWORD_RESET_EXPIRY_MINUTES": "30",
}

# Keys _load_or_create_settings() auto-generates a value for when blank.
_GENERATED_KEYS = ("JWT_SECRET", "DB_ENCRYPTION_KEY", "ADMIN_PASSWORD")


def _generate_value(key: str) -> str:
    """Random value for a blank secret-bearing key. The admin password
    gets a shorter urlsafe value since a human may need to type it once;
    the two crypto keys get a full 32-byte hex token."""
    if key == "ADMIN_PASSWORD":
        return secrets.token_urlsafe(12)
    return secrets.token_hex(32)


def _int_setting(values: dict[str, str], key: str) -> int:
    """Convert values[key] to int, raising a clear error naming the
    settings.cfg field if a non-technical user hand-edits it into
    something non-numeric."""
    try:
        return int(values[key])
    except ValueError:
        raise ValueError(
            f"settings.cfg: {key}={values[key]!r} is not a valid integer"
        ) from None


def _float_setting(values: dict[str, str], key: str) -> float:
    """Same as _int_setting, for a settings.cfg field that takes a
    fractional value (currently only SENTRY_TRACES_SAMPLE_RATE)."""
    try:
        return float(values[key])
    except ValueError:
        raise ValueError(
            f"settings.cfg: {key}={values[key]!r} is not a valid number"
        ) from None


def _parse_allowed_origins(value: str) -> list[str | re.Pattern[str]]:
    """Parse a comma-separated ALLOWED_ORIGINS setting into the list
    flask-cors' CORS(origins=...) expects. An entry ending in ':*' (e.g.
    'http://localhost:*') matches that scheme+host on any port - the
    scheme+host portion is escaped via re.escape() before being embedded
    in a regex, so a literal '.' in a hostname (e.g. 'my.host') matches
    only a literal '.', not "any character". Any other entry is used as a
    literal exact-match origin string, passed through unmodified."""
    origins: list[str | re.Pattern[str]] = []
    for raw_entry in value.split(","):
        entry = raw_entry.strip()
        if entry.endswith(":*"):
            host_part = entry[: -len(":*")]
            origins.append(re.compile(f"^{re.escape(host_part)}:\\d+$"))
        else:
            origins.append(entry)
    return origins


def _load_or_create_settings(settings_path: Path) -> dict[str, str]:
    """Ensure a settings.cfg exists at settings_path with every field
    populated, auto-generating (and persisting) a value for any blank
    secret-bearing field. Returns the fully-populated key/value mapping.

    Only ever rewrites the specific keys it generates a value for (via
    dotenv's set_key, which edits a single line in place) - an existing
    file's other fields, comments, and formatting are left untouched. If
    ADMIN_PASSWORD is one of the keys generated this call, it's printed to
    the console once, since this is the only time this password will ever
    be visible outside of settings.cfg itself.

    ADMIN_PASSWORD is the one generated key that is deliberately allowed to
    go back to blank permanently: mark_admin_bootstrapped() clears it once
    the account exists, and ADMIN_BOOTSTRAPPED=1 is what stops this
    function generating a replacement on the next start-up. Without that
    flag, clearing the plaintext password would simply mint a new one and
    print a "Admin created" line for an account created months ago.
    """
    if not settings_path.exists():
        settings_path.write_text(
            "\n".join(f"{key}={value}" for key, value in _DEFAULTS.items()) + "\n"
        )

    values: dict[str, str] = {
        **_DEFAULTS,
        **{k: v for k, v in dotenv_values(settings_path).items() if v is not None},
    }

    admin_password_generated = False
    for key in _GENERATED_KEYS:
        if key == "ADMIN_PASSWORD" and values.get("ADMIN_BOOTSTRAPPED") == "1":
            continue  # account already exists - a blank value here is correct, not missing
        if not values.get(key):
            values[key] = _generate_value(key)
            set_key(str(settings_path), key, values[key], quote_mode="never")
            if key == "ADMIN_PASSWORD":
                admin_password_generated = True

    if admin_password_generated:
        print(
            f"Admin created: user={values['ADMIN_USERNAME']} "
            f"pass={values['ADMIN_PASSWORD']}\n"
            "Change this password after first login."
        )

    return values


def mark_admin_bootstrapped() -> None:
    """Record that the admin account exists, and drop the plaintext
    ADMIN_PASSWORD from settings.cfg now that nothing will ever read it
    again.

    ADMIN_PASSWORD is only ever used once, by
    auth_db.ensure_admin_account, which creates the account and never
    resets it. Leaving the plaintext on disk afterwards buys nothing and
    parks a working credential next to the hash it produced, in a file that
    also holds JWT_SECRET and DB_ENCRYPTION_KEY.

    Safe to call on every start-up: both writes are idempotent, and the
    second and later calls have nothing left to clear."""
    if _settings.get("ADMIN_BOOTSTRAPPED") == "1" and not _settings.get("ADMIN_PASSWORD"):
        return
    set_key(str(SETTINGS_FILE), "ADMIN_BOOTSTRAPPED", "1", quote_mode="never")
    set_key(str(SETTINGS_FILE), "ADMIN_PASSWORD", "", quote_mode="never")
    _settings["ADMIN_BOOTSTRAPPED"] = "1"
    _settings["ADMIN_PASSWORD"] = ""


# Where settings.cfg lives - same directory BASE_DIR resolves to for
# everything else (real install dir or the pytest session's throwaway
# temp dir), so tests never touch a real settings.cfg.
SETTINGS_FILE = BASE_DIR / "settings.cfg"
_settings = _load_or_create_settings(SETTINGS_FILE)

# Typed config constants loaded from settings.cfg at module import time.
# Task 3 (app.py) consumes SC_HOST, SC_PORT, SC_DEBUG.
SC_HOST = _settings["SC_HOST"]
SC_PORT = _int_setting(_settings, "SC_PORT")
SC_DEBUG = _settings["SC_DEBUG"] == "1"
JWT_SECRET = _settings["JWT_SECRET"]
JWT_EXPIRY_MINUTES = _int_setting(_settings, "JWT_EXPIRY_MINUTES")
REFRESH_EXPIRY_DAYS = _int_setting(_settings, "REFRESH_EXPIRY_DAYS")
DB_ENCRYPTION_KEY = _settings["DB_ENCRYPTION_KEY"]
ADMIN_USERNAME = _settings["ADMIN_USERNAME"]
ADMIN_PASSWORD = _settings["ADMIN_PASSWORD"]
ALLOWED_ORIGINS = _parse_allowed_origins(_settings["ALLOWED_ORIGINS"])
def _resolve_redis_url(values: dict[str, str]) -> str:
    """SC_REDIS_URL (env var, Docker-only - see docker-compose.yml) takes
    precedence over settings.cfg's REDIS_URL key when set to a non-blank
    value, letting the compose file wire Redis in without editing
    settings.cfg via a running-container `sed`. Falls back to
    settings.cfg's REDIS_URL (blank by default) otherwise - unchanged
    behavior for Option A (`python3 app.py`), which never sets
    SC_REDIS_URL."""
    return os.environ.get("SC_REDIS_URL") or values["REDIS_URL"]


# Blank (the default, from settings.cfg or SC_REDIS_URL both unset) means
# rate_limit.py uses in-memory storage, which only works correctly with a
# single gunicorn worker (see Dockerfile's GUNICORN_WORKERS). Set to a
# redis:// URL (settings.cfg's REDIS_URL, or Docker's SC_REDIS_URL env
# var) to share rate-limit counters across multiple workers/processes -
# see README's "Scaling beyond one worker".
REDIS_URL = _resolve_redis_url(_settings)

# Everything below is observability/notification config - all optional,
# all blank/off by default, same "opt-in, no janky forced dependency on
# an external service" pattern as REDIS_URL above.
LOG_LEVEL = _settings["LOG_LEVEL"]
# Blank disables Sentry entirely (see observability.py's init_sentry) -
# error tracking is opt-in since it requires an operator's own Sentry
# project/DSN, which this app has no way to provision for them.
SENTRY_DSN = _settings["SENTRY_DSN"]
SENTRY_TRACES_SAMPLE_RATE = _float_setting(_settings, "SENTRY_TRACES_SAMPLE_RATE")
# Blank SMTP_HOST disables real email sending (see mailer.py) - the
# password-reset flow still works end to end without it, logging the
# reset link instead of emailing it, same graceful-degradation pattern as
# ADMIN_PASSWORD's console-print for the very first admin account.
SMTP_HOST = _settings["SMTP_HOST"]
SMTP_PORT = _int_setting(_settings, "SMTP_PORT")
SMTP_USERNAME = _settings["SMTP_USERNAME"]
SMTP_PASSWORD = _settings["SMTP_PASSWORD"]
SMTP_FROM = _settings["SMTP_FROM"]
SMTP_USE_TLS = _settings["SMTP_USE_TLS"] == "1"
# Where a password-reset email's link points a user's browser/frontend at
# - this backend serves JSON only (see app.py's docstring), so the actual
# reset *page* lives on whatever frontend is deployed alongside it, not
# here. Change this to that frontend's real origin in production.
APP_BASE_URL = _settings["APP_BASE_URL"]
PASSWORD_RESET_EXPIRY_MINUTES = _int_setting(_settings, "PASSWORD_RESET_EXPIRY_MINUTES")
# How many reverse-proxy hops in front of this backend are trusted, which
# is how many X-Forwarded-For entries app.py's ProxyFix will strip before
# believing one (see app.py). Getting this wrong is quiet but expensive.
#
# Too low and request.remote_addr becomes the nearest proxy's own address,
# so every client shares a single rate-limit bucket. Too high and a client
# can forge X-Forwarded-For to appear as any address it likes, which is
# worse: it makes per-IP limits and any IP-based blocking trivially
# bypassable.
#
# 1 is right for the shipped docker-compose.yml, where Caddy is the only
# thing in front. Count the proxies that actually append to
# X-Forwarded-For, and only the ones you control - a CDN that overwrites
# the header rather than appending to it is not an extra hop.
TRUSTED_PROXY_HOPS = _int_setting(_settings, "TRUSTED_PROXY_HOPS")
