"""Tests for app.py's global error handlers (handle_http_exception,
handle_unexpected_exception) - both exist so the frontend's api() helper,
which always calls r.json() on every response, never has to parse an HTML
error page as JSON."""

import app as app_module
from helpers import expect


def test_404_on_unknown_route_returns_json(client):
    resp = client.get("/api/v1/this-route-does-not-exist")
    expect("status code", 404, resp.status_code)
    expect("content type is JSON", "application/json", resp.content_type.split(";")[0])
    expect("error key present", True, "error" in resp.get_json())


def test_unhandled_exception_returns_json_500(client, monkeypatch):
    """handle_unexpected_exception only fires with TESTING/PROPAGATE_EXCEPTIONS
    off (see its docstring) - conftest.py sets app.testing = True for every
    other test, so this one temporarily flips both off, and patches an
    existing endpoint to raise, to reach that handler deliberately."""
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setitem(app_module.app.view_functions, "items.list_items", boom)
    monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)
    monkeypatch.setattr(app_module.app, "testing", False)

    resp = client.get("/api/v1/items")
    expect("status code", 500, resp.status_code)
    expect("content type is JSON", "application/json", resp.content_type.split(";")[0])
    expect("error key present", True, "error" in resp.get_json())


def test_auto_backup_runs_before_every_request(client, monkeypatch):
    """auto_backup() used to run only from the (now-removed) "/" route;
    it's called from app.py's before_request hook instead now, so any
    authenticated request should trigger it. (Public paths like /health
    are skipped - see app.py's _daily_auto_backup - since there's no
    g.user_id there to back up a database for.)"""
    import app as app_module

    calls = []
    monkeypatch.setattr(app_module, "auto_backup", lambda user_id: calls.append(user_id))

    client.get("/api/v1/items")

    expect("auto_backup called", True, len(calls) >= 1)
