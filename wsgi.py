"""WSGI entrypoint for a production server (gunicorn/waitress) that imports
`app:app` directly instead of running `python3 app.py`. That import path
never executes app.py's `if __name__ == "__main__":` block, so this module
runs app.py's bootstrap() (migrations + admin-account creation) itself, at
import time, before the server starts accepting requests.

Point the WSGI server at `wsgi:app`, not `app:app`, to pick this up, e.g.:
    gunicorn -w 1 -b 0.0.0.0:5000 wsgi:app
(see the Dockerfile, which does exactly this).
"""

from app import app, bootstrap

bootstrap()

__all__ = ["app"]
