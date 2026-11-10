"""S05 tests: shared query service & minimal review interface (plan §11).

Acceptance mapping (plan §11 + §17):
- Scope derived or verified, never unfiltered ........ test_search_scope_*
- Structured + full-text candidates, exact hits without vectors
                                                      test_fulltext_hits_block_without_vector
- Candidate = object/revision/region/reason/score ... test_candidates_carry_*
- knowledge_as_of selects the then-valid revision ... test_knowledge_as_of_returns_old_value
- Corrections after as_of reported separately ....... test_pending_corrections_separate
- Current snapshot uses new value after correction .. test_current_snapshot_returns_new_value
- AnswerClaim kinds + two-layer citation verify ..... test_answer_reports_fact_with_citations
- 130 °C fragment cannot support a 150 °C claim ..... test_answer_assertion_unsupported_becomes_gap
- Plan vs actual vs estimate amounts distinguished .. test_answer_plan_vs_operation_semantics
- Derived values record formula + inputs ............ test_answer_derived_sum_records_formula
- Insufficient records → unknown, not invention ..... test_answer_insufficient_records_unknown
- Web and machine see the same snapshot result ...... test_render_matches_claims / test_web_and_machine_agree
- Reports persist source revisions + stale marking .. test_report_save_read_stale
- Scope tightening restricts reports immediately .... test_report_access_denied_after_membership_removal
- Audit log without private text (§11.11) ........... test_query_audit_log_never_stores_private_text
- External read-only HTTP client use cases (§11.7) .. test_external_readonly_client_end_to_end
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from chemclaw_lab import (  # noqa: E402
    answers,
    auth,
    capabilities,
    clients,
    db,
    experiments,
    ocr_port,
    query,
    proposal as proposal_mod,
)


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "s05.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("INSERT INTO projects (project_id, name) VALUES ('proj_lab', 'Lab')")
    conn.execute("INSERT INTO projects (project_id, name) VALUES ('proj_other', 'Other')")
    for pid, name in (
        ("prin_member", "Member"),
        ("prin_lab", "LabMember"),
        ("prin_other", "OtherMember"),
        ("prin_viewer", "Viewer"),
        ("prin_nobody", "Nobody"),
    ):
        conn.execute(
            "INSERT INTO principals (principal_id, kind, display_name) VALUES (?, 'human', ?)",
            (pid, name),
        )
    conn.commit()
    conn.close()
    registry = ocr_port.default_registry()
    registry.register(ocr_port.FixtureRasterTranscriber(name="fixture_ocr"))
    from chemclaw_lab.service_config import ServiceSettings

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
        "nobody": auth.PrincipalContext(
            principal_id="prin_nobody",
            kind="user",
            display_name="Nobody",
            memberships={},
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


def _setup_page(env, lines, *, document_id=None, notebook=None, operation_id="op-setup",
                context=None, project_id="proj_lab"):
    if document_id is None:
        envelope = _invoke(
            env,
            "evidence.document.create",
            {
                "project_id": project_id,
                "notebook_code": notebook or f"NB-{operation_id}",
                "title": "S05 notebook",
                "author": "yu",
                "source_channel": "photo_upload",
            },
            context=context,
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
        context=context,
        operation_id=f"{operation_id}-page",
    )
    page_id = page_env["result"]["page"]["page_id"]
    blocks = [_block(line, i) for i, line in enumerate(lines)]
    run_env = _invoke(
        env,
        "evidence.transcribe.run",
        {"page_id": page_id, "provider": "fixture_ocr", "config": {"fixture_blocks": blocks}},
        context=context,
        operation_id=f"{operation_id}-run",
    )
    assert run_env["result"]["status"] == "succeeded"
    return page_id


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


def _apply_operation(env, lines, *, sample_id, operation_id, context=None,
                     project_id="proj_lab"):
    """Transcribe lines → propose → confirm all → apply. Returns event ids."""
    page_id = _setup_page(env, lines, operation_id=operation_id,
                          context=context, project_id=project_id)
    proposal = _invoke(
        env,
        "memory.proposal.create",
        {
            "project_id": project_id,
            "page_id": page_id,
            "transcript_revision": 1,
            "profile_id": "chemistry_notebook",
            "profile_version": "1.0",
            "acceptance_mode": "review",  # This helper explicitly exercises confirm -> apply.
            "operation_id": f"{operation_id}-prop",
        },
        context=context,
        operation_id=f"{operation_id}-prop",
    )["result"]["proposal"]
    items = proposal["items"]
    _invoke(
        env,
        "memory.proposal.confirm_items",
        {
            "proposal_id": proposal["proposal_id"],
            "item_keys": [i["item_key"] for i in items],
            "decision": "confirmed",
            "expected_proposal_revision": proposal["revision"],
        },
        context=context,
        operation_id=f"{operation_id}-cfm",
    )
    outcome = _invoke(
        env,
        "memory.proposal.apply",
        {"proposal_id": proposal["proposal_id"], "operation_id": f"{operation_id}-apply"},
        context=context,
        operation_id=f"{operation_id}-apply",
    )["result"]
    event_ids = [a["event_id"] for a in outcome["applied"]]
    return event_ids, page_id


# ---------------------------------------------------------------------------
# scope resolution (§11.8/§11.11): derive or verify, never unfiltered
# ---------------------------------------------------------------------------


def test_search_scope_derived_from_membership(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_scope1")
    _apply_operation(env, ["实际取用 0.5 g S-1"], sample_id="smp_scope1",
                     operation_id="op-scope-lab")
    _seed_sample(env, "proj_other", "S-1", "smp_scope2")
    _apply_operation(env, ["实际取用 1 mL S-1"], sample_id="smp_scope2",
                     operation_id="op-scope-other", context=env["other_member"],
                     project_id="proj_other")

    result = _invoke(env, "query.events.search", {})
    assert sorted(result["result"]["scope"]) == ["proj_lab", "proj_other"]

    result_other = _invoke(
        env, "query.events.search", {}, context=env["other_member"],
        operation_id="op-scope-read",
    )
    assert result_other["result"]["scope"] == ["proj_other"]
    projects = {
        c["payload"]["project_id"]
        for c in result_other["result"]["candidates"]
        if c["object_type"] == "experiment_event"
    }
    assert projects <= {"proj_other"}


def test_search_scope_outside_membership_rejected(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_denied")
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(
            env, "query.events.search", {"project_ids": ["proj_other"]},
            context=env["lab_member"], operation_id="op-scope-deny",
        )
    assert excinfo.value.code == "forbidden_scope"


def test_search_no_membership_yields_no_candidates(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_nobody")
    _apply_operation(env, ["实际取用 0.5 g S-1"], sample_id="smp_nobody",
                     operation_id="op-nobody")
    # A principal with no memberships has NO role granting the capability:
    # the call is refused outright (§11.11 "derive or refuse"), and it can
    # never fall back to unfiltered scanning.
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(
            env, "query.events.search", {}, context=env["nobody"],
            operation_id="op-nobody-read",
        )
    assert excinfo.value.code == "forbidden_capability"
    # library level: an explicitly empty derived scope yields zero
    # candidates, never an unfiltered scan.
    conn = db.connect(env["db_path"])
    try:
        result = query.search(conn, env["nobody"], text="S-1")
    finally:
        conn.close()
    assert result["scope"] == []
    assert result["candidates"] == []


# ---------------------------------------------------------------------------
# candidates: structure + full-text without vectors
# ---------------------------------------------------------------------------


def test_candidates_carry_revision_region_context(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_cand")
    _apply_operation(
        env,
        ["搅拌 S-1 30 分钟", "实际取用 0.5 g S-1"],
        sample_id="smp_cand",
        operation_id="op-cand",
    )
    conn = db.connect(env["db_path"])
    try:
        result = query.search(conn, env["member"], sample_label="S-1")
    finally:
        conn.close()
    candidates = result["candidates"]
    assert candidates, "expected structured candidates"
    for cand in candidates:
        assert cand["revision"] >= 1
        assert cand["knowledge_as_of"] is not None
        assert cand["hit_reason"] == "structured_filter"
        assert 0.0 < cand["score"] <= 1.0
    with_text = [c for c in candidates if c["object_type"] == "experiment_event"]
    assert with_text
    ref = with_text[-1]
    assert set(ref["payload"]["fields"]) >= {"event_kind", "fields", "sample_id"}
    # evidence refs must point at real blocks of the source page (§11.9)
    for eref in ref["payload"]["evidence_refs"]:
        assert eref["page_id"] and eref["block_id"]


def test_fulltext_hits_block_without_vector(env):
    _setup_page(
        env,
        ["加热到 130 degC 保持 2 小时", "SecretReagentXYZ 溶解"],
        operation_id="op-ft",
    )
    conn = db.connect(env["db_path"])
    try:
        result = query.search(conn, env["member"], text="130 degC")
        assert result["vector_enabled"] is False
        hits = [c for c in result["candidates"] if c["object_type"] == "transcript_block"]
        assert hits, "exact fulltext must hit without any vector store"
        assert "130 degC" in hits[0]["chunk"]
        assert hits[0]["page_id"]
        assert hits[0]["hit_reason"] == "fulltext:130 degc"
        # neighbouring block context is attached (§11.8)
        assert hits[0]["context_before"] is None
        assert "SecretReagentXYZ" in (hits[0]["context_after"] or "")

        # exact sample-id style strings hit too
        result2 = query.search(conn, env["member"], text="SecretReagentXYZ")
        assert any(
            "SecretReagentXYZ" in c["chunk"]
            for c in result2["candidates"]
            if c["object_type"] == "transcript_block"
        )
        # superseded runs are not searched: only active transcript revisions
        nothing = query.search(conn, env["member"], text="不存在的字符串xyz")
        assert nothing["candidates"] == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# snapshot semantics (§11.2/§11.10): as-of revision selection
# ---------------------------------------------------------------------------


def _seed_corrected_event(env, *, operation_id="op-snap"):
    """One applied operation (0.5 g) + one applied correction (0.2 g)."""
    _seed_sample(env, "proj_lab", "S-1", "smp_snap")
    event_ids, _ = _apply_operation(
        env, ["实际取用 0.5 g S-1"], sample_id="smp_snap", operation_id=operation_id,
    )
    event_id = event_ids[0]
    revision = _invoke(
        env, "memory.event.read", {"event_id": event_id}, operation_id=f"{operation_id}-rd",
    )["result"]["revision"]
    correction = _invoke(
        env,
        "memory.event.correct",
        {
            "event_id": event_id,
            "expected_revision": revision,
            "changes": {"fields": {"amount": {"normalized": 0.2, "original_text": "0.2 g"}}},
            "reason": "错看天平，实际 0.2 g",
        },
        operation_id=f"{operation_id}-corr",
    )
    assert correction["result"]["outcome"] == "applied"
    return event_id


def _set_knowledge_times(env, event_id, *, created: str, corrected: str):
    """Inject a deterministic knowledge timeline for as-of testing:
    revision 1 became knowledge at `created`, revision 2 at `corrected`."""
    conn = db.connect(env["db_path"])
    try:
        conn.execute(
            """
            UPDATE event_revisions SET knowledge_as_of = ?
            WHERE entity_type='experiment_event' AND entity_id=? AND revision=1
            """,
            (created, event_id),
        )
        conn.execute(
            """
            UPDATE event_revisions SET knowledge_as_of = ?
            WHERE entity_type='experiment_event' AND entity_id=? AND revision=2
            """,
            (corrected, event_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_knowledge_as_of_returns_old_value(env):
    event_id = _seed_corrected_event(env, operation_id="op-asof")
    _set_knowledge_times(
        env, event_id,
        created="2026-10-01 08:00:00",
        corrected="2026-10-05 12:00:00",
    )
    conn = db.connect(env["db_path"])
    try:
        # as_of BEFORE the correction: the then-valid revision is 1 (0.5 g)
        result = query.search(
            conn, env["member"],
            knowledge_as_of="2026-10-02 00:00:00",
        )
    finally:
        conn.close()
    events = [
        c for c in result["candidates"]
        if c["object_type"] == "experiment_event" and c["object_id"] == event_id
    ]
    assert events and events[0]["revision"] == 1
    assert events[0]["payload"]["fields"]["fields"]["amount"]["normalized"] == 0.5
    # the later correction is reported SEPARATELY, never folded in (§11.2)
    pending = result["pending_corrections"]
    assert any(p["event_id"] == event_id and p["current_revision"] == 2 for p in pending)


def test_current_snapshot_returns_new_value(env):
    event_id = _seed_corrected_event(env, operation_id="op-curr")
    _set_knowledge_times(
        env, event_id,
        created="2026-10-01 08:00:00",
        corrected="2026-10-05 12:00:00",
    )
    conn = db.connect(env["db_path"])
    try:
        result = query.search(conn, env["member"])
    finally:
        conn.close()
    events = [
        c for c in result["candidates"]
        if c["object_type"] == "experiment_event" and c["object_id"] == event_id
    ]
    assert events and events[0]["revision"] == 2
    assert events[0]["payload"]["fields"]["fields"]["amount"]["normalized"] == 0.2


def test_pending_corrections_separate_from_snapshot(env):
    event_id = _seed_corrected_event(env, operation_id="op-pend")
    _set_knowledge_times(
        env, event_id,
        created="2026-10-01 08:00:00",
        corrected="2026-10-05 12:00:00",
    )
    conn = db.connect(env["db_path"])
    try:
        result = query.search(
            conn, env["member"],
            sample_label="S-1",
            knowledge_as_of="2026-10-02 00:00:00",
        )
        timeline = query.sample_timeline(
            conn, env["member"], sample_id="smp_snap",
            knowledge_as_of="2026-10-02 00:00:00",
        )
    finally:
        conn.close()
    # the snapshot answer carries revision 1 only
    for cand in result["candidates"]:
        if cand["object_type"] == "experiment_event":
            assert cand["revision"] == 1
    assert any(p["event_id"] == event_id for p in result["pending_corrections"])
    # timeline behaves the same way (§5: sample timeline page)
    assert all(e["revision"] == 1 for e in timeline["timeline"])
    assert any(p["event_id"] == event_id for p in timeline["pending_corrections"])
    # ...and the current timeline uses revision 2 without pending entries
    conn = db.connect(env["db_path"])
    try:
        current = query.sample_timeline(conn, env["member"], sample_id="smp_snap")
    finally:
        conn.close()
    assert all(e["revision"] == 2 for e in current["timeline"])
    assert current["pending_corrections"] == []


# ---------------------------------------------------------------------------
# answers: claims, citations, semantics (§11.4/§11.9)
# ---------------------------------------------------------------------------


def test_answer_reports_fact_with_citations(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_ans1")
    event_ids, _ = _apply_operation(
        env, ["实际取用 0.5 g S-1"], sample_id="smp_ans1", operation_id="op-ans1",
    )
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(
            conn, env["member"], "S-1 实际取用了多少？",
        )
    finally:
        conn.close()
    assert answer["status"] == "answered"
    amount_claims = [
        c for c in answer["claims"]
        if c["kind"] == "reported_fact" and c["field_pointer"] == "fields.amount"
    ]
    assert amount_claims, "expected a cited amount claim"
    claim = amount_claims[0]
    assert claim["value"] == 0.5
    assert claim["object_id"] == event_ids[0]
    assert claim["revision"] == 1
    assert claim["usage_semantics"] == "实际取用"
    # three independent verification layers (§R07): the citation opens, the
    # value matches the structured field, AND the source supports the value.
    assert claim["verify"]["ref_opens"] is True
    assert claim["verify"]["field_matches"] is True
    assert claim["verify"]["source_supports"] is True
    assert claim["verify"]["support_kind"] == "source_text"
    assert claim["verify"]["value_matches"] is True
    assert claim["evidence_refs"], "fact claims must carry evidence refs"
    assert not answer["gaps"]


def test_answer_plan_vs_operation_semantics(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_ans2")
    _apply_operation(
        env, ["明天拟取 2 mL S-1", "实际取用 1 mL S-1"],
        sample_id="smp_ans2", operation_id="op-ans2",
    )
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(
            conn, env["member"], "S-1 的用量情况？",
        )
    finally:
        conn.close()
    by_usage = {}
    for c in answer["claims"]:
        if c["field_pointer"] == "fields.amount":
            by_usage.setdefault(c["usage_semantics"], []).append(c)
    assert "计划/配方用量（未执行）" in by_usage
    assert "实际取用" in by_usage
    assert by_usage["计划/配方用量（未执行）"][0]["value"] == 2
    assert by_usage["实际取用"][0]["value"] == 1
    # the plan amount is NEVER reported as an actual usage (§17)
    assert all(c["value"] != 2 for c in by_usage["实际取用"])


def test_answer_assertion_unsupported_becomes_gap(env):
    """§17: a similar fragment (130 degC) cannot support a 150 degC claim."""
    _setup_page(
        env, ["加热到 130 degC 保持 2 小时"], operation_id="op-gap",
    )
    _seed_sample(env, "proj_lab", "S-1", "smp_gap")
    _apply_operation(
        env, ["Sample S-1 0.5 g stirred at 25 degC"],
        sample_id="smp_gap", operation_id="op-gap2",
    )
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(
            conn, env["member"], "S-1 已在 150 degC 完成反应了吗？",
        )
    finally:
        conn.close()
    assert answer["status"] == "unknown"
    # the 150 degC assertion is NOT answered as fact anywhere
    assert not any(c.get("value") == 150 for c in answer["claims"])
    unsupported = [
        g for g in answer["gaps"] if g["kind"] == "assertion_unsupported"
    ]
    assert unsupported and unsupported[0]["asserted"] == {"value": 150.0, "unit": "degC"}
    # full-text similarity to "130 degC" must not upgrade it to support
    assert all(c.get("value") != 150 for c in answer["claims"])


def test_answer_derived_sum_records_formula(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_der")
    _apply_operation(
        env,
        ["实际取用 0.5 g S-1", "第二次实际取用 0.2 g S-1"],
        sample_id="smp_der", operation_id="op-der1",
    )
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(conn, env["member"], "S-1 实际取用总量？")
    finally:
        conn.close()
    derived = [c for c in answer["claims"] if c["kind"] == "derived"]
    assert derived, "two actual usages should produce one derived sum"
    claim = derived[0]
    assert claim["value"] == 0.7
    assert claim["formula"].startswith("sum(")
    assert len(claim["inputs"]) == 2
    input_ids = {c["claim_id"] for c in answer["claims"]}
    assert set(claim["inputs"]) <= input_ids
    assert "非湿实验观测" in claim["statement"]


def test_answer_insufficient_records_unknown(env):
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(conn, env["member"], "完全无关的问题 zzqq？")
    finally:
        conn.close()
    assert answer["status"] == "unknown"
    assert answer["claims"] == []
    assert answer["gaps"], "unknown answers must explain the gap"


# ---------------------------------------------------------------------------
# rendering + report lifecycle (§11.3/§11.6)
# ---------------------------------------------------------------------------


def test_render_matches_claims(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_ren")
    _apply_operation(env, ["实际取用 0.5 g S-1"], sample_id="smp_ren",
                     operation_id="op-ren")
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(conn, env["member"], "S-1 用量？")
    finally:
        conn.close()
    text = answers.render_text(answer)
    # same structured result feeds JSON and rendering (§11.3)
    assert text == answer["rendered_text"]
    for claim in answer["claims"]:
        assert f"[{claim['claim_id']}]" in text
    assert f"[snapshot {answer['snapshot_version']}]" in text
    assert "0.5" in text


def test_report_save_read_stale(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_rep")
    event_ids, _ = _apply_operation(env, ["实际取用 0.5 g S-1"], sample_id="smp_rep",
                                    operation_id="op-rep")
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(conn, env["member"], "S-1 用量？")
        report_id = query.save_report(
            conn, env["member"], project_id="proj_lab",
            question=answer["question"], answer=answer,
        )
        report = query.read_report(conn, env["member"], report_id)
    finally:
        conn.close()
    assert report["stale"] is False
    assert report["based_on"], "report must persist cited source revisions"
    assert all(
        entry["revision"] == 1 for entry in report["based_on"]
        if entry["object_type"] == "experiment_event"
    )

    # correct the underlying event → report becomes stale (§11.6)
    revision = _invoke(
        env, "memory.event.read", {"event_id": event_ids[0]},
        operation_id="op-rep-rd",
    )["result"]["revision"]
    _invoke(
        env,
        "memory.event.correct",
        {
            "event_id": event_ids[0],
            "expected_revision": revision,
            "changes": {"fields": {"amount": {"normalized": 0.2, "original_text": "0.2 g"}}},
            "reason": "更正用量",
        },
        operation_id="op-rep-corr",
    )
    conn = db.connect(env["db_path"])
    try:
        stale_report = query.read_report(conn, env["member"], report_id)
    finally:
        conn.close()
    assert stale_report["stale"] is True
    assert stale_report["superseded_objects"]
    assert stale_report["superseded_objects"][0]["based_on_revision"] == 1
    assert stale_report["superseded_objects"][0]["current_revision"] == 2


def test_report_access_denied_after_membership_removal(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_acc")
    _apply_operation(env, ["实际取用 0.5 g S-1"], sample_id="smp_acc",
                     operation_id="op-acc")
    conn = db.connect(env["db_path"])
    try:
        answer = answers.build_answer(conn, env["lab_member"], "S-1 用量？")
        report_id = query.save_report(
            conn, env["lab_member"], project_id="proj_lab",
            question=answer["question"], answer=answer,
        )
    finally:
        conn.close()
    # scope tightening: the principal loses ALL memberships → immediate 403
    revoked = auth.PrincipalContext(
        principal_id="prin_lab", kind="user", display_name="LabMember",
        memberships={},
    )
    conn = db.connect(env["db_path"])
    try:
        with pytest.raises(query.QueryError) as excinfo:
            query.read_report(conn, revoked, report_id)
    finally:
        conn.close()
    assert excinfo.value.code == "forbidden_scope"
    # the report itself stays on disk for audit (not deleted)
    conn = db.connect(env["db_path"])
    try:
        count = conn.execute("SELECT COUNT(*) FROM query_reports").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# audit log hygiene (§11.11)
# ---------------------------------------------------------------------------


def test_query_audit_log_never_stores_private_text(env):
    _setup_page(env, ["SecretCompoundQ 溶解"], operation_id="op-audit")
    _invoke(
        env, "query.events.search",
        {"text": "SecretCompoundQ 溶解"}, operation_id="op-audit-search",
    )
    conn = db.connect(env["db_path"])
    try:
        rows = conn.execute(
            "SELECT filters_json, candidate_refs_json FROM query_audit_log"
        ).fetchall()
    finally:
        conn.close()
    assert rows
    for row in rows:
        filters = json.loads(row["filters_json"])
        assert "text" not in filters, "free-text queries are private (§11.11)"
        assert "SecretCompoundQ" not in json.dumps(filters)
        refs = json.loads(row["candidate_refs_json"])
        for ref in refs:
            # only ids, revisions, scores — never chunk content
            assert set(ref) == {"object_type", "object_id", "revision", "score"}


# ---------------------------------------------------------------------------
# external read-only HTTP client (§11.7) + web/machine consistency
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_env(env, tmp_path):
    from chemclaw_lab.api import create_api_server

    conn = db.connect(env["db_path"])
    try:
        auth.register_principal(conn, principal_id="hum_member", kind="human", display_name="M")
        auth.grant_membership(conn, principal_id="hum_member", project_id="proj_lab", role="member")
        auth.grant_membership(conn, principal_id="hum_member", project_id="proj_other", role="member")
        auth.register_credential(
            conn, credential_id="cred_member", principal_id="hum_member", secret="member-key",
        )
        auth.register_principal(conn, principal_id="hum_viewer", kind="human", display_name="V")
        auth.grant_membership(conn, principal_id="hum_viewer", project_id="proj_lab", role="viewer")
        auth.register_credential(
            conn, credential_id="cred_viewer", principal_id="hum_viewer", secret="viewer-key",
        )
        conn.commit()
    finally:
        conn.close()
    server = create_api_server(env["db_path"], host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env["base_url"] = f"http://127.0.0.1:{server.server_port}"
    yield env
    server.shutdown()
    server.server_close()


def _seed_http_data(env):
    _seed_sample(env, "proj_lab", "S-1", "smp_http")
    _seed_sample(env, "proj_other", "S-9", "smp_http_other")
    _apply_operation(env, ["实际取用 0.5 g S-1"], sample_id="smp_http",
                     operation_id="op-http")
    _apply_operation(
        env, ["实际取用 1 mL S-9"], sample_id="smp_http_other",
        operation_id="op-http2", context=env["other_member"], project_id="proj_other",
    )


def test_external_readonly_client_end_to_end(http_env):
    _seed_http_data(http_env)
    viewer_client = clients.ReadOnlyQueryClient(http_env["base_url"], "viewer-key")

    # protocol: capabilities list contains the query surface (viewer roles)
    caps = {c["capability_id"] for c in viewer_client.list_capabilities()}
    assert "query.events.search" in caps
    assert "query.sample.timeline" in caps
    assert "query.answer.build" not in caps  # member-only (viewer excluded)

    # read-only surface: search + sample history + provenance + unknowns
    search = viewer_client.search_events(sample_label="S-1")
    events = [
        c for c in search["candidates"]
        if c["object_type"] == "experiment_event"
    ]
    assert events and events[0]["payload"]["fields"]["fields"]["amount"]["normalized"] == 0.5
    assert events[0]["payload"]["evidence_refs"]

    history = viewer_client.sample_history("proj_lab", "smp_http")
    assert history["sample"]["label"] == "S-1"
    assert history["timeline"]

    answer = viewer_client.render_answer("S-1 实际取用了多少？")
    assert answer["status"] == "answered"
    assert any(
        c["value"] == 0.5 and c["usage_semantics"] == "实际取用"
        for c in answer["claims"]
    )

    # cross-project data is NOT visible to the viewer client
    other = viewer_client.search_events(sample_label="S-9")
    assert other["scope"] == ["proj_lab"]
    assert other["candidates"] == []

    # write capabilities cannot be invoked through the read-only client
    with pytest.raises(clients.ClientError) as excinfo:
        viewer_client.invoke(
            "memory.proposal.create", {"project_id": "proj_lab"},
            operation_id="ext-write-1",
        )
    assert excinfo.value.code == "read_only_client"


def test_web_and_machine_agree(http_env):
    """Acceptance: same snapshot → Web (HTTP envelope) and machine (direct
    service call) return the SAME facts, revisions and unknowns."""
    _seed_http_data(http_env)
    member_client = clients.ReadOnlyQueryClient(http_env["base_url"], "member-key")
    # member secret is registered; build answer over HTTP
    envelope = member_client._request(
        "POST",
        "/v2/capabilities/query.answer.build/invoke",
        {"params": {"question": "S-1 实际取用了多少？", "project_ids": ["proj_lab"]},
         "operation_id": "web-op-1"},
    )
    http_answer = envelope["result"]
    conn = db.connect(http_env["db_path"])
    try:
        machine = auth.authenticate_principal(conn, "member-key")
        direct = answers.build_answer(
            conn, machine, "S-1 实际取用了多少？", project_ids=["proj_lab"],
        )
    finally:
        conn.close()
    # strip nondeterministic ids; compare the substance
    def _substance(answer):
        return {
            "status": answer["status"],
            "snapshot_version": answer["snapshot_version"],
            "scope": answer["scope"],
            "gaps": [g["kind"] for g in answer["gaps"]],
            "facts": sorted(
                (c["object_id"], c["revision"], c["field_pointer"], c["value"],
                 c["unit"], c["usage_semantics"])
                for c in answer["claims"]
                if c["kind"] == "reported_fact"
            ),
        }
    assert _substance(http_answer) == _substance(direct)
