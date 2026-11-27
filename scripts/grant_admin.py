#!/usr/bin/env python3
"""Grant or revoke ZEMYIPTV admin access for an existing Neon Auth account.

    python3 scripts/grant_admin.py list
    python3 scripts/grant_admin.py grant simonasgir@gmail.com [owner|admin]
    python3 scripts/grant_admin.py revoke <neon-user-id>

The account must already exist in Neon Auth (neon_auth."user"); sign up first,
then grant. Permission is stored in public.admin_users and checked on every
admin request, so revoking takes effect immediately.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import db  # noqa: E402


def main(argv):
    db.init_schema()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command = argv[0]
    if command == "list":
        rows = auth.list_admins()
        if not rows:
            print("No explicit admin grants yet.")
        for row in rows:
            print(f"{row['role']:<6} {row['email']:<40} user={row['user_id']} last_seen={row['last_seen_at']}")
        return 0
    if command == "grant":
        if len(argv) < 2:
            print("Usage: grant_admin.py grant <email> [owner|admin]")
            return 2
        role = argv[2] if len(argv) > 2 else "admin"
        result = auth.grant_admin(argv[1], role, {"email": "cli"}, ip="cli")
        auth.audit("admin.grant.cli", f"{result['email']} -> {result['role']}", {"email": "cli"}, "cli")
        print(f"Granted {result['role']} to {result['email']} ({result['user_id']})")
        return 0
    if command == "revoke":
        if len(argv) < 2:
            print("Usage: grant_admin.py revoke <neon-user-id>")
            return 2
        ok = auth.revoke_admin(argv[1], {"email": "cli"}, "cli")
        print("Revoked" if ok else "No explicit grant for that user")
        return 0 if ok else 1
    print(f"Unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
