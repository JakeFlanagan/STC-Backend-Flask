"""Shared assertion helper used by every test instead of a bare `assert`.

Prints a verbose PASS/FAIL line with the expected and actual value for
*every* check (not just failures), then asserts so pytest still reports the
test as failed normally on a mismatch. This is what makes `pytest -v -s`
(the default via pytest.ini) show full expected-vs-actual detail for every
single test, not just a pass/fail summary.
"""


def expect(label, expected, actual):
    """Assert `expected == actual`, printing full verbose detail either way.

    `label` should describe what's being checked (e.g. "status code",
    "item name"), not restate the values - those are printed underneath.
    """
    ok = expected == actual
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}")
    print(f"    expected: {expected!r}")
    print(f"    actual:   {actual!r}")
    assert ok, f"{label}: expected {expected!r}, got {actual!r}"
