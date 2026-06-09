# User management

This document covers how human accounts work on lange-invest: the auth model,
the roles and entitlements they unlock, and the CLI you use to create, inspect
and remove them. It does **not** cover MCP machine clients — those use bearer
tokens issued separately (see `app/routes/mcp.py`).

---

## TL;DR

```bash
# Create / overwrite an admin (prompts for password, prints TOTP enrolment URI)
python scripts/manage_users.py add --username johannes --role admin

# Create a subscriber with the `signals` entitlement
python scripts/manage_users.py add --username alice \
    --role subscriber --entitlements signals

# Inspect / debug
python scripts/manage_users.py list
python scripts/manage_users.py secret --username alice   # re-enrol in a new app
python scripts/manage_users.py code   --username alice   # current 6-digit code

# Remove
python scripts/manage_users.py remove --username alice
```

User records live in `data/private/users.json` (path overridable with
`USERS_FILE` / `PRIVATE_DIR`). That file is **git-ignored** and must never be
committed.

---

## Auth model

Login is **password + TOTP 2FA** with a signed session cookie. The flow:

1. `POST /login` — username + password. On success the server issues a short
   `li_pending` cookie (5 min) and redirects to `/login/verify`.
2. `POST /login/verify` — the 6-digit TOTP code from an authenticator app.
   On success the server issues the long `li_session` cookie (12 h).
3. Every subsequent request resolves the cookie back to a `User` once per
   request (cached on `request.state.user`).

Implementation: `app/auth.py` (cookies, session decode, role/entitlement
resolution) + `app/users.py` (the JSON store) + `app/routes/auth_routes.py`
(the `/login`, `/login/verify`, `/logout` endpoints).

### Password hashing

Passwords are hashed with stdlib `hashlib.pbkdf2_hmac("sha256", ...)` over
240 000 rounds, with a per-user random 16-byte salt. Comparison uses
`hmac.compare_digest` (constant-time). No bcrypt/argon2 dependency.

### TOTP

`pyotp.random_base32()` generates the secret on `add`. The standard
`otpauth://` provisioning URI is printed at creation time — paste it (or scan
its QR rendering) into Google Authenticator, 1Password, Authy, etc.

If the user loses their device, **re-enrol** by running
`manage_users.py secret --username <name>` and importing the same secret into
the new app. The secret only changes if you run `add` again with the same
username (which also resets the password).

---

## Roles

There are exactly two human roles. Every user gets one. Anonymous is the
implicit role for un-signed-in visitors.

| Role         | Purpose                                                   |
|--------------|-----------------------------------------------------------|
| `anonymous`  | Implicit — visitors with no session cookie.               |
| `subscriber` | Authenticated reader. Sees live signals by default.       |
| `admin`      | Site owner. Implicitly holds **every** entitlement; can mount the ArcticDB admin viewer at `/admin` and CRUD articles. |

Roles are flat — there is no hierarchy beyond admin's implicit "holds
everything". A subscriber is **not** a super-set of anonymous; the distinction
matters because some surfaces (the access wall, the request-access page) only
exist for unauthenticated visitors.

## Entitlements

Components on the site are gated by string entitlements. A user's `User.role`
plus their `entitlements` set determines what renders.

| Entitlement      | What it gates                                                |
|------------------|--------------------------------------------------------------|
| `signals`        | The live signals table on the strategy pages.                |
| `real_portfolio` | The live IBKR book (NAV, positions, P&L) on `/portfolio`.    |

Defaults are encoded in `app/auth.py`:

- `subscriber` implicitly gets `signals` (no need to grant it explicitly).
- `admin` implicitly gets **every** entitlement (`User.is_admin → True` short-
  circuits the entitlement check).
- `real_portfolio` is **owner/admin only** — even a subscriber holding the
  literal `real_portfolio` string in their entitlements list will be refused,
  unless the env flag `WIDEN_REAL_PORTFOLIO_TO_SUBSCRIBERS=1` is set.

That last rule is intentional belt-and-suspenders: it's hard to accidentally
expose the live book to paying subscribers via a typo or a misconfigured row
in `users.json`.

---

## CLI reference

All commands live in `scripts/manage_users.py`. The script prepends the repo
root to `sys.path`, so run it from anywhere as `python scripts/manage_users.py
<cmd>`.

### `add` — create or overwrite a user

```bash
python scripts/manage_users.py add \
    --username alice \
    --role subscriber \
    --entitlements signals
```

- Prompts for password twice (uses `getpass`, never echoed).
- Generates a fresh TOTP secret.
- Prints the secret and the `otpauth://` URI for enrolment.
- Running `add` with an existing username **overwrites the record** — new
  password, new TOTP secret, new entitlements. Use this as the "rotate
  credentials" path.

