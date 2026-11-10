"""Create a local principal credential for the read-only frontend bridge.

The frontend does not accept a self-reported identity. It needs a real Core
principal secret (the same value stored as a SHA-256 digest in
``auth_credentials``). This helper creates/rotates one for local testing.

It does NOT seed real lab data and does not read an existing production DB
unless the operator explicitly passes that database path.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from chemclaw_lab import auth, db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--project-id", default="proj_lab")
    parser.add_argument("--project-name", default="Local Lab")
    parser.add_argument("--principal-id", default="hum_frontend")
    parser.add_argument("--role", choices=("viewer", "member", "owner"), default="viewer")
    parser.add_argument("--credential-id", default="cred_frontend")
    parser.add_argument("--secret", default="")
    args = parser.parse_args()

    db.init_db(args.db)
    secret = args.secret or secrets.token_urlsafe(24)
    conn = db.connect(args.db)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO projects (project_id, name) VALUES (?, ?)",
            (args.project_id, args.project_name),
        )
        conn.execute(
            "INSERT OR IGNORE INTO principals (principal_id, kind, display_name) "
            "VALUES (?, 'human', ?)",
            (args.principal_id, args.principal_id),
        )
        conn.execute(
            "INSERT INTO memberships (principal_id, project_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT(principal_id, project_id) DO UPDATE SET role = excluded.role",
            (args.principal_id, args.project_id, args.role),
        )
        conn.execute(
            "INSERT INTO auth_credentials (credential_id, principal_id, kind, secret_hash, status) "
            "VALUES (?, ?, 'api_key', ?, 'active') "
            "ON CONFLICT(credential_id) DO UPDATE SET principal_id = excluded.principal_id, "
            "secret_hash = excluded.secret_hash, status = 'active'",
            (args.credential_id, args.principal_id, auth._hash_secret(secret)),
        )
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({
        "projectId": args.project_id,
        "principalId": args.principal_id,
        "role": args.role,
        "credentialId": args.credential_id,
        "apiKey": secret,
        "note": "Store this secret only in the frontend connection form; it is shown once.",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
