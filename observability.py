"""Structured logging, optional Sentry error tracking, and a /metrics
endpoint - the observability layer previously entirely missing (see
TODO.md history). Every piece here is either always-safe-by-default
(logging, metrics) or strictly opt-in via a blank-by-default settings.cfg
key (Sentry), same pattern as config.REDIS_URL - see config.py's comment
on each key.
"""

import logging
import sys
import time

from flask import Flask, Response, g, request
from pythonjsonlogger.json import JsonFormatter

import config

_JSON_FORMAT = "%(levelname)s %(message)s %(name)s %(asctime)s"


def configure_logging() -> None:
    """Route the root logger (and everything under it - Flask's
    app.logger, gunicorn's, any other module's module-level logger) to
    stdout as one-line JSON records. PYTHONUNBUFFERED (Dockerfile)
    already makes stdout reach `docker logs`/`docker compose logs`
    immediately; this makes each line machine-parseable by whatever
    collects those logs, instead of a bare message string with no
    consistent structure to grep/filter on. Level is config.LOG_LEVEL
    (default INFO). Replaces any existing handlers rather than adding to
    them, so re-importing app.py within the same process (every test
    file does) doesn't duplicate every log line per import."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(_JSON_FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(config.LOG_LEVEL)


def init_request_logging(app: Flask) -> None:
    """Log one structured line per request - method, path, status,
    duration, and the authenticated user id if there is one - the
    minimum an operator needs to reconstruct "what happened" without
    shelling into the box. g.get(...) (not g.<attr>) throughout: a
    request that never reaches app.py's auth gate (public paths, CORS
    preflight) has neither _request_start_monotonic nor user_id set, and
    this must never raise on those."""

    @app.before_request
    def _start_timer() -> None:
        g._request_start_monotonic = time.monotonic()

    @app.after_request
    def _log_request(response: Response) -> Response:
        started = g.get("_request_start_monotonic")
        duration_ms = round((time.monotonic() - started) * 1000, 2) if started is not None else None
        logging.getLogger("stc.request").info(
            "request",
            extra={
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "user_id": g.get("user_id"),
            },
        )
        return response


def init_sentry() -> None:
    """Wire in Sentry error tracking if config.SENTRY_DSN is set - a
    no-op otherwise (see config.py's SENTRY_DSN comment: blank by default,
    since this app has no way to provision an operator's own Sentry
    project for them). send_default_pii is left off: this app's request
    bodies can carry usernames and financial data, neither of which
    Sentry needs in order to capture a stack trace. Imported lazily
    (function-local, not module-level) so importing this module at all
    doesn't require sentry-sdk's transitive dependencies to be resolvable
    in an environment that never sets SENTRY_DSN in the first place."""
    if not config.SENTRY_DSN:
        return
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration

    sentry_sdk.init(
        dsn=config.SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=config.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
    )


def init_metrics(app: Flask) -> None:
    """Expose GET /metrics in Prometheus text format - request count and
    latency histograms, auto-instrumented per route. See app.py's
    _PUBLIC_PATHS, which excludes this path from the bearer-token auth
    gate the same way /health already is - a scrape target requiring a
    JWT isn't how Prometheus (or any scraper) is set up to work. Always
    on, no config key: unlike Sentry/SMTP this needs no external account
    to provision, and an exposed scrape endpoint nothing is scraping is
    inert - restrict network access to it at the reverse-proxy/firewall
    layer if it shouldn't be public, same as any other metrics endpoint."""
    from prometheus_flask_exporter import PrometheusMetrics

    PrometheusMetrics(app, path="/metrics")
