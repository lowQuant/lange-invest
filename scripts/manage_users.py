#!/usr/bin/env python3
"""Manage human users (password + TOTP 2FA) in the private user store.

    python scripts/manage_users.py add --username alice --role admin
    python scripts/manage_users.py add --username bob  --role subscriber --entitlements signals
    python scripts/manage_users.py list

On `add`, prints the otpauth:// URI to enroll in an authenticator app.
Passwords are prompted (never passed on the command line).
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_config  # noqa: E402
from app.users import _load_raw, upsert_user  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage lange-invest users.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="Create or update a user.")
    add.add_argument("--username", required=True)
    add.add_argument("--role", default="subscriber", choices=["subscriber", "admin"])
    add.add_argument("--entitlements", default="", help="Comma-separated (e.g. signals,real_portfolio)")

    sub.add_parser("list", help="List users (no secrets).")

    code = sub.add_parser("code", help="Print the CURRENT 6-digit 2FA code for a user (testing).")
    code.add_argument("--username", required=True)

    secret = sub.add_parser("secret", help="Show a user's TOTP secret + otpauth URI (to enroll in an app).")
    secret.add_argument("--username", required=True)

    args = ap.parse_args()

    def _find(username):
        u = next((x for x in _load_raw().get("users", []) if x.get("username") == username), None)
        if not u:
            raise SystemExit(f"No user named {username!r}. Run: manage_users.py list")
        return u

    if args.cmd == "list":
        for u in _load_raw().get("users", []):
            print(f"{u['username']:20} role={u.get('role'):10} entitlements={u.get('entitlements')}")
        return

    if args.cmd == "code":
        import pyotp
        u = _find(args.username)
        t = pyotp.TOTP(u["totp_secret"])
        print(f"\n  Current 6-digit code for {args.username!r}:  {t.now()}")
        print(f"  (valid for ~{30 - int(__import__('time').time()) % 30}s — type it at /login)\n")
        return

    if args.cmd == "secret":
        import pyotp
        u = _find(args.username)
        uri = pyotp.TOTP(u["totp_secret"]).provisioning_uri(name=args.username, issuer_name=get_config().name)
        print(f"\n  TOTP secret : {u['totp_secret']}")
        print(f"  otpauth URI : {uri}")
        print("  In your authenticator app: Add account → enter this secret manually.\n")
        return

    ents = [e.strip() for e in args.entitlements.split(",") if e.strip()]
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")

    secret = upsert_user(args.username, pw, args.role, ents)

    import pyotp

    uri = pyotp.TOTP(secret).provisioning_uri(name=args.username, issuer_name=get_config().name)
    print(f"\nUser {args.username!r} saved (role={args.role}, entitlements={ents}).")
    print("Enroll this in your authenticator app:")
    print(f"  TOTP secret : {secret}")
    print(f"  otpauth URI : {uri}")


if __name__ == "__main__":
    main()
