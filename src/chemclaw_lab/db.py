from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import (
    SCHEMA_SQL,
    S02_SCHEMA_SQL,
    S03_SCHEMA_SQL,
    S04_SCHEMA_SQL,
    S05_SCHEMA_SQL,
    S06_SCHEMA_SQL,
    S07_ADJUSTMENTS_SQL,
    S07_SCHEMA_SQL,
    S09_SCHEMA_SQL,
    S10_SCHEMA_SQL,
    S11_SCHEMA_SQL,
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_read_only(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if str(db_path) == ":memory:" or not path.is_file():
        raise ValueError(f"Inventory database does not exist: {db_path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str | Path) -> None:
    path = Path(db_path)
    if str(db_path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(S02_SCHEMA_SQL)
        conn.executescript(S03_SCHEMA_SQL)
        conn.executescript(S04_SCHEMA_SQL)
        conn.executescript(S05_SCHEMA_SQL)
        conn.executescript(S06_SCHEMA_SQL)
        conn.executescript(S07_SCHEMA_SQL)
        conn.executescript(S07_ADJUSTMENTS_SQL)
        conn.executescript(S09_SCHEMA_SQL)
        conn.executescript(S10_SCHEMA_SQL)
        conn.executescript(S11_SCHEMA_SQL)
        _migrate_v02(conn)
        _migrate_v03(conn)
        _migrate_v04(conn)
        _migrate_v05(conn)
        _migrate_v06(conn)
        _migrate_v07(conn)
        _migrate_v08(conn)
        _migrate_v09(conn)
        _migrate_v11(conn)
        _migrate_v12(conn)
        _migrate_v13(conn)
        _ensure_indexes(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (2, "reliable_alice"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (3, "messaging_excel"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (4, "shared_contracts_v02"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (5, "evidence_ingest_v03"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (6, "memory_service_v04"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (7, "query_service_v05"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (8, "agent_host_v06"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (9, "cross_domain_inventory_v07"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (10, "extension_domains_v09"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (11, "messaging_channels_v10"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (12, "repair_round_g1"),
        )
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (13, 'durable_assistant_jobs')")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    migrations: dict[str, str],
) -> None:
    existing_columns = _column_names(conn, table)
    for column, sql in migrations.items():
        if column not in existing_columns:
            conn.execute(sql)


def _migrate_v02(conn: sqlite3.Connection) -> None:
    _add_missing_columns(
        conn,
        "inventory_transactions",
        {
            "message_id": "ALTER TABLE inventory_transactions ADD COLUMN message_id TEXT",
            "event_id": "ALTER TABLE inventory_transactions ADD COLUMN event_id TEXT",
            "user_id": "ALTER TABLE inventory_transactions ADD COLUMN user_id TEXT",
            "project_id": "ALTER TABLE inventory_transactions ADD COLUMN project_id TEXT",
            "confidence": "ALTER TABLE inventory_transactions ADD COLUMN confidence REAL",
            "raw_text": "ALTER TABLE inventory_transactions ADD COLUMN raw_text TEXT",
            "created_at": "ALTER TABLE inventory_transactions ADD COLUMN created_at TEXT",
            "confirmation_status": (
                "ALTER TABLE inventory_transactions "
                "ADD COLUMN confirmation_status TEXT NOT NULL DEFAULT 'approved'"
            ),
        },
    )
    _add_missing_columns(
        conn,
        "message_logs",
        {
            "source_message_id": "ALTER TABLE message_logs ADD COLUMN source_message_id TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "audit_logs",
        {
            "event_id": "ALTER TABLE audit_logs ADD COLUMN event_id TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "chemicals",
        {
            "hazard_level": (
                "ALTER TABLE chemicals ADD COLUMN hazard_level TEXT NOT NULL DEFAULT 'low'"
            ),
            "max_auto_delta": "ALTER TABLE chemicals ADD COLUMN max_auto_delta REAL",
        },
    )


def _migrate_v03(conn: sqlite3.Connection) -> None:
    _add_missing_columns(
        conn,
        "chemicals",
        {
            "updated_at": "ALTER TABLE chemicals ADD COLUMN updated_at TEXT",
        },
    )
    conn.execute(
        "UPDATE chemicals SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
    )


def _migrate_v04(conn: sqlite3.Connection) -> None:
    """S02 additive columns: stock revision counters and provenance markers.

    - ``chemicals.revision`` backs ``expected_revision`` optimistic concurrency
      (bumped by the capability layer in the same transaction as the apply).
    - ``experiment_events.provenance`` marks pre-S02 rows as
      ``legacy_source_unknown``; their origin is never fabricated afterwards.
    """
    _add_missing_columns(
        conn,
        "chemicals",
        {
            "revision": "ALTER TABLE chemicals ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
        },
    )
    _add_missing_columns(
        conn,
        "experiment_events",
        {
            "provenance": (
                "ALTER TABLE experiment_events "
                "ADD COLUMN provenance TEXT NOT NULL DEFAULT 'legacy_source_unknown'"
            ),
        },
    )


def _migrate_v05(conn: sqlite3.Connection) -> None:
    """S03 additive indexes only; new tables come from S03_SCHEMA_SQL.

    Kept as a named step so legacy v4 databases reproduce the same final
    schema as fresh initialization (plan AC: migrations are reproducible).
    """
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pages_document
          ON evidence_pages(document_id, page_order)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_page
          ON transcript_runs(page_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_blocks_run
          ON transcript_blocks(run_id, reading_order)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fragments_status
          ON risk_fragments(status, run_id)
        """
    )


def _migrate_v06(conn: sqlite3.Connection) -> None:
    """S04 additive indexes only; new tables come from S04_SCHEMA_SQL.

    Legacy v5 databases reproduce the same final schema as fresh init.
    """
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_proposals_page
          ON memory_proposals(page_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_proposals_status
          ON memory_proposals(project_id, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clarifications_proposal
          ON clarifications(proposal_id, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_confirmations_proposal
          ON event_confirmations(proposal_id, proposal_revision, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_artifacts_entity
          ON memory_artifacts(entity_type, entity_id, stale)
        """
    )


def _migrate_v07(conn: sqlite3.Connection) -> None:
    """S05 additive indexes only; new tables come from S05_SCHEMA_SQL.

    Legacy v6 databases reproduce the same final schema as fresh init.
    """
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_query_reports_project
          ON query_reports(project_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_revisions_entity
          ON event_revisions(entity_type, entity_id, revision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_revisions_asof
          ON event_revisions(entity_type, entity_id, knowledge_as_of)
        """
    )


def _migrate_v08(conn: sqlite3.Connection) -> None:
    """S06 additive columns + indexes; new tables come from S06_SCHEMA_SQL.

    ``agent_runs`` gains host fields (agent identity, trace, attempts, tool
    budget, heartbeat). ``tasks``/``outbox``/``deliveries`` gain trace_id so a
    single trace spans inbound -> task -> run -> tool ops -> delivery. Legacy
    v7 databases reproduce the same final schema as fresh init.
    """
    _add_missing_columns(
        conn,
        "agent_runs",
        {
            "agent_id": "ALTER TABLE agent_runs ADD COLUMN agent_id TEXT",
            "manifest_version": "ALTER TABLE agent_runs ADD COLUMN manifest_version TEXT",
            "trace_id": "ALTER TABLE agent_runs ADD COLUMN trace_id TEXT",
            "attempts": "ALTER TABLE agent_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "heartbeat_at": "ALTER TABLE agent_runs ADD COLUMN heartbeat_at TEXT",
            "used_tool_calls": (
                "ALTER TABLE agent_runs ADD COLUMN used_tool_calls INTEGER NOT NULL DEFAULT 0"
            ),
            "budget_tool_calls": "ALTER TABLE agent_runs ADD COLUMN budget_tool_calls INTEGER",
            "error_code": "ALTER TABLE agent_runs ADD COLUMN error_code TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "tasks",
        {
            "trace_id": "ALTER TABLE tasks ADD COLUMN trace_id TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "agent_run_tokens",
        {
            "trace_id": "ALTER TABLE agent_run_tokens ADD COLUMN trace_id TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "outbox",
        {
            "trace_id": "ALTER TABLE outbox ADD COLUMN trace_id TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "deliveries",
        {
            "trace_id": "ALTER TABLE deliveries ADD COLUMN trace_id TEXT",
            "next_retry_at": "ALTER TABLE deliveries ADD COLUMN next_retry_at TEXT",
            "last_error": "ALTER TABLE deliveries ADD COLUMN last_error TEXT",
        },
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_status_lease
          ON agent_runs(status, lease_expires_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_tokens_run
          ON agent_run_tokens(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_deliveries_retry
          ON deliveries(status, next_retry_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_workflow_task
          ON workflow_runs(task_id)
        """
    )


def _migrate_v09(conn: sqlite3.Connection) -> None:
    """S07 additive columns; new tables come from S07_SCHEMA_SQL.

    ``tasks`` gains ``parent_task_id`` so a mixed inbound request can link its
    child subtasks (plan §13.6). Legacy v8 databases reproduce the same final
    schema as fresh init.
    """
    _add_missing_columns(
        conn,
        "tasks",
        {
            "parent_task_id": "ALTER TABLE tasks ADD COLUMN parent_task_id TEXT",
        },
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subtasks_status
          ON inventory_subtasks(status, chemical_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subtasks_source_event
          ON inventory_subtasks(source_event_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_adjustments_source_event
          ON inventory_adjustment_proposals(source_event_id)
        """
    )


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """S10 additive columns on deliveries; new tables come from S10_SCHEMA_SQL.

    ``deliveries`` gains ``channel_account_id`` and ``platform_thread_id`` so
    receive/deliver relations are stored per channel account and thread
    (plan §16.7). Legacy v10 databases reproduce the same final schema as
    fresh init.
    """
    _add_missing_columns(
        conn,
        "deliveries",
        {
            "channel_account_id": (
                "ALTER TABLE deliveries ADD COLUMN channel_account_id TEXT"
            ),
            "platform_thread_id": (
                "ALTER TABLE deliveries ADD COLUMN platform_thread_id TEXT"
            ),
        },
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_channel_messages_account_time
          ON channel_messages(channel_account_id, received_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_channel_messages_status
          ON channel_messages(status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_channel_identities_principal
          ON channel_identities(principal_id)
        """
    )


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """Repair-round columns (G1): authority, delivery state, source manifest,
    UTC booking instants and approval grants.

    The migration is additive and idempotent. It never rewrites an existing
    business fact: unknown legacy values are marked as such instead of being
    guessed.
    """
    from datetime import datetime, timezone

    _add_missing_columns(
        conn,
        "operation_results",
        {
            "authority_scope": "ALTER TABLE operation_results ADD COLUMN authority_scope TEXT",
            "project_context_json": (
                "ALTER TABLE operation_results ADD COLUMN project_context_json TEXT"
            ),
        },
    )
    _add_missing_columns(
        conn,
        "agent_run_tokens",
        {
            "claim_epoch": (
                "ALTER TABLE agent_run_tokens ADD COLUMN claim_epoch INTEGER NOT NULL DEFAULT 0"
            ),
            "worker_id": "ALTER TABLE agent_run_tokens ADD COLUMN worker_id TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "deliveries",
        {
            "state": "ALTER TABLE deliveries ADD COLUMN state TEXT NOT NULL DEFAULT 'queued'",
            "error_code": "ALTER TABLE deliveries ADD COLUMN error_code TEXT",
            "logical_delivery_id": "ALTER TABLE deliveries ADD COLUMN logical_delivery_id TEXT",
            "last_attempt_at": "ALTER TABLE deliveries ADD COLUMN last_attempt_at TEXT",
            "accepted_at": "ALTER TABLE deliveries ADD COLUMN accepted_at TEXT",
            "receipt_json": "ALTER TABLE deliveries ADD COLUMN receipt_json TEXT",
        },
    )
    _add_missing_columns(
        conn,
        "query_reports",
        {
            "source_manifest_json": (
                "ALTER TABLE query_reports ADD COLUMN source_manifest_json TEXT NOT NULL DEFAULT '[]'"
            ),
            "source_project_ids_json": (
                "ALTER TABLE query_reports ADD COLUMN source_project_ids_json "
                "TEXT NOT NULL DEFAULT '[]'"
            ),
        },
    )
    _add_missing_columns(
        conn,
        "resource_bookings",
        {
            "slot_start_utc": "ALTER TABLE resource_bookings ADD COLUMN slot_start_utc INTEGER",
            "slot_end_utc": "ALTER TABLE resource_bookings ADD COLUMN slot_end_utc INTEGER",
            "time_normalization": (
                "ALTER TABLE resource_bookings ADD COLUMN time_normalization TEXT "
                "DEFAULT 'legacy_unknown'"
            ),
        },
    )
    _add_missing_columns(
        conn,
        "channel_messages",
        {
            "actor_principal_id": (
                "ALTER TABLE channel_messages ADD COLUMN actor_principal_id TEXT"
            ),
            "identity_version": (
                "ALTER TABLE channel_messages ADD COLUMN identity_version INTEGER DEFAULT 1"
            ),
        },
    )
    _add_missing_columns(
        conn,
        "inventory_adjustment_proposals",
        {
            "approval_id": (
                "ALTER TABLE inventory_adjustment_proposals ADD COLUMN approval_id TEXT"
            ),
            "approval_source": (
                "ALTER TABLE inventory_adjustment_proposals ADD COLUMN approval_source TEXT"
            ),
        },
    )

    # Legacy delivery rows: derive the explicit state from the old status.
    conn.execute(
        """
        UPDATE deliveries
        SET state = CASE
              WHEN status = 'delivered' THEN 'delivered'
              WHEN status = 'failed' AND next_retry_at IS NOT NULL THEN 'retry_wait'
              WHEN status = 'failed' THEN 'failed_permanent'
              ELSE 'queued'
            END
        WHERE (state IS NULL OR state = 'queued') AND status <> 'queued'
        """
    )
    conn.execute(
        "UPDATE deliveries SET logical_delivery_id = delivery_id "
        "WHERE logical_delivery_id IS NULL OR logical_delivery_id = ''"
    )

    # Existing reports: retain the cited claims as the manifest. Project lists
    # are derived from the report's own project (the only ownership evidence
    # available); genuinely cross-project legacy reports are marked restricted
    # by read_report when a cited source cannot be re-authorized.
    conn.execute(
        """
        UPDATE query_reports
        SET source_manifest_json = claims_json
        WHERE source_manifest_json IS NULL OR source_manifest_json = '[]'
        """
    )
    conn.execute(
        """
        UPDATE query_reports
        SET source_project_ids_json = json_array(project_id)
        WHERE source_project_ids_json IS NULL OR source_project_ids_json = '[]'
        """
    )

    # Legacy booking times: only offset-aware values can be converted without
    # guessing. Naive values stay NULL + legacy_unknown and are treated as
    # unverifiable for conflict enforcement.
    rows = conn.execute(
        "SELECT booking_id, slot_start, slot_end, slot_start_utc, slot_end_utc "
        "FROM resource_bookings WHERE slot_start_utc IS NULL OR slot_end_utc IS NULL"
    ).fetchall()
    for row in rows:
        try:
            start = datetime.fromisoformat(str(row["slot_start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(row["slot_end"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if start.tzinfo is None or end.tzinfo is None:
            conn.execute(
                "UPDATE resource_bookings SET time_normalization = 'legacy_unknown' "
                "WHERE booking_id = ?",
                (row["booking_id"],),
            )
            continue
        conn.execute(
            "UPDATE resource_bookings SET slot_start_utc = ?, slot_end_utc = ?, "
            "time_normalization = 'migrated_utc' WHERE booking_id = ?",
            (
                int(start.astimezone(timezone.utc).timestamp()),
                int(end.astimezone(timezone.utc).timestamp()),
                row["booking_id"],
            ),
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_grants_proposal "
        "ON approval_grants(proposal_type, proposal_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_attempts_delivery "
        "ON delivery_attempts(delivery_id, attempt_no)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bookings_utc "
        "ON resource_bookings(resource_id, slot_start_utc, slot_end_utc)"
    )


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_message_source_identity
          ON message_logs(source, source_message_id)
          WHERE source_message_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_event_message
          ON event_proposals(message_id);

        CREATE INDEX IF NOT EXISTS idx_event_status
          ON event_proposals(status);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_event_single_revision
          ON event_proposals(supersedes_event_id)
          WHERE supersedes_event_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_event
          ON inventory_transactions(event_id)
          WHERE event_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_audit_event
          ON audit_logs(event_id);

        CREATE INDEX IF NOT EXISTS idx_gateway_identity_user
          ON gateway_identities(user_id);

        CREATE INDEX IF NOT EXISTS idx_import_batch_created
          ON import_batches(created_at);

        CREATE INDEX IF NOT EXISTS idx_membership_project
          ON memberships(project_id);

        CREATE INDEX IF NOT EXISTS idx_credential_hash
          ON auth_credentials(secret_hash);

        CREATE INDEX IF NOT EXISTS idx_task_principal
          ON tasks(principal_id);

        CREATE INDEX IF NOT EXISTS idx_task_project
          ON tasks(project_id);

        CREATE INDEX IF NOT EXISTS idx_run_task
          ON agent_runs(task_id);

        CREATE INDEX IF NOT EXISTS idx_outbox_pending
          ON outbox(status, created_at);

        CREATE INDEX IF NOT EXISTS idx_delivery_outbox
          ON deliveries(outbox_id);

        CREATE INDEX IF NOT EXISTS idx_event_revision_entity
          ON event_revisions(entity_type, entity_id, revision);

        CREATE INDEX IF NOT EXISTS idx_experiment_records_project
          ON experiment_records(project_id);
        """
    )


def _migrate_v13(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS assistant_jobs (
            job_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, project_id TEXT NOT NULL,
            request_id TEXT NOT NULL, request_hash TEXT NOT NULL, session_id TEXT NOT NULL,
            text TEXT NOT NULL, context_json TEXT NOT NULL, attachments_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'queued', run_id TEXT, worker_id TEXT,
            claim_epoch INTEGER NOT NULL DEFAULT 0, lease_expires_at TEXT,
            result_json TEXT, started_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(principal_id, project_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS ix_assistant_jobs_owner ON assistant_jobs(principal_id,project_id,created_at);
        CREATE TABLE IF NOT EXISTS assistant_job_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            run_id TEXT, event_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES assistant_jobs(job_id)
        );
        CREATE INDEX IF NOT EXISTS ix_assistant_events_job ON assistant_job_events(job_id,seq);
        CREATE TABLE IF NOT EXISTS assistant_runtime_checkpoints (
            run_id TEXT PRIMARY KEY, checkpoint_json TEXT NOT NULL,
            claim_epoch INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(run_id) REFERENCES agent_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS assistant_delegations (
            parent_run_id TEXT NOT NULL, delegation_id TEXT NOT NULL,
            request_hash TEXT NOT NULL, child_run_id TEXT NOT NULL,
            PRIMARY KEY(parent_run_id, delegation_id)
        );
    """)
