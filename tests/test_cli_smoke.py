from pathlib import Path

from chemclaw_lab.db import init_db, connect
from chemclaw_lab.inventory import process_inventory_message
from chemclaw_lab.seed import seed_from_json


def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "lab.db"
    seed_path = Path("demo_data/lab_seed.json")

    init_db(db_path)
    seed_from_json(db_path, seed_path)
    return db_path


def chemical_amount(db_path: Path, chemical_id: str) -> float:
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT amount FROM chemicals WHERE chemical_id = ?",
            (chemical_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def test_seed_smoke(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    conn = connect(db_path)
    try:
        chemical_count = conn.execute("SELECT COUNT(*) FROM chemicals").fetchone()[0]
        booking_count = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        message_count = conn.execute("SELECT COUNT(*) FROM message_logs").fetchone()[0]
    finally:
        conn.close()

    assert chemical_count >= 3
    assert booking_count >= 2
    assert message_count == 0


def test_inventory_message_approved_use_updates_stock(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Used 5 g KBr for adsorption batch")

    assert result["audit"]["decision"] == "approve"
    assert result["applied"] is True
    assert result["event"]["item_id"] == "chem_002"
    assert chemical_amount(db_path, "chem_002") == 37.0


def test_inventory_message_missing_unit_does_not_apply(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Used 5 KBr")

    assert result["audit"]["decision"] == "pending_confirmation"
    assert result["audit"]["required_action"] == "confirm_unit"
    assert result["applied"] is False
    assert chemical_amount(db_path, "chem_002") == 42.0


def test_inventory_message_unknown_chemical_does_not_apply(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Used 5 g NaCl")

    assert result["audit"]["decision"] == "pending_confirmation"
    assert result["audit"]["required_action"] == "confirm_chemical_identity"
    assert result["applied"] is False


def test_inventory_message_over_stock_does_not_apply(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Used 500 g KBr")

    assert result["audit"]["decision"] == "pending_confirmation"
    assert result["audit"]["required_action"] == "confirm_stock_deduction"
    assert result["applied"] is False
    assert chemical_amount(db_path, "chem_002") == 42.0


def test_init_db_migrates_existing_inventory_transactions_table(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    conn = connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE inventory_transactions (
                transaction_id TEXT PRIMARY KEY,
                chemical_id TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                amount_delta REAL NOT NULL,
                unit TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chemicals (
                chemical_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                unit TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO chemicals
              (chemical_id, name, amount, unit, created_at)
            VALUES ('chem_legacy', 'Legacy KBr', 1, 'g', '2025-01-02 03:04:05')
            """
        )
        conn.commit()
    finally:
        conn.close()

    init_db(db_path)

    conn = connect(db_path)
    try:
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(inventory_transactions)"
            ).fetchall()
        }
        event_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'event_proposals'"
        ).fetchone()
        chemical_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(chemicals)").fetchall()
        }
        updated_at = conn.execute(
            """
            SELECT updated_at
            FROM chemicals
            WHERE chemical_id = 'chem_legacy'
            """
        ).fetchone()[0]
        v03_tables = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name IN ('gateway_identities', 'import_batches')
                """
            ).fetchall()
        }
        s02_tables = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                    'principals', 'memberships', 'auth_credentials',
                    'session_namespaces', 'operation_results', 'tasks',
                    'agent_runs', 'task_checkpoints', 'pending_interactions',
                    'inbound_events', 'outbox', 'deliveries', 'typed_messages',
                    'principal_user_links', 'experiment_runs', 'samples',
                    'experiment_records', 'evidence_objects', 'event_revisions'
                  )
                """
            ).fetchall()
        }
        experiment_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(experiment_events)"
            ).fetchall()
        }
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        # S03 (schema v5): evidence ingestion & transcription tables exist.
        s03_tables = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN (
                  'evidence_documents', 'evidence_pages', 'page_relations',
                  'derived_artifacts', 'transcript_runs', 'transcript_blocks',
                  'page_transcripts', 'risk_fragments', 'competing_readings'
                )
                """
            ).fetchall()
        }
        # S04 (schema v6) adds the experiment-memory tables on top of v5;
        # S05 (schema v7) adds the query report + audit log tables on top;
        # S06 (schema v8) adds the agent host tables (manifests/tokens/workflows).
        # S09 (schema v10) adds the extension domains (resources/bookings,
        # research milestones, external results).
        s09_tables = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN (
                  'resources', 'resource_bookings', 'research_milestones',
                  'milestone_criteria', 'external_results'
                )
                """
            ).fetchall()
        }
    finally:
        conn.close()

    assert {
        "message_id",
        "event_id",
        "user_id",
        "project_id",
        "confidence",
        "confirmation_status",
        "raw_text",
        "created_at",
    }.issubset(columns)
    assert event_table is not None
    assert {"hazard_level", "max_auto_delta", "updated_at"}.issubset(
        chemical_columns
    )
    assert updated_at == "2025-01-02 03:04:05"
    assert v03_tables == {"gateway_identities", "import_batches"}
    # S02 (schema v4): shared contracts/identity tables exist, legacy
    # experiment history is marked source-unknown, and stock rows carry
    # a revision counter for optimistic concurrency.
    assert len(s02_tables) == 19
    assert "revision" in chemical_columns
    assert "provenance" in experiment_columns
    assert len(s03_tables) == 9
    assert len(s09_tables) == 5
    assert schema_version == 13
