"""ERPNext REST client with cookie-based session auth.

Modern Frappe builds disable HTTP Basic Auth on /api/resource/ by default
and instead require either an API key+secret pair or a session cookie. We
go with session cookie — one login at startup, all subsequent requests on
the same `requests.Session`.

This module exposes a singleton session shared by both the grader and the
pre-seed code, so we authenticate exactly once per benchmark process.
"""

from __future__ import annotations

import os
import threading
import requests

ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
# Frappe's admin user is literally "Administrator". The --admin-password
# flag in docker-compose.yml sets THIS user's password (default "admin"),
# not a separate "admin" user. Override either via env vars.
ERPNEXT_USR = os.environ.get("ERPNEXT_USR", "Administrator")
ERPNEXT_PWD = os.environ.get("ERPNEXT_PWD", "admin")

_session: requests.Session | None = None
_session_base_url: str | None = None
_lock = threading.Lock()


def get_session(base_url: str = ERPNEXT_URL) -> requests.Session:
    """Return a cached, logged-in requests.Session for ERPNext.

    Subsequent calls reuse the same session object. If the base_url
    differs from the cached one (rare; harness only talks to one ERPNext),
    a fresh session is created.
    """
    global _session, _session_base_url
    with _lock:
        if _session is not None and _session_base_url == base_url:
            return _session

        s = requests.Session()
        # Frappe's /api/method/login expects form-encoded body.
        r = s.post(
            f"{base_url}/api/method/login",
            data={"usr": ERPNEXT_USR, "pwd": ERPNEXT_PWD},
            timeout=30,
        )
        if r.status_code >= 300:
            raise RuntimeError(
                f"ERPNext login failed: HTTP {r.status_code} — {r.text[:300]}"
            )
        _session = s
        _session_base_url = base_url
        return s
