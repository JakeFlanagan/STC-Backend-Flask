#!/bin/sh
# Copies snapshots to a remote rclone target - see README's "Off-site
# backups". Runs inside the opt-in `backup-sync` compose service
# (docker-compose.yml, profile "offsite-backup").
#
# Requires:
#   - RCLONE_REMOTE set to a configured rclone remote:path (e.g.
#     "stc-encrypted:stc-snapshots")
#   - rclone.conf mounted at /config/rclone/rclone.conf with that remote
#     already configured (see rclone.conf.example; gitignored, it holds
#     your remote's credentials)
#
# The source is /snapshots, written by the `snapshot` service. It used to
# be /data/databases with an --include for backups/**, which copied only
# the archives a user had explicitly asked the app to create. That left
# out the live databases, and it left out settings.cfg entirely - which
# holds DB_ENCRYPTION_KEY, the key those archives are encrypted with. The
# result was an off-site copy that could not be decrypted using anything
# else in the off-site copy. See scripts/snapshot.py for the longer
# version.
#
# ENCRYPTION: point RCLONE_REMOTE at an rclone `crypt` remote, not
# directly at the bucket. A snapshot contains JWT_SECRET,
# DB_ENCRYPTION_KEY and every user's data in the clear, because the host
# already has all of that in the clear and encrypting it there protects
# nothing. Encrypting it on the way out is what stops whoever runs the
# bucket from reading it. rclone.conf.example shows the two-remote setup.
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
    echo "sync_backups.sh: syncing snapshots to ${RCLONE_REMOTE}..."
    if rclone sync /snapshots "${RCLONE_REMOTE}" -v; then
        echo "sync_backups.sh: sync completed"
    else
        echo "sync_backups.sh: sync failed, will retry next interval" >&2
    fi
    sleep "$interval"
done
