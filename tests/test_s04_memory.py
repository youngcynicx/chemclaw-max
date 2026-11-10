"""S04 tests: experiment memory service (plan §10).

Acceptance mapping (plan §10 + §17):
- Plans never recorded as done; only actual usage reaches inventory . test_plan_vs_operation_separation
- Computed values never become wet-lab observations ..... test_computed_vs_observed
- No sample invented without a number .................. test_no_sample_invention / sample_creation_needs_confirmation
- Scratched-out / same-name / cross-project never auto-merge  test_sample_identity_*
- Unclear values stay unclear + centralized clarification  test_uncertain_amount_clarification_roundtrip
- Correction appends immutable revision; old stays queryable  test_correction_*
- Same page retried never duplicates events ............ test_proposal_create_idempotent_resend
- Corrected source invalidates old confirmations ....... test_source_correction_invalidates_confirmations
"""

from __future__ import annotations

import base64
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from chemclaw_lab import auth, capabilities, db, experiments, ocr_port, proposal as proposal_mod  # noqa: E402
from chemclaw_lab.ocr_port import FixtureRasterTranscriber  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "s04.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("INSERT INTO projects (project_id, name) VALUES ('proj_lab', 'Lab')")
    conn.execute("INSERT INTO projects (project_id, name) VALUES ('proj_other', 'Other')")
    for pid, name in (
        ("prin_member", "Member"),
        ("prin_lab", "LabMember"),
        ("prin_other", "OtherMember"),
        ("prin_viewer", "Viewer"),
    ):
        conn.execute(
            "INSERT INTO principals (principal_id, kind, display_name) VALUES (?, 'human', ?)",
            (pid, name),
        )
    conn.commit()
    conn.close()
    registry = ocr_port.default_registry()
    registry.register(FixtureRasterTranscriber(name="fixture_ocr"))
    settings = ServiceSettings(
        storage_root=tmp_path / "storage",
        registry_factory=lambda: registry,
    )
    return {
        "db_path": str(db_path),
        "settings": settings,
        "member": auth.PrincipalContext(
            principal_id="prin_member",
            kind="user",
            display_name="Member",
            memberships={"proj_lab": "member", "proj_other": "member"},
        ),
        "lab_member": auth.PrincipalContext(
            principal_id="prin_lab",
            kind="user",
            display_name="LabMember",
            memberships={"proj_lab": "member"},
        ),
        "other_member": auth.PrincipalContext(
            principal_id="prin_other",
            kind="user",
            display_name="OtherMember",
            memberships={"proj_other": "member"},
        ),
        "viewer": auth.PrincipalContext(
            principal_id="prin_viewer",
            kind="user",
            display_name="Viewer",
            memberships={"proj_lab": "viewer"},
        ),
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


def _block(text, order, block_kind="handwriting", block_id=None):
    return {
        "block_id": block_id or f"b-{uuid.uuid4().hex[:10]}",
        "block_kind": block_kind,
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


def _setup_page(env, lines, *, document_id=None, notebook=None, operation_id="op-setup"):
    """Document + page + one successful fixture transcription. Returns page_id."""
    if document_id is None:
        envelope = _invoke(
            env,
            "evidence.document.create",
            {
                "project_id": "proj_lab",
                # (project_id, notebook_code) is UNIQUE: default to a per-call
                # notebook so successive _setup_page calls get fresh documents.
                "notebook_code": notebook or f"NB-{operation_id}",
                "title": "S04 notebook",
                "author": "yu",
                "source_channel": "photo_upload",
            },
            operation_id=f"{operation_id}-doc",
        )
        document_id = envelope["result"]["document_id"]
    from chemclaw_lab import pnglite

    # evidence page_id derives from the content digest: vary the fixture
    # pixels per call so distinct setup calls never collide cross-document.
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
    blocks = [_block(line, i) for i, line in enumerate(lines)]
    run_env = _invoke(
        env,
        "evidence.transcribe.run",
        {"page_id": page_id, "provider": "fixture_ocr", "config": {"fixture_blocks": blocks}},
        operation_id=f"{operation_id}-run",
    )
    assert run_env["result"]["status"] == "succeeded"
    return page_id


def _make_proposal(env, page_id, lines, *, project_id="proj_lab", operation_id="op-prop",
                   context=None, items=None):
    # These legacy scenarios deliberately exercise explicit human review;
    # default knowledge adoption is covered in repair/test_default_knowledge.py.
    return _invoke(
        env,
        "memory.proposal.create",
        {
            "project_id": project_id,
            "page_id": page_id,
            "transcript_revision": 1,
            "profile_id": "chemistry_notebook",
            "profile_version": "1.0",
            "acceptance_mode": "review",
            "operation_id": operation_id,
            **({"items": items} if items is not None else {}),
        },
        context=context,
        operation_id=operation_id,
    )


def _confirm(env, proposal_id, item_keys, expected, *, principal=None, operation_id="op-cfm",
             decision="confirmed"):
    return _invoke(
        env,
        "memory.proposal.confirm_items",
        {
            "proposal_id": proposal_id,
            "item_keys": item_keys,
            "decision": decision,
            "expected_proposal_revision": expected,
        },
        context=principal,
        operation_id=operation_id,
    )


def _apply(env, proposal_id, operation_id, *, principal=None):
    return _invoke(
        env,
        "memory.proposal.apply",
        {"proposal_id": proposal_id, "operation_id": operation_id},
        context=principal,
        operation_id=operation_id,
    )


def _proposal_items(env, proposal_id):
    # Unique operation_id per call: envelope-level idempotency would otherwise
    # replay the FIRST read's cached payload and hide later item mutations.
    return _invoke(
        env, "memory.proposal.read", {"proposal_id": proposal_id},
        operation_id=f"read-{uuid.uuid4().hex[:10]}",
    )["result"]["proposal"]["items"]


def _seed_sample(env, project_id, label, sample_id):
    conn = db.connect(env["db_path"])
    try:
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES (?, ?, ?)",
            (sample_id, project_id, label),
        )
        conn.commit()
    finally:
        conn.close()


def _outbox_count(env, message_type="inventory_proposal"):
    conn = db.connect(env["db_path"])
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE message_type = ?", (message_type,)
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# proposal creation & idempotency (§17: retried page never duplicates events)


def test_proposal_create_idempotent_resend(env):
    page_id = _setup_page(env, ["Sample S-1 weighed 0.5 g"], operation_id="op-a")
    first = _make_proposal(env, page_id, None, operation_id="op-prop-1")
    assert first["action_status"] == "applied"
    assert first["result"]["idempotent"] is False
    # Same operation_id + same request → envelope-level idempotent replay.
    second = _make_proposal(env, page_id, None, operation_id="op-prop-1")
    assert second["idempotent_replay"] is True
    assert second["result"]["proposal"]["proposal_id"] == first["result"]["proposal"]["proposal_id"]

    # Same key + DIFFERENT request → conflict (S02 contract). The params
    # must actually differ: supply explicit items instead of the proposer.
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _make_proposal(
            env, page_id, None, operation_id="op-prop-1",
            items=[{
                "item_key": "t1",
                "event_kind": "operation",
                "raw_text": "changed content",
                "evidence_refs": [{
                    "page_id": page_id,
                    "transcript_revision": 1,
                    "block_id": _proposal_items(env, first["result"]["proposal"]["proposal_id"])[0]["evidence_refs"][0]["block_id"],
                }],
            }],
        )
    assert excinfo.value.code == "idempotency_conflict"


def test_offline_proposer_builds_items_from_transcript(env):
    page_id = _setup_page(
        env,
        [
            "Sample S-1 0.5 g stirred at 25 degC",
            "明天拟取 2 mL S-1",
            "观察出现白色沉淀",
            "计算得产率 76%",
        ],
        operation_id="op-b",
    )
    envelope = _make_proposal(env, page_id, None, operation_id="op-prop-2")
    items = envelope["result"]["proposal"]["items"]
    kinds = [i["event_kind"] for i in items]
    assert kinds == ["operation", "plan", "observation", "computed"]
    # Evidence refs point at real blocks of this page (§10.5)
    for item in items:
        ref = item["evidence_refs"][0]
        assert ref["page_id"] == page_id and ref["transcript_revision"] == 1
        assert ref["block_id"].startswith("b-")
    # Every item needs confirmation in the first round (§10.5)
    assert all(i["needs_confirmation"] for i in items)


def test_invalid_evidence_ref_rejected(env):
    page_id = _setup_page(env, ["Sample S-1 0.5 g"], operation_id="op-c")
    items = [{
        "item_key": "t1",
        "event_kind": "operation",
        "raw_text": "0.5 g",
        "evidence_refs": [{"page_id": page_id, "transcript_revision": 1, "block_id": "b-nope"}],
    }]
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _make_proposal(env, page_id, None, operation_id="op-prop-bad", items=items)
    assert excinfo.value.code == "invalid_evidence"


# ---------------------------------------------------------------------------
# modality separation (§10.3, §17)


def test_plan_vs_operation_separation(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_seed_plan")
    page_id = _setup_page(
        env,
        ["明天拟取 2 mL S-1", "实际取用 1 mL S-1"],
        operation_id="op-d",
    )
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-3")["result"]["proposal"]
    items = {i["event_kind"]: i for i in proposal["items"]}
    plan_item, op_item = items["plan"], items["operation"]

    _confirm(env, proposal["proposal_id"],
             [plan_item["item_key"], op_item["item_key"]], proposal["revision"],
             operation_id="op-cfm-3")
    outcome = _apply(env, proposal["proposal_id"], "op-apply-3")
    assert outcome["result"]["proposal_status"] == "applied"
    assert len(outcome["result"]["applied"]) == 2

    # Only the ACTUAL usage reaches inventory; the plan never does (§17).
    assert _outbox_count(env) == 1
    conn = db.connect(env["db_path"])
    try:
        plan_event = outcome["result"]["applied"][0]
        rows = conn.execute(
            """
            SELECT field_json, provenance_json FROM event_revisions
            WHERE entity_type='experiment_event' AND entity_id = ?
            """,
            (plan_event["event_id"],),
        ).fetchall()
        plan_fields = __import__("json").loads(rows[0]["field_json"])
        plan_prov = __import__("json").loads(rows[0]["provenance_json"])
    finally:
        conn.close()
    assert plan_fields["event_kind"] == "plan"
    assert plan_prov["result_origin"] == "reported"


def test_computed_vs_observed(env):
    page_id = _setup_page(env, ["计算得产率 76%"], operation_id="op-e")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-4")["result"]["proposal"]
    item = proposal["items"][0]
    assert item["event_kind"] == "computed"
    _confirm(env, proposal["proposal_id"], [item["item_key"]], proposal["revision"],
             operation_id="op-cfm-4")
    outcome = _apply(env, proposal["proposal_id"], "op-apply-4")
    event_id = outcome["result"]["applied"][0]["event_id"]
    event = _invoke(env, "memory.event.read", {"event_id": event_id}, context=env["viewer"])["result"]
    assert event["provenance"]["result_origin"] == "computed"


# ---------------------------------------------------------------------------
# sample identity (§10.4, §17)


def test_no_sample_invention(env):
    # 1) An operation WITHOUT any sample label is recorded, but invents no
    #    sample and proposes no inventory consumption (§17).
    page_id = _setup_page(env, ["加入 0.5 g 未知粉末"], operation_id="op-f")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-5")["result"]["proposal"]
    item = proposal["items"][0]
    assert "sample_label" not in item["fields"]
    _confirm(env, proposal["proposal_id"], [item["item_key"]], proposal["revision"],
             operation_id="op-cfm-5")
    outcome = _apply(env, proposal["proposal_id"], "op-apply-5")
    assert outcome["result"]["applied"], "label-less operation is still recordable"
    event = outcome["result"]["applied"][0]
    assert event["sample_id"] is None
    assert _outbox_count(env) == 0  # no sample → no inventory proposal
    conn = db.connect(env["db_path"])
    try:
        created = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE project_id = 'proj_lab'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert created == 0

    # 2) A NEW sample label requires an explicit creation confirmation.
    page_id2 = _setup_page(env, ["Sample S-9 0.5 g"], operation_id="op-f2")
    proposal2 = _make_proposal(env, page_id2, None, operation_id="op-prop-5b")["result"]["proposal"]
    item2 = proposal2["items"][0]
    assert item2["sample_match"]["action"] == "propose_creation"
    _confirm(env, proposal2["proposal_id"], [item2["item_key"]], proposal2["revision"],
             operation_id="op-cfm-5b")
    outcome2 = _apply(env, proposal2["proposal_id"], "op-apply-5b")
    assert outcome2["result"]["pending"] == [
        {"item_key": item2["item_key"], "reason": "sample_creation_needs_confirmation"}
    ]
    conn = db.connect(env["db_path"])
    try:
        created = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE label = 'S-9'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert created == 0  # NOT auto-created

    # Dedicated sample-creation confirmation → applied, sample exists.
    _confirm(env, proposal2["proposal_id"], [f"sample:create:{item2['item_key']}"],
             proposal2["revision"], operation_id="op-cfm-5c")
    outcome3 = _apply(env, proposal2["proposal_id"], "op-apply-5c")
    assert outcome3["result"]["applied"]
    assert outcome3["result"]["applied"][0]["sample_id"]
    assert _outbox_count(env) == 1  # actual usage now reaches inventory once


def test_sample_creation_confirmation_is_revision_bound(env):
    page_id = _setup_page(env, ["Sample S-7 1 g"], operation_id="op-n")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-13")["result"]["proposal"]
    item = proposal["items"][0]
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _confirm(env, proposal["proposal_id"], [f"sample:create:{item['item_key']}"],
                 proposal["revision"] + 1, operation_id="op-cfm-13a")
    assert excinfo.value.code == "revision_conflict"


def test_sample_identity_cross_project_isolated(env):
    # Seed two same-label samples in DIFFERENT projects.
    conn = db.connect(env["db_path"])
    try:
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_a1', 'proj_lab', 'S1')"
        )
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_b1', 'proj_other', 'S1')"
        )
        conn.commit()
    finally:
        conn.close()

    # A member of BOTH projects searching in proj_lab sees ONLY proj_lab's S1.
    match = _invoke(
        env, "memory.sample.search", {"project_id": "proj_lab", "label": "S1"},
        operation_id="op-s1",
    )["result"]
    assert match["action"] == "link"
    assert match["candidates"] == [{"sample_id": "smp_a1", "label": "S1"}]

    # A member of only proj_other sees their own S1 and never proj_lab's.
    match_other = _invoke(
        env, "memory.sample.search", {"project_id": "proj_other", "label": "S1"},
        context=env["other_member"], operation_id="op-s2",
    )["result"]
    assert match_other["action"] == "link"
    assert match_other["candidates"][0]["sample_id"] == "smp_b1"

    # Outsider to a project cannot search it at all (AuthError carries the
    # same stable code; require_project fires before capability dispatch).
    with pytest.raises((capabilities.CapabilityError, auth.AuthError)) as excinfo:
        _invoke(
            env, "memory.sample.search", {"project_id": "proj_lab", "label": "S1"},
            context=env["other_member"], operation_id="op-s3",
        )
    assert excinfo.value.code == "forbidden_scope"


