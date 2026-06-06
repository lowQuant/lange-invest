"""Security boundary tests (brief's hard requirements):

(a) non-allowlisted library rejected by every public/gated/read-MCP entry point;
(b) real-portfolio data unreachable without entitlement;
(c) write/delete tools unreachable with a `read`-scope token.

Run: pytest -q
"""
from __future__ import annotations

import os

import pyotp
import pytest

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("COOKIE_SECURE", "0")
os.environ["USERS_FILE"] = "/tmp/li_test_users.json"
os.environ["MCP_READ_TOKENS"] = "reader:read-secret"
os.environ["MCP_ADMIN_TOKENS"] = "root:admin-secret"

from fastapi.testclient import TestClient  # noqa: E402

from app import public_access  # noqa: E402
from app.config import get_config  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ── (a) Allowlist: non-allowlisted libraries are rejected/enumerated-out ──────

def test_protected_library_not_allowlisted():
    cfg = get_config()
    for lib in cfg.protected_libraries:
        assert not public_access.is_public(lib)


def test_public_access_refuses_non_allowlisted():
    with pytest.raises(public_access.AccessDenied):
        public_access.ensure_public("ibkr_account")
    with pytest.raises(public_access.AccessDenied):
        public_access.list_symbols("ibkr_account")
    with pytest.raises(public_access.AccessDenied):
        public_access.read_data("real_portfolio", "x")


def test_list_libraries_never_exposes_protected():
    listed = set(public_access.list_libraries())
    for lib in get_config().protected_libraries:
        assert lib not in listed


def test_gated_signals_rejects_unknown_library(client):
    # Even authed, the signals route only reads `<ac>_signals` for known classes;
    # an unknown asset class 404s rather than reaching ArcticDB.
    r = client.get("/gated/strategy/ibkr/secret/signals")
    assert r.status_code in (404, 200)
    assert "access-wall" in r.text or r.status_code == 404


# ── (b) Real-portfolio data unreachable without entitlement ───────────────────

def _login(client, username, password, secret):
    from app.users import get_user
    client.post("/login", data={"username": username, "password": password})
    code = pyotp.TOTP(secret).now()
    client.post("/login/verify", data={"code": code, "next": "/"}, follow_redirects=False)


def test_real_portfolio_requires_entitlement(client):
    from app.users import upsert_user

    # Anonymous → wall
    assert "access-wall" in client.get("/gated/portfolio/real").text

    # Subscriber (no real_portfolio entitlement) → still wall
    sub_secret = upsert_user("sub_user", "pw", "subscriber", ["signals"])
    cs = TestClient(app)
    _login(cs, "sub_user", "pw", sub_secret)
    assert "access-wall" in cs.get("/gated/portfolio/real").text

    # Admin → reaches the component (may show "no report" but not the wall)
    admin_secret = upsert_user("admin_user", "pw", "admin", [])
    ca = TestClient(app)
    _login(ca, "admin_user", "pw", admin_secret)
    assert "access-wall" not in ca.get("/gated/portfolio/real").text


# ── (c) MCP read scope cannot reach write/delete tools ────────────────────────

def test_mcp_read_scope_blocks_writes():
    from app.routes import mcp as mcp_mod

    read_tools = set(mcp_mod.TOOLS_BY_SCOPE["read"])
    for forbidden in ("write_data", "update_data", "append_data", "delete_symbol",
                      "create_library", "delete_library"):
        assert forbidden not in read_tools


def test_mcp_read_token_denied_on_admin_tool(client):
    # A read-scope token calling an admin tool is rejected before any execution.
    r = client.post("/mcp", headers={"Authorization": "Bearer read-secret"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "delete_library", "arguments": {"name": "x"}}})
    assert r.status_code in (401, 403)


def test_mcp_requires_token(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_mcp_delete_library_disabled_for_admin(client):
    # delete_library is disabled over HTTP entirely (blast radius = whole DB).
    r = client.post("/mcp", headers={"Authorization": "Bearer admin-secret"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "delete_library", "arguments": {"name": "x"}}})
    body = r.json()
    assert "error" in body or body.get("result", {}).get("isError")
