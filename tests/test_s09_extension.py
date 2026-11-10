"""S09 extension domains (plan §15).

Acceptance mapping:
- Plans, candidate slots, formal bookings returned separately; a booking is
  never an experiment result ...... tests C-1..C-3
- Conflict and transaction re-check on confirm .... test C-2
- Milestones/criteria are objects checked against locatable evidence;
  human-only decisions; no overall research percentage ... tests D-1..D-3
- Source-evidence correction expires dependent criteria ... test D-2
- External results keep provenance; computation ≠ wet-lab measurement
  ................................ tests E-1..E-2
- MCP same-origin adapter: scoped tools/list, /v2 capability semantics on
  tools/call ........................ tests M-1..M-2
- New role plugs in via manifest + domain capabilities without touching old
  roles ............................ test X-1
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from chemclaw_lab import auth, capabilities, db, mcp  # noqa: E402
from chemclaw_lab import pnglite  # noqa: E402
from chemclaw_lab.ocr_port import FixtureRasterTranscriber, default_registry  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "s09.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("INSERT INTO projects (project_id, name) VALUES ('proj_lab', 'Lab')")
    conn.commit()
    conn.close()

    registry = ocr_registry()
    settings = ServiceSettings(
        storage_root=tmp_path / "storage",
        registry_factory=lambda: registry,
    )
    return {
        "db_path": str(db_path),
        "settings": settings,
        "owner": auth.PrincipalContext(
            principal_id="prin_root", kind="user", display_name="Owner",
            memberships={"proj_lab": "owner"},
        ),
        "member": auth.PrincipalContext(
            principal_id="prin_lab", kind="user", display_name="Member",
            memberships={"proj_lab": "member"},
        ),
        "viewer": auth.PrincipalContext(
            principal_id="prin_view", kind="user", display_name="Viewer",
            memberships={"proj_lab": "viewer"},
        ),
        "outsider": auth.PrincipalContext(
            principal_id="prin_out", kind="user", display_name="Out",
            memberships={"proj_other": "member"},
        ),
    }


def ocr_registry():
    registry = default_registry()
    registry.register(FixtureRasterTranscriber(name="fixture_ocr"))
    return registry


def _invoke(env, capability, params, context=None, operation_id="op-default"):
    return capabilities.invoke_capability(
        env["db_path"],
        context or env["member"],
        capability,
        params,
        operation_id=operation_id,
        services=env["settings"],
    )


def _png_bytes(width=60, height=40):
    return pnglite.solid_png(width, height, (240, 240, 245))


def _block(text, order, block_id):
    return {
        "block_id": block_id, "block_kind": "handwriting", "reading_order": order,
        "text": text, "table_cell": None,
        "geometry": {"x": 5, "y": 5 + order * 10, "width": 40, "height": 8},
        "coordinate_system": {"origin": "TOPLEFT", "units": "pixel",
                              "image_ref": "original", "frame_width": 60,
                              "frame_height": 40},
        "source_page": 1,
        "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer",
                      "version": "0.3.0"},
        "quality_flags": [],
    }


def _page_with_blocks(env, texts, *, operation_id):
    doc = _invoke(env, "evidence.document.create", {
        "project_id": "proj_lab", "notebook_code": f"NB-{operation_id}",
        "title": "S09 evidence", "author": "yu", "source_channel": "photo_upload",
    }, operation_id=f"{operation_id}-doc")["result"]["document_id"]
    page_id = _invoke(env, "evidence.page.ingest", {
        "document_id": doc, "page_order": 1, "filename": "p.png",
        "content_data_url": "data:image/png;base64,"
        + base64.b64encode(_png_bytes()).decode("ascii"),
    }, operation_id=f"{operation_id}-page")["result"]["page"]["page_id"]
    _invoke(env, "evidence.transcribe.run", {
        "page_id": page_id, "provider": "fixture_ocr",
        "config": {"fixture_blocks": [
            _block(t, i, f"{operation_id}-b{i}") for i, t in enumerate(texts)
        ]},
    }, operation_id=f"{operation_id}-run")
    return page_id


def _fact_counts(env):
    conn = db.connect(env["db_path"])
    try:
        return {
            "experiment_events": conn.execute(
                "SELECT COUNT(*) FROM experiment_events").fetchone()[0],
            "event_revisions": conn.execute(
                "SELECT COUNT(*) FROM event_revisions").fetchone()[0],
            "outbox": conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            "memory_proposals": conn.execute(
                "SELECT COUNT(*) FROM memory_proposals").fetchone()[0],
            "inventory_subtasks": conn.execute(
                "SELECT COUNT(*) FROM inventory_subtasks").fetchone()[0],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C: resources & bookings
# ---------------------------------------------------------------------------


def test_c1_plan_candidates_and_booking_are_separate_returns(env):
    _invoke(env, "resource.register", {
        "project_id": "proj_lab", "name": "NMR-400", "kind": "instrument",
        "capacity": 1,
    }, context=env["owner"], operation_id="op-reg")

    avail = _invoke(env, "resource.list", {}, operation_id="op-list")
    resource_id = avail["result"]["resources"][0]["resource_id"]

    candidates = _invoke(env, "resource.availability", {
        "resource_id": resource_id,
        "window_start": "2026-10-14T09:00:00+08:00",
        "window_end": "2026-10-14T12:00:00+08:00",
        "duration_minutes": 60,
    }, operation_id="op-avail")
    assert candidates["action_status"] == "applied"
    assert len(candidates["result"]["candidates"]) == 5  # 09:00..11:00 every 30 min

    # A plan is returned as proposed; it is NOT a confirmed booking.
    plan = _invoke(env, "resource.booking.propose", {
        "resource_id": resource_id, "project_id": "proj_lab",
        "slot_start": "2026-10-14T09:00:00+08:00",
        "slot_end": "2026-10-14T10:00:00+08:00",
        "plan_summary": "Run the 1H NMR queue for batch S-1",
        "purpose_note": "characterization",
    }, operation_id="op-plan")
    assert plan["action_status"] == "proposed"
    assert plan["result"]["status"] == "planned"

    bookings = _invoke(env, "resource.booking.status", {
        "project_ids": ["proj_lab"], "status": "planned",
    }, operation_id="op-st1")
    assert [b["booking_id"] for b in bookings["result"]["bookings"]] == [
        plan["result"]["booking_id"]]

    # The formal booking is a separate action and returns applied/confirmed.
    booking = _invoke(env, "resource.booking.confirm", {
        "booking_id": plan["result"]["booking_id"],
    }, operation_id="op-confirm")
    assert booking["action_status"] == "applied"
    assert booking["result"]["status"] == "confirmed"
    assert booking["result"]["conflict_detected"] is False

    confirmed = _invoke(env, "resource.booking.status", {
        "project_ids": ["proj_lab"], "status": "confirmed",
    }, operation_id="op-st2")
    assert len(confirmed["result"]["bookings"]) == 1

    # Availability now excludes the confirmed slot.
    after = _invoke(env, "resource.availability", {
        "resource_id": resource_id,
        "window_start": "2026-10-14T09:00:00+08:00",
        "window_end": "2026-10-14T12:00:00+08:00",
        "duration_minutes": 60,
    }, operation_id="op-avail2")
    starts = [c["slot_start"] for c in after["result"]["candidates"]]
    assert "2026-10-14T09:00:00+08:00" not in starts


def test_c2_conflict_recheck_rejects_second_confirmation(env):
    rid = _invoke(env, "resource.register", {
        "project_id": "proj_lab", "name": "Glovebox A", "kind": "equipment",
    }, context=env["owner"], operation_id="op-reg")["result"]["resource_id"]

    first = _invoke(env, "resource.booking.propose", {
        "resource_id": rid, "project_id": "proj_lab",
        "slot_start": "2026-10-14T09:00:00+08:00",
        "slot_end": "2026-10-14T10:00:00+08:00",
    }, operation_id="op-p1")
    second = _invoke(env, "resource.booking.propose", {
        "resource_id": rid, "project_id": "proj_lab",
        "slot_start": "2026-10-14T09:30:00+08:00",
        "slot_end": "2026-10-14T10:30:00+08:00",
    }, operation_id="op-p2")

    # Plans may overlap; only formal bookings conflict.
    assert first["result"]["conflict_detected"] is False
    _invoke(env, "resource.booking.confirm", {
        "booking_id": first["result"]["booking_id"]}, operation_id="op-c1")

    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "resource.booking.confirm", {
            "booking_id": second["result"]["booking_id"]}, operation_id="op-c2")
    assert excinfo.value.code == "conflict_detected"
    assert excinfo.value.http_status == 409

    # The conflict left no confirmed row behind (transaction rolled back).
    rows = _invoke(env, "resource.booking.status", {
        "project_ids": ["proj_lab"], "status": "confirmed"}, operation_id="op-st")[
        "result"]["bookings"]
    assert len(rows) == 1

    # A non-overlapping slot confirms fine after the rejection.
    third = _invoke(env, "resource.booking.propose", {
        "resource_id": rid, "project_id": "proj_lab",
        "slot_start": "2026-10-14T11:00:00+08:00",
        "slot_end": "2026-10-14T12:00:00+08:00",
    }, operation_id="op-p3")
    ok = _invoke(env, "resource.booking.confirm", {
        "booking_id": third["result"]["booking_id"]}, operation_id="op-c3")
    assert ok["result"]["status"] == "confirmed"


def test_c3_booking_never_becomes_an_experiment_result(env):
    before = _fact_counts(env)
    rid = _invoke(env, "resource.register", {
        "project_id": "proj_lab", "name": "Room 302", "kind": "room",
    }, context=env["owner"], operation_id="op-reg")["result"]["resource_id"]
    plan = _invoke(env, "resource.booking.propose", {
        "resource_id": rid, "project_id": "proj_lab",
        "slot_start": "2026-10-14T09:00:00+08:00",
        "slot_end": "2026-10-14T10:00:00+08:00",
    }, operation_id="op-p")
    _invoke(env, "resource.booking.confirm", {
        "booking_id": plan["result"]["booking_id"]}, operation_id="op-c")
    _invoke(env, "resource.booking.cancel", {
        "booking_id": plan["result"]["booking_id"]}, operation_id="op-x")
    after = _fact_counts(env)
    assert after == before  # no experiment facts, no outbox, no proposals


def test_c4_scope_and_role_guards(env):
    rid = _invoke(env, "resource.register", {
        "project_id": "proj_lab", "name": "HPLC-1", "kind": "instrument",
    }, context=env["owner"], operation_id="op-reg")["result"]["resource_id"]

    with pytest.raises(auth.AuthError) as excinfo:
        _invoke(env, "resource.booking.propose", {
            "resource_id": rid, "project_id": "proj_lab",
            "slot_start": "2026-10-14T09:00:00+08:00",
            "slot_end": "2026-10-14T10:00:00+08:00",
        }, context=env["outsider"], operation_id="op-o")
    assert excinfo.value.code == "forbidden_scope"

    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "resource.booking.propose", {
            "resource_id": rid, "project_id": "proj_lab",
            "slot_start": "2026-10-14T09:00:00+08:00",
            "slot_end": "2026-10-14T10:00:00+08:00",
        }, context=env["viewer"], operation_id="op-v")
    assert excinfo.value.code == "forbidden_capability"

    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "resource.register", {
            "project_id": "proj_lab", "name": "X", "kind": "instrument",
        }, context=env["member"], operation_id="op-m")
    assert excinfo.value.code == "forbidden_capability"


# ---------------------------------------------------------------------------
# D: research milestones
# ---------------------------------------------------------------------------


def test_d1_criterion_checked_against_locatable_evidence(env):
    page_id = _page_with_blocks(
        env, ["Product isolated; mp 187-189 C recorded"], operation_id="s09d1")
    ms = _invoke(env, "research.milestone.create", {
        "project_id": "proj_lab", "title": "Target compound characterized",
        "description": "mp and identity evidence attached",
    }, operation_id="op-ms")["result"]
    crit = _invoke(env, "research.milestone.criterion.attach", {
        "milestone_id": ms["milestone_id"],
        "description": "Melting point recorded on notebook page",
        "evidence_refs": [{"page_id": page_id, "block_id": "s09d1-b0"}],
    }, operation_id="op-att")["result"]

    checked = _invoke(env, "research.milestone.criterion.check", {
        "criterion_id": crit["criterion_id"]}, operation_id="op-chk")
    assert checked["result"]["status"] == "satisfied"
    opened = checked["result"]["evidence_opened"][0]
    assert opened["open"] is True
    assert "187-189" in opened["text_excerpt"]
    assert checked["result"]["evidence_opened"][0]["current_revision"] == 1

    overview = _invoke(env, "research.milestone.status", {
        "project_ids": ["proj_lab"]}, operation_id="op-ov")
    milestone = overview["result"]["milestones"][0]
    assert milestone["criteria"][0]["status"] == "satisfied"
    # No overall research percentage anywhere (§15 D).
    assert not any("percent" in k or "progress" in k for k in milestone)

    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "research.milestone.decide", {
            "milestone_id": ms["milestone_id"], "decision": "accepted"},
            operation_id="op-dec-m")  # member cannot decide
    assert excinfo.value.code == "forbidden_capability"

    decided = _invoke(env, "research.milestone.decide", {
        "milestone_id": ms["milestone_id"], "decision": "accepted"},
        context=env["owner"], operation_id="op-dec")
    assert decided["result"]["status"] == "accepted"
    assert decided["result"]["decided_by"] == "prin_root"


def test_d2_correction_stales_dependent_criteria(env):
    page_id = _page_with_blocks(
        env, ["Yield recorded as 5.0 g"], operation_id="s09d2")
    ms = _invoke(env, "research.milestone.create", {
        "project_id": "proj_lab", "title": "Yield milestone",
    }, operation_id="op-ms")["result"]
    crit = _invoke(env, "research.milestone.criterion.attach", {
        "milestone_id": ms["milestone_id"],
        "description": "Yield value on the page",
        "evidence_refs": [{"page_id": page_id, "block_id": "s09d2-b0"}],
    }, operation_id="op-att")["result"]
    first = _invoke(env, "research.milestone.criterion.check", {
        "criterion_id": crit["criterion_id"]}, operation_id="op-chk1")
    assert first["result"]["status"] == "satisfied"

    # Human correction replaces the block set on a NEW revision.
    corrected = _invoke(env, "evidence.transcript.correct", {
        "page_id": page_id,
        "blocks": [_block("Yield corrected: 0.50 g", 0, "s09d2-human-0")],
        "expected_revision": 1,
        "reason": "decimal misplaced",
    }, operation_id="op-fix")
    assert corrected["action_status"] == "applied"

    overview = _invoke(env, "research.milestone.status", {
        "project_ids": ["proj_lab"]}, operation_id="op-ov")
    assert overview["result"]["milestones"][0]["criteria"][0]["status"] == "stale"

    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "research.milestone.criterion.check", {
            "criterion_id": crit["criterion_id"]}, operation_id="op-chk2")
    assert excinfo.value.code == "invalid_state"

    # Re-attaching fresh evidence refs works against the new revision.
    crit2 = _invoke(env, "research.milestone.criterion.attach", {
        "milestone_id": ms["milestone_id"],
        "description": "Corrected yield on the page",
        "evidence_refs": [{"page_id": page_id, "block_id": "s09d2-human-0"}],
    }, operation_id="op-att2")["result"]
    rechecked = _invoke(env, "research.milestone.criterion.check", {
        "criterion_id": crit2["criterion_id"]}, operation_id="op-chk3")
    assert rechecked["result"]["status"] == "satisfied"
    assert rechecked["result"]["evidence_opened"][0]["current_revision"] == 2


def test_d3_no_progress_percentage_schema(env):
    conn = db.connect(env["db_path"])
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(research_milestones)")}
        crit_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(milestone_criteria)")}
    finally:
        conn.close()
    assert not any("percent" in c or "progress" in c for c in cols | crit_cols)


# ---------------------------------------------------------------------------
# E: external results
# ---------------------------------------------------------------------------


def test_e1_computation_is_not_a_wetlab_measurement(env):
    before = _fact_counts(env)
    submitted = _invoke(env, "external.result.submit", {
        "project_id": "proj_lab", "source_kind": "computation",
        "title": "DFT optimization of conformer A",
        "tool_name": "orca", "tool_version": "5.0.4",
        "method": "B3LYP-D3/def2-SVP", "input_source": "SMILES from notebook NB-1",
        "inputs": {"smiles": "CCO", "charge": 0},
        "artifacts": [{"kind": "file", "path": "out/orca.out"}],
    }, operation_id="op-ext")["result"]
    assert submitted["source_kind"] == "computation"
    assert submitted["usage_semantics"] == "computation"
    assert len(submitted["inputs_hash"]) == 64

    # Wet-lab-only measurement fields are rejected on a computation result.
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "external.result.submit", {
            "project_id": "proj_lab", "source_kind": "computation",
            "title": "Not a measurement", "tool_name": "orca",
            "inputs": {"yield_percent": 82},
        }, operation_id="op-ext2")
    assert excinfo.value.code == "schema_violation"

    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "external.result.submit", {
            "project_id": "proj_lab", "source_kind": "computation",
            "title": "No provenance",
        }, operation_id="op-ext3")
    assert excinfo.value.code == "missing_provenance"

    after = _fact_counts(env)
    assert after == before  # nothing leaked into experiment facts / outbox

    rows = _invoke(env, "external.result.status", {
        "project_ids": ["proj_lab"], "source_kind": "computation"},
        operation_id="op-erd")["result"]["results"]
    assert len(rows) == 1
    assert rows[0]["artifact_refs"]["tool_name"] == "orca"


def test_e2_external_result_scope(env):
    _invoke(env, "external.result.submit", {
        "project_id": "proj_lab", "source_kind": "literature",
        "title": "Known mp range",
    }, operation_id="op-e1")
    with pytest.raises(auth.AuthError) as excinfo:
        _invoke(env, "external.result.submit", {
            "project_id": "proj_lab", "source_kind": "literature",
            "title": "from outside",
        }, context=env["outsider"], operation_id="op-e2")
    assert excinfo.value.code == "forbidden_scope"

    visible = _invoke(env, "external.result.status", {
        "project_ids": ["proj_lab"]}, context=env["viewer"],
        operation_id="op-e3")
    assert len(visible["result"]["results"]) == 1
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "external.result.status", {
            "project_ids": ["proj_lab"]}, context=env["outsider"],
            operation_id="op-e4")
    assert excinfo.value.code == "forbidden_scope"


# ---------------------------------------------------------------------------
# M: MCP same-origin adapter
# ---------------------------------------------------------------------------


def _mcp(env, message, context=None):
    return mcp.handle_mcp_request(env["db_path"], context or env["owner"], message)


def test_m1_initialize_and_scoped_tools_list(env):
    init = _mcp(env, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert init["result"]["serverInfo"]["name"] == "chemclaw-lab-mcp"

    tools = _mcp(env, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[
        "result"]["tools"]
    names = {t["name"] for t in tools}
    assert "resource.register" in names
    assert "external.result.submit" in names
    # Exactly the implemented, authorized capabilities — no leaks, no stubs.
    authorized = {c["capability_id"] for c in capabilities.list_capabilities(env["owner"])}
    assert names == authorized

    viewer_tools = {t["name"] for t in _mcp(
        env, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        context=env["viewer"])["result"]["tools"]}
    assert "resource.register" not in viewer_tools
    assert "external.result.submit" not in viewer_tools
    assert "resource.booking.status" in viewer_tools

    # Notifications get no reply.
    assert _mcp(env, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    unsupported = _mcp(env, {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
    assert unsupported["error"]["code"] == -32601


def test_m2_tools_call_reuses_capability_semantics(env):
    # Happy path through /v2 semantics, with a client-supplied idempotency key.
    call = _mcp(env, {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {
        "name": "resource.register",
        "arguments": {"project_id": "proj_lab", "name": "MCP-Rig",
                      "kind": "instrument", "operation_id": "mcp-op-1"},
    }})
    assert call["result"]["isError"] is False
    envelope = json.loads(call["result"]["content"][0]["text"])
    assert envelope["action_status"] == "applied"
    assert envelope["namespace"] == "mcp"
    assert envelope["operation_id"] == "mcp-op-1"

    replay = _mcp(env, {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {
        "name": "resource.register",
        "arguments": {"project_id": "proj_lab", "name": "MCP-Rig",
                      "kind": "instrument", "operation_id": "mcp-op-1"},
    }})
    envelope2 = json.loads(replay["result"]["content"][0]["text"])
    assert envelope2["result"]["resource_id"] == envelope["result"]["resource_id"]

    # Authorization is identical to the capability layer.
    denied = _mcp(env, {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {
        "name": "resource.register",
        "arguments": {"project_id": "proj_lab", "name": "No", "kind": "instrument"},
    }}, context=env["member"])
    assert denied["result"]["isError"] is True
    assert json.loads(denied["result"]["content"][0]["text"])[
        "error_code"] == "forbidden_capability"

    # Unimplemented capabilities are not callable either.
    unknown = _mcp(env, {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {
        "name": "research.paper.auto_publish", "arguments": {}}})
    assert unknown["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# X: extension mechanism
# ---------------------------------------------------------------------------


def test_x1_planner_manifest_plugs_in_without_touching_old_roles(env):
    from chemclaw_lab import agents as agents_mod

    manifest_dir = REPO_ROOT / "runtime" / "agents"
    old_ids = ("b_capture.json", "b_query.json", "observer.json")
    old_norm = {}
    for name in old_ids:
        data = json.loads((manifest_dir / name).read_text(encoding="utf-8"))
        old_norm[name] = agents_mod.validate_manifest(data)
        # Old manifests stay valid and untouched.
        assert old_norm[name]["agent_id"] == data["agent_id"]

    planner_data = json.loads(
        (manifest_dir / "planner.json").read_text(encoding="utf-8"))
    planner = agents_mod.validate_manifest(planner_data)
    assert planner["agent_id"] == "planner"
    assert "resource.booking.propose" in planner["required_capabilities"]
    # The planner never gets fact-writing or human-decision capabilities.
    assert "research.milestone.decide" not in planner["required_capabilities"]
    assert "inventory.message.process" not in planner["required_capabilities"]

    # S08 lesson: the model generates arguments strictly from the gateway
    # schemas — every planner tool MUST have a declared param schema with
    # its required keys, otherwise calls silently fail with schema_violation.
    from chemclaw_lab.agent_gateway import AGENT_TOOL_PARAM_SCHEMAS
    for capability_id in planner["required_capabilities"]:
        schema = AGENT_TOOL_PARAM_SCHEMAS.get(capability_id)
        assert schema is not None and schema.get("properties"), (
            f"planner tool {capability_id} has no param schema"
        )
    assert set(AGENT_TOOL_PARAM_SCHEMAS["resource.availability"]["required"]) == {
        "resource_id", "window_start", "window_end"}
    assert set(AGENT_TOOL_PARAM_SCHEMAS["resource.booking.propose"]["required"]) == {
        "resource_id", "project_id", "slot_start", "slot_end"}

    conn = db.connect(env["db_path"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        summary = agents_mod.load_registry(conn, manifest_dir)
        conn.commit()
    finally:
        conn.close()
    loaded_ids = {m["agent_id"] for m in summary["loaded"]}
    assert {"planner", "b_capture", "b_query", "observer"} <= loaded_ids
    assert summary["invalid"] == []

    rows_conn = db.connect(env["db_path"])
    try:
        manifests = {
            r["agent_id"]: r["enabled"]
            for r in rows_conn.execute(
                "SELECT agent_id, enabled FROM agent_manifests").fetchall()
        }
    finally:
        rows_conn.close()
    assert manifests["planner"] == 0  # disabled until an admin enables it
    assert manifests["b_capture"] == 0
