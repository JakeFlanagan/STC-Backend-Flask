# STC Backend

Flask + SQLite backend for Sole-Trader Companion (STC) - tracks second-hand
sales across Vinted, eBay, and other platforms. Pure JSON API under
`/api/v1/*` - no frontend of any kind is served from here. Any frontend
(the dev web SPA, the desktop AppImage, the future Android app) talks to
this over HTTP.

**Frontend developers:** see [`docs/API.md`](docs/API.md) for the full
endpoint reference (every route, request/response shape, auth
requirements).

## Prerequisites

- **Running directly:** Python 3.12+ and `pip`.
- **Running with Docker:** Docker Engine and the `docker compose` plugin
  (Compose v2). No local Python install needed - everything runs inside
  the container.

## Running it

There are two ways to run this backend. Pick the one that matches your
situation - they're not meant to be combined.

### Option A: run directly with Python (local dev / trusted network only)

No TLS, no reverse proxy - only use this on your own machine or a network
you trust. This is also how the test suite runs.

```bash
pip install -r requirements.txt
python3 app.py
# http://localhost:5000/api/v1
```

First run creates `settings.cfg` next to `app.py` automatically (see
"Configuration" below) and prints the auto-generated admin password once:

```
Admin created: user=admin pass=<random>
Change this password after first login.
```

