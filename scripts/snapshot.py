"""Take a consistent, restorable snapshot of everything in SC_DATA_DIR.

Run inside the `snapshot` compose service (docker-compose.yml, profile
"offsite-backup"), which uses this project's own image because it needs
Python's sqlite3 module. The `backup-sync` service then copies the result
off-host.

Why this exists as well as scripts/sync_backups.sh:

  sync_backups.sh copies databases/*/backups/**, which is only the
  point-in-time archives a user has explicitly asked the app to make. That
  is the right thing to sync but it is not a disaster-recovery backup, for
  two reasons.

  First, it excludes the live .db files. If the host dies, everything since
  each user last pressed "backup" is gone.

  Second, and worse, it excludes settings.cfg, which is not under
  databases/ at all. Those backup files are encrypted with
  DB_ENCRYPTION_KEY, and DB_ENCRYPTION_KEY lives in settings.cfg. Syncing
  only the archives means shipping ciphertext off-site while leaving the
  only key that opens it on the machine you are protecting against. The
  restore you would eventually attempt would produce a directory of
  permanently undecryptable files.

Consistency: every .db is copied with sqlite3's online backup API, not by
reading the file. A plain copy of a live SQLite database can catch it
mid-write and produce a torn file, and with WAL enabled (see db.py's
get_db) the -wal sidecar has to be accounted for too. The backup API takes
care of both and does not need the writer to stop.

The output is a plain tar.gz. It is NOT encrypted here, deliberately: the
host already holds this data in the clear, so encrypting it on the same
host protects nothing. Encryption belongs on the way out, which is what
the rclone crypt remote in rclone.conf.example does. See the README's
"Off-site backups".
"""

import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("SC_DATA_DIR", "/data"))
OUT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/snapshots"))
KEEP = int(os.environ.get("SNAPSHOT_KEEP", "7"))


def _log(message: str) -> None:
    print(f"snapshot: {message}", flush=True)


def _copy_database(source: Path, target: Path) -> None:
    """Copy one SQLite file using the online backup API.

    sqlite3.Connection.backup holds a read lock only for as long as each
    step takes, so a writer is delayed rather than failed, and the result
    is always a complete database rather than whatever the file happened
    to look like partway through a transaction.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        # Same reasoning as db.py's get_db: without a busy timeout, a
        # concurrent writer makes this raise "database is locked" straight
        # away instead of waiting a moment for its turn.
        src.execute("PRAGMA busy_timeout = 5000")
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _prune(out_dir: Path, keep: int) -> None:
    """Keep the newest `keep` snapshots and delete the rest.

    Local disk is a staging area, not the archive. Retention that actually
    matters is whatever the off-site remote is configured to keep, because
    that is the copy that survives losing this host.
    """
    snapshots = sorted(out_dir.glob("stc-*.tar.gz"))
    for stale in snapshots[:-keep] if keep > 0 else []:
        stale.unlink()
        _log(f"pruned {stale.name}")


def main() -> int:
    if not DATA_DIR.is_dir():
        _log(f"{DATA_DIR} does not exist - nothing to snapshot")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = OUT_DIR / f"stc-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "stc"
        staging.mkdir()

        settings = DATA_DIR / "settings.cfg"
        if settings.exists():
            # The whole point. JWT_SECRET and DB_ENCRYPTION_KEY live here,
            # and without DB_ENCRYPTION_KEY every encrypted backup in the
            # archive below is so much noise.
            (staging / "settings.cfg").write_bytes(settings.read_bytes())
            _log("captured settings.cfg")
        else:
            _log("WARNING: no settings.cfg found - a restore from this "
                 "snapshot will not be able to decrypt any backup file")

        auth = DATA_DIR / "auth.db"
        if auth.exists():
            _copy_database(auth, staging / "auth.db")
            _log("captured auth.db")

        count = 0
        for db_path in sorted((DATA_DIR / "databases").glob("*/*.db")):
            relative = db_path.relative_to(DATA_DIR)
            _copy_database(db_path, staging / relative)
            count += 1
        _log(f"captured {count} user database(s)")

        # The user-triggered encrypted archives too. They are already
        # covered by sync_backups.sh, but a single self-contained snapshot
        # is a much easier thing to restore from at 2am than two half
        # sets.
        backups = 0
        for backup in sorted((DATA_DIR / "databases").glob("*/backups/**/*")):
            if backup.is_file():
                target = staging / backup.relative_to(DATA_DIR)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup.read_bytes())
                backups += 1
        _log(f"captured {backups} existing backup file(s)")

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname="stc")

    _log(f"wrote {archive.name} ({archive.stat().st_size} bytes)")
    _prune(OUT_DIR, KEEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
