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
with it via `POST /api/v1/auth/login`, then change it with
`POST /api/v1/auth/password` (see "Changing a password" below).

`ADMIN_PASSWORD` is cleared from `settings.cfg` automatically once the
account exists, and `ADMIN_BOOTSTRAPPED` is set to `1` to record that it
was. Putting a value back in `ADMIN_PASSWORD` afterwards does nothing:
`ensure_admin_account` only ever *creates* the account, it never resets
one.

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
```

That's enough to use it from the hosted web app: open
https://app.sole-traders.uk, tap the gear on the login screen and enter
`https://your-domain/api/v1`. The hosted app's origin is in
`ALLOWED_ORIGINS` by default. If you also host your own copy of the
frontend, add its origin too:

```bash
docker compose exec app sh -c \
  "sed -i 's|^ALLOWED_ORIGINS=.*|&,https://your-frontend.example.com|' /data/settings.cfg"
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

`docker compose up` runs **one** gunicorn worker with in-memory rate
limiting and no Redis container. That is the default on purpose, and it is
the only combination that is correct out of the box.

Rate limiting is the reason. `flask-limiter` falls back to in-memory
storage when `SC_REDIS_URL` is blank, and in-memory counters live in a
single process. Run four workers against them and every limit becomes
roughly four times looser and non-deterministic, depending on which worker
happens to answer. Worse, nothing warns you: the app starts cleanly and
serves traffic, it just is not enforcing the limits you think it is. A
default that is quietly wrong is worse than one that is merely slow, so
the default is one worker.

To scale up, turn on Redis and raise the worker count together. They are
one decision, not two:

```bash
cat > .env <<'EOF'
COMPOSE_PROFILES=scale
SC_REDIS_URL=redis://redis:6379/0
GUNICORN_WORKERS=4
EOF
docker compose up -d
```

`COMPOSE_PROFILES=scale` is what starts the `redis` service at all. A
compose file cannot enable its own profile, which is exactly why the safe
setting has to be the default rather than something a config file switches
on for you. `.env` is gitignored, so it is yours to write and it is never
shipped.

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
- **Off-site backups.** Backups are encrypted but live on the same host
  and volume as the live data, so losing that host loses them too. The
  `offsite-backup` profile adds a `snapshot` service (a consistent copy of
  everything, `settings.cfg` included) and a `backup-sync` service that
  pushes it to S3/R2/B2/GCS through an rclone `crypt` remote. See
  "Off-site backups" below for the full walkthrough, including how to
  restore.

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
| `ADMIN_PASSWORD` | auto-generated | Printed once to the console the first time the app starts with no existing `settings.cfg`, then cleared from the file once the account exists. Only ever used to create the admin account, never to reset it. |
| `ADMIN_BOOTSTRAPPED` | `0` | Set to `1` automatically once the admin account exists. Stops a cleared `ADMIN_PASSWORD` being read as "never generated" and re-minted on the next start-up. |
| `ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*,https://app.sole-traders.uk` | Comma-separated CORS origins. The hosted web app is included so it works with your server out of the box. Add your own frontend's origin if you host one. |
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
| `TRUSTED_PROXY_HOPS` | `1` | How many reverse proxies in front of this backend are trusted to have appended to `X-Forwarded-For`. Too low and every client shares one rate-limit bucket; too high and a client can forge its own address. |

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
database, so losing that host loses the backups too. Two opt-in services
in the `offsite-backup` profile fix that: `snapshot` takes a consistent
copy of everything, and `backup-sync` pushes it to a remote of your choice
via [rclone](https://rclone.org). Neither starts on a plain
`docker compose up`, because the second one needs your own storage and
credentials that this project cannot provision for you.

**What a snapshot contains.** Everything under `SC_DATA_DIR`:
`settings.cfg`, `auth.db`, every user's live `.db` file, and the encrypted
`backups/` archives. Every database is copied with SQLite's online backup
API rather than read off disk, so a snapshot taken while people are using
the app is a complete database rather than whatever the file looked like
partway through a write.

`settings.cfg` is in there on purpose, and it is the part that matters
most. The `backups/` archives are encrypted with `DB_ENCRYPTION_KEY`, and
`DB_ENCRYPTION_KEY` lives in `settings.cfg`. An off-site copy without it
is a pile of ciphertext with the key left behind on the machine you were
protecting against.

**Encrypt on the way out.** A snapshot holds `JWT_SECRET`,
`DB_ENCRYPTION_KEY` and every user's data in the clear. That is fine on
the host, which already has all of it in the clear, and unacceptable in
someone else's bucket. Point `RCLONE_REMOTE` at an rclone `crypt` remote
wrapping your storage remote, not at the storage remote directly. See
`rclone.conf.example` for the two-remote layout.

```bash
# 1. Build a config from the example, or generate one interactively:
cp rclone.conf.example rclone.conf   # then fill in your credentials
#   ...or: rclone config && cp ~/.config/rclone/rclone.conf ./rclone.conf

# 2. Point the sync at the CRYPT remote and start both services:
cat > .env <<'EOF'
RCLONE_REMOTE=stc-encrypted:
SNAPSHOT_INTERVAL_SECONDS=21600
BACKUP_SYNC_INTERVAL_SECONDS=21600
SNAPSHOT_KEEP=7
EOF
docker compose --profile offsite-backup up -d snapshot backup-sync
```

`SNAPSHOT_KEEP` is local disk only. That directory is a staging area; the
retention that matters is whatever you set on the remote, because that is
the copy that survives losing this host.

**Write the crypt passphrases down somewhere else.** They are in
`rclone.conf`, on the machine the backups exist to survive. If the host
dies you will have an encrypted bucket and no way into it, which is worse
than having no backup, because you spent the intervening months believing
you had one.

**Restoring.**

```bash
# 1. Fetch the newest snapshot through the same crypt remote:
rclone lsf stc-encrypted: | sort | tail -1
rclone copy "stc-encrypted:stc-20260907-041500.tar.gz" ./restore/

# 2. Unpack it:
tar -xzf ./restore/stc-*.tar.gz -C ./restore/

# 3. Check it before you trust it. Every .db should say "ok":
for f in $(find ./restore/stc -name '*.db'); do
    echo "$f: $(sqlite3 "$f" 'PRAGMA integrity_check;')"
done

# 4. Load it into a fresh volume and start up:
docker compose up -d app
docker compose cp ./restore/stc/. app:/data/
docker compose restart app
```

Test this before you need it. A backup nobody has restored is a
hypothesis, not a backup - and the failure you are guarding against is
exactly the situation in which you will least want to be finding out.

`docker compose logs -f snapshot backup-sync` shows each run. If
`RCLONE_REMOTE` is unset, the sync logs a clear warning rather than
silently doing nothing or crash-looping.

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

## Choosing a database per request

Each account can have several databases. Which one a request runs against
is chosen with a header:

```
X-STC-Database: clearout
```

Send it on every request. If it is absent, the request falls back to the
account's `.active_db` marker (set by `POST /api/v1/databases/switch`), so
a client that does not know about the header keeps working unchanged.

**Why a header rather than server-side state.** `.active_db` is per-user,
stored in the user's own directory, so every tab that user has open shares
it. Switching database in one tab silently switched it under the others,
mid-edit. That is not a bug in how the marker is stored - any "current
database" the server holds is shared by definition. The only fix is to
make the database a property of the request.

**Why a header rather than a path segment.** `/api/v1/db/<name>/items`
would mean touching every route in every blueprint to get the same result
a header gets by being read in one place.

The header says *which* database, never *whose*. The name is validated as
a filename and then looked for inside the caller's own directory, so
naming another account's database returns `404` for the same reason naming
a database that never existed does - there is nothing of anyone else's in
that directory to find. An invalid name is a `400` before any path is
built from it.

**Frontend note:** if you keep the selection in browser storage, use
`sessionStorage`, not `localStorage`. `localStorage` is shared across
every tab on an origin, which recreates the exact bug this header exists
to fix, one layer up.

## Changing a password

`POST /api/v1/auth/password {"current_password": "...", "new_password": "..."}`,
authenticated. Returns `200` on success.

The current password is required even though the request already carries a
valid access token. A token can be stolen; without this check, a stolen one
could be traded for permanent ownership of the account. The route is rate
limited to 10 attempts an hour for the same reason - otherwise it would be
a password-guessing oracle that bypasses the per-username login lockout.

Every session is ended on success, including the caller's own, so the
response is a `200` and then a fresh login rather than a new token pair.
Handing back new tokens here would be friendlier and also unreliable:
`check_auth` compares a token's `iat` against `tokens_revoked_at` using
`<=`, and both have one-second resolution, so a token minted in the same
second as the revoke is rejected anyway.

If the current password has been forgotten rather than merely changed, use
the reset flow below instead.

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

Set `APP_BASE_URL` to your real frontend's origin, and
`PASSWORD_RESET_PATH` to the path on it that handles the link. The
emailed link is `{APP_BASE_URL}{PASSWORD_RESET_PATH}?token=...`, so both
halves are configuration: this backend is JSON-only (see `app.py`'s
docstring), the reset *page* is the frontend's responsibility, and its
router decides the path. `PASSWORD_RESET_PATH` defaults to
`/reset-password`; a frontend that names the route something else sets
this rather than every user landing on a 404.

## Administration

Everything under `/api/v1/admin/*` requires an access token whose `role`
claim is `admin` (`blueprints/admin.py`'s `_require_admin`, checked on top
of the ordinary auth gate). A non-admin gets a 403; an unauthenticated
caller gets a 401 from the global gate before the role check is reached.

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/admin/users` | Every account, each with its live session count. |
| POST | `/admin/users/<id>/disable` | Block future logins and reject the access token it already holds. |
| POST | `/admin/users/<id>/enable` | Undo the above. |
| DELETE | `/admin/users/<id>` | Delete the account. Its files are orphaned, not deleted (see below). |
| POST | `/admin/users/<id>/password` | Set a password without knowing the old one, ending every session on that account and clearing its lockout. |
| DELETE | `/admin/users/<id>/sessions` | End every session without changing the password. |
| GET | `/admin/lockouts` | Usernames currently locked out of login. |
| DELETE | `/admin/lockouts/<username>` | Clear one lockout and its failure count. |
| GET | `/admin/system` | Runtime, disk and account figures. Secret-free by construction - see `system_info.py`. |
| GET | `/admin/storage` | Every database file on disk, by account, plus orphans and legacy files. |
| POST | `/admin/storage/<user_id>/<name>/backup` | An encrypted backup of any account's database, landing in that account's own backups. |
| DELETE | `/admin/storage/orphans/<user_id>` | Permanently delete the files of an account that no longer exists. |

Three of these deserve reading twice before use.

`POST /admin/users/<id>/password` exists because `SMTP_HOST` is optional:
with no mail configured the entire password-reset flow is a silent no-op,
and this is the only remaining way to recover an account without editing
`auth.db` by hand. It is refused for the caller's own account -
`/auth/password` already does that properly, by asking for the current
password first.

`DELETE /admin/storage/orphans/<user_id>` is the only route in this
backend that destroys somebody's data, and it does not move the files
aside first. It refuses any id that still has an account, so it can only
ever touch what `DELETE /admin/users/<id>` already orphaned - which is
what makes "I deleted the wrong user" recoverable right up until this
call.

`POST /admin/storage/<user_id>/<name>/backup` writes into the target
user's own backup directory, encrypted with `DB_ENCRYPTION_KEY` like every
other backup. The admin who pressed the button cannot read the result;
this is a safety net to take before doing something irreversible, not an
export of someone else's books.

`GET /admin/system` reports the running commit as `runtime.build_sha`,
read from the `SC_BUILD_SHA` env var that the Dockerfile bakes in at build
time. A local checkout reports `unknown`, which is the honest answer.

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
  `/auth/password` also requires auth, on top of the current password.
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
  manage other accounts, sessions, lockouts and stored data via
  `/api/v1/admin/*` - see "Administration" above and `blueprints/admin.py`.
  Disabling an account also invalidates any access token it already holds,
  not just its future logins/refreshes. There is no way to *become* an
  admin through the API: `/auth/register` always creates a `user`-role
  account, and no route changes a role.
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
