"""Enable explicitly selected, installed roles for the local Pi assistant.

Run once as the local database administrator. No credentials or experiment
facts are created. Loading files alone continues to leave roles disabled.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from chemclaw_lab import agents, db  # noqa: E402

DEFAULT_ROLES = ("orchestrator", "b_capture", "b_query", "alice_inventory", "planner")


def configure(db_path: Path, roles: list[str]) -> list[dict]:
    if not db_path.is_file():
        raise ValueError("Database does not exist; initialize the lab database first.")
    conn = db.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        summary = agents.load_registry(conn, REPO / "runtime" / "agents")
        if summary["invalid"]:
            raise ValueError(f"Invalid role manifests: {summary['invalid']}")
        selected = []
        for role in dict.fromkeys(roles):
            matches = [m for m in summary["loaded"] if m["agent_id"] == role]
            if len(matches) != 1:
                raise ValueError(f"Role {role!r} must have exactly one installed version.")
            selected.append(matches[0])
        for item in selected:
            agents.set_manifest_enabled(conn, agent_id=item["agent_id"], version=item["version"], enabled=True)
        conn.commit()
        return selected
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable-defaults", action="store_true", help="Enable the shipped coordinator and four specialist roles.")
    group.add_argument("--roles", nargs="+", help="Enable only these installed agent IDs.")
    args = parser.parse_args()
    selected = configure(args.db, list(DEFAULT_ROLES) if args.enable_defaults else args.roles)
    print("Enabled roles: " + ", ".join(f"{m['agent_id']}@{m['version']}" for m in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
