#!/bin/sh
# Runs scripts/snapshot.py on a fixed interval inside the `snapshot`
# compose service (docker-compose.yml, profile "offsite-backup").
#
# A plain loop and sleep, not cron, for the same reason sync_backups.sh
# uses one: the container has no cron daemon and pulling one in would be
# more moving parts than the job needs.
#
# Takes one immediately on start rather than waiting out the first
# interval. A service that has been up for five hours and has produced
# nothing is indistinguishable from a broken one.

set -eu

interval="${SNAPSHOT_INTERVAL_SECONDS:-21600}"

while true; do
    if python3 /app/scripts/snapshot.py; then
        :
    else
        # Keep going rather than exiting. Under restart: unless-stopped an
        # exit here would crash-loop, and a single failed snapshot is worth
        # retrying at the next interval, not worth spamming the log every
        # few seconds.
        echo "run_snapshots.sh: snapshot failed, will retry in ${interval}s" >&2
    fi
    sleep "$interval"
done
