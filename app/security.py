"""Password hashing + verification.

Direct bcrypt usage. Passlib is officially unmaintained and broken against
bcrypt 5.x, so we skip it.

bcrypt only uses the first 72 bytes of the input password. We enforce that
limit at the input boundary (form validation) rather than silently truncating.
"""
from __future__ import annotations

import bcrypt

MAX_PASSWORD_BYTES = 72


class PasswordTooLongError(ValueError):
    """Raised when a password exceeds bcrypt's 72-byte hard limit."""


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"Password is {len(raw)} bytes; bcrypt only supports up to {MAX_PASSWORD_BYTES}."
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
