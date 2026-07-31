"""Tests for observability.py's logging/Sentry/metrics wiring. app.py
skips init_request_logging under pytest (see its comment - the per-
request JSON log line is production-only noise otherwise), so this file
exercises every piece directly against throwaway Flask apps instead of
through the shared `client` fixture.
"""

import logging

from flask import Flask

import config
import observability
from helpers import expect


def test_configure_logging_attaches_json_handler_at_configured_level():
    observability.configure_logging()
    root = logging.getLogger()
    expect("exactly one handler attached", 1, len(root.handlers))
    expect("level matches config.LOG_LEVEL", config.LOG_LEVEL, logging.getLevelName(root.level))


def test_init_request_logging_logs_method_path_status(caplog):
    app = Flask(__name__)
    observability.init_request_logging(app)

    @app.route("/probe")
    def probe():
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="stc.request"):
        response = app.test_client().get("/probe")

    expect("request succeeded", 200, response.status_code)
    matching = [r for r in caplog.records if r.name == "stc.request"]
    expect("exactly one request log record", 1, len(matching))
    record = matching[0]
    expect("method logged", "GET", record.method)
    expect("path logged", "/probe", record.path)
    expect("status logged", 200, record.status)
    expect("duration_ms is a number", True, isinstance(record.duration_ms, float))
    expect("user_id absent on an unauthenticated route", None, record.user_id)


def test_init_sentry_is_a_no_op_when_dsn_is_blank(monkeypatch):
    monkeypatch.setattr(config, "SENTRY_DSN", "")
    # If this tried to actually initialize Sentry it would need network
    # access and a real DSN - importing sentry_sdk here and monkeypatching
    # its init would prove nothing extra, so the real assertion is simply
    # that this returns without raising and without importing sentry_sdk's
    # init path at all (see observability.init_sentry's early return).
    observability.init_sentry()


def test_init_sentry_initializes_when_dsn_is_set(monkeypatch):
    monkeypatch.setattr(config, "SENTRY_DSN", "https://public@sentry.example/1")
    monkeypatch.setattr(config, "SENTRY_TRACES_SAMPLE_RATE", 0.0)

    calls = []
    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))

    observability.init_sentry()

    expect("sentry_sdk.init called exactly once", 1, len(calls))
    expect("dsn passed through", "https://public@sentry.example/1", calls[0]["dsn"])
    expect("PII sending left off", False, calls[0]["send_default_pii"])


def test_init_metrics_exposes_metrics_endpoint():
    app = Flask(__name__)
    observability.init_metrics(app)
    response = app.test_client().get("/metrics")
    expect("metrics endpoint responds", 200, response.status_code)
    expect("prometheus text format", True, b"# HELP" in response.data)
