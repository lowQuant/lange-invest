# lange-invest

A single deployable web app for **lange-invest.com** with four surfaces, all on
one domain, all funnelling through one read path to ArcticDB:

| Surface | Path | Auth | Data |
|---------|------|------|------|
| **Public** | `/`, `/articles`, `/equities`, `/futures`, `/portfolio` | none | precomputed JSON snapshots + markdown |
| **Database** | `/database` | none | live, read-only browse of `futures` + `market_data` (allowlisted) with charting |
| **Gated** | `/gated/...` HTMX fragments | login + TOTP 2FA + entitlement | live signals (allowlisted) / real portfolio (private) |
| **Admin** | `/admin` | admin role | the mounted [arcticdb-viewer](https://github.com/lowQuant/arcticdb-viewer) (full CRUD) |
| **MCP** | `POST /mcp` | scoped bearer token | `read` (allowlisted) / `admin` (full CRUD) |

It is a **new repo that reuses the viewer**: the viewer's `core/` engine is
vendored (unforked) as the single path to ArcticDB, its theme tokens + Chart.js
options + Jinja conventions are reused so all surfaces look identical, and its
full app is mounted wholesale under `/admin`. Two repos, **one deployed app**.

## Architecture

```
app/
  main.py            FastAPI app: route groups + admin mount + MCP
  config.py          loads config/site.toml (allowlist + taxonomy) via tomllib
  public_access.py   READ-ONLY allowlist gate in front of core.operations
  snapshots.py       precomputed-snapshot reader (public read path)
  portfolio_store.py PRIVATE reader for real (IBKR) portfolio data
  articles.py        markdown-backed posts (frontmatter + render)
  auth.py            signed sessions, roles, entitlements
  users.py           private user store (PBKDF2 + TOTP secret)
  admin_mount.py     imports + auth-guards the viewer app at /admin
  routes/
    public.py        articles, asset-class & strategy pages, model portfolio
    auth_routes.py   login (password -> TOTP), logout, request-access
    gated.py         signals + real-portfolio HTMX fragments (access-wall fallback)
    mcp.py           stateless Streamable-HTTP MCP, scoped tokens, guardrails, audit
  templates/         base + partials (nav, subnav, stat tiles, chart panel, access wall)
  static/            css/site.css (design tokens), js/charts.js (shared Chart.js)
vendor/core/         vendored arcticdb-viewer engine (single path to ArcticDB)
config/site.toml     allowlist + data-driven asset-class/variant taxonomy
content/articles/    markdown posts
scripts/             precompute.py, ingest_ibkr.py, manage_users.py, gen_sample_data.py
tests/               security boundary tests
```

**Security boundary.** Public, gated, and the MCP `read` scope import only from
`public_access`, which refuses to list/read/enumerate any library not on the
central allowlist (`config/site.toml`). The protected IBKR / real-account
libraries are never allowlisted and are reachable only via `/admin` and the MCP
`admin` scope. Real-portfolio data has its own private read path
(`portfolio_store`), is never snapshotted, and is git-ignored if cached.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env                 # fill in S3 + secrets (never commit)

# Dev data so the public site renders without S3:
python scripts/gen_sample_data.py    # synthetic snapshots
python scripts/ingest_ibkr.py --sample   # synthetic private portfolio

# Local data engine for the public Database tab (futures + market_data):
export LANGE_DB_URI=lmdb:///tmp/lange_db
python scripts/seed_local_db.py      # sample OHLCV; omit in prod (use S3 env)

# For local HTTP dev only (cookies/MCP are TLS-only by default):
export COOKIE_SECURE=0 MCP_ALLOW_INSECURE=1 SESSION_SECRET=dev
python run_web.py                    # http://localhost:8000
```

### Create a member (password + 2FA)

```bash
python scripts/manage_users.py add --username you --role admin
# prints an otpauth:// URI — enroll it in your authenticator app
python scripts/manage_users.py add --username sub --role subscriber --entitlements signals
```

### Production data pipeline (scheduled tasks)

```bash
python scripts/precompute.py         # ArcticDB -> public snapshots (via public_access)
python scripts/ingest_ibkr.py        # IBKR Flex report -> private portfolio store
```

## MCP

```bash
# env: MCP_READ_TOKENS="reader:SECRET"  MCP_ADMIN_TOKENS="root:SECRET"
curl -s https://lange-invest.com/mcp \
  -H "Authorization: Bearer SECRET" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

- `read` scope → `list_libraries` (allowlisted), `list_symbols`, `describe_symbol`,
  `read_data`. The only scope you'd consider sharing.
- `admin` scope → full CRUD. `delete_library` is **disabled over HTTP**;
  destructive ops on protected libraries are refused unless
  `MCP_ALLOW_PROTECTED_OVERRIDE=1`. Constant-time token compare, per-token rate
  limit, TLS-only, every call audited to `data/private/mcp_audit.log`.

## Deployment (PythonAnywhere)

One web app, one domain. Point the WSGI/ASGI server at `app.main:app`. The public
read path serves precomputed snapshots (near-zero per-request compute) to respect
the metered-CPU budget. No long-lived streams: the MCP endpoint is stateless
request/response, not SSE.

## Tests

```bash
MCP_ALLOW_INSECURE=1 python -m pytest -q
```

Asserts: (a) non-allowlisted libraries rejected by every public/gated/read-MCP
entry point; (b) real-portfolio data unreachable without entitlement; (c)
write/delete tools unreachable with a `read`-scope token (and `delete_library`
disabled over HTTP).

> Research/education. Not investment advice. Past performance is not indicative of
> future results.
