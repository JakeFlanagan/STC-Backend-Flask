"""Test for wsgi.py - the entrypoint a production WSGI server (gunicorn/
waitress) imports instead of running `python3 app.py`, which never
executes app.py's `__main__` block (see app.py's bootstrap() docstring).
Confirms importing wsgi actually runs that bootstrap (migrations +
admin-account creation) rather than silently skipping it.
"""

import app as app_module
import auth_db
import config
from helpers import expect


def test_importing_wsgi_runs_bootstrap_and_creates_admin_account():
    import wsgi

    expect("wsgi.app is the same Flask app", app_module.app, wsgi.app)

    with app_module.app.app_context():
        db = auth_db.get_auth_db()
        row = db.execute(
            "SELECT role FROM users WHERE username = ?", (config.ADMIN_USERNAME,)
        ).fetchone()
    expect("admin account exists after importing wsgi", "admin", row["role"] if row else None)