Write that password down - it's never shown again after this first run
(it's stored, hashed, in `auth.db`, not recoverable from there). Log in
with it via `POST /api/v1/auth/login` and change it via your frontend's
account settings, or leave `ADMIN_PASSWORD` set in `settings.cfg` if you'd
rather pin it yourself.

`SC_DEBUG=1 python3 app.py` turns on Werkzeug's interactive debugger and
auto-reloader for local development - off by default, since leaving it on
would expose a debugger that allows arbitrary code execution.

By default the server binds to `127.0.0.1:5000` (loopback only). To reach
it from another machine on your network, set `SC_HOST=0.0.0.0` in
`settings.cfg` first - see "Configuration" below.

### Option B: run with Docker (production-style, TLS included)

`docker-compose.yml` is a reference deployment: this backend (via
gunicorn, see `Dockerfile`/`wsgi.py`) behind Caddy doing real TLS
termination (automatic Let's Encrypt certificates - no manual cert
management, Caddy provisions and renews them on first request).

```bash
# 1. Point your domain's DNS A/AAAA record at this host, then edit
#    Caddyfile - replace the placeholder domain with your real one.
docker compose up -d --build

# 2. Capture the auto-generated admin password from the first-run logs -
#    it's only ever shown once, here:
docker compose logs app | grep "Admin created"

# 3. Add your real frontend origin to ALLOWED_ORIGINS:
docker compose exec app sh -c \
  "sed -i 's|^ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=https://your-frontend.example.com|' /data/settings.cfg"
docker compose restart app
```

Useful day-to-day commands:

```bash
docker compose ps                 # is everything up and healthy?
docker compose logs -f app        # tail the backend's logs
docker compose exec app sh        # shell inside the app container
docker compose restart app        # apply a settings.cfg edit
docker compose down                # stop everything (data survives - see below)
```

`settings.cfg`, `auth.db`, and `databases/` all live on one directory
volume (`stc_data`, mounted at `/data` via `SC_DATA_DIR` - see
`config.py`) so they survive container restarts/rebuilds; losing that
volume regenerates every secret in `settings.cfg` (`JWT_SECRET`,
`DB_ENCRYPTION_KEY`, `ADMIN_PASSWORD`) from scratch, invalidating every
existing session and making every existing encrypted backup/export
permanently undecryptable - back it up like you would any other
production data (`docker volume inspect stc-backend_stc_data` to find
where it lives on disk).

#### Scaling beyond one worker

`docker compose up` runs multiple gunicorn workers with Redis-backed rate
limiting by default (see the committed `.env`: `GUNICORN_WORKERS=4`,
`SC_REDIS_URL=redis://redis:6379/0`, `COMPOSE_PROFILES=scale` - the last
one is what starts the `redis` service at all). No extra steps needed.

To change the worker count, override `GUNICORN_WORKERS` in your own shell
env or an untracked `.env.local`-style file:

```bash
echo "GUNICORN_WORKERS=8" >> .env.local
docker compose --env-file .env --env-file .env.local up -d --force-recreate app
```

##### Running single-worker without Redis

To go back to a single worker with in-memory rate limiting (no Redis
container at all), override all three defaults:

```bash
cat > .env.local <<'EOF'
COMPOSE_PROFILES=
SC_REDIS_URL=
GUNICORN_WORKERS=1
EOF
docker compose --env-file .env --env-file .env.local up -d --force-recreate app
```

##### Password-protecting Redis

Redis has no `requirepass` by default - fine as long as it's only
reachable over the compose-internal network (no published port, which is
the default here). If you ever expose this Redis service beyond that
network, set a password:

```bash
cat > .env.local <<'EOF'
REDIS_PASSWORD=some-strong-password
SC_REDIS_URL=redis://:some-strong-password@redis:6379/0
EOF
docker compose --env-file .env --env-file .env.local up -d --force-recreate redis app
```

Both `REDIS_PASSWORD` and `SC_REDIS_URL` need to be set together - compose
can't conditionally build the `redis://:password@...` URL for you.

## Optional production setup

The backend runs fully without any of this - each is off/inert by
default, and turning it on is just filling in a config value. Worth
doing before real users depend on this deployment:

- **Password reset emails.** Without this, a forgotten password can only
  be fixed by an admin or `auth.db` surgery. Set `SMTP_HOST`,
  `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` in `settings.cfg` to a
  real SMTP provider's details, and `APP_BASE_URL` to your frontend's
  real origin. Until set, reset links are only logged, never emailed -
  see "Password reset" below for the full flow.
- **Error tracking.** Set `SENTRY_DSN` in `settings.cfg` to a
  [Sentry](https://sentry.io) project's DSN (free tier available, or
  self-host) to get unhandled exceptions reported somewhere other than
  `docker compose logs`. See "Observability" below.
- **Off-site backups.** Backups are encrypted but live on the same host/
  volume as the live data - losing that host loses them too. Run `rclone
  config` once, copy the resulting `rclone.conf` next to
  `docker-compose.yml`, and set `RCLONE_REMOTE` to sync backups to S3/B2/
  GCS/etc on a schedule. See "Off-site backups" below for the full
  walkthrough.

## Configuration

Both ways of running this backend read `settings.cfg` (git-ignored - see
`settings.cfg.example` for the committed template with comments on every
key). If it doesn't exist yet, it's created automatically on first start
with every secret-bearing field (`JWT_SECRET`, `DB_ENCRYPTION_KEY`,
`ADMIN_PASSWORD`) auto-generated - you don't need to create it by hand
before the first run. To customize a value up front instead, copy
`settings.cfg.example` to `settings.cfg` and fill it in before starting
the app.

| Key | Default | Meaning |
|---|---|---|
| `SC_HOST` | `127.0.0.1` | Bind address for `python3 app.py` (Option A only - Docker always binds `0.0.0.0` via gunicorn's `-b` flag). |
| `SC_PORT` | `5000` | Bind port for `python3 app.py`. |
| `SC_DEBUG` | `0` | Set to `1` for Werkzeug's interactive debugger/auto-reloader. Local dev only - allows arbitrary code execution. |
| `JWT_SECRET` | auto-generated | Signs access tokens. Losing/rotating this invalidates every existing session. |
| `JWT_EXPIRY_MINUTES` | `60` | How long an access token is valid before a `/auth/refresh` is required. |
| `REFRESH_EXPIRY_DAYS` | `30` | How long a refresh token is valid. |
| `DB_ENCRYPTION_KEY` | auto-generated | Encrypts backup/auto-backup files at rest. Losing/rotating this makes existing backups permanently undecryptable. |
| `ADMIN_USERNAME` | `admin` | First admin account, created on first run. |
| `ADMIN_PASSWORD` | auto-generated | Printed once to the console the first time the app starts with no existing `settings.cfg`. Change it after first login. |
| `ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*` | Comma-separated CORS origins. Add your real frontend origin before hosting anywhere other than localhost. |
| `REDIS_URL` | blank | Set to a `redis://` URL to share rate-limit counters across multiple gunicorn workers - see "Scaling beyond one worker". Docker sets this via `SC_REDIS_URL` instead (below) by default; this key matters for Option A or a manual override. |
| `LOG_LEVEL` | `INFO` | Minimum level logged - see "Observability" below. |
| `SENTRY_DSN` | blank | Enables Sentry error tracking when set - see "Observability" below. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | Fraction of requests to send full performance traces for (0.0-1.0). |
| `SMTP_HOST` | blank | Enables real password-reset emails when set - see "Password reset" below. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USERNAME` | blank | SMTP auth username. |
| `SMTP_PASSWORD` | blank | SMTP auth password. |
| `SMTP_FROM` | blank | "From" address on password-reset emails. |
| `SMTP_USE_TLS` | `1` | Set to `0` to disable STARTTLS (only for an SMTP server that doesn't support it). |
| `APP_BASE_URL` | `http://localhost:3000` | Frontend origin a password-reset email's link points at - see "Password reset" below. |
| `PASSWORD_RESET_EXPIRY_MINUTES` | `30` | How long a password-reset link stays valid. |

Environment variables (as opposed to `settings.cfg` keys) that matter:

| Variable | Used by | Meaning |
|---|---|---|
| `SC_DATA_DIR` | Docker only | Where `settings.cfg`/`auth.db`/`databases/` live, instead of next to the code - set to `/data` by `Dockerfile`, backed by the `stc_data` volume. Don't set this for Option A. |
| `GUNICORN_WORKERS` | Docker only | Worker count for gunicorn - see "Scaling beyond one worker". Defaults to `4`. |
| `SC_REDIS_URL` | Docker only | Overrides settings.cfg's `REDIS_URL` - see "Scaling beyond one worker". Defaults to `redis://redis:6379/0`. |
| `REDIS_PASSWORD` | Docker only | Sets Redis's `--requirepass` - see "Password-protecting Redis". Blank (no auth) by default. |

## Data & backups

Each "database" you create is its own SQLite file, private to your
account, under `databases/<user_id>/`. Whichever one is active for your
account is tracked in `databases/<user_id>/.active_db`. Backups - one
automatic per day, plus on-demand manual backups and automatic
pre-restore safety snapshots - live under `databases/<user_id>/backups/<db
name>/`, encrypted with `settings.cfg`'s `DB_ENCRYPTION_KEY`. Exporting a
database (`POST /api/v1/databases/export`) encrypts it with a password
you supply, so it can be moved to another machine and imported there
(`POST /api/v1/databases/import`) with that same password - no server
secret needs to travel with the file. None of `databases/`, `auth.db`, or
`settings.cfg` are committed to git.

#### Off-site backups

Everything above lives on the same `stc_data` volume as the live
database - losing that host loses the backups too. The opt-in
`backup-sync` service (profile `offsite-backup`) periodically syncs every
account's encrypted `backups/` directories to a remote of your choice via
[rclone](https://rclone.org) (S3, Backblaze B2, GCS, and dozens more -
anything rclone supports). Not started by a plain `docker compose up`,
nor by the `scale` profile: it needs your own remote + credentials, which
this project has no way to provision for you.

```bash
# 1. Configure a remote locally (interactive - pick your provider,
#    supply its credentials):
rclone config
# This writes a config file (usually ~/.config/rclone/rclone.conf) -
# copy it next to docker-compose.yml as `rclone.conf` (gitignored - it
# holds your remote's credentials):
cp ~/.config/rclone/rclone.conf ./rclone.conf

# 2. Point the sync at the remote you just configured (use the same name
#    you gave it in step 1) and start the service:
cat > .env.local <<'EOF'
RCLONE_REMOTE=your-remote-name:your-bucket/stc-backups
BACKUP_SYNC_INTERVAL_SECONDS=21600
EOF
docker compose --env-file .env --env-file .env.local --profile offsite-backup up -d backup-sync
```

Only `databases/<user_id>/backups/` subtrees are synced (already
encrypted at rest with `DB_ENCRYPTION_KEY`) - never the live, currently-
open `.db` files or `.active_db` markers, which change constantly and
aren't what a point-in-time off-site copy is for. `docker compose logs -f
backup-sync` shows each sync run; if `RCLONE_REMOTE` is unset it logs a
clear warning instead of silently doing nothing or crash-looping.

## Observability

- **Logging:** every log line (from the app, gunicorn, or any library) is
  structured JSON on stdout - `docker compose logs -f app` gives you
  machine-parseable output out of the box. Level is `LOG_LEVEL` (default
  `INFO`). Each request also logs one line with method/path/status/
  duration/user_id.
- **Metrics:** `GET /metrics` exposes Prometheus text-format metrics
  (request counts, latency histograms per route) - unauthenticated, same
  as `/health`, since a scraper isn't expected to carry a JWT. Point a
  Prometheus instance at it, or leave it unscraped (inert either way).
  Restrict network access to it at the reverse-proxy/firewall layer if
  it's ever reachable from somewhere it shouldn't be.
- **Error tracking:** set `SENTRY_DSN` in `settings.cfg` to send
  unhandled exceptions to [Sentry](https://sentry.io) (or a self-hosted
  instance) - blank (the default) disables this entirely. Request bodies
  are never attached to error reports (`send_default_pii=False`).

## Password reset

Accounts have an optional `email` (set at registration, or afterward via
authenticated `POST /api/v1/auth/email`). With one on file, self-service
reset works:

1. `POST /api/v1/auth/password-reset/request {"username": "..."}` -
   always returns the same generic response, whether or not the account
   exists or has an email (enumeration-safe). If it does, a one-time
   link valid for `PASSWORD_RESET_EXPIRY_MINUTES` (default 30) is emailed
   to it (or logged - see below).
2. `POST /api/v1/auth/password-reset/confirm {"token": "...",
   "new_password": "..."}` - sets the new password and revokes every
   existing session (refresh tokens and already-issued access tokens),
   same "assume compromise" reasoning as reused-refresh-token detection.

Accounts created before this feature (or via `ensure_admin_account`) have
no email until one is set via `/auth/email` - there's no other way to
reach them with a reset link.

**Emailing the link:** set `SMTP_HOST` (and `SMTP_USERNAME`/
`SMTP_PASSWORD`/`SMTP_FROM` as your provider requires) in `settings.cfg`.
Leave `SMTP_HOST` blank (the default) and the reset link is logged
instead (`stc.mailer` logger) rather than emailed - functional for local
dev/testing without any SMTP server, not suitable for real users who
need the email itself.

Set `APP_BASE_URL` to your real frontend's origin - the emailed link is
`{APP_BASE_URL}/reset-password?token=...`; this backend is JSON-only
(see `app.py`'s docstring), so the actual reset *page* is the frontend's
responsibility to implement.

## Tests

```bash
pip install -r requirements-dev.txt
./run_tests.sh                                # everything
./run_tests.sh tests/test_items_routes.py      # one file
ruff check .                                  # lint
mypy .                                         # types
```

See `tests/README.md` for what the suite covers and how its isolation
works.

## Security notes

- **Authentication:** every `/api/v1/*` route except `/health`,
  `/metrics`, and `/auth/*` (except `/auth/email`, which requires auth)
  requires a valid JWT access token (`Authorization: Bearer <token>`).
  Register/login via `/api/v1/auth/register` and `/login`; refresh via
  `/refresh` before the access token expires (`JWT_EXPIRY_MINUTES` in
  `settings.cfg`, default 60). `/register` accepts an optional `email`
  field - see "Password reset" below for why.
- **Refresh-token theft response:** reusing a refresh token that's already
  been rotated out (i.e. replaying one after its one-time use) revokes
  every refresh token the account holds, not just the reused one, and
  also invalidates any access token already issued to that account (the
  next request with it gets a 401, instead of it riding out its remaining
  TTL) - see `auth_db.get_reused_refresh_token_owner` and
  `auth_db.revoke_all_refresh_tokens_for_user`.
- **Rate limiting:** `/auth/register` (5/hour), `/auth/login`
  (10/minute), `/auth/password-reset/request` (5/hour), and
  `/auth/password-reset/confirm` (10/hour) are throttled per client IP -
  see `rate_limit.py`. Behind a
  reverse proxy (the standard deployment, see "Running it" above), the
  real client IP is read from `X-Forwarded-For` via `ProxyFix` (`app.py`),
  trusting exactly one proxy hop. `/auth/login` separately locks out
  repeated failures against one *username*, regardless of source IP
  (escalating lockout after 5 failures, capped at 1 hour) - see
  `auth_db.record_login_failure`.
- **Authorization:** an `admin`-role account (bootstrapped from
  `settings.cfg`'s `ADMIN_USERNAME`/`ADMIN_PASSWORD` on first startup) can
  list/disable/enable/delete other accounts via `/api/v1/admin/*` - see
  `blueprints/admin.py`. Disabling an account also invalidates any access
  token it already holds, not just its future logins/refreshes.
- **Data isolation:** every account's databases are private to that
  account - see "Data & backups" above.
- **CORS** is configurable via `settings.cfg`'s `ALLOWED_ORIGINS`
  (defaults to any port on `localhost`/`127.0.0.1`, i.e. local dev only).
  Add a real domain there before hosting this backend anywhere else.
- **TLS:** this backend never terminates TLS itself - it expects a reverse
  proxy in front doing that (see Option B in "Running it" above). Don't
  expose it directly to the internet without one; JWTs and credentials
  travel in plaintext over whatever transport fronts it otherwise. Option
  A (`python3 app.py`) has no TLS and no reverse proxy, so keep it off the
  open internet.

## License

Copyright (C) 2026 Jake Flanagan

Licensed under the GNU Affero General Public License v3.0 (AGPLv3). See
[`LICENSE`](LICENSE) for the full text. Anyone running a modified version
of this backend as a network service must make their modified source
available to users of that service.
