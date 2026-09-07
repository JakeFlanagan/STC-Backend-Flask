FROM python:3.12-slim

# Unbuffered stdout - without this, config.py's one-time "Admin created:
# user=... pass=..." print (the only place a freshly-generated admin
# password is ever shown) sits in Python's stdout buffer indefinitely
# under gunicorn instead of reaching `docker logs`, since a long-running
# worker process has no other reason to flush it.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# A fixed uid, not a name. The uid is what the host sees on the volume, so
# it has to stay stable across rebuilds or an existing /data becomes
# unreadable after an image update.
RUN useradd --system --uid 10001 --user-group --no-create-home app

# The lock, not requirements.txt: the lock pins transitive dependencies
# too, so the image CI builds today and the one it builds next month
# contain the same code. requirements.txt stays the human-edited list of
# direct dependencies.
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

EXPOSE 5000

# settings.cfg, auth.db, and databases/ all live under SC_DATA_DIR instead
# of next to the code (see config.py's BASE_DIR) - a volume mounted at
# /data (see docker-compose.yml) must persist across restarts, or a
# restart regenerates settings.cfg's secrets (JWT_SECRET,
# DB_ENCRYPTION_KEY, ADMIN_PASSWORD) from scratch, invalidating every
# existing JWT/refresh token and making every existing encrypted
# backup/export permanently undecryptable.
ENV SC_DATA_DIR=/data

# /data must be created and owned by the app user IN THE IMAGE, not just
# mounted. Docker copies ownership from the image's directory when it first
# populates a named volume, so a volume created against a root-owned /data
# stays root-owned, and the app cannot write settings.cfg no matter what
# USER is set below. Creating it here is what makes the two work together.
#
# An existing volume created before this change keeps its old root
# ownership, because Docker only seeds a volume once. Upgrading needs a
# one-off fix on the host:
#   docker compose run --rm --user root backend chown -R 10001:10001 /data
RUN mkdir -p /data && chown 10001:10001 /data

# Everything below runs unprivileged. The application code stays root-owned
# and read-only to the app user, which is what you want: the only writable
# paths are /data and the tmpfs at /tmp.
USER 10001
#
# GUNICORN_WORKERS defaults to 4 and SC_REDIS_URL defaults to Redis
# (docker-compose.yml's committed .env sets both) - rate_limit.py's
# per-IP throttling needs Redis-backed storage shared across processes
# once more than one worker is running; in-memory storage (SC_REDIS_URL
# unset) only works correctly with a single worker. See README's
# "Scaling beyond one worker" for the single-worker/no-Redis opt-out.
# wsgi:app, not app:app - only the wsgi module runs migrations + the
# admin-account bootstrap at import time (see wsgi.py/app.py's
# bootstrap()); gunicorn never executes app.py's `if __name__ ==
# "__main__":` block.

# python3, not curl - this base image doesn't ship curl and there's no
# reason to add a package just for a liveness probe. Hits the same
# /api/v1/health route any frontend uses (app.py) - a 200 means the
# process is alive and able to serve a request end-to-end, not just that
# gunicorn's master process is running.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/api/v1/health', timeout=2).status == 200 else 1)"

CMD ["sh", "-c", "gunicorn -w ${GUNICORN_WORKERS:-4} -b 0.0.0.0:5000 wsgi:app"]
