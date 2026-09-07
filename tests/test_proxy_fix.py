"""Tests for app.py's ProxyFix wiring - this backend runs behind a reverse
proxy doing TLS termination (see docker-compose.yml), so
request.remote_addr must reflect the client's real IP from
X-Forwarded-For, not the proxy's own IP. Getting this wrong would silently
break rate_limit.py's per-IP throttling (see test_rate_limit.py) - every
client behind the proxy would share one bucket.
"""

import app as app_module
import config
from helpers import expect
from rate_limit import limiter
from werkzeug.middleware.proxy_fix import ProxyFix


def test_wsgi_app_wrapped_in_proxy_fix():
    expect("app.wsgi_app is wrapped in ProxyFix", True, isinstance(app_module.app.wsgi_app, ProxyFix))


def test_trusted_hop_count_comes_from_settings():
    """The hop count is a deployment fact the code cannot see, so it is a
    setting rather than a literal. This pins the wiring: if the two ever
    drift apart, a deployment that correctly sets TRUSTED_PROXY_HOPS would
    silently keep using the old hardcoded value."""
    expect(
        "ProxyFix trusts exactly the configured number of hops",
        config.TRUSTED_PROXY_HOPS,
        app_module.app.wsgi_app.x_for,
    )


def test_trusted_hop_count_defaults_to_one():
    """One proxy, matching the shipped docker-compose.yml where Caddy is
    the only thing in front. A default above 1 would let a client forge
    X-Forwarded-For and pick its own rate-limit bucket."""
    expect("default is a single trusted hop", 1, config.TRUSTED_PROXY_HOPS)


def test_rate_limit_key_follows_x_forwarded_for():
    """Two requests carrying different X-Forwarded-For client IPs must be
    throttled independently - if ProxyFix weren't wired in, both would
    collapse onto the test client's fixed loopback address and share one
    limiter bucket.

    Uses a different username for the second IP's request: auth_db.py's
    per-username login lockout (see tests/test_auth_routes.py) is
    deliberately keyed on username, not source IP, so reusing the same
    username here would make resp_b 429 for a reason unrelated to what this
    test checks (the per-IP limiter bucket, via ProxyFix) - a different
    username keeps this test isolated to that one mechanism."""
    limiter.reset()
    limiter.enabled = True
    try:
        c = app_module.app.test_client()
        last_status_a = None
        for _ in range(11):  # limit is "10 per minute"
            resp = c.post(
                "/api/v1/auth/login",
                json={"username": "nobody", "password": "wrong"},
                headers={"X-Forwarded-For": "10.0.0.1"},
            )
            last_status_a = resp.status_code
        expect("11th attempt from 10.0.0.1 is rate-limited", 429, last_status_a)

        resp_b = c.post(
            "/api/v1/auth/login",
            json={"username": "somebody-else", "password": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.2"},
        )
        expect("a different forwarded IP has its own, unspent bucket", 401, resp_b.status_code)
    finally:
        limiter.enabled = False
        limiter.reset()
