#!/bin/sh
# Periodically syncs every account's encrypted backups directory to a
# remote rclone target - see README's "Off-site backups". Runs inside the
# opt-in `backup-sync` compose service (docker-compose.yml, profile
# "offsite-backup").
#
# Requires:
#   - RCLONE_REMOTE set to a configured rclone remote:path (e.g.
#     "s3remote:my-bucket/stc-backups")
#   - rclone.conf mounted at /config/rclone/rclone.conf with that remote
#     already configured (create locally with `rclone config`, copy the
#     resulting file next to docker-compose.yml - see README; gitignored,
#     it holds your remote's credentials)
#
# A plain loop + sleep, not cron: the official rclone image is a single
# static binary with no cron daemon, and pulling one in just for this
# would be more moving parts than the job needs.

set -eu

if [ -z "${RCLONE_REMOTE:-}" ]; then
    echo "sync_backups.sh: RCLONE_REMOTE is not set - see README's 'Off-site backups'. Not syncing." >&2
    # Sleep rather than exit: exiting under `restart: unless-stopped`
    # would crash-loop and spam logs every few seconds for what is
    # actually just a one-line, already-logged configuration problem.
    while true; do
        sleep 3600
        echo "sync_backups.sh: still not syncing - RCLONE_REMOTE is still unset." >&2
    done
fi

interval="${BACKUP_SYNC_INTERVAL_SECONDS:-21600}"

while true; do
    echo "sync_backups.sh: syncing backups to ${RCLONE_REMOTE}..."
    # Source is /data/databases (see config.py's DB_DIR under
    # SC_DATA_DIR) - every account's databases/<user_id>/backups/<db
    # name>/ tree. --include targets only the backups/ subdirectories,
    # not the live .db files or .active_db marker: those change
    # constantly and aren't what an off-site copy is for - the backups
    # (already encrypted at rest with settings.cfg's DB_ENCRYPTION_KEY,
    # see blueprints/databases.py) are the point-in-time snapshots worth
    # copying off-host.
    if rclone sync /data/databases "${RCLONE_REMOTE}" --include "*/backups/**" -v; then
        echo "sync_backups.sh: sync completed"
    else
        echo "sync_backups.sh: sync failed, will retry next interval" >&2
    fi
    sleep "$interval"
done
