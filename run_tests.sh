#!/bin/bash
# Convenience wrapper for the test suite. Equivalent to `pytest tests/ -v -s`
# (pytest.ini already sets those defaults, so plain `pytest` also works).
#
# Usage:
#   ./run_tests.sh                              # run everything
#   ./run_tests.sh tests/test_items_routes.py    # run one file
#   ./run_tests.sh tests/test_items_routes.py::test_create_item_defaults  # run one test
set -e
cd "$(dirname "$0")"
pytest "$@"
