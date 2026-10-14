"""
Short-lived, in-memory holder for freshly generated temporary passwords.

Passwords are NEVER written to the database (only their hashes are). To let an administrator
download the credentials right after creating accounts, the plaintext lives here for a few
minutes, keyed by an unguessable token and bound to the admin + school that created it. After
the TTL (or a process restart) it is gone for good; admins then use "Reset password".

Limitation: the store is per process. The documented deployment runs one gunicorn worker; with
several workers use the Redis-backed variant noted in the README.
"""

import secrets
import threading
import time

TTL_SECONDS = 15 * 60
_lock = threading.Lock()
_items = {}


def _purge(now):
    for token in [t for t, v in _items.items() if v["expires"] < now]:
        _items.pop(token, None)


def put(school_id: int, user_id: int, payload: dict) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _purge(now)
        _items[token] = {"school_id": school_id, "user_id": user_id, "expires": now + TTL_SECONDS, "payload": payload}
    return token


def get(token: str, school_id: int, user_id: int):
    """Payload dict or None (unknown, expired, or belongs to another admin/school)."""
    now = time.time()
    with _lock:
        _purge(now)
        item = _items.get(token)
        if not item or item["school_id"] != school_id or item["user_id"] != user_id:
            return None
        return item["payload"]


def drop_secrets(token: str, school_id: int, user_id: int):
    """Forget the plaintext passwords but keep the (password-free) summary."""
    with _lock:
        item = _items.get(token)
        if item and item["school_id"] == school_id and item["user_id"] == user_id:
            for r in item["payload"].get("results", []):
                r.pop("password", None)
            item["payload"]["secrets_cleared"] = True


def clear_all():  # tests
    with _lock:
        _items.clear()
