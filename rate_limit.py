"""Per-IP request throttling for the unauthenticated endpoints that are
reachable with no proof of identity at all (register/login) - the ones
brute-force and username-enumeration attempts actually hit. A single
Limiter instance, wired into the app once (see app.py's init_app call);
blueprints/auth.py imports it directly to decorate the routes that opt in.

Keyed per-IP, not per-username: per-username throttling would need a
persistent store keyed on attacker-controlled input (an attacker can always
supply a different username), while per-IP is what actually bounds a single
attacker's request rate regardless of which username(s) they're trying.
(blueprints/auth.py's login() separately layers a per-username lockout on
top of this, via auth_db.record_login_failure - that one targets a
distributed attacker hammering one username from many IPs, which per-IP
throttling alone can't slow down.)

Storage is in-memory (config.REDIS_URL blank, the default) unless
REDIS_URL is set in settings.cfg - in-memory only works correctly with a
single gunicorn worker (see Dockerfile's GUNICORN_WORKERS default of 1),
since each worker process would otherwise get its own, independent
counters. Setting REDIS_URL shares counters across any number of workers/
processes - see README's "Scaling beyond one worker".

Disabled outright under pytest (config.IS_TEST, applied by app.py right
after init_app - see its comment for why it can't be passed in here at
construction time) - the test suite's `client` fixture calls
/auth/register once per test function, hundreds of times per run, all from
the same test-client "IP"; without this it would trip the register limit
almost immediately and fail unrelated tests. Tests that exercise the
limiter itself flip `limiter.enabled` on directly (see
tests/test_rate_limit.py).
"""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import config


def _storage_uri() -> str:
    """redis:// if config.REDIS_URL is set, else flask-limiter's
    in-memory default - see this module's docstring."""
    return config.REDIS_URL or "memory://"


limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri=_storage_uri())
