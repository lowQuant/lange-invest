"""Auth & entitlements.

Humans authenticate with a password + TOTP 2FA and carry a signed session
cookie (``itsdangerous``). Entitlements gate the components:

    * ``signals``        — live signals table (subscribers + admins)
    * ``real_portfolio`` — real IBKR book (owner/admin only by default; widening
                           to subscribers is a deliberate env flag)

MCP machine clients use bearer tokens, not this module (see app.routes.mcp).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

ROLE_ANON = "anonymous"
ROLE_SUBSCRIBER = "subscriber"
ROLE_ADMIN = "admin"

ENT_SIGNALS = "signals"
ENT_REAL_PORTFOLIO = "real_portfolio"

SESSION_COOKIE = "li_session"
PENDING_COOKIE = "li_pending"
SESSION_MAX_AGE = 60 * 60 * 12  # 12h
PENDING_MAX_AGE = 60 * 5        # 5m to complete 2FA

# Subscribers see live signals by default; the real book stays owner/admin only
# unless explicitly widened.
_DEFAULT_SUBSCRIBER_ENTS = frozenset({ENT_SIGNALS})
WIDEN_REAL_TO_SUBSCRIBERS = os.getenv("WIDEN_REAL_PORTFOLIO_TO_SUBSCRIBERS", "0") == "1"


def _secret() -> str:
    # Falls back to an ephemeral key in dev; production MUST set SESSION_SECRET.
    return os.getenv("SESSION_SECRET") or "dev-insecure-secret-change-me"


def cookie_secure() -> bool:
    """Session cookies are Secure (HTTPS-only) by default; set COOKIE_SECURE=0
    for local HTTP development only."""
    return os.getenv("COOKIE_SECURE", "1") != "0"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="li-session")


@dataclass(frozen=True)
class User:
    username: str
    role: str = ROLE_SUBSCRIBER
    entitlements: frozenset[str] = frozenset()

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def has_entitlement(self, name: str) -> bool:
        if self.is_admin:
            return True  # admins implicitly hold every entitlement
        if name == ENT_REAL_PORTFOLIO and not WIDEN_REAL_TO_SUBSCRIBERS:
            return False  # owner/admin only unless explicitly widened
        return name in self.entitlements


def _resolve_entitlements(role: str, explicit: frozenset[str]) -> frozenset[str]:
    if role == ROLE_SUBSCRIBER:
        return _DEFAULT_SUBSCRIBER_ENTS | explicit
    return explicit


# ── Session issue / read ─────────────────────────────────────────────────────

def issue_session(username: str) -> str:
    return _serializer().dumps({"u": username})


def issue_pending(username: str) -> str:
    return URLSafeTimedSerializer(_secret(), salt="li-pending").dumps({"u": username})


def read_pending(token: str) -> str | None:
    try:
        data = URLSafeTimedSerializer(_secret(), salt="li-pending").loads(token, max_age=PENDING_MAX_AGE)
        return data.get("u")
    except (BadSignature, SignatureExpired):
        return None


def _user_from_cookie(token: str) -> User | None:
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    username = data.get("u")
    if not username:
        return None

    from app.users import get_user  # local import avoids cycle

    stored = get_user(username)
    if stored is None:
        return None
    return User(
        username=stored.username,
        role=stored.role,
        entitlements=_resolve_entitlements(stored.role, frozenset(stored.entitlements)),
    )


def current_user(request: Request) -> User | None:
    """Return the authenticated user, or ``None``. Caches on request.state."""
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    if getattr(request.state, "user_resolved", False):
        return None
    token = request.cookies.get(SESSION_COOKIE)
    user = _user_from_cookie(token) if token else None
    request.state.user = user
    request.state.user_resolved = True
    return user
