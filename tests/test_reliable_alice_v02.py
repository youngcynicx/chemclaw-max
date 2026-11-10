import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from chemclaw_lab.db import connect, init_db
from chemclaw_lab.inventory import (
    list_inventory_events,
    process_inventory_message,
    resolve_inventory_event,
)
from chemclaw_lab.models import InventoryEvent
from chemclaw_lab.seed import seed_from_json
from chemclaw_lab.validation import validate_inventory_event


def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "lab.db"
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


def test_applied_event_persists_complete_trace(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Used 5 g KBr")

    assert result["status"] == "applied"
    assert result["event"]["requires_confirmation"] is False
    conn = connect(db_path)
    try:
        proposal = conn.execute(
            "SELECT payload_json, status FROM event_proposals WHERE event_id = ?",
            (result["event_id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT event_id FROM audit_logs WHERE event_id = ?",
            (result["event_id"],),
        ).fetchone()
        transaction = conn.execute(
            "SELECT event_id FROM inventory_transactions WHERE event_id = ?",
            (result["event_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert json.loads(proposal["payload_json"])["raw_text"] == "Used 5 g KBr"
    assert proposal["status"] == "applied"
    assert audit["event_id"] == result["event_id"]
    assert transaction["event_id"] == result["event_id"]


def test_unusually_large_addition_requires_confirmation(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Added 999999 g KBr")

    assert result["status"] == "pending_confirmation"
    assert result["event"]["requires_confirmation"] is True
    assert result["audit"]["required_action"] == "confirm_large_inventory_change"
    assert result["applied"] is False
    assert chemical_amount(db_path, "chem_002") == 42.0


def test_large_fractional_usage_requires_confirmation(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Used 30 g KBr")

    assert result["status"] == "pending_confirmation"
    assert result["audit"]["required_action"] == "confirm_large_inventory_change"
    assert chemical_amount(db_path, "chem_002") == 42.0


def test_large_change_can_be_explicitly_confirmed_once(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    pending = process_inventory_message(db_path, "Added 100 g KBr")

    confirmed = resolve_inventory_event(
        db_path,
        pending["event_id"],
        approve=True,
        confirmed_by="user_001",
    )
    replay = resolve_inventory_event(
        db_path,
        confirmed["event_id"],
        approve=True,
        confirmed_by="user_001",
    )

    assert confirmed["status"] == "applied"
    assert confirmed["supersedes_event_id"] == pending["event_id"]
    assert confirmed["event"]["requires_confirmation"] is False
    assert replay["confirmation_replay"] is True
    assert chemical_amount(db_path, "chem_002") == 142.0
    conn = connect(db_path)
    try:
        original_status = conn.execute(
            "SELECT status FROM event_proposals WHERE event_id = ?",
            (pending["event_id"],),
        ).fetchone()[0]
        transaction_count = conn.execute(
            "SELECT COUNT(*) FROM inventory_transactions WHERE event_id = ?",
            (confirmed["event_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert original_status == "superseded"
    assert transaction_count == 1


def test_concurrent_confirmation_creates_one_revision(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    pending = process_inventory_message(db_path, "Added 100 g KBr")

    def confirm():
        return resolve_inventory_event(
            db_path,
            pending["event_id"],
            approve=True,
            confirmed_by="user_001",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: confirm(), range(2)))

    assert results[0]["event_id"] == results[1]["event_id"]
    assert sum(result.get("confirmation_replay", False) for result in results) == 1
    assert chemical_amount(db_path, "chem_002") == 142.0
    conn = connect(db_path)
    try:
        revisions = conn.execute(
            "SELECT COUNT(*) FROM event_proposals WHERE supersedes_event_id = ?",
            (pending["event_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert revisions == 1


def test_missing_unit_can_be_corrected_and_applied(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    pending = process_inventory_message(db_path, "Used 5 KBr")

    confirmed = resolve_inventory_event(
        db_path,
        pending["event_id"],
        approve=True,
        corrections={"unit": "g"},
    )

    assert confirmed["status"] == "applied"
    assert confirmed["event"]["unit"] == "g"
    assert chemical_amount(db_path, "chem_002") == 37.0


def test_pending_event_can_be_rejected(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    pending = process_inventory_message(db_path, "Used 500 g KBr")

    rejected = resolve_inventory_event(db_path, pending["event_id"], approve=False)

    assert rejected["status"] == "rejected"
    assert rejected["applied"] is False
    assert chemical_amount(db_path, "chem_002") == 42.0


def test_gateway_message_id_prevents_duplicate_delivery(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    first = process_inventory_message(
        db_path,
        "Used 1 g KBr",
        source="qq",
        source_message_id="qq-message-001",
    )
    duplicate = process_inventory_message(
        db_path,
        "Used 999 g KBr",
        source="qq",
        source_message_id="qq-message-001",
    )

    assert first["event_id"] == duplicate["event_id"]
    assert duplicate["idempotent_replay"] is True
    assert chemical_amount(db_path, "chem_002") == 41.0


def test_concurrent_duplicate_delivery_is_applied_once(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    def deliver():
        return process_inventory_message(
            db_path,
            "Used 1 g KBr",
            source="webhook",
            source_message_id="concurrent-001",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: deliver(), range(2)))

    assert results[0]["event_id"] == results[1]["event_id"]
    assert sum(result["idempotent_replay"] for result in results) == 1
    assert chemical_amount(db_path, "chem_002") == 41.0


def test_compatible_units_are_converted_to_inventory_unit(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Used 0.005 kg KBr")

    assert result["status"] == "applied"
    assert result["event"]["amount"] == 0.005
    assert result["event"]["unit"] == "kg"
    assert chemical_amount(db_path, "chem_002") == 37.0
    conn = connect(db_path)
    try:
        transaction = conn.execute(
            "SELECT amount_delta, unit FROM inventory_transactions WHERE event_id = ?",
            (result["event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert transaction["amount_delta"] == -5.0
    assert transaction["unit"] == "g"


def test_chinese_native_unit_is_supported(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "用了 5 克 KBr")

    assert result["status"] == "applied"
    assert result["event"]["unit"] == "g"
    assert chemical_amount(db_path, "chem_002") == 37.0


def test_formula_digit_is_not_mistaken_for_amount(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Added ZnBr2 20 g")

    assert result["event"]["amount"] == 20.0
    assert result["event"]["item_id"] == "chem_001"
    assert result["status"] == "applied"


def test_hazardous_item_requires_confirmation(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    pending = process_inventory_message(db_path, "Used 1 g TBABr3")
    confirmed = resolve_inventory_event(db_path, pending["event_id"], approve=True)

    assert pending["status"] == "pending_confirmation"
    assert pending["audit"]["required_action"] == "confirm_hazardous_inventory_change"
    assert confirmed["status"] == "applied"
    assert chemical_amount(db_path, "chem_003") == 17.0


def test_negative_amount_is_rejected(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(db_path, "Added -5 g KBr")

    assert result["status"] == "rejected"
    assert result["event"]["requires_confirmation"] is False
    assert result["audit"]["required_action"] == "provide_positive_amount"
    assert chemical_amount(db_path, "chem_002") == 42.0


def test_confirmed_overdraw_is_hard_rejected(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    pending = process_inventory_message(db_path, "Used 500 g KBr")

    confirmed = resolve_inventory_event(db_path, pending["event_id"], approve=True)

    assert confirmed["status"] == "rejected"
    assert confirmed["audit"]["required_action"] == "correct_stock_or_amount"
    assert chemical_amount(db_path, "chem_002") == 42.0


def test_seed_is_idempotent_and_does_not_reset_live_stock(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    process_inventory_message(db_path, "Used 5 g KBr")

    seed_from_json(db_path, Path("demo_data/lab_seed.json"))

    assert chemical_amount(db_path, "chem_002") == 37.0


def test_event_schema_is_executed(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    result = process_inventory_message(db_path, "Used 1 g KBr", apply_approved=False)
    event = InventoryEvent.from_payload(result["event"])

    with pytest.raises(ValueError, match="confidence"):
        validate_inventory_event(replace(event, confidence=1.5))


def test_confirmation_rejects_unknown_actor_identity(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    pending = process_inventory_message(db_path, "Used 5 KBr")

    with pytest.raises(ValueError, match="Unknown user_id"):
        resolve_inventory_event(
            db_path,
            pending["event_id"],
            approve=True,
            corrections={"unit": "g", "user_id": "missing_user"},
        )

    assert chemical_amount(db_path, "chem_002") == 42.0


def test_list_events_can_filter_by_status(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    process_inventory_message(db_path, "Used 1 g KBr")
    process_inventory_message(db_path, "Added 100 g KBr")

    pending = list_inventory_events(db_path, status="pending_confirmation")

    assert len(pending) == 1
    assert pending[0]["status"] == "pending_confirmation"


def test_cli_confirmation_round_trip(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    ingest = subprocess.run(
        [
            sys.executable,
            "-m",
            "chemclaw_lab.cli",
            "ingest-message",
            "Used 5 KBr",
            "--db",
            str(db_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    pending = json.loads(ingest.stdout)

    confirmation = subprocess.run(
        [
            sys.executable,
            "-m",
            "chemclaw_lab.cli",
            "confirm-event",
            pending["event_id"],
            "--db",
            str(db_path),
            "--unit",
            "g",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    confirmed = json.loads(confirmation.stdout)

    assert confirmed["status"] == "applied"
    assert chemical_amount(db_path, "chem_002") == 37.0
