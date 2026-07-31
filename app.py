"""Sole-Trader Companion (STC) backend - Flask app entrypoint.

This file only wires things together: create the Flask app, hook up the
database connection lifecycle, register each feature's blueprint. All
actual route logic lives in blueprints/, and all database/schema/migration
logic lives in db.py and migrations.py - see those modules for the
interesting bits. This backend serves JSON only - no template rendering,
no static files - any frontend (web, desktop, mobile) talks to it purely
over the /api/v1/* HTTP API.
"""

import config

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS

import auth_db
import db
import migrations
import observability
from auth_middleware import check_auth
from rate_limit import limiter
from blueprints.admin import admin_bp
from blueprints.auth import auth_bp
from blueprints.dashboard import dashboard_bp
from blueprints.databases import auto_backup, databases_bp
from blueprints.expenses import expenses_bp
from blueprints.items import items_bp
from blueprints.settings_bp import settings_bp
from blueprints.tax import tax_bp
from blueprints.templates_bp import templates_bp

observability.configure_logging()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB - caps the only file-upload route (/api/v1/databases/import) against unbounded memory/disk use; every other route only reads JSON bodies
observability.init_sentry()
# Skipped under pytest, same reasoning as rate_limit.py's
# `limiter.enabled = not config.IS_TEST` right below - per-request JSON
# log lines are only useful in production, and would otherwise spam
# every test run's output for 270+ requests per test session. The hooks
# themselves are still covered directly in tests/test_observability.py.
if not config.IS_TEST:
    observability.init_request_logging(app)
observability.init_metrics(app)
# This backend is meant to sit behind exactly one reverse proxy doing TLS
# termination (see docker-compose.yml's Caddy service) - without ProxyFix,
# request.remote_addr on every request would be the proxy's own IP, not the
# real client's, which breaks rate_limit.py's per-IP throttling (every
# client would share one bucket) and any future IP-based logging/blocking.
# x_for=1 trusts exactly one hop of X-Forwarded-For, matching one proxy in
# front - raise it only if another trusted hop is added in front of that.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # type: ignore[method-assign]
db.init_app(app)  # hooks up close_db() to run at the end of every request
auth_db.init_app(app)  # hooks up close_auth_db() to run at the end of every request
limiter.init_app(app)  # per-IP throttling for blueprints/auth.py's register/login routes
# Must run after init_app (which needs enabled=True to set up its storage
# backend at all - see rate_limit.py's docstring) - this only flips the
# per-request enforcement flag, which flask-limiter re-reads on every
# request rather than caching from init_app time.
limiter.enabled = not config.IS_TEST

# Which origins may make cross-origin requests to this API - configurable
# via settings.cfg's ALLOWED_ORIGINS (see config.py's _parse_allowed_origins).
# Defaults to any port on localhost/127.0.0.1 (dev web frontend, future
# local webview frontends). Change ALLOWED_ORIGINS in settings.cfg to add
# a real domain when hosting this backend somewhere other than localhost.
CORS(app, origins=config.ALLOWED_ORIGINS)

# Every blueprint keeps the exact same relative paths the routes had
# before this app was split up, just reprefixed under /api/v1 (see
# blueprints/*.py's url_prefix).
for bp in (databases_bp, items_bp, templates_bp, settings_bp, dashboard_bp, tax_bp, expenses_bp, auth_bp, admin_bp):
    app.register_blueprint(bp)


# Every /api/v1/* route requires a valid access token except these -
# health must stay reachable pre-login, and you need to be able to
# register/login/refresh/logout before you have a token to prove who you
# are. check_auth() (auth_middleware.py) does the actual verification;
# this hook just decides which paths skip it.
_PUBLIC_PATHS = {
    "/api/v1/health",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/password-reset/request",
    "/api/v1/auth/password-reset/confirm",
    "/metrics",
}


@app.before_request
def _require_auth() -> tuple[Response, int] | None:
    # CORS preflight requests never carry an Authorization header (browsers
    # don't send one on OPTIONS), so gating them here would make flask-cors'
    # automatic preflight handling (which runs after before_request hooks)
    # unreachable - every authenticated cross-origin call from a browser
    # frontend would fail its preflight and never be sent at all.
    if request.method == "OPTIONS":
        return None
    if request.path in _PUBLIC_PATHS:
        return None
    return check_auth()


@app.errorhandler(HTTPException)
def handle_http_exception(e: HTTPException) -> tuple[Response, int]:
    """Turn Flask/Werkzeug's own HTML error pages (404s, 405s, aborts, ...)
    into JSON, so every response any frontend's fetch() ever receives is
    actually JSON."""
    return jsonify({"error": e.description}), e.code  # type: ignore[return-value]


@app.errorhandler(Exception)
def handle_unexpected_exception(e: Exception) -> tuple[Response, int]:
    """Catch-all for bugs that reach here unhandled - same JSON contract as
    handle_http_exception. Only fires with TESTING/DEBUG off, since Flask
    re-raises instead of routing here when either is on (see
    PROPAGATE_EXCEPTIONS) - exactly what's wanted: tests and local dev
    should still fail loudly."""
    app.logger.exception("Unhandled exception")
    return jsonify({"error": "Internal server error"}), 500


@app.before_request
def _daily_auto_backup() -> None:
    """Take today's daily snapshot of the current user's active database,
    if one hasn't been taken yet. Runs on every authenticated request -
    auto_backup() itself no-ops instantly once today's backup already
    exists, so this stays cheap. Skipped entirely for requests that never
    went through the auth gate (public paths, CORS preflight OPTIONS) -
    those have no g.user_id, so there's no per-user database to back up."""
    if "user_id" not in g:
        return
    auto_backup(g.user_id)


@app.route("/api/v1/health", methods=["GET"])
def health() -> Response:
    """Trivial liveness probe any frontend can call to check the backend is
    up and reachable before making real API calls."""
    return jsonify({"ok": True})


def bootstrap() -> None:
    """Run pending per-database migrations and ensure the admin account
    exists. Deliberately not called at plain import time - a test or a
    one-off script that imports `app` just to reuse a route function
    shouldn't have this side effect - so both callers below trigger it
    explicitly: the `__main__` block (for `python3 app.py`) and wsgi.py
    (for a production WSGI server like gunicorn, which imports `wsgi:app`
    directly and never runs this file's `__main__` block at all)."""
    migrations.run_migrations()
    with app.app_context():
        auth_db.ensure_admin_account(auth_db.get_auth_db(), config.ADMIN_USERNAME, config.ADMIN_PASSWORD)


if __name__ == "__main__":
    bootstrap()
    print("\n  Sole-Trader Companion (STC) backend")
    print(f"  http://{config.SC_HOST}:{config.SC_PORT}/api/v1\n")
    # The interactive debugger/auto-reloader are opt-in via SC_DEBUG=1 in
    # settings.cfg, not on by default, so a "just run it" install isn't
    # exposing Werkzeug's debugger (which allows arbitrary code execution)
    # out of the box.
    app.run(host=config.SC_HOST, port=config.SC_PORT, debug=config.SC_DEBUG)
