"""User store for human auth.

Users live in ``PRIVATE_DIR/users.json`` (git-ignored), each with a PBKDF2
password hash, a TOTP secret for 2FA, a role, and explicit entitlements.
Passwords are hashed with stdlib ``hashlib.pbkdf2_hmac`` (no extra dependency).

Manage users with ``scripts/manage_users.py``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from app.config import PRIVATE_DIR

USERS_FILE = Path(os.getenv("USERS_FILE", str(PRIVATE_DIR / "users.json")))
_PBKDF2_ROUNDS = 240_000


@dataclass(frozen=True)
class StoredUser:
    username: str
    pw_hash: str
    pw_salt: str
    totp_secret: str
    role: str
    entitlements: tuple[str, ...]


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return dk.hex(), salt


def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, pw_hash)


def _load_raw() -> dict:
    if not USERS_FILE.exists():
        return {"users": []}
    try:
        return json.loads(USERS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"users": []}


def _save_raw(data: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(data, indent=2))


def get_user(username: str) -> StoredUser | None:
    for u in _load_raw().get("users", []):
        if u.get("username") == username:
            return StoredUser(
                username=u["username"],
                pw_hash=u["pw_hash"],
                pw_salt=u["pw_salt"],
                totp_secret=u.get("totp_secret", ""),
                role=u.get("role", "subscriber"),
                entitlements=tuple(u.get("entitlements", [])),
            )
    return None


def upsert_user(username: str, password: str, role: str, entitlements: list[str],
                totp_secret: str | None = None) -> str:
    """Create/update a user. Returns the TOTP secret (new one if not provided)."""
    import pyotp

    data = _load_raw()
    totp_secret = totp_secret or pyotp.random_base32()
    pw_hash, salt = hash_password(password)
    record = {
        "username": username, "pw_hash": pw_hash, "pw_salt": salt,
        "totp_secret": totp_secret, "role": role, "entitlements": entitlements,
    }
    users = [u for u in data.get("users", []) if u.get("username") != username]
    users.append(record)
    data["users"] = users
    _save_raw(data)
    return totp_secret
