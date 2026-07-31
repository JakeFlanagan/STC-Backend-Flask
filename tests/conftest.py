"""Pytest session bootstrap: points the whole app at a throwaway temp
directory before anything imports it, so a test run can never touch the
real databases/ (which now also holds backups/, nested under it) or
.active_db under the project root.

How the isolation works:
  1. This file runs once, before pytest collects/imports any test module.
  2. It creates one temp directory for the entire pytest session and sets
     SC_TEST_DATA_DIR to it *before* `import app` - config.py reads that env
     var when it first runs (see config.py's BASE_DIR), so DB_DIR (and every
     per-user subdirectory created under it) already points into the temp
     directory by the time any other module sees them.
  3. Because Python caches imported modules, this only needs to happen once
     per pytest process - every test module importing `app`/`config`/`db`
     gets the same already-isolated instances.
  4. Individual tests still don't share data: the `client` fixture below
     gives every test function its own uniquely-named database within that
     shared temp directory, so tests can run in any order without
     interfering with each other.
"""

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

_SESSION_DIR = tempfile.mkdtemp(prefix="sellers_companion_pytest_")
os.environ["SC_TEST_DATA_DIR"] = _SESSION_DIR

# tests/ has no __init__.py, so pytest already puts this directory on
# sys.path - but app.py lives one level up, so add the project root too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module  # noqa: E402  (must come after the env var is set)
import auth_middleware  # noqa: E402

app_module.app.testing = True


@pytest.fixture(scope="session", autouse=True)
def _cleanup_session_dir():
    """Delete the whole throwaway temp directory once, after every test in
    the session has finished."""
    yield
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)


@pytest.fixture
def client():
    """A Flask test client with its own brand-new, uniquely-named active
    database, authenticated as its own throwaway user. Every test function
    gets a fresh one, so tests never see each other's data and can run in
    any order (including standalone, e.g.
    `pytest tests/test_items_routes.py::test_create_item_defaults`).

    Every route except /health and /auth/* now requires a valid access
    token (see app.py's _require_auth) - registering a user here and
    setting a default Authorization header via environ_base means every
    existing test file's client.get/post/... calls keep working unchanged,
    without editing every call site individually.

    The registered user's id is stashed on `client.user_id`, for tests
    that need to call db.py functions directly (they take a user_id
    parameter - see db.py's per-user storage layer) rather than only
    through the HTTP API.
    """
    test_client = app_module.app.test_client()

    username = f"fixtureuser_{uuid.uuid4().hex[:12]}"
    auth_resp = test_client.post(
        "/api/v1/auth/register", json={"username": username, "password": "fixture-password-123"}
    )
    assert auth_resp.status_code == 201, f"fixture auth setup failed: {auth_resp.get_json()}"
    access_token = auth_resp.get_json()["access_token"]
    test_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

    payload = auth_middleware.decode_access_token(access_token)
    assert payload is not None
    test_client.user_id = payload["user_id"]

    db_name = f"test_{uuid.uuid4().hex[:12]}"
    resp = test_client.post("/api/v1/databases", json={"name": db_name})
    assert resp.status_code == 200, f"fixture setup failed: {resp.get_json()}"
    return test_client
