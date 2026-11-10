"""Issue one project-scoped service reader and save its secret to a new file.

The existing database is never initialized or seeded. The API key is generated
inside this process and appears only in the exclusively created config file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from urllib.parse import urlsplit
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from chemclaw_lab import auth  # noqa: E402


class SetupError(Exception):
    """An issuance failure with a safe, non-secret diagnostic."""


def create_external_reader(db_path, project_id, config_path, base_url="http://127.0.0.1:8876"):
    db_path, config_path = Path(db_path).resolve(), Path(config_path).absolute()
    if not db_path.is_file():
        raise SetupError("The database must already exist.")
    if not isinstance(project_id, str) or not project_id.strip():
        raise SetupError("A project ID is required.")
    if config_path.exists() or config_path.is_symlink():
        raise SetupError("The config path already exists; refusing to overwrite it.")
    if not config_path.parent.is_dir():
        raise SetupError("The config parent directory must already exist.")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SetupError("The base URL must be an HTTP(S) service URL without credentials, query or fragment.")
    principal_id = f"svc_external_{uuid.uuid4().hex}"
    credential_id = f"cred_external_{uuid.uuid4().hex}"
    secret = secrets.token_urlsafe(32)
    conn, created_identity = None, None
    try:
        # mode=rw cannot accidentally create a database if it disappears.
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT project_id FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
            raise SetupError("The requested project does not exist.")
        auth.register_principal(conn, principal_id=principal_id, kind="service", display_name="External AI reader")
        auth.grant_membership(conn, principal_id=principal_id, project_id=project_id, role="viewer")
        auth.register_credential(conn, credential_id=credential_id, principal_id=principal_id, secret=secret)
        descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        info = os.fstat(descriptor)
        created_identity = (info.st_dev, info.st_ino)
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        except Exception:
            os.close(descriptor)
            raise
        with stream:
            json.dump({"base_url": base_url.rstrip("/"), "project_id": project_id,
                "principal_id": principal_id, "credential_id": credential_id, "api_key": secret},
                stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        conn.commit()
        return {"config": str(config_path), "project_id": project_id,
                "principal_id": principal_id, "credential_id": credential_id}
    except Exception as error:
        if conn is not None:
            conn.rollback()
        if created_identity is not None:
            try:
                info = config_path.stat()
                if (info.st_dev, info.st_ino) == created_identity:
                    config_path.unlink()
            except OSError:
                pass
        if isinstance(error, SetupError):
            raise
        raise SetupError("Reader setup failed; no identity was committed. Check the database and config location.") from error
    finally:
        if conn is not None:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing ChemClaw database")
    parser.add_argument("--project-id", required=True, help="Existing project for viewer access")
    parser.add_argument("--config", type=Path, required=True, help="New local secret config path")
    parser.add_argument("--base-url", default="http://127.0.0.1:8876")
    args = parser.parse_args(argv)
    try:
        result = create_external_reader(args.db, args.project_id, args.config, args.base_url)
    except SetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    # Deliberately omit the secret, including from errors and shell arguments.
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
