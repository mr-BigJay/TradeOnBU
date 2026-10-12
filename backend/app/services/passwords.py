from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def password_strength_ok(password: str) -> tuple[bool, str]:
    if len(password) < 10:
        return False, "Password must be at least 10 characters"
    if not re.search(r"[A-Za-z]", password):
        return False, "Password must include a letter"
    if not re.search(r"\d", password):
        return False, "Password must include a digit"
    return True, ""
