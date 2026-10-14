"""Append-only audit logging; every row carries the school it happened in."""

import json

from flask import g, request

from database.db import get_db


def log_action(action: str, resource_type: str = None, resource_id: int = None,
               previous_value: dict = None, new_value: dict = None, reason: str = None,
               school_id: int = None):
    db = get_db()
    user = g.get("user")
    db.execute(
        """INSERT INTO audit_logs (school_id, user_id, action, resource_type, resource_id,
                                    previous_value, new_value, reason, ip_address)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            school_id if school_id is not None else g.get("school_id"),
            user["id"] if user is not None else None,
            action, resource_type, resource_id,
            json.dumps(previous_value, default=str) if previous_value is not None else None,
            json.dumps(new_value, default=str) if new_value is not None else None,
            reason, request.remote_addr if request else None,
        ),
    )
    db.commit()
