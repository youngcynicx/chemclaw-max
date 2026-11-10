"""S07 cross-domain inventory closure tests (plan §13).

Covers the six acceptance checks plus the fence rules: monotonic balance
revisions, cross-channel duplicate detection, plans/recipes never reaching
stock, historical imports rejected with a trace, stale-revision fencing,
correction-linked adjustment proposals, and mixed-request status listing.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest

from chemclaw_lab import auth, capabilities, db, inventory
from chemclaw_lab.db import connect, init_db
from chemclaw_lab.ocr_port import FixtureRasterTranscriber
from chemclaw_lab.service_config import ServiceSettings

# ---------------------------------------------------------------------------
# Fixture: same env shape as S04 plus a two-chemical catalog.
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "s07.db"
    init_db(str(db_path))
    principals = {
        "owner": auth.PrincipalContext(
            principal_id="prin_root",
            kind="user",
            display_name="Owner",
            memberships={"proj_lab": "owner", "proj_other": "owner"},
        ),
        "member": auth.PrincipalContext(
            principal_id="prin_lab",
            kind="user",
            display_name="Member",
            memberships={"proj_lab": "member", "proj_other": "member"},
        ),
    }
    from chemclaw_lab import ocr_port

    ocr_registry = ocr_port.default_registry()
    ocr_registry.register(FixtureRasterTranscriber(name="fixture_ocr"))
    settings = ServiceSettings(
        storage_root=tmp_path / "storage",
        registry_factory=lambda: ocr_registry,
    )
    conn = connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO principals (principal_id, kind, display_name) VALUES (?, ?, ?)",
            [
                ("prin_root", "human", "Owner"),
                ("prin_lab", "human", "Member"),
            ],
        )
        conn.executemany(
            "INSERT INTO projects (project_id, name, description) VALUES (?, ?, ?)",
            [
                ("proj_lab", "Lab", "S07 lab project"),
                ("proj_other", "Other", "S07 other project"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO chemicals
              (chemical_id, name, formula, location, amount, unit, threshold,
               hazard_level, max_auto_delta, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            [
                ("chm_nacl", "Sodium chloride", "NaCl", "A-1", 100.0, "g", 5.0, "low", 50.0),
                ("chm_water", "Deionized water", "H2O", "B-2", 500.0, "ml", 50.0, "low", 300.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "db_path": str(db_path),
        "settings": settings,
        "owner": principals["owner"],
        "member": principals["member"],
    }


def _invoke(env, capability, params, context=None, operation_id="op-default"):
    return capabilities.invoke_capability(
        env["db_path"],
        context or env["member"],
        capability,
        params,
        operation_id=operation_id,
        services=env["settings"],
    )


def _png_bytes():
    from chemclaw_lab import pnglite

    return pnglite.solid_png(60, 40, (240, 240, 245))


def _block(text, order, block_id=None):
    return {
        "block_id": block_id or f"b-{order}",
        "block_kind": "handwriting",
        "reading_order": order,
        "text": text,
        "table_cell": None,
        "geometry": {"x": 5, "y": 5 + order * 10, "width": 40, "height": 8},
        "coordinate_system": {
            "origin": "TOPLEFT",
            "units": "pixel",
            "image_ref": "original",
            "frame_width": 60,
            "frame_height": 40,
            "dpi": 300,
        },
        "source_page": 1,
        "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer", "version": "0.3.0"},
        "quality_flags": [],
    }


def _setup_page(env, lines, *, operation_id):
    envelope = _invoke(
        env,
        "evidence.document.create",
        {
            "project_id": "proj_lab",
            "notebook_code": f"NB-{operation_id}",
            "title": "S07 notebook",
            "author": "yu",
            "source_channel": "photo_upload",
        },
        operation_id=f"{operation_id}-doc",
    )
    document_id = envelope["result"]["document_id"]
    from chemclaw_lab import pnglite

    seed = sum(ord(c) for c in operation_id) % 200
    page_env = _invoke(
        env,
        "evidence.page.ingest",
        {
            "document_id": document_id,
            "page_order": 1,
            "filename": "page_1.png",
            "content_data_url": "data:image/png;base64,"
            + base64.b64encode(pnglite.solid_png(60, 40, (240, seed, 245))).decode("ascii"),
        },
        operation_id=f"{operation_id}-page",
    )
    page_id = page_env["result"]["page"]["page_id"]
    blocks = [_block(line, i, block_id=f"b-{operation_id}-{i}") for i, line in enumerate(lines)]
    run_env = _invoke(
        env,
        "evidence.transcribe.run",
        {"page_id": page_id, "provider": "fixture_ocr", "config": {"fixture_blocks": blocks}},
        operation_id=f"{operation_id}-run",
    )
    assert run_env["result"]["status"] == "succeeded"
    return page_id


def _seed_sample(env, sample_id=None, label="S-1"):
    """Idempotent on label: reuses an existing same-label sample instead of
    manufacturing a same-name ambiguity (which the S04 linker would turn
    into a clarification)."""
    sid = sample_id or f"smp_{uuid.uuid4().hex[:10]}"
    conn = connect(env["db_path"])
    try:
        existing = conn.execute(
            "SELECT sample_id FROM samples WHERE project_id = ? AND label = ? LIMIT 1",
            ("proj_lab", label),
        ).fetchone()
        if existing is not None:
            return existing["sample_id"]
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES (?, ?, ?)",
            (sid, "proj_lab", label),
        )
        conn.commit()
    finally:
        conn.close()
    return sid


def _operation_item(page_id, raw_text, amount, unit, *, item_key="t1", occurred_at=None,
                    historical=False, block_id="b-0", sample_id=None):
    fields = {
        "amount": {"normalized": amount, "unit": unit},
        "unit": {"normalized": unit},
    }
    item = {
        "item_key": item_key,
        "event_kind": "operation",
        "raw_text": raw_text,
        "fields": fields,
        "evidence_refs": [{"page_id": page_id, "transcript_revision": 1, "block_id": block_id}],
    }
    if sample_id is not None:
        fields["sample_label"] = {"normalized": "S-1", "raw": "S-1", "uncertain": False}
        item["sample_match"] = {"action": "link", "candidates": [{"sample_id": sample_id, "label": "S-1"}]}
    if occurred_at is not None:
        item["occurred_at"] = occurred_at
    if historical:
        item["historical_import"] = True
    return item


def _seed_task(env, task_id=None, project_id="proj_lab"):
    tid = task_id or f"task_{uuid.uuid4().hex[:10]}"
    conn = connect(env["db_path"])
    try:
        conn.execute(
            """
            INSERT INTO tasks (task_id, task_type, title, principal_id, project_id, status, revision)
            VALUES (?, 'lab_request', 'S07 cross-domain request', 'prin_lab', ?, 'queued', 1)
            """,
            (tid, project_id),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def _apply_operation(env, item, *, operation_id, page_id=None, sample="S-1",
                     with_sample=True):
    setup_id = f"{operation_id}-pg"
    page_id = page_id or _setup_page(env, [item["raw_text"]], operation_id=setup_id)
    item = dict(item)
    # Bind the intended facts to the actual page's active run/block. The old
    # fixture changed block_id but retained a placeholder page_id such as "p".
    conn = connect(env["db_path"])
    try:
        source = conn.execute(
            "SELECT pt.active_revision,b.block_id,b.block_json FROM page_transcripts pt "
            "JOIN transcript_blocks b ON b.run_id=pt.active_run_id "
            "WHERE pt.page_id=? ORDER BY b.reading_order LIMIT 1", (page_id,),
        ).fetchone()
        assert source is not None
        assert json.loads(source["block_json"])["text"] == item["raw_text"]
    finally:
        conn.close()
    item["evidence_refs"] = [{"page_id": page_id, "transcript_revision": source["active_revision"],
                              "block_id": source["block_id"]}]
    task_id = _seed_task(env)
    if with_sample:
        sid = _seed_sample(env, label=sample)
        item = json.loads(json.dumps(item))
        item["fields"]["sample_label"] = {"normalized": sample, "raw": sample, "uncertain": False}
        item["sample_match"] = {
            "action": "link",
            "candidates": [{"sample_id": sid, "label": sample}],
        }
    created = _invoke(
        env,
        "memory.proposal.create",
        {
            "project_id": "proj_lab",
            "page_id": page_id,
            "transcript_revision": 1,
            "profile_id": "chemistry_notebook",
            "profile_version": "1.0",
            "acceptance_mode": "review",  # Explicit A-side confirmation precedes inventory bridging.
            "operation_id": f"{operation_id}-create",
            "items": [item],
            "task_id": task_id,
        },
        operation_id=f"{operation_id}-create",
    )
    proposal_id = created["result"]["proposal"]["proposal_id"]
    revision = created["result"]["proposal"]["revision"]
    _invoke(
        env,
        "memory.proposal.confirm_items",
        {
            "proposal_id": proposal_id,
            "item_keys": [item["item_key"]],
            "decision": "confirmed",
            "expected_proposal_revision": revision,
        },
        operation_id=f"{operation_id}-confirm",
    )
    applied = _invoke(
        env,
        "memory.proposal.apply",
        {"proposal_id": proposal_id, "operation_id": f"{operation_id}-apply"},
        operation_id=f"{operation_id}-apply",
    )
    return proposal_id, applied


def _deliver_and_ingest(env, operation_id="op-bridge"):
    _invoke(env, "delivery.process", {}, context=env["owner"], operation_id=f"{operation_id}-deliver")
    return _invoke(
        env,
        "inventory.inbound.process",
        {},
        context=env["owner"],
        operation_id=f"{operation_id}-inbound",
    )


def _stock(env, chemical_id="chm_nacl"):
    conn = connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT amount, revision FROM chemicals WHERE chemical_id = ?",
            (chemical_id,),
        ).fetchone()
        return float(row["amount"]), int(row["revision"])
    finally:
        conn.close()


def _subtasks(env, **filters):
    conn = connect(env["db_path"])
    try:
        rows = inventory.list_inventory_subtasks(conn, **filters)
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Revision monotonicity (§13.3)
# ---------------------------------------------------------------------------


def test_apply_inventory_event_bumps_revision(env):
    conn = connect(env["db_path"])
    try:
        event = inventory.InventoryEvent(
            event_type="inventory_update",
            event_subtype="use",
            item_type="chemical",
            item_id="chm_nacl",
            item_name="Sodium chloride",
            amount=5.0,
            unit="g",
            transaction_type="use",
            confidence=1.0,
            requires_confirmation=False,
            raw_text="first scoop",
            project_id="proj_lab",
        )
        for msg in ("msg-r1", "msg-r2"):
            conn.execute(
                "INSERT INTO message_logs (message_id, source, raw_text) VALUES (?, 'test', ?)",
                (msg, "first scoop"),
            )
        inventory.persist_inventory_event(conn, "msg-r1", event)
        inventory.persist_inventory_event(conn, "msg-r2", event)
        row = conn.execute(
            "SELECT amount, revision FROM chemicals WHERE chemical_id = 'chm_nacl'"
        ).fetchone()
        assert float(row["amount"]) == pytest.approx(90.0)
        assert int(row["revision"]) == 2
    finally:
        conn.close()


def test_stale_expected_revision_still_rejected(env):
    amount, revision = _stock(env)
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(
            env,
            "inventory.message.process",
            {
                "project_id": "proj_lab",
                "raw_text": "Used 5 g of Sodium chloride",
                "expected_revision": revision + 3,
            },
            operation_id="op-stale",
        )
    assert excinfo.value.code == "revision_conflict"
    assert _stock(env)[0] == amount


# ---------------------------------------------------------------------------
# Cross-domain happy path (§13.1, §13.6)
# ---------------------------------------------------------------------------


def test_cross_domain_happy_path(env):
    _, _ = _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g"),
        operation_id="op-happy",
    )
    # outbox produced by the B-domain apply
    conn = connect(env["db_path"])
    try:
        rows = conn.execute(
            "SELECT outbox_id, payload_json FROM outbox WHERE message_type = 'inventory_proposal'"
        ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["amount"] == 5.0 and payload["unit"] == "g"
    finally:
        conn.close()

    _invoke(env, "delivery.process", {}, context=env["owner"], operation_id="op-happy-deliver")
    bridge = _invoke(env, "inventory.inbound.process", {}, context=env["owner"], operation_id="op-happy-inbound")
    subtasks = bridge["result"]["subtasks"]
    assert len(subtasks) == 1
    sub = subtasks[0]
    assert sub["status"] == "pending"
    assert sub["chemical_id"] == "chm_nacl"
    assert sub["amount_delta"] == pytest.approx(-5.0)
    assert sub["parent_task_id"]  # B-side task linked (§13.6)
    assert sub["expected_revision"] == 0

    amount, revision = _stock(env)
    assert (amount, revision) == (100.0, 0)

    executed = _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": sub["subtask_id"]},
        operation_id="op-happy-exec",
    )
    assert executed["action_status"] == "applied"
    assert executed["result"]["inventory_transaction_id"]
    assert executed["result"]["event_id"]
    refs = {ref["kind"]: ref["id"] for ref in executed["object_refs"]}
    assert refs["transaction"] == executed["result"]["inventory_transaction_id"]
    assert _stock(env) == (95.0, 1)


def test_reingest_replays_same_subtask(env):
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g"),
        operation_id="op-replay",
    )
    _deliver_and_ingest(env, operation_id="op-replay-1")
    first = _subtasks(env)
    assert len(first) == 1

    again = _deliver_and_ingest(env, operation_id="op-replay-2")
    items = again["result"]["subtasks"]
    assert len(items) == 1
    assert items[0]["idempotent_replay"] is True
    assert items[0]["subtask_id"] == first[0]["subtask_id"]
    assert len(_subtasks(env)) == 1


# ---------------------------------------------------------------------------
# Cross-channel duplicates (§13.4)
# ---------------------------------------------------------------------------


def test_cross_channel_duplicate_suspect_then_confirm(env):
    occurred = "2026-10-11T10:00:00+08:00"
    # Photo page reports the usage...
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g", occurred_at=occurred),
        operation_id="op-dup-a",
    )
    _deliver_and_ingest(env, operation_id="op-dup-a")
    # ...and the same operation arrives again as a chat message (new page,
    # new event, identical facts).
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g", occurred_at=occurred),
        operation_id="op-dup-b",
    )
    _deliver_and_ingest(env, operation_id="op-dup-b")

    subtasks = _subtasks(env)
    assert len(subtasks) == 2
    statuses = sorted(s["status"] for s in subtasks)
    # First arrival stays pending; the re-report from another channel is the
    # suspect. Nothing is consumed until a human executes the first one.
    assert statuses == ["duplicate_suspect", "pending"]
    suspect = next(s for s in subtasks if s["status"] == "duplicate_suspect")
    assert suspect["error_code"].startswith("duplicate_of:")
    assert _stock(env)[0] == 100.0

    confirmed = _invoke(
        env,
        "inventory.subtask.resolve",
        {"subtask_id": suspect["subtask_id"], "confirm_duplicate": True},
        operation_id="op-dup-confirm",
    )
    assert confirmed["result"]["status"] == "pending"
    executed = _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": suspect["subtask_id"]},
        operation_id="op-dup-exec",
    )
    assert executed["result"]["status"] == "applied"
    assert _stock(env)[0] == 95.0  # human confirmed: two real usages


def test_duplicate_reject_cancels(env):
    occurred = "2026-10-11T11:00:00+08:00"
    _apply_operation(
        env,
        _operation_item("p", "Used 3 g of Sodium chloride into S-1", 3.0, "g", occurred_at=occurred),
        operation_id="op-rej-a",
    )
    _deliver_and_ingest(env, operation_id="op-rej-a")
    _apply_operation(
        env,
        _operation_item("p", "Used 3 g of Sodium chloride into S-1", 3.0, "g", occurred_at=occurred),
        operation_id="op-rej-b",
    )
    _deliver_and_ingest(env, operation_id="op-rej-b")
    suspect = next(s for s in _subtasks(env) if s["status"] == "duplicate_suspect")
    rejected = _invoke(
        env,
        "inventory.subtask.resolve",
        {"subtask_id": suspect["subtask_id"], "confirm_duplicate": False},
        operation_id="op-rej-resolve",
    )
    assert rejected["result"]["status"] == "cancelled"
    # The first arrival was never executed, so nothing was ever consumed.
    assert _stock(env)[0] == 100.0


def test_unknown_time_not_flagged_as_duplicate(env):
    # Same facts, no occurred_at: impossible to compare -> normal pending.
    _apply_operation(
        env,
        _operation_item("p", "Used 2 g of Sodium chloride into S-1", 2.0, "g"),
        operation_id="op-nodup-a",
    )
    _deliver_and_ingest(env, operation_id="op-nodup-a")
    _apply_operation(
        env,
        _operation_item("p", "Used 2 g of Sodium chloride into S-1", 2.0, "g"),
        operation_id="op-nodup-b",
    )
    _deliver_and_ingest(env, operation_id="op-nodup-b")
    statuses = sorted(s["status"] for s in _subtasks(env))
    assert statuses == ["pending", "pending"]


# ---------------------------------------------------------------------------
# Clarification: identity and units (§13.2, §13.5)
# ---------------------------------------------------------------------------


def test_ambiguous_chemical_clarifies_with_candidates(env):
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride and 5 g of Deionized water into S-1", 5.0, "g"),
        operation_id="op-amb",
    )
    _deliver_and_ingest(env, operation_id="op-amb")
    subtasks = _subtasks(env, status="clarification")
    assert len(subtasks) == 1
    sub = subtasks[0]
    assert sub["error_code"] == "chemical_identity"
    candidates = json.loads(
        connect(env["db_path"]).execute(
            "SELECT last_error FROM inventory_subtasks WHERE subtask_id = ?",
            (sub["subtask_id"],),
        ).fetchone()[0]
    )
    assert {c["chemical_id"] for c in candidates["candidates"]} == {"chm_nacl", "chm_water"}

    resolved = _invoke(
        env,
        "inventory.subtask.resolve",
        {"subtask_id": sub["subtask_id"], "chemical_id": "chm_nacl"},
        operation_id="op-amb-resolve",
    )
    assert resolved["result"]["status"] == "pending"
    assert resolved["result"]["chemical_id"] == "chm_nacl"
    executed = _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": sub["subtask_id"]},
        operation_id="op-amb-exec",
    )
    assert executed["result"]["status"] == "applied"
    assert _stock(env)[0] == 95.0


def test_unit_conversion_needs_authorized_condition(env):
    _apply_operation(
        env,
        _operation_item("p", "Used 10 ml of Sodium chloride into S-1", 10.0, "ml"),
        operation_id="op-unit",
    )
    _deliver_and_ingest(env, operation_id="op-unit")
    subtasks = _subtasks(env, status="clarification")
    assert len(subtasks) == 1
    assert subtasks[0]["error_code"] == "unit_conversion_needed"
    assert _stock(env)[0] == 100.0  # nothing auto-derived (§13.5)

    # Human supplies the authorized conversion as a corrected amount.
    resolved = _invoke(
        env,
        "inventory.subtask.resolve",
        {"subtask_id": subtasks[0]["subtask_id"], "unit": "g", "amount": 11.7},
        operation_id="op-unit-resolve",
    )
    assert resolved["result"]["status"] == "pending"
    assert resolved["result"]["amount_delta"] == pytest.approx(-11.7)
    _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": subtasks[0]["subtask_id"]},
        operation_id="op-unit-exec",
    )
    assert _stock(env)[0] == pytest.approx(88.3)


# ---------------------------------------------------------------------------
# Plans, recipes, historical imports never touch stock (§13.3)
# ---------------------------------------------------------------------------


def test_plan_event_never_reaches_inventory(env):
    page_id = _setup_page(env, ["Tomorrow take 5 g of Sodium chloride for S-1"], operation_id="op-plan-pg")
    item = {
        "item_key": "t1",
        "event_kind": "plan",
        "raw_text": "Tomorrow take 5 g of Sodium chloride for S-1",
        "fields": {"amount": {"normalized": 5.0, "unit": "g"}, "unit": {"normalized": "g"}},
        "evidence_refs": [{"page_id": page_id, "transcript_revision": 1, "block_id": "b-op-plan-pg-0"}],
    }
    _apply_operation(env, item, operation_id="op-plan", page_id=page_id)
    conn = connect(env["db_path"])
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE message_type = 'inventory_proposal'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0
    bridge = _invoke(env, "inventory.inbound.process", {}, context=env["owner"], operation_id="op-plan-inbound")
    assert bridge["result"]["scanned"] == 0
    assert _subtasks(env) == []
    assert _stock(env)[0] == 100.0


def test_historical_import_rejected_with_trace(env):
    # apply-side: the historical marker blocks the outbox entirely.
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g", historical=True),
        operation_id="op-hist",
    )
    conn = connect(env["db_path"])
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE message_type = 'inventory_proposal'"
        ).fetchone()[0]
        head = conn.execute(
            """
            SELECT field_json FROM event_revisions
            WHERE entity_type = 'experiment_event'
            ORDER BY revision DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert count == 0
    assert json.loads(head[0]).get("historical_import") is True

    # ingest-side backstop: an old message still cannot touch current stock.
    conn = connect(env["db_path"])
    try:
        event_id = conn.execute(
            "SELECT entity_id FROM event_revisions WHERE entity_type = 'experiment_event' LIMIT 1"
        ).fetchone()[0]
        result = inventory.ingest_inventory_proposal(
            conn,
            {
                "event_id": event_id,
                "amount": 5.0,
                "unit": "g",
                "raw_text": "Used 5 g of Sodium chloride into S-1",
            },
        )
        conn.commit()
    finally:
        conn.close()
    assert result["status"] == "rejected"
    assert result["error_code"] == "historical_import"
    assert _stock(env)[0] == 100.0


