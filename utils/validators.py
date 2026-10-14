"""Server-side input validation helpers. Never trust client-side checks alone."""

import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    return bool(value) and bool(EMAIL_RE.match(value.strip()))


def is_non_empty(value: str) -> bool:
    return bool(value) and bool(value.strip())


def is_valid_password(value: str, min_length: int = 8) -> bool:
    return bool(value) and len(value) >= min_length


def validate_required_fields(data: dict, fields: list[str]) -> list[str]:
    """Return a list of error messages for any missing/blank required fields."""
    errors = []
    for field in fields:
        if not is_non_empty(data.get(field, "")):
            errors.append(f"{field.replace('_', ' ').title()} is required.")
    return errors
