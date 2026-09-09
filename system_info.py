"""Read-only introspection of the running backend, for blueprints/admin.py.

Everything here answers a question an operator would otherwise have to ssh
into the box for: what is running, how long has it been up, how much disk
is left, and which files under DB_DIR belong to whom. Kept out of the
blueprint so the routes stay a thin translation layer, and out of db.py
because none of it is per-request database access.

One rule this module exists to enforce in one place: NOTHING here may
return a secret. settings.cfg holds JWT_SECRET, DB_ENCRYPTION_KEY and the
SMTP password in the same file as the log level and the token lifetimes,
so a well-meaning "just dump the config" endpoint is one commit away from
publishing the signing key to any admin session (and to anything that ever
steals one). config_summary() below therefore names every field it
returns, and reports secret-bearing ones only as a configured true/false.
"""

import os
import platform
import shutil
import sqlite3
import sys
import time
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

import config
from db import get_active_db_name, user_db_dir

# Wall-clock time this worker started, captured at import. Gunicorn runs
# several workers, so this is the age of whichever one served the request,
# not of the deployment - which is the honest thing to report, and why the
# field is named worker_uptime_seconds rather than uptime.
_STARTED_AT = time.time()


def _package_version(name: str) -> str:
    """Installed version of a dependency, or "unknown" if it is not
    installed under that distribution name. Never raises: a missing
    optional dependency must not be able to take down the one endpoint an
    operator reaches for when something is already wrong."""
    try:
        return package_version(name)
    except PackageNotFoundError:
        return "unknown"


