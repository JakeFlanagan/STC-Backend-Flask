"""Tests for the per-IP throttling on /auth/register and /auth/login (see
rate_limit.py). The limiter is disabled under pytest by default (every
other test file relies on unlimited register/login calls) - these tests
flip it on for their own duration only, and reset the in-memory counters
both before and after so they don't interfere with each other or leak a
tripped limit into any other test.
"""

import uuid

import app as app_module
import rate_limit
from helpers import expect
from rate_limit import limiter


def _unique_username() -> str:
    return f"ratelimituser_{uuid.uuid4().hex[:12]}"


def _with_limiting_enabled():
    """Context manager-free setup/teardown pair, used via a fixture-style
    try/finally in each test below so a failing assertion still restores
    limiter.enabled=False and clears counters for the next test."""
    limiter.reset()
    limiter.enabled = True


def _restore_limiting_disabled():
    limiter.enabled = False
    limiter.reset()


def test_login_rate_limited_after_threshold():
    _with_limiting_enabled()
    try:
        c = app_module.app.test_client()
        username = _unique_username()
        c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})

        last_status = None
        for _ in range(11):  # limit is "10 per minute"
            resp = c.post(
                "/api/v1/auth/login", json={"username": username, "password": "wrong-password"}
            )
            last_status = resp.status_code
        expect("11th login attempt within a minute is rate-limited", 429, last_status)
    finally:
        _restore_limiting_disabled()


def test_login_under_threshold_not_rate_limited():
    _with_limiting_enabled()
    try:
        c = app_module.app.test_client()
        username = _unique_username()
        c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})

        resp = c.post("/api/v1/auth/login", json={"username": username, "password": "password123"})
        expect("login under the limit succeeds normally", 200, resp.status_code)
    finally:
        _restore_limiting_disabled()


def test_register_rate_limited_after_threshold():
    _with_limiting_enabled()
    try:
        c = app_module.app.test_client()

        last_status = None
        for _ in range(6):  # limit is "5 per hour"
            resp = c.post(
                "/api/v1/auth/register",
                json={"username": _unique_username(), "password": "password123"},
            )
            last_status = resp.status_code
        expect("6th registration within an hour is rate-limited", 429, last_status)
    finally:
        _restore_limiting_disabled()


def test_storage_uri_defaults_to_memory_when_redis_url_unset(monkeypatch):
    monkeypatch.setattr(rate_limit.config, "REDIS_URL", "")
    expect("blank REDIS_URL uses in-memory storage", "memory://", rate_limit._storage_uri())


def test_storage_uri_uses_redis_url_when_set(monkeypatch):
    monkeypatch.setattr(rate_limit.config, "REDIS_URL", "redis://redis:6379/0")
    expect("set REDIS_URL is used as-is", "redis://redis:6379/0", rate_limit._storage_uri())
