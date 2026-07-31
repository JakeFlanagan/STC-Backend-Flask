# Test suite

Covers the whole Flask backend: all 29 routes across the 6 blueprints, the
3 migrations, and db.py's core behavior (schema seeding, default settings,
active-database tracking). **64 tests total.**

## Running

```bash
pip install -r requirements-dev.txt   # installs pytest, once

./run_tests.sh                                   # everything
./run_tests.sh tests/test_items_routes.py         # one file
./run_tests.sh tests/test_items_routes.py::test_create_item_defaults   # one test
```

`pytest.ini` already sets verbose + uncaptured output (`-v -s`) as the
default, so plain `pytest` from the project root works the same as
`./run_tests.sh`.

## Output format

Every check prints its expected and actual value, not just failures:

```
tests/test_items_routes.py::test_create_item_defaults
[PASS] status code
    expected: 201
    actual:   201
[PASS] default primary_status
    expected: 'Not listed'
    actual:   'Not listed'
PASSED
```

A failing check looks the same but with `[FAIL]`, and pytest additionally
reports the assertion as a normal test failure. The final line of any run
gives the overall count: `N passed in Xs` (or `N passed, M failed`).

## How tests stay isolated from your real data

Every path the app uses (`databases/` - which also holds `databases/backups/`
- and `.active_db`) is derived from `config.BASE_DIR`. `tests/conftest.py`
sets the `SC_TEST_DATA_DIR`
environment variable to a fresh temp directory *before* anything imports
`app`/`config`/`db`, so an entire pytest run - however many tests, in
whatever order - only ever touches that throwaway directory. It's deleted
automatically when the run finishes. **Your real `clearout.db` and friends
are never opened by the test suite.**

Within that shared temp directory, every individual test gets its own
uniquely-named database (via the `client` fixture in `conftest.py`), so
tests never see each other's data even though they share one process.

The one exception is `test_migrations.py`, which calls the `migrate_*()`
functions directly - those glob *every* `.db` file in their directory, so
those specific tests use their own private `tmp_path` per test (see the
`isolated_db_dir` fixture there) rather than the shared session directory.

## What's not covered here

This is the backend suite only (pytest, `tests/`). The pure-logic parts of
`static/js/` (no DOM dependency - date/currency formatting, badge/pipeline
logic) have their own Vitest suite instead - see
`static/js/__tests__/README.md`. Everything else in `static/js/` (rendering,
event wiring, the inline-handler/`window` pattern in `main.js`) still has no
automated coverage - that would need a real browser (Playwright), not a
plain-Node test runner.
