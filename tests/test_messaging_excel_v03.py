import json
import threading
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import pytest

from chemclaw_lab.api import create_api_server
from chemclaw_lab.db import connect, init_db
from chemclaw_lab.excel import (
    export_inventory_workbook,
    import_inventory_workbook,
    read_inventory_workbook,
)
from chemclaw_lab.gateway import ChemClawClient
from chemclaw_lab.inventory import process_inventory_message
from chemclaw_lab.seed import seed_from_json


def seeded_db(tmp_path: Path, name: str = "lab.db") -> Path:
    db_path = tmp_path / name
    init_db(db_path)
    seed_from_json(db_path, Path("demo_data/lab_seed.json"))
    return db_path


def chemical_amount(db_path: Path, chemical_id: str) -> float:
    conn = connect(db_path)
    try:
        return float(
            conn.execute(
                "SELECT amount FROM chemicals WHERE chemical_id = ?",
                (chemical_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_excel_projection_is_valid_and_formula_driven(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    output = tmp_path / "inventory.xlsx"

    export_inventory_workbook(db_path, output)
    rows = read_inventory_workbook(output)

    assert output.read_bytes().startswith(b"PK")
    assert len(rows) == 3
    assert {row["chemical_id"] for row in rows} == {
        "chem_001",
        "chem_002",
        "chem_003",
    }
    with zipfile.ZipFile(output) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        inventory_sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert all(name in workbook for name in ["Inventory", "Transactions", "Events", "README"])
    assert "<f>E2&lt;=G2</f>" in inventory_sheet
    assert "conditionalFormatting" in inventory_sheet


def test_excel_projection_sanitizes_oversized_or_invalid_text(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    conn = connect(db_path)
    try:
        conn.execute(
            """
            UPDATE inventory_transactions
            SET raw_text = ?
            WHERE transaction_id = 'txn_001'
            """,
            ("A" * 40_000 + "\x01",),
        )
        conn.commit()
    finally:
        conn.close()
    output = tmp_path / "sanitized.xlsx"

    export_inventory_workbook(db_path, output)

    with zipfile.ZipFile(output) as archive:
        transaction_xml = archive.read("xl/worksheets/sheet2.xml")
    ElementTree.fromstring(transaction_xml)
    decoded = transaction_xml.decode("utf-8")
    assert "\x01" not in decoded
    assert "\u2026 [truncated]" in decoded


def test_excel_create_import_preserves_event_ledger(tmp_path: Path):
    source_db = seeded_db(tmp_path, "source.db")
    workbook = tmp_path / "inventory.xlsx"
    export_inventory_workbook(source_db, workbook)
    target_db = tmp_path / "target.db"
    init_db(target_db)

    result = import_inventory_workbook(
        target_db,
        workbook,
        mode="create",
        imported_by="user_001",
    )

    assert result["row_count"] == 3
    assert result["event_count"] == 3
    assert result["idempotent_replay"] is False
    conn = connect(target_db)
    try:
        statuses = {
            row[0]
            for row in conn.execute("SELECT DISTINCT status FROM event_proposals")
        }
        projection = conn.execute(
            """
            SELECT c.amount, COALESCE(SUM(t.amount_delta), 0) AS ledger_amount
            FROM chemicals c
            LEFT JOIN inventory_transactions t USING (chemical_id)
            GROUP BY c.chemical_id
            """
        ).fetchall()
        imported_transactions = conn.execute(
            """
            SELECT COUNT(*)
            FROM inventory_transactions
            WHERE transaction_type = 'opening_balance'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert statuses == {"applied"}
    assert imported_transactions == 3
    assert all(abs(row["amount"] - row["ledger_amount"]) < 1e-9 for row in projection)


def test_excel_import_is_content_idempotent(tmp_path: Path):
    source_db = seeded_db(tmp_path, "source.db")
    workbook = tmp_path / "inventory.xlsx"
    export_inventory_workbook(source_db, workbook)
    target_db = tmp_path / "target.db"
    init_db(target_db)

    first = import_inventory_workbook(target_db, workbook, mode="create")
    second = import_inventory_workbook(target_db, workbook, mode="merge")

    assert first["batch_id"] == second["batch_id"]
    assert second["idempotent_replay"] is True
    conn = connect(target_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM event_proposals").fetchone()[0] == 3
    finally:
        conn.close()


def test_excel_merge_records_adjustment_events(tmp_path: Path):
    target_db = seeded_db(tmp_path, "target.db")
    source_db = seeded_db(tmp_path, "source.db")
    process_inventory_message(source_db, "Used 5 g KBr")
    workbook = tmp_path / "updated_inventory.xlsx"
    export_inventory_workbook(source_db, workbook)

    result = import_inventory_workbook(target_db, workbook, mode="merge")

    assert result["event_count"] == 1
    assert chemical_amount(target_db, "chem_002") == 37.0
    conn = connect(target_db)
    try:
        latest = conn.execute(
            """
            SELECT transaction_type, amount_delta, confirmation_status
            FROM inventory_transactions
            WHERE event_id IS NOT NULL
            ORDER BY rowid DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert latest["transaction_type"] == "adjust"
    assert latest["amount_delta"] == -5.0
    assert latest["confirmation_status"] == "confirmed"


def test_excel_merge_converts_threshold_metadata_to_stored_unit(tmp_path: Path):
    target_db = seeded_db(tmp_path, "target.db")
    source_db = seeded_db(tmp_path, "source.db")
    conn = connect(source_db)
    try:
        conn.execute(
            """
            UPDATE chemicals
            SET amount = 0.25, unit = 'kg', threshold = 0.05,
                max_auto_delta = 0.1
            WHERE chemical_id = 'chem_001'
            """
        )
        conn.commit()
    finally:
        conn.close()
    workbook = tmp_path / "converted_inventory.xlsx"
    export_inventory_workbook(source_db, workbook)

    import_inventory_workbook(target_db, workbook, mode="merge")

    conn = connect(target_db)
    try:
        chemical = conn.execute(
            """
            SELECT amount, unit, threshold, max_auto_delta
            FROM chemicals
            WHERE chemical_id = 'chem_001'
            """
        ).fetchone()
    finally:
        conn.close()
    assert chemical["amount"] == 250.0
    assert chemical["unit"] == "g"
    assert chemical["threshold"] == 50.0
    assert chemical["max_auto_delta"] == 100.0


def test_excel_merge_rejects_name_collision_with_existing_inventory(
    tmp_path: Path,
):
    target_db = seeded_db(tmp_path, "target.db")
    source_db = tmp_path / "source.db"
    init_db(source_db)
    conn = connect(source_db)
    try:
        conn.execute(
            """
            INSERT INTO chemicals
              (chemical_id, name, amount, unit)
            VALUES
              ('chem_new_1', 'AAA valid first row', 1, 'g'),
              ('chem_new_2', 'KBr', 1, 'g')
            """
        )
        conn.commit()
    finally:
        conn.close()
    workbook = tmp_path / "name_collision.xlsx"
    export_inventory_workbook(source_db, workbook)

    with pytest.raises(ValueError, match="already used by chem_002"):
        import_inventory_workbook(target_db, workbook, mode="merge")

    assert chemical_amount(target_db, "chem_002") == 42.0
    conn = connect(target_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM chemicals WHERE chemical_id = 'chem_new_1'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    finally:
        conn.close()


def test_create_import_refuses_nonempty_inventory_atomically(tmp_path: Path):
    db_path = seeded_db(tmp_path, "target.db")
    workbook = tmp_path / "inventory.xlsx"
    export_inventory_workbook(db_path, workbook)

    with pytest.raises(ValueError, match="empty chemical inventory"):
        import_inventory_workbook(db_path, workbook, mode="create")

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.fixture
def api_server(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    server = create_api_server(db_path, port=0, api_key="test-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield db_path, server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_gateway_identity_message_and_idempotency(api_server):
    db_path, server = api_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    client = ChemClawClient(base_url, api_key="test-secret")

    identity = client.map_identity(
        source="webchat",
        external_user_id="web-user-001",
        user_id="user_002",
        display_name="Lin",
    )
    first = client.send_message(
        source="webchat",
        source_message_id="message-001",
        raw_text="Used 1 g KBr",
        external_user_id="web-user-001",
        project_id="proj_001",
    )
    duplicate = client.send_message(
        source="webchat",
        source_message_id="message-001",
        raw_text="Used 999 g KBr",
        external_user_id="web-user-001",
        project_id="proj_001",
    )

    assert client.health()["status"] == "ok"
    assert identity["user_id"] == "user_002"
    assert first["event"]["user_id"] == "user_002"
    assert first["event"]["project_id"] == "proj_001"
    assert duplicate["event_id"] == first["event_id"]
    assert duplicate["idempotent_replay"] is True
    assert chemical_amount(db_path, "chem_002") == 41.0


def test_http_confirmation_and_queries(api_server):
    _, server = api_server
    client = ChemClawClient(
        f"http://127.0.0.1:{server.server_port}",
        api_key="test-secret",
    )
    pending = client.send_message(
        source="webhook",
        source_message_id="message-002",
        raw_text="Added 100 g KBr",
    )

    confirmed = client.confirm_event(
        pending["event_id"],
        confirmed_by="user_001",
    )

    assert pending["status"] == "pending_confirmation"
    assert confirmed["status"] == "applied"
    assert any(item["chemical_id"] == "chem_002" for item in client.list_chemicals())
    assert any(item["event_id"] == confirmed["event_id"] for item in client.list_events())


def test_http_api_rejects_missing_credentials(api_server):
    _, server = api_server
    client = ChemClawClient(f"http://127.0.0.1:{server.server_port}")

    with pytest.raises(ValueError, match="401"):
        client.list_chemicals()


def test_http_excel_download(api_server, tmp_path: Path):
    _, server = api_server
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/inventory/export.xlsx",
        headers={"Authorization": "Bearer test-secret"},
    )
    with urlopen(request, timeout=10) as response:
        content = response.read()
        content_type = response.headers["Content-Type"]
    output = tmp_path / "download.xlsx"
    output.write_bytes(content)

    assert content.startswith(b"PK")
    assert "spreadsheetml.sheet" in content_type
    assert len(read_inventory_workbook(output)) == 3


def test_http_rejects_unmapped_gateway_identity(api_server):
    _, server = api_server
    client = ChemClawClient(
        f"http://127.0.0.1:{server.server_port}",
        api_key="test-secret",
    )

    with pytest.raises(ValueError, match="Unmapped gateway identity"):
        client.send_message(
            source="webchat",
            source_message_id="message-003",
            raw_text="Used 1 g KBr",
            external_user_id="missing-user",
        )


def test_remote_binding_requires_api_key(tmp_path: Path):
    db_path = tmp_path / "lab.db"

    with pytest.raises(ValueError, match="requires an API key"):
        create_api_server(db_path, host="0.0.0.0", port=0)
    with pytest.raises(ValueError, match="requires an API key"):
        create_api_server(db_path, host="0.0.0.0", port=0, api_key="")
    with pytest.raises(ValueError, match="requires an API key"):
        create_api_server(db_path, host="0.0.0.0", port=0, api_key="   ")


def test_http_requires_boolean_flags(api_server):
    _, server = api_server
    payload = json.dumps(
        {
            "source": "webhook",
            "source_message_id": "message-004",
            "raw_text": "Used 1 g KBr",
            "dry_run": "false",
        }
    ).encode()
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/inventory/messages",
        method="POST",
        data=payload,
        headers={
            "Authorization": "Bearer test-secret",
            "Content-Type": "application/json",
        },
    )

    with pytest.raises(Exception) as caught:
        urlopen(request, timeout=10)
    assert "400" in str(caught.value)