`--entitlements` is comma-separated. For admins it's redundant; for
subscribers it's normally just `signals`. The string `real_portfolio` is
accepted but only honoured if the `WIDEN_REAL_…` env flag is set (see above).

### `list` — show all users

```bash
python scripts/manage_users.py list
```

Prints one line per user: username, role, entitlements list. No secrets are
printed.

### `secret` — show TOTP secret + enrolment URI

```bash
python scripts/manage_users.py secret --username alice
```

Use this when a user needs to re-enrol in a new authenticator app without
rotating their password. The secret has not changed since `add` was last run
for that user.

### `code` — print the current 6-digit code

```bash
python scripts/manage_users.py code --username alice
```

Computes the current TOTP value plus seconds-remaining-in-window. Useful for
testing the login flow without an authenticator app, or for one-off recovery.
**Do not** make this a habit — typing codes into a chat or notebook negates
2FA.

### `remove` — delete a user

```bash
python scripts/manage_users.py remove --username alice
```

Removes the record from `users.json`. Existing session cookies for that user
become invalid on the next request (the session-decode step calls
`get_user(...)`, which returns `None` and forces a re-login).

---

## Storage

```
data/private/users.json          # git-ignored
```

Override with either env var:

- `USERS_FILE=/absolute/path/to/users.json` — exact file path.
- `PRIVATE_DIR=/absolute/path/to/private_root` — folder; `users.json` is
  resolved under it.

The file is plain JSON. Schema:

```json
{
  "users": [
    {
      "username": "johannes",
      "pw_hash": "…hex…",
      "pw_salt": "…hex16…",
      "totp_secret": "BASE32SECRET",
      "role": "admin",
      "entitlements": []
    }
  ]
}
```

You **can** edit this file by hand (e.g. to flip a role or add an
entitlement) — but never to change a password directly. Use
`manage_users.py add` for that, so the salt/hash get regenerated correctly.

---

## Sessions & cookies

| Cookie       | Purpose                              | Lifetime |
|--------------|--------------------------------------|----------|
| `li_pending` | Carries the username through the 2FA step | 5 min |
| `li_session` | Authenticated session                | 12 h |

Both are signed (HMAC, via `itsdangerous.URLSafeTimedSerializer`) using
`SESSION_SECRET`. Cookies have `Secure` set by default; in local HTTP
development set `COOKIE_SECURE=0`.

### Production requirements

- `SESSION_SECRET` must be set to a strong random value. The fallback
  (`"dev-insecure-secret-change-me"`) is for local dev only and is fatal in
  any environment that handles real credentials. Generate one with:

  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```

- `PRIVATE_DIR` should point at a host path that is **not** on the
  git-tracked tree, e.g. a `/var/lib/lange-invest/private` directory writable
  only by the web user. Back it up.

- TLS must terminate in front of the app — `Secure` cookies are a sham over
  plain HTTP.

---

## Quick scenarios

### Bootstrap a fresh install

```bash
# 1. Set a real session secret in the env (or .env)
SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")

# 2. Create yourself as admin
python scripts/manage_users.py add --username johannes --role admin

# 3. Scan the printed otpauth:// URI into your authenticator.
# 4. Visit /login → password → 2FA. Done.
```

### Invite a paying subscriber

```bash
python scripts/manage_users.py add \
    --username friend --role subscriber --entitlements signals
# Share the printed otpauth:// URI with them out-of-band, plus the password
# you chose. Tell them to change the password by asking you to re-`add` later.
```

### Lost device, same user

```bash
python scripts/manage_users.py secret --username friend
# Paste the secret into the new authenticator app. Password unchanged.
```

### Compromised account — rotate everything

```bash
# Same as `add` — new password and new TOTP secret.
python scripts/manage_users.py add --username friend --role subscriber \
    --entitlements signals
```

### Promote a subscriber to admin

```bash
python scripts/manage_users.py add --username friend --role admin
```

This overwrites the record, so the password and TOTP secret are reset. If you
want to keep them intact, you can hand-edit `users.json` and flip just the
`role` field — but you'll lose the audit trail of having gone through the
CLI, and you have to restart the web process for the change to take effect
on the next session decode (the `get_user` lookup reads the file each time,
but cached `request.state.user` may linger for the duration of a request).

---

## What this document does **not** cover

- **MCP tokens** — machine clients (Claude, etc.) authenticate with bearer
  tokens, not session cookies. See `app/routes/mcp.py` and the MCP-specific
  admin scope.
- **IBKR / ArcticDB credentials** — those live in `.env` (S3 / IBKR Flex)
  and are unrelated to human auth.
- **Web-based password reset** — there is no `/reset-password` route by
  design. Recovery happens out-of-band by running `manage_users.py add` again
  for that username.