# ---------------------------------------------------------------------------
# Revision fencing on the subtask path (§13.3)
# ---------------------------------------------------------------------------


def test_stale_subtask_revision_fenced_then_retry(env):
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g"),
        operation_id="op-fence",
    )
    _deliver_and_ingest(env, operation_id="op-fence")
    sub = _subtasks(env)[0]
    assert sub["expected_revision"] == 0

    # Concurrent writer moves the balance before the subtask executes.
    conn = connect(env["db_path"])
    try:
        conn.execute(
            "UPDATE chemicals SET amount = amount + 1, revision = revision + 1 WHERE chemical_id = 'chm_nacl'"
        )
        conn.commit()
    finally:
        conn.close()

    fenced = _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": sub["subtask_id"], "expected_revision": 0},
        operation_id="op-fence-exec1",
    )
    assert fenced["action_status"] == "failed"
    assert fenced["result"]["error_code"] == "stale_revision"
    assert _stock(env) == (101.0, 1)

    retried = _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": sub["subtask_id"]},
        operation_id="op-fence-exec2",
    )
    assert retried["result"]["status"] == "applied"
    assert _stock(env) == (96.0, 2)


def test_policy_gate_keeps_event_and_stock_pending(env):
    # §13.7: an over-limit usage passes the record (B-side event facts) but
    # never the stock — the S02 policy asks for human confirmation instead.
    _apply_operation(
        env,
        _operation_item("p", "Used 500 g of Sodium chloride into S-1", 500.0, "g"),
        operation_id="op-neg",
    )
    _deliver_and_ingest(env, operation_id="op-neg")
    sub = _subtasks(env)[0]
    failed = _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": sub["subtask_id"]},
        operation_id="op-neg-exec",
    )
    assert failed["action_status"] == "clarification_needed"
    assert failed["result"]["status"] == "clarification"
    assert failed["result"]["error_code"] == "policy_pending"
    assert failed["result"]["attempts"] == 1
    # B-side event facts still exist; "record saved, inventory pending" (§13.7)
    conn = connect(env["db_path"])
    try:
        head = conn.execute(
            "SELECT COUNT(*) FROM event_revisions WHERE entity_type = 'experiment_event'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert head == 1
    assert _stock(env)[0] == 100.0


def test_apply_inventory_event_refuses_negative_stock(env):
    # The policy converts an over-draft into a confirmation request, so the
    # hard guard in apply_inventory_event is a concurrency backstop.
    conn = connect(env["db_path"])
    try:
        event = inventory.InventoryEvent(
            event_type="inventory_update",
            event_subtype="use",
            item_type="chemical",
            item_id="chm_nacl",
            item_name="Sodium chloride",
            amount=500.0,
            unit="g",
            transaction_type="use",
            confidence=1.0,
            requires_confirmation=False,
            raw_text="overshoot",
            project_id="proj_lab",
        )
        conn.execute(
            "INSERT INTO message_logs (message_id, source, raw_text) VALUES ('msg-neg', 'test', 'overshoot')"
        )
        with pytest.raises(ValueError, match="negative"):
            inventory.apply_inventory_event(conn, "msg-neg", "evt-neg", event)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Corrections -> adjustment proposals (§13.8)
# ---------------------------------------------------------------------------


def test_usage_correction_creates_and_applies_adjustment(env):
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g"),
        operation_id="op-corr",
    )
    _deliver_and_ingest(env, operation_id="op-corr")
    sub = _subtasks(env)[0]
    _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": sub["subtask_id"]},
        operation_id="op-corr-exec",
    )
    # Re-read after execution: the applied transaction id is backfilled then.
    sub = _subtasks(env)[0]
    assert _stock(env) == (95.0, 1)

    event_id = sub["source_event_id"]
    corrected = _invoke(
        env,
        "memory.event.correct",
        {
            "event_id": event_id,
            "changes": {
                "fields": {
                    "amount": {"raw": "7 g", "normalized": 7.0, "uncertain": False},
                },
            },
            "reason": "weighing transcription error",
            "expected_revision": 1,
        },
        context=env["owner"],
        operation_id="op-corr-fix",
    )
    assert corrected["action_status"] in ("applied", "proposed")

    # Usage was 5 g applied as -5; corrected usage is 7 g -> -2 more needed.
    proposal = _invoke(
        env,
        "inventory.adjustment.propose",
        {
            "source_event_id": event_id,
            "corrected_amount": 7.0,
            "corrected_unit": "g",
            "reason": "corrected usage after transcription fix",
        },
        operation_id="op-corr-prop",
    )
    assert proposal["action_status"] == "proposed"
    assert proposal["result"]["amount_delta"] == pytest.approx(-2.0)
    assert proposal["result"]["original_transaction_id"] == sub["inventory_transaction_id"]

    # Idempotent re-propose replays the open proposal.
    again = _invoke(
        env,
        "inventory.adjustment.propose",
        {
            "source_event_id": event_id,
            "corrected_amount": 7.0,
            "corrected_unit": "g",
            "reason": "duplicate correction request",
        },
        operation_id="op-corr-prop-2",
    )
    assert again["result"]["idempotent_replay"] is True

    applied = _invoke(
        env,
        "inventory.adjustment.apply",
        {"adjustment_id": proposal["result"]["adjustment_id"], "expected_revision": 1},
        operation_id="op-corr-adjust-apply",
    )
    assert applied["action_status"] == "applied"
    assert _stock(env) == (93.0, 2)
    # History intact: the original transaction is untouched (§13.8).
    conn = connect(env["db_path"])
    try:
        original = conn.execute(
            "SELECT amount_delta FROM inventory_transactions WHERE transaction_id = ?",
            (sub["inventory_transaction_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert float(original[0]) == pytest.approx(-5.0)


def test_adjustment_stale_revision_marks_stale(env):
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g"),
        operation_id="op-adj",
    )
    _deliver_and_ingest(env, operation_id="op-adj")
    sub = _subtasks(env)[0]
    executed = _invoke(
        env,
        "inventory.subtask.execute",
        {"subtask_id": sub["subtask_id"]},
        operation_id="op-adj-exec",
    )
    assert executed["result"]["status"] == "applied"
    event_id = sub["source_event_id"]
    proposal = _invoke(
        env,
        "inventory.adjustment.propose",
        {
            "source_event_id": event_id,
            "corrected_amount": 8.0,
            "corrected_unit": "g",
            "reason": "second weighing pass",
        },
        operation_id="op-adj-prop",
    )
    stale = _invoke(
        env,
        "inventory.adjustment.apply",
        {"adjustment_id": proposal["result"]["adjustment_id"], "expected_revision": 42},
        operation_id="op-adj-adjust-apply",
    )
    assert stale["action_status"] == "failed"
    assert stale["result"]["status"] == "stale"
    assert _stock(env) == (95.0, 1)


# ---------------------------------------------------------------------------
# Mixed-request status (§13.6)
# ---------------------------------------------------------------------------


def test_mixed_request_status_listing(env):
    _apply_operation(
        env,
        _operation_item("p", "Used 5 g of Sodium chloride into S-1", 5.0, "g"),
        operation_id="op-mix",
    )
    _deliver_and_ingest(env, operation_id="op-mix")
    parent = _subtasks(env)[0]["parent_task_id"]
    listing = _invoke(
        env,
        "inventory.subtask.status",
        {"parent_task_id": parent},
        operation_id="op-mix-list",
    )
    items = listing["result"]["items"]
    assert len(items) == 1
    assert items[0]["parent_task_id"] == parent
    assert items[0]["source_event_id"]
    assert items[0]["evidence_id"]
