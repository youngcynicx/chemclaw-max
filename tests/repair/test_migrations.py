"""R14/F14: additive migration, idempotent init and backup/restore."""

from __future__ import annotations

import sqlite3

from chemclaw_lab import db


def test_init_is_idempotent_and_preserves_business_rows(tmp_path):
    path = tmp_path / "migrate.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        conn.execute("INSERT INTO projects (project_id, name) VALUES ('P1', 'alpha')")
        conn.execute(
            "INSERT INTO chemicals (chemical_id, name, amount, unit) "
            "VALUES ('chm_x', 'X', 5.0, 'g')"
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(path)
    db.init_db(path)
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert conn.execute(
            "SELECT amount FROM chemicals WHERE chemical_id = 'chm_x'"
        ).fetchone()[0] == 5.0
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"approval_grants", "delivery_attempts", "assistant_jobs", "assistant_job_events", "assistant_runtime_checkpoints", "assistant_delegations"} <= tables
        migrations = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert {12, 13} <= migrations
    finally:
        conn.close()


def test_legacy_delivery_status_backfills_explicit_state(tmp_path):
    path = tmp_path / "legacy_delivery.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        conn.execute(
            "INSERT INTO outbox (outbox_id, aggregate_type, aggregate_id, message_type, "
            "payload_json, status) VALUES ('out1', 'x', 'a', 'm', '{}', 'pending')"
        )
        conn.execute(
            "INSERT INTO deliveries (delivery_id, outbox_id, channel, target, status, "
            "attempt_count, next_retry_at) VALUES ('dlv1', 'out1', 'echo', 't', "
            "'failed', 1, NULL)"
        )
        conn.execute("UPDATE deliveries SET state = 'queued' WHERE delivery_id = 'dlv1'")
        conn.commit()
        db._migrate_v12(conn)
        row = conn.execute(
            "SELECT state, logical_delivery_id FROM deliveries WHERE delivery_id = 'dlv1'"
        ).fetchone()
        assert row["state"] == "failed_permanent"
        assert row["logical_delivery_id"] == "dlv1"
    finally:
        conn.close()


def test_sqlite_backup_restore_roundtrip(tmp_path):
    path = tmp_path / "live.db"
    backup = tmp_path / "backup.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        conn.execute("INSERT INTO projects (project_id, name) VALUES ('P1', 'alpha')")
        conn.commit()
        target = sqlite3.connect(str(backup))
        try:
            conn.backup(target)
        finally:
            target.close()
    finally:
        conn.close()
    restored = db.connect(backup)
    try:
        assert restored.execute(
            "SELECT name FROM projects WHERE project_id = 'P1'"
        ).fetchone()[0] == "alpha"
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()
