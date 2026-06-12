"""Stateless Streamable-HTTP MCP endpoint, scoped by bearer token.

One route (`POST /mcp`) speaking MCP JSON-RPC (initialize, tools/list,
tools/call) with JSON responses — no SSE, no stdio, no bound port (none are
reachable on the host, and SSE dies at the 5-minute cap).

Two scopes, never one master token:
    read  — read-only tools routed through public_access (allowlisted). Never
            lists or reads IBKR/protected libraries.
    admin — full CRUD via core.operations. Root-level power; owner-only.

Hardening (endpoint-level):
    * Bearer token compared constant-time against env-stored secrets; multiple
      named tokens per scope; rotatable/revocable by editing env + restart.
    * delete_library is disabled over HTTP entirely (blast radius = whole DB).
    * Destructive ops refuse the protected (IBKR) libraries unless an explicit
      override env flag is set.
    * Every tool call audited (token, tool, library, symbol, ts) to a private log.
    * Per-token rate limit.
    * TLS-only: a token over plain HTTP is refused (set MCP_ALLOW_INSECURE=1 for
      local dev only).
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import time
from collections import defaultdict, deque
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import futures_overview, public_access
from app.config import PRIVATE_DIR, get_config

router = APIRouter()

PROTOCOL_VERSION = "2025-06-18"
AUDIT_LOG = PRIVATE_DIR / "mcp_audit.log"

# delete_library is disabled over HTTP regardless of scope.
HTTP_DISABLED_TOOLS = {"delete_library"}
# Tools that mutate data/structure — guarded against protected libraries.
DESTRUCTIVE_TOOLS = {"write_data", "update_data", "append_data", "delete_symbol",
                     "create_library", "delete_library"}
# Libraries that feed the cached /futures overview. A successful write to one
# of these through this endpoint invalidates the in-process cache so the page
# serves fresh prices immediately (no restart, no TTL wait).
OVERVIEW_LIBRARIES = {"futures", "universe"}


# ── Token scopes ─────────────────────────────────────────────────────────────

def _parse_tokens(env_value: str) -> dict[str, str]:
    """Parse "name:secret,name2:secret2" → {secret: name}."""
    out: dict[str, str] = {}
    for pair in (env_value or "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, secret = pair.partition(":")
        if name.strip() and secret.strip():
            out[secret.strip()] = name.strip()
    return out


def _scopes() -> dict[str, dict[str, str]]:
    return {
        "read": _parse_tokens(os.getenv("MCP_READ_TOKENS", "")),
        "admin": _parse_tokens(os.getenv("MCP_ADMIN_TOKENS", "")),
    }


def authenticate(token: str | None) -> tuple[str, str] | None:
    """Return (scope, token_name) for a valid token, else None. Constant-time."""
    if not token:
        return None
    for scope, tokens in _scopes().items():
        for secret, name in tokens.items():
            if hmac.compare_digest(token, secret):
                return scope, name
    return None


# ── Rate limiting (per token name, in-process) ───────────────────────────────

_RATE_MAX = int(os.getenv("MCP_RATE_MAX", "60"))      # requests
_RATE_WINDOW = int(os.getenv("MCP_RATE_WINDOW", "60"))  # seconds
_hits: dict[str, deque] = defaultdict(deque)


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > _RATE_WINDOW:
        q.popleft()
    if len(q) >= _RATE_MAX:
        return True
    q.append(now)
    return False


# ── Audit ────────────────────────────────────────────────────────────────────

def audit(token_name: str, scope: str, tool: str, args: dict, ok: bool, note: str = "") -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "token": token_name, "scope": scope, "tool": tool,
        "library": args.get("name") or args.get("library"),
        "symbol": args.get("symbol"), "ok": ok, "note": note,
    }
    with AUDIT_LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── Tool definitions ─────────────────────────────────────────────────────────

def _t(name, desc, schema, handler):
    return {"name": name, "description": desc, "inputSchema": schema, "handler": handler}


def _str_schema(*required, **props):
    properties = {k: {"type": v} for k, v in props.items()}
    return {"type": "object", "properties": properties, "required": list(required)}


# read-scope handlers (via public_access — allowlist enforced)
def _read_list_libraries(args):
    return public_access.list_libraries()


def _read_list_symbols(args):
    return public_access.list_symbols(args["library"])


def _read_describe(args):
    return public_access.describe_symbol(args["library"], args["symbol"])


def _read_data(args):
    rows = int(args.get("rows", 100))
    offset = int(args.get("offset", 0))
    df = public_access.read_data(args["library"], args["symbol"], row_range=(offset, offset + rows))
    return {"columns": list(df.columns), "row_count": len(df),
            "data": df.head(rows).to_dict(orient="records")}


# admin-scope handlers (via core.operations — full power)
def _ops():
    from core import operations as ops
    return ops


def _admin_list_libraries(args):
    return _ops().list_libraries()


def _admin_list_symbols(args):
    return _ops().list_symbols(args["library"])


def _admin_describe(args):
    return _ops().get_description(args["library"], args["symbol"])


def _admin_read(args):
    rows = int(args.get("rows", 100)); offset = int(args.get("offset", 0))
    df = _ops().read_data(args["library"], args["symbol"], row_range=(offset, offset + rows))
    return {"columns": list(df.columns), "row_count": len(df), "data": df.head(rows).to_dict(orient="records")}


def _csv_df(csv_data: str):
    import io
    import pandas as pd
    return pd.read_csv(io.StringIO(csv_data))


def _admin_write(args):
    _ops().write_data(args["library"], args["symbol"], _csv_df(args["csv_data"]))
    return f"written to {args['library']}/{args['symbol']}"


def _admin_update(args):
    _ops().update_data(args["library"], args["symbol"], _csv_df(args["csv_data"]))
    return f"updated {args['library']}/{args['symbol']}"


def _admin_append(args):
    _ops().append_data(args["library"], args["symbol"], _csv_df(args["csv_data"]))
    return f"appended {args['library']}/{args['symbol']}"


def _admin_create_library(args):
    _ops().create_library(args["name"])
    return f"created library {args['name']}"


def _admin_delete_symbol(args):
    _ops().delete_symbol(args["library"], args["symbol"])
    return f"deleted {args['library']}/{args['symbol']}"


def _admin_delete_library(args):  # disabled at the gateway; never actually runs
    raise RuntimeError("delete_library is disabled over HTTP.")


READ_TOOLS = [
    _t("list_libraries", "List public (allowlisted) libraries.", _str_schema(), _read_list_libraries),
    _t("list_symbols", "List symbols in an allowlisted library.", _str_schema("library", library="string"), _read_list_symbols),
    _t("describe_symbol", "Describe a symbol (rows, columns, dtypes).", _str_schema("library", "symbol", library="string", symbol="string"), _read_describe),
    _t("read_data", "Read rows from an allowlisted symbol.", _str_schema("library", "symbol", library="string", symbol="string", rows="integer", offset="integer"), _read_data),
]

ADMIN_TOOLS = [
    _t("list_libraries", "List ALL libraries.", _str_schema(), _admin_list_libraries),
    _t("list_symbols", "List symbols in a library.", _str_schema("library", library="string"), _admin_list_symbols),
    _t("describe_symbol", "Describe a symbol.", _str_schema("library", "symbol", library="string", symbol="string"), _admin_describe),
    _t("read_data", "Read rows from a symbol.", _str_schema("library", "symbol", library="string", symbol="string", rows="integer", offset="integer"), _admin_read),
    _t("write_data", "Write (overwrite) a symbol from CSV.", _str_schema("library", "symbol", "csv_data", library="string", symbol="string", csv_data="string"), _admin_write),
    _t("update_data", "Update a symbol from CSV.", _str_schema("library", "symbol", "csv_data", library="string", symbol="string", csv_data="string"), _admin_update),
    _t("append_data", "Append CSV rows to a symbol.", _str_schema("library", "symbol", "csv_data", library="string", symbol="string", csv_data="string"), _admin_append),
    _t("create_library", "Create a library.", _str_schema("name", name="string"), _admin_create_library),
    _t("delete_symbol", "Delete a symbol.", _str_schema("library", "symbol", library="string", symbol="string"), _admin_delete_symbol),
    _t("delete_library", "Delete a library (DISABLED over HTTP).", _str_schema("name", name="string"), _admin_delete_library),
]

TOOLS_BY_SCOPE: dict[str, list[dict]] = {
    "read": {t["name"]: t for t in READ_TOOLS},
    "admin": {t["name"]: t for t in ADMIN_TOOLS},
}


# ── JSON-RPC helpers ─────────────────────────────────────────────────────────

def _rpc_result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_error(rpc_id, code, message):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_list_payload(scope: str) -> dict:
    tools = [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOLS_BY_SCOPE[scope].values()
        if t["name"] not in HTTP_DISABLED_TOOLS
    ]
    return {"tools": tools}


def _tls_ok(request: Request) -> bool:
    if os.getenv("MCP_ALLOW_INSECURE", "0") == "1":
        return True
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.post("/mcp")
async def mcp_endpoint(request: Request):
    if not _tls_ok(request):
        return JSONResponse({"error": "TLS required"}, status_code=403)

    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    ident = authenticate(token)
    if ident is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    scope, token_name = ident

    if _rate_limited(token_name):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)

    method = body.get("method")
    rpc_id = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return JSONResponse(_rpc_result(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"lange-invest-mcp ({scope})", "version": "1.0.0"},
        }))

    if method in ("notifications/initialized", "ping"):
        return JSONResponse(_rpc_result(rpc_id, {}))

    if method == "tools/list":
        return JSONResponse(_rpc_result(rpc_id, _tool_list_payload(scope)))

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        scope_tools = TOOLS_BY_SCOPE[scope]

        # Out-of-scope tool → hard 403 (a read token can never call an admin tool).
        if name not in scope_tools:
            audit(token_name, scope, name, args, ok=False, note="out-of-scope")
            return JSONResponse({"error": f"tool {name!r} not available in scope {scope!r}"},
                                status_code=403)

        # delete_library disabled over HTTP entirely.
        if name in HTTP_DISABLED_TOOLS:
            audit(token_name, scope, name, args, ok=False, note="http-disabled")
            return JSONResponse(_rpc_result(rpc_id, {
                "content": [{"type": "text", "text": "delete_library is disabled over HTTP."}],
                "isError": True}))

        # Destructive ops refuse protected libraries unless override is set.
        target = args.get("name") or args.get("library")
        if name in DESTRUCTIVE_TOOLS and target in get_config().protected_libraries:
            if os.getenv("MCP_ALLOW_PROTECTED_OVERRIDE", "0") != "1":
                audit(token_name, scope, name, args, ok=False, note="protected-refused")
                return JSONResponse(_rpc_result(rpc_id, {
                    "content": [{"type": "text", "text": f"Refused: {target!r} is a protected library."}],
                    "isError": True}))

        handler: Callable[[dict], Any] = scope_tools[name]["handler"]
        try:
            result = handler(args)
            audit(token_name, scope, name, args, ok=True)
            if name in DESTRUCTIVE_TOOLS and (target is None or target in OVERVIEW_LIBRARIES):
                futures_overview.invalidate_cache()
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            return JSONResponse(_rpc_result(rpc_id, {"content": [{"type": "text", "text": text}]}))
        except public_access.AccessDenied as e:
            audit(token_name, scope, name, args, ok=False, note="access-denied")
            return JSONResponse(_rpc_result(rpc_id, {
                "content": [{"type": "text", "text": str(e)}], "isError": True}))
        except Exception as e:  # noqa: BLE001
            audit(token_name, scope, name, args, ok=False, note=f"error:{type(e).__name__}")
            return JSONResponse(_rpc_result(rpc_id, {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}))

    return JSONResponse(_rpc_error(rpc_id, -32601, f"Method not found: {method}"))
