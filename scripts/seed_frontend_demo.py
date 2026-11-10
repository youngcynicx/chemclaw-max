"""Seed a small synthetic Core DB for manual frontend testing.

This never touches a production database unless the operator passes that path.
It creates one project, one viewer credential, one sample/event, one task and
one resource/booking so all read-only frontend views have real Core data.
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
    parser.add_argument("--principal-id", default="hum_frontend")
    parser.add_argument("--secret", default="")
    args = parser.parse_args()
    secret = args.secret or secrets.token_urlsafe(24)

    db.init_db(args.db)
    conn = db.connect(args.db)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO projects (project_id, name) VALUES (?, ?)",
            (args.project_id, "Manual UI Test Lab"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO principals (principal_id, kind, display_name) "
            "VALUES (?, 'human', ?)",
            (args.principal_id, "Frontend Tester"),
        )
        conn.execute(
            "INSERT INTO memberships (principal_id, project_id, role) VALUES (?, ?, 'member') "
            "ON CONFLICT(principal_id, project_id) DO UPDATE SET role = 'member'",
            (args.principal_id, args.project_id),
        )
        conn.execute(
            "INSERT INTO auth_credentials (credential_id, principal_id, kind, secret_hash, status) "
            "VALUES ('cred_frontend', ?, 'api_key', ?, 'active') "
            "ON CONFLICT(credential_id) DO UPDATE SET secret_hash = excluded.secret_hash, "
            "status = 'active'",
            (args.principal_id, auth._hash_secret(secret)),
        )
        for chemical_id, name, formula, location, amount, threshold in (
            ("chem_kbr", "KBr", "KBr", "Cabinet A3", 42.0, 50.0),
            ("chem_tbabr3", "TBABr3", "TBABr3", "Cold Room Shelf 1", 18.0, 20.0),
            ("chem_nacl", "Sodium chloride", "NaCl", "Cabinet A1", 95.0, 10.0),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO chemicals "
                "(chemical_id, name, formula, location, amount, unit, threshold, "
                "hazard_level, max_auto_delta) VALUES (?, ?, ?, ?, ?, 'g', ?, 'low', 100)",
                (chemical_id, name, formula, location, amount, threshold),
            )
        conn.execute(
            "INSERT OR IGNORE INTO samples (sample_id, project_id, label) "
            "VALUES ('smp_sample002', ?, 'SAMPLE-002')",
            (args.project_id,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO evidence_objects "
            "(evidence_id, project_id, storage_path, sha256, mime) "
            "VALUES ('ev_demo', ?, 'demo.txt', ?, 'text/plain')",
            (args.project_id, "0" * 64),
        )
        conn.execute(
            "INSERT OR IGNORE INTO evidence_documents "
            "(document_id, project_id, notebook_code, title) "
            "VALUES ('doc_demo', ?, 'NB-DEMO', 'Manual UI demo notebook')",
            (args.project_id,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO evidence_pages "
            "(page_id, document_id, evidence_id, page_order, status) "
            "VALUES ('pg_demo', 'doc_demo', 'ev_demo', 1, 'transcribed')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO transcript_runs "
            "(run_id, page_id, provider, status, input_sha256, origin) "
            "VALUES ('run_demo', 'pg_demo', 'text_input', 'succeeded', 'h', 'ocr')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO transcript_blocks "
            "(block_id, run_id, block_kind, reading_order, block_json) "
            "VALUES ('b_demo', 'run_demo', 'paragraph', 0, ?)",
            (json.dumps({"text": "实际取用 8 g 氯化钠加入 SAMPLE-002 缓冲液。"},
                        ensure_ascii=False),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO page_transcripts "
            "(page_id, active_run_id, active_revision) VALUES ('pg_demo', 'run_demo', 1)"
        )
        event_id = "evt_demo_usage"
        exists = conn.execute(
            "SELECT 1 FROM event_revisions WHERE entity_type='experiment_event' "
            "AND entity_id=? AND revision=1",
            (event_id,),
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO event_revisions "
                "(revision_id, entity_type, entity_id, revision, field_json, "
                "knowledge_as_of, evidence_refs_json, provenance_json, created_by) "
                "VALUES ('evr_demo', 'experiment_event', ?, 1, ?, CURRENT_TIMESTAMP, ?, ?, ?)",
                (
                    event_id,
                    json.dumps(
                        {
                            "event_kind": "operation",
                            "raw_text": "实际取用 8 g 氯化钠加入 SAMPLE-002 缓冲液。",
                            "fields": {
                                "amount": {"normalized": 8.0, "unit": "g"},
                                "unit": {"raw": "g", "normalized": "g"},
                                "sample_label": {"normalized": "SAMPLE-002"},
                            },
                            "sample_id": "smp_sample002",
                            "page_id": "pg_demo",
                            "project_id": args.project_id,
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps([{"page_id": "pg_demo", "transcript_revision": 1,
                                 "block_id": "b_demo"}]),
                    json.dumps({"source_kind": "human_report"}, ensure_ascii=False),
                    args.principal_id,
                ),
            )
        conn.execute(
            "INSERT OR IGNORE INTO tasks "
            "(task_id, task_type, title, principal_id, project_id, status, revision) "
            "VALUES ('task_demo', 'agent.capture_page', '人工测试：整理 SAMPLE-002 记录', "
            "?, ?, 'waiting_input', 0)",
            (args.principal_id, args.project_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO resources "
            "(resource_id, project_id, name, kind, capacity, status, created_by) "
            "VALUES ('res_nmr', ?, 'NMR-400', 'instrument', 1, 'active', ?)",
            (args.project_id, args.principal_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO resource_bookings "
            "(booking_id, resource_id, project_id, status, slot_start, slot_end, "
            "slot_start_utc, slot_end_utc, time_normalization, purpose_note, created_by) "
            "VALUES ('bkg_demo', 'res_nmr', ?, 'confirmed', "
            "'2026-10-18T09:00:00+08:00', '2026-10-18T10:00:00+08:00', "
            "1, 2, 'utc', 'manual UI demo booking', ?)",
            (args.project_id, args.principal_id),
        )
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({
        "db": str(args.db),
        "projectId": args.project_id,
        "principalId": args.principal_id,
        "apiKey": secret,
        "sample": "smp_sample002",
        "task": "task_demo",
        "resource": "res_nmr",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
