"""Tests for GET /api/v1/health - the liveness probe any frontend can call
before making real API calls."""

from helpers import expect


def test_health_returns_ok(client):
    resp = client.get("/api/v1/health")
    expect("status code", 200, resp.status_code)
    expect("response body", {"ok": True}, resp.get_json())


def test_health_does_not_crash_the_auto_backup_hook(client):
    # /health is a public path - app.py's _require_auth skips it entirely
    # (see _PUBLIC_PATHS), so g.user_id is never set for this request even
    # though this client normally carries a valid Authorization header.
    # app.py's _daily_auto_backup before_request hook (which runs on every
    # request) must not crash trying to back up a database with no
    # associated user.
    resp = client.get("/api/v1/health")
    expect("status code", 200, resp.status_code)
