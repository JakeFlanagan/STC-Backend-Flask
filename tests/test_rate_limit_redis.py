"""Exercises rate_limit.py's Redis storage path (config.REDIS_URL /
SC_REDIS_URL set) against a real Redis instance. Without this, only
_storage_uri()'s selection logic would be covered, never the actual
shared-counter behavior multiple gunicorn workers rely on.

Skipped entirely if no Redis is reachable at TEST_REDIS_URL (defaults to
redis://localhost:6379/0) - CI's tests.yml starts one as a service
container; local runs without Redis installed just skip this file, same
as any other environment-dependent integration test.

Builds two independent Flask apps, each with their own Limiter pointed at
the same Redis URL and the same fixed (non-IP-based) key - standing in
for two separate gunicorn worker processes that would otherwise each get
an independent in-memory counter. If the limit trips on the *second*
app after only exhausting it on the *first*, the counter is proven to be
shared through Redis, not per-process.
"""

import os

import pytest
from flask import Flask
from flask_limiter import Limiter

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/0")


def _redis_reachable() -> bool:
    try:
        import redis

        redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_reachable(), reason=f"no Redis reachable at {TEST_REDIS_URL}"
)


def _make_app(shared_key: str) -> tuple[Flask, Limiter]:
    app = Flask(__name__)
    app.testing = True
    limiter = Limiter(
        key_func=lambda: shared_key,
        default_limits=[],
        storage_uri=TEST_REDIS_URL,
        app=app,
    )

    @app.route("/probe")
    @limiter.limit("2 per minute")
    def probe():
        return {"ok": True}

    return app, limiter


def test_redis_backed_counter_is_shared_across_independent_processes():
    shared_key = "shared-worker-simulation-key"
    app_one, _ = _make_app(shared_key)
    app_two, _ = _make_app(shared_key)
    client_one = app_one.test_client()
    client_two = app_two.test_client()

    assert client_one.get("/probe").status_code == 200
    assert client_one.get("/probe").status_code == 200
    # The limit (2/minute) is already exhausted via app_one - app_two,
    # simulating a second gunicorn worker, must see the same Redis-backed
    # count and reject immediately, proving the counter isn't process-local.
    assert client_two.get("/probe").status_code == 429
