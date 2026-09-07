"""Tests for choosing a database per REQUEST via the X-STC-Database
header, rather than per USER via the server-side .active_db marker.

The bug this closes: .active_db lives in the user's own directory, so it
is shared by every tab that user has open. Switching database in one tab
silently switched it under the others, mid-edit. Any server-held "current
database" has that property by definition, which is why the fix is to make
the database a property of the request instead of state the server keeps.

Cross-USER isolation was never the problem and is covered in
tests/test_data_isolation.py. What is checked here is that the new header
does not become a way around it.
"""

import uuid

import app as app_module
from helpers import expect


def _authenticated_client():
    """A fresh test client authenticated as its own throwaway user."""
    c = app_module.app.test_client()
    username = f"hdruser_{uuid.uuid4().hex[:12]}"
    resp = c.post("/api/v1/auth/register", json={"username": username, "password": "header-password-1"})
    assert resp.status_code == 201
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {resp.get_json()['access_token']}"
    return c


def _add_item(c, name, headers=None):
    return c.post("/api/v1/items", json={"name": name}, headers=headers or {})


def _item_names(c, headers=None):
    body = c.get("/api/v1/items", headers=headers or {}).get_json()
    items = body["items"] if isinstance(body, dict) else body
    return sorted(i["name"] for i in items)


def test_two_headers_in_one_test_see_different_data():
    """The whole point. Two requests from the same authenticated user,
    differing only by header, must land in different databases - that is
    what makes two tabs independent."""
    c = _authenticated_client()
    c.post("/api/v1/databases", json={"name": "tab_one"})
    c.post("/api/v1/databases", json={"name": "tab_two"})

    _add_item(c, "only_in_one", headers={"X-STC-Database": "tab_one"})
    _add_item(c, "only_in_two", headers={"X-STC-Database": "tab_two"})

    expect("tab_one sees only its own item", ["only_in_one"], _item_names(c, {"X-STC-Database": "tab_one"}))
    expect("tab_two sees only its own item", ["only_in_two"], _item_names(c, {"X-STC-Database": "tab_two"}))


def test_header_does_not_change_the_active_database():
    """A header names a database for one request. It must not write
    .active_db, or one tab's read would still reach across and change what
    another tab gets next - the original bug, reintroduced by the fix."""
    c = _authenticated_client()
    c.post("/api/v1/databases", json={"name": "stays_active"})
    c.post("/api/v1/databases", json={"name": "just_visiting"})
    c.post("/api/v1/databases/switch", json={"name": "stays_active"})

    _add_item(c, "written_via_header", headers={"X-STC-Database": "just_visiting"})

    active = c.get("/api/v1/databases").get_json()["active"]
    expect("the active database is untouched", "stays_active", active)
    expect("and a headerless request still reads it", [], _item_names(c))


def test_absent_header_falls_back_to_the_active_database():
    """An older client that sends no header has to keep working exactly as
    it did, or this becomes a breaking change for every deployed frontend
    at once."""
    c = _authenticated_client()
    c.post("/api/v1/databases", json={"name": "fallback_db"})
    c.post("/api/v1/databases/switch", json={"name": "fallback_db"})

    _add_item(c, "no_header_item")

    expect("the item landed in the active database", ["no_header_item"], _item_names(c, {"X-STC-Database": "fallback_db"}))


def test_naming_another_users_database_is_a_404():
    """The header is trusted to say WHICH database, never WHOSE. The
    existence check happens inside the caller's own directory, so someone
    else's database is indistinguishable from one that never existed."""
    alice = _authenticated_client()
    bob = _authenticated_client()
    alice.post("/api/v1/databases", json={"name": "alices_ledger"})

    resp = bob.get("/api/v1/items", headers={"X-STC-Database": "alices_ledger"})
    expect("status code", 404, resp.status_code)


def test_another_users_database_is_not_merely_hidden_but_unreachable():
    """Confirms the 404 above is a refusal rather than an empty read: if
    the header were honoured before the ownership check, bob would get
    alice's rows back with a 200."""
    alice = _authenticated_client()
    bob = _authenticated_client()
    alice.post("/api/v1/databases", json={"name": "alices_stock"})
    _add_item(alice, "alices_secret_item", headers={"X-STC-Database": "alices_stock"})

    resp = bob.get("/api/v1/items", headers={"X-STC-Database": "alices_stock"})
    expect("bob is refused", 404, resp.status_code)
    expect("and gets none of alice's data", False, "alices_secret_item" in resp.get_data(as_text=True))


def test_a_traversal_attempt_in_the_header_is_rejected():
    """Database names become filenames, so this is the check that keeps a
    name inside the user's own directory. Rejected as malformed at the
    header, before anything builds a path out of it."""
    c = _authenticated_client()
    for hostile in ["../../etc/passwd", "..", "../other_user/secrets", "a/b", "with space", ""]:
        resp = c.get("/api/v1/items", headers={"X-STC-Database": hostile})
        expect(f"{hostile!r} is rejected", 400, resp.status_code)


def test_header_naming_a_database_that_does_not_exist_is_a_404():
    c = _authenticated_client()
    c.post("/api/v1/databases", json={"name": "real_db"})
    resp = c.get("/api/v1/items", headers={"X-STC-Database": "no_such_db"})
    expect("status code", 404, resp.status_code)


def test_unauthenticated_request_with_a_header_is_still_a_401():
    """The database hook runs after the auth gate, so an anonymous caller
    is turned away before anything reads the header. If the ordering ever
    inverted, this would start returning 400 or 404 - both of which would
    confirm the existence of a database to someone with no account."""
    c = app_module.app.test_client()
    resp = c.get("/api/v1/items", headers={"X-STC-Database": "anything"})
    expect("status code", 401, resp.status_code)
