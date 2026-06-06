"""Auth & entitlements (Phase 1 stub → fleshed out in Phase 4).

For now this exposes the shape the templates and routes depend on:
    * a ``User`` model with a ``role`` and ``is_admin`` / entitlement helpers;
    * ``current_user(request)`` which returns the signed-in user or ``None``.

Phase 1 has no user store wired, so ``current_user`` returns ``None`` (every
visitor is anonymous). Phase 4 replaces the body with signed-session decoding,
TOTP 2FA, and a real entitlement check — without changing this interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from fastapi import Request

# Roles, ordered by privilege. Entitlement to gated components = `subscriber`+.
ROLE_ANON = "anonymous"
ROLE_SUBSCRIBER = "subscriber"
ROLE_ADMIN = "admin"


@dataclass(frozen=True)
class User:
    username: str
    role: str = ROLE_SUBSCRIBER
    entitlements: frozenset[str] = frozenset()

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def has_entitlement(self, name: str) -> bool:
        # Admins implicitly hold every entitlement.
        return self.is_admin or name in self.entitlements


def current_user(request: Request) -> User | None:
    """Return the authenticated user, or ``None``.

    Phase 1: no session backend wired → always anonymous. Phase 4 fills this in.
    """
    return getattr(request.state, "user", None)