def test_sample_same_name_in_project_clarifies(env):
    conn = db.connect(env["db_path"])
    try:
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_x1', 'proj_lab', 'S1')"
        )
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_x2', 'proj_lab', 'S1')"
        )
        conn.commit()
    finally:
        conn.close()
    match = _invoke(
        env, "memory.sample.search", {"project_id": "proj_lab", "label": "S1"},
        operation_id="op-s4",
    )["result"]
    assert match["action"] == "clarify"
    assert len(match["candidates"]) == 2


# ---------------------------------------------------------------------------
# verify-then-apply (§10.5)


def test_verify_then_apply_requires_confirmation(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_seed_vta")
    page_id = _setup_page(env, ["Sample S-1 0.5 g"], operation_id="op-g")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-6")["result"]["proposal"]
    item = proposal["items"][0]
    assert item["needs_confirmation"] is True

    outcome = _apply(env, proposal["proposal_id"], "op-apply-6")
    assert outcome["result"]["pending"] == [
        {"item_key": item["item_key"], "reason": "missing_confirmation"}
    ]
    assert outcome["result"]["applied"] == []
    conn = db.connect(env["db_path"])
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM event_revisions WHERE entity_type='experiment_event'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert events == 0

    # Confirm with a STALE revision → rejected.
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _confirm(env, proposal["proposal_id"], [item["item_key"]], proposal["revision"] + 5,
                 operation_id="op-cfm-6a")
    assert excinfo.value.code == "revision_conflict"

    _confirm(env, proposal["proposal_id"], [item["item_key"]], proposal["revision"],
             operation_id="op-cfm-6")
    outcome2 = _apply(env, proposal["proposal_id"], "op-apply-6b")
    assert outcome2["result"]["applied"] and outcome2["result"]["pending"] == []


def test_confirmations_are_not_derived_from_chat_text(env):
    """Clarification answers never create business approvals (§10.10)."""
    _seed_sample(env, "proj_lab", "S-1", "smp_seed_chat")
    page_id = _setup_page(env, ["Sample S-1 ?? g"], operation_id="op-h")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-7")["result"]["proposal"]
    item = proposal["items"][0]

    task = _invoke(env, "task.create", {"task_type": "memory_review", "project_id": "proj_lab"},
                   operation_id="op-task-7")["result"]
    clar = _invoke(
        env,
        "memory.clarification.open",
        {
            "proposal_id": proposal["proposal_id"],
            "task_id": task["task_id"],
            "expected_task_revision": task["revision"],
            "questions": [
                {"question_id": "q1", "prompt": "amount value?", "field": "amount",
                 "item_key": item["item_key"]}
            ],
        },
        operation_id="op-clz-7",
    )["result"]

    _invoke(
        env,
        "memory.clarification.answer",
        {
            "clarification_id": clar["clarification_id"],
            "expected_task_revision": clar["task"]["revision"],
            "expected_proposal_revision": clar["proposal_revision"],
            "answers": [{"question_id": "q1", "value": 0.5, "field": "amount",
                         "item_key": item["item_key"]}],
        },
        operation_id="op-ans-7",
    )
    conn = db.connect(env["db_path"])
    try:
        confirmations = conn.execute(
            "SELECT COUNT(*) FROM event_confirmations WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    # The chat-style answer filled a value but granted NO approval.
    assert confirmations == 0
    outcome = _apply(env, proposal["proposal_id"], "op-apply-7")
    assert outcome["result"]["pending"][0]["reason"] == "missing_confirmation"


# ---------------------------------------------------------------------------
# clarification persistence (§10.6, §10.10, §17)


def test_uncertain_amount_clarification_roundtrip(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_seed_unc")
    page_id = _setup_page(env, ["Sample S-1 ?? g stirred"], operation_id="op-i")
    envelope = _make_proposal(env, page_id, None, operation_id="op-prop-8")
    proposal = envelope["result"]["proposal"]
    item = proposal["items"][0]
    # The ?? value stays uncertain, never invented.
    assert item["uncertain_fields"] == ["amount"]
    assert item["fields"]["amount"]["uncertain"] is True
    assert item["fields"]["amount"]["normalized"] is None

    task = _invoke(env, "task.create", {"task_type": "memory_review", "project_id": "proj_lab"},
                   operation_id="op-task-8")["result"]
    clar = _invoke(
        env,
        "memory.clarification.open",
        {
            "proposal_id": proposal["proposal_id"],
            "task_id": task["task_id"],
            "expected_task_revision": task["revision"],
            "questions": [
                {"question_id": "q-amount", "prompt": "What was the mass?", "field": "amount",
                 "item_key": item["item_key"]},
                {"question_id": "q-state", "prompt": "Observed state?", "field": "observed_state",
                 "item_key": item["item_key"]},
            ],
        },
        operation_id="op-clz-8",
    )["result"]
    assert clar["task"]["status"] == "waiting_input"

    # Wrong owner cannot answer (§10.10: off-owner rejected).
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(
            env,
            "memory.clarification.answer",
            {
                "clarification_id": clar["clarification_id"],
                "expected_task_revision": clar["task"]["revision"],
                "expected_proposal_revision": clar["proposal_revision"],
                "answers": [{"question_id": "q-amount", "value": 0.5}],
            },
            context=env["other_member"],
            operation_id="op-ans-8a",
        )
    assert excinfo.value.code == "forbidden_scope"

    # Stale proposal revision rejected.
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(
            env,
            "memory.clarification.answer",
            {
                "clarification_id": clar["clarification_id"],
                "expected_task_revision": clar["task"]["revision"],
                "expected_proposal_revision": clar["proposal_revision"] + 3,
                "answers": [{"question_id": "q-amount", "value": 0.5}],
            },
            operation_id="op-ans-8b",
        )
    assert excinfo.value.code == "revision_conflict"

    # "Unsure" is allowed (§10.6) and leaves the field pending.
    first = _invoke(
        env,
        "memory.clarification.answer",
        {
            "clarification_id": clar["clarification_id"],
            "expected_task_revision": clar["task"]["revision"],
            "expected_proposal_revision": clar["proposal_revision"],
            "answers": [{"question_id": "q-amount", "value": 0.5, "field": "amount",
                         "item_key": item["item_key"]}],
        },
        operation_id="op-ans-8c",
    )["result"]
    assert first["status"] == "partially_answered"

    # Replay of the SAME answers is idempotent.
    replay = _invoke(
        env,
        "memory.clarification.answer",
        {
            "clarification_id": clar["clarification_id"],
            "expected_task_revision": clar["task"]["revision"],
            "expected_proposal_revision": clar["proposal_revision"],
            "answers": [{"question_id": "q-amount", "value": 0.5, "field": "amount",
                         "item_key": item["item_key"]}],
        },
        operation_id="op-ans-8d",
    )["result"]
    assert replay["idempotent"] is True

    # Finishing the remaining question moves to answered. The first answer
    # bumped the proposal AND task revisions, so the caller re-reads both
    # before supplementing (§10.6/§10.10).
    current_rev = _invoke(
        env, "memory.proposal.read", {"proposal_id": proposal["proposal_id"]},
        operation_id="op-prop-read-8i",
    )["result"]["proposal"]["revision"]
    task_now = _invoke(env, "task.read", {"task_id": task["task_id"]},
                       operation_id="op-task-8r")["result"]
    final = _invoke(
        env,
        "memory.clarification.answer",
        {
            "clarification_id": clar["clarification_id"],
            "expected_task_revision": task_now["revision"],
            "expected_proposal_revision": current_rev,
            "answers": [{"question_id": "q-state", "value": "unsure",
                         "field": "observed_state", "item_key": item["item_key"]}],
        },
        operation_id="op-ans-8e",
    )["result"]
    assert final["status"] == "answered"

    items = _proposal_items(env, proposal["proposal_id"])
    assert items[0]["fields"]["amount"]["clarified"] is True
    assert any(u["field"] == "observed_state" for u in items[0].get("unresolved", []))


# ---------------------------------------------------------------------------
# corrections (§10.7, §17)


def test_correction_appends_immutable_revision(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_seed_corr")
    page_id = _setup_page(env, ["Sample S-1 5 g"], operation_id="op-j")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-9")["result"]["proposal"]
    item = proposal["items"][0]
    _confirm(env, proposal["proposal_id"], [item["item_key"]], proposal["revision"],
             operation_id="op-cfm-9")
    outcome = _apply(env, proposal["proposal_id"], "op-apply-9")
    event_id = outcome["result"]["applied"][0]["event_id"]

    # Conflict → parked as pending, NOT applied, NOT lost.
    parked = _invoke(
        env,
        "memory.event.correct",
        {
            "event_id": event_id,
            "expected_revision": 7,
            "changes": {"fields": {"amount": {"raw": "0.5", "normalized": 0.5}}},
            "reason": "misread scale",
        },
        operation_id="op-corr-9a",
    )["result"]
    assert parked["outcome"] == "parked_as_pending"
    assert parked["correction_proposal_id"]

    # Correct revision applies.
    corr = _invoke(
        env,
        "memory.event.correct",
        {
            "event_id": event_id,
            "expected_revision": 1,
            "changes": {"fields": {"amount": {"raw": "0.5", "normalized": 0.5}}},
            "reason": "不是 5 g，是 0.5 g",
        },
        operation_id="op-corr-9",
    )["result"]
    assert corr["outcome"] == "applied"
    assert corr["revision"] == 2

    # Default read shows ONLY the effective value.
    current = _invoke(env, "memory.event.read", {"event_id": event_id},
                      context=env["viewer"], operation_id="op-read-cur-9")["result"]
    assert current["revision"] == 2 and current["is_corrected"] is True
    assert current["field_json"]["fields"]["amount"]["normalized"] == 0.5

    # History keeps the OLD revision queryable and explains the change.
    history = _invoke(
        env, "memory.event.read", {"event_id": event_id, "include_history": True},
        context=env["viewer"], operation_id="op-read-hist-9",
    )["result"]
    assert [r["revision"] for r in history["revisions"]] == [1, 2]
    assert history["revisions"][0]["field_json"]["fields"]["amount"]["normalized"] == 5.0
    assert history["revisions"][1]["provenance"]["reason"] == "不是 5 g，是 0.5 g"


def test_correction_requires_reason_and_marks_stale(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_seed_stale")
    page_id = _setup_page(env, ["Sample S-1 0.25 g"], operation_id="op-k")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-10")["result"]["proposal"]
    item = proposal["items"][0]
    _confirm(env, proposal["proposal_id"], [item["item_key"]], proposal["revision"],
             operation_id="op-cfm-10")
    outcome = _apply(env, proposal["proposal_id"], "op-apply-10")
    event_id = outcome["result"]["applied"][0]["event_id"]

    # Artifact built on revision 1.
    conn = db.connect(env["db_path"])
    try:
        art = experiments.write_artifact(
            conn, project_id="proj_lab", entity_type="sample", entity_id="smp_x",
            artifact_kind="timeline",
            content={"timeline": [{"event_id": event_id, "revision": 1}]},
            based_on_revisions=[{"event_id": event_id, "revision": 1}],
        )
        conn.commit()
        with pytest.raises(experiments.MemoryError):
            experiments.correct_event(
                conn, event_id=event_id, expected_revision=1,
                changes={"fields": {"amount": {"raw": "0.5", "normalized": 0.5}}},
                reason="", principal_id="prin_member",
            )
        conn.execute("BEGIN IMMEDIATE")
        experiments.correct_event(
            conn, event_id=event_id, expected_revision=1,
            changes={"fields": {"amount": {"raw": "0.5", "normalized": 0.5}}},
            reason="reweighed", principal_id="prin_member",
        )
        conn.commit()
        stale = conn.execute(
            "SELECT stale FROM memory_artifacts WHERE artifact_id = ?",
            (art["artifact_id"],),
        ).fetchone()["stale"]
        assert stale == 1
        rebuilt = experiments.rebuild_artifact(conn, artifact_id=art["artifact_id"])
        assert rebuilt["stale"] is False and rebuilt["events"] == 1
        derived_flag = conn.execute(
            "SELECT derived FROM memory_artifacts WHERE artifact_id = ?",
            (art["artifact_id"],),
        ).fetchone()["derived"]
        assert derived_flag == 1  # derived artifacts NEVER become facts
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# source-evidence correction invalidates confirmations (§10.10)


def test_source_correction_invalidates_confirmations(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_seed_inv")
    page_id = _setup_page(env, ["Sample S-1 5 g"], operation_id="op-l")
    proposal = _make_proposal(env, page_id, None, operation_id="op-prop-11")["result"]["proposal"]
    item = proposal["items"][0]
    _confirm(env, proposal["proposal_id"], [item["item_key"]], proposal["revision"],
             operation_id="op-cfm-11")

    # Human corrects the TRANSCRIPT (S03 capability): active revision moves.
    _invoke(
        env,
        "evidence.transcript.correct",
        {
            "page_id": page_id,
            "expected_revision": 1,
            "blocks": [_block("Sample S-1 0.5 g", 0, block_id="c-0")],
            "reason": "OCR misread",
        },
        operation_id="op-tcorr-11",
    )

    # Applying now must NOT use the stale confirmation: source moved.
    outcome = _apply(env, proposal["proposal_id"], "op-apply-11")
    assert outcome["result"]["pending"], "stale confirmations must not auto-apply"

    # And the old confirmation is marked invalidated.
    conn = db.connect(env["db_path"])
    try:
        statuses = conn.execute(
            "SELECT status, invalidated_reason FROM event_confirmations WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        ).fetchall()
    finally:
        conn.close()
    assert statuses and all(s["status"] == "invalidated" for s in statuses)
    assert statuses[0]["invalidated_reason"] == "source_transcript_corrected"


# ---------------------------------------------------------------------------
# policy gate: no auto-apply in the first round (§10.5)


def test_profile_gates_auto_apply(env):
    from chemclaw_lab.profiles import CHEMISTRY_NOTEBOOK_V1, profile_to_contract

    contract = profile_to_contract(CHEMISTRY_NOTEBOOK_V1)
    assert contract["auto_apply_inputs"] == []
    rules = contract["rules"]
    assert "NOT" in rules["unwritten_methods_never_inferred"]
    assert "never" in rules["plan_vs_reported"]

    item = {"item_key": "t1", "event_kind": "operation", "raw_text": "x",
            "input_type": "handwritten_number", "evidence_refs": [
                {"page_id": "p", "transcript_revision": 1, "block_id": "b"}]}
    assert item["input_type"] not in CHEMISTRY_NOTEBOOK_V1.auto_apply_inputs


def test_unwritten_method_never_inferred(env):
    """An observation-only page produces no invented operation fields (§10.2)."""
    page_id = _setup_page(env, ["观察到溶液变蓝"], operation_id="op-m")
    envelope = _make_proposal(env, page_id, None, operation_id="op-prop-12")
    item = envelope["result"]["proposal"]["items"][0]
    assert item["event_kind"] == "observation"
    # No operation/procedure field hallucinated from convention.
    assert "operation" not in item["fields"]
    assert "amount" not in item["fields"]
    assert item["fields"].get("observed_state") is None  # not guessed either