def dir_size(path: Path) -> int:
    """Total bytes of every file under path, following no symlinks and
    ignoring anything that disappears mid-walk (a backup being pruned
    while this runs is normal, not an error)."""
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _mtime(path: Path) -> str | None:
    """path's modification time as 'YYYY-MM-DD HH:MM:SS', or None if it
    vanished between the listing and the stat."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        return None


def config_summary() -> dict[str, Any]:
    """The non-secret half of settings.cfg, plus a configured true/false
    for each secret-bearing field. See this module's docstring for why the
    field list is written out by hand instead of iterating the settings
    mapping."""
    return {
        "allowed_origins": [
            pattern.pattern if hasattr(pattern, "pattern") else pattern
            for pattern in config.ALLOWED_ORIGINS
        ],
        "app_base_url": config.APP_BASE_URL,
        "admin_username": config.ADMIN_USERNAME,
        "jwt_expiry_minutes": config.JWT_EXPIRY_MINUTES,
        "refresh_expiry_days": config.REFRESH_EXPIRY_DAYS,
        "password_reset_expiry_minutes": config.PASSWORD_RESET_EXPIRY_MINUTES,
        "max_backups_per_db": config.MAX_BACKUPS_PER_DB,
        "log_level": config.LOG_LEVEL,
        "trusted_proxy_hops": config.TRUSTED_PROXY_HOPS,
        # Presence, never the value. A blank SMTP_HOST is why password
        # reset silently does nothing on a box that has never had mail
        # configured, so it is worth surfacing - the credentials are not.
        "redis_configured": bool(config.REDIS_URL),
        "smtp_configured": bool(config.SMTP_HOST),
        "sentry_configured": bool(config.SENTRY_DSN),
    }


def runtime_summary() -> dict[str, Any]:
    """What is running and for how long. build_sha comes from the
    SC_BUILD_SHA env var, which the image sets at build time - "unknown"
    when running from a checkout rather than a container, which is itself
    the useful answer."""
    return {
        "build_sha": os.environ.get("SC_BUILD_SHA", "unknown"),
        "worker_pid": os.getpid(),
        "worker_uptime_seconds": int(time.time() - _STARTED_AT),
        "python_version": sys.version.split()[0],
        "flask_version": _package_version("flask"),
        "sqlite_version": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "workers_configured": os.environ.get("GUNICORN_WORKERS", "unknown"),
        "data_dir": str(config.BASE_DIR),
    }


def disk_summary() -> dict[str, Any]:
    """Free space on the filesystem holding the data directory, and how
    much of it this app's own data accounts for. The two are separate
    numbers on purpose: the data directory is usually a small fraction of a
    shared volume, and "the app is using 40MB" says nothing about whether
    the box is about to run out of room."""
    usage = shutil.disk_usage(config.BASE_DIR)
    auth_db_path = config.BASE_DIR / "auth.db"
    return {
        "filesystem_total_bytes": usage.total,
        "filesystem_used_bytes": usage.used,
        "filesystem_free_bytes": usage.free,
        "data_dir_bytes": dir_size(config.BASE_DIR),
        "databases_dir_bytes": dir_size(config.DB_DIR),
        "auth_db_bytes": auth_db_path.stat().st_size if auth_db_path.exists() else 0,
    }


def _user_storage(user_id: int) -> dict[str, Any]:
    """One user's databases and backups. Deliberately does NOT call
    db.user_db_dir for a user that has no directory yet - that function
    creates the directory as a side effect, which would turn a read-only
    inventory endpoint into something that writes to disk for every
    account that has never logged in."""
    user_dir = config.DB_DIR / str(user_id)
    if not user_dir.is_dir():
        return {"databases": [], "backup_count": 0, "backup_bytes": 0, "total_bytes": 0}

    active = get_active_db_name(user_id) if any(user_dir.glob("*.db")) else None
    databases = []
    for path in sorted(user_dir.glob("*.db")):
        backup_dir = user_dir / "backups" / path.stem
        backups = sorted(backup_dir.glob("*.db.enc")) if backup_dir.is_dir() else []
        databases.append(
            {
                "name": path.stem,
                "size_bytes": path.stat().st_size,
                "modified": _mtime(path),
                "active": path.stem == active,
                "backup_count": len(backups),
                "backup_bytes": sum(b.stat().st_size for b in backups),
                "latest_backup": _mtime(backups[-1]) if backups else None,
            }
        )

    backups_root = user_dir / "backups"
    return {
        "databases": databases,
        "backup_count": sum(d["backup_count"] for d in databases),
        "backup_bytes": dir_size(backups_root) if backups_root.is_dir() else 0,
        "total_bytes": dir_size(user_dir),
    }


def storage_inventory(users: list[dict[str, Any]]) -> dict[str, Any]:
    """Every database file on disk, grouped by the account that owns it.

    `users` is auth_db.list_users' output, so this module never opens
    auth.db itself. Directories under DB_DIR whose name matches no account
    are reported separately as orphans rather than silently skipped: they
    are exactly what auth_db.delete_user leaves behind (it deletes the
    account and never the files), so without this they accumulate
    invisibly and the only way to find them is `ls` on the box.

    Legacy flat DB_DIR/*.db files - from before per-user isolation shipped
    - are listed too, for the same reason: unreachable through the API,
    still occupying disk, and otherwise invisible.
    """
    known_ids = {u["id"] for u in users}
    accounts = []
    for user in users:
        accounts.append({**user, **_user_storage(user["id"])})

    orphans = []
    legacy = []
    for entry in sorted(config.DB_DIR.iterdir()) if config.DB_DIR.is_dir() else []:
        if entry.is_dir():
            if entry.name.isdigit() and int(entry.name) not in known_ids:
                orphans.append(
                    {
                        "user_id": int(entry.name),
                        "database_count": len(list(entry.glob("*.db"))),
                        "total_bytes": dir_size(entry),
                        "modified": _mtime(entry),
                    }
                )
        elif entry.suffix == ".db":
            legacy.append(
                {"name": entry.name, "size_bytes": entry.stat().st_size, "modified": _mtime(entry)}
            )

    return {
        "accounts": accounts,
        "orphans": orphans,
        "legacy_files": legacy,
        "totals": {
            "database_count": sum(len(a["databases"]) for a in accounts),
            "database_bytes": sum(
                d["size_bytes"] for a in accounts for d in a["databases"]
            ),
            "backup_count": sum(a["backup_count"] for a in accounts),
            "backup_bytes": sum(a["backup_bytes"] for a in accounts),
            "orphan_bytes": sum(o["total_bytes"] for o in orphans),
        },
    }


def orphan_summary(user_id: int) -> dict[str, Any]:
    """What a single user id has sitting under DB_DIR, without walking
    every other account's directory the way storage_inventory does.

    Called right after an account is deleted, where the only question is
    how much this one account left behind. Reads the directory directly
    rather than via db.user_db_dir, which would create it as a side effect
    - the last thing wanted immediately after deleting its owner.
    """
    user_dir = config.DB_DIR / str(user_id)
    if not user_dir.is_dir():
        return {"user_id": user_id, "database_count": 0, "total_bytes": 0}
    return {
        "user_id": user_id,
        "database_count": len(list(user_dir.glob("*.db"))),
        "total_bytes": dir_size(user_dir),
    }


def database_path(user_id: int, name: str) -> Path:
    """Path to one user's database file. Callers must have validated
    `name` with db.is_valid_db_name first - the name becomes a filename
    directly, and that validation is the whole reason a name cannot escape
    the user's own directory."""
    return user_db_dir(user_id) / f"{name}.db"
