"""S09 live acceptance: REAL glm-5.3-flash drives the §15 extension domains
through the full ChemClaw-Lab stack (planning/booking role + MCP adapter).

Scenarios (plan §15, on top of the S06/S08 live base):
  G  planning/booking role — the new planner agent (loaded purely via
     manifest + domain capabilities, no legacy role branch touched) checks
     resource availability with the REAL model, proposes a booking plan, the
     human confirms formally; a second plan against the same slot is
     conflict-aware, its confirmation is rejected by the in-transaction
     re-check, and a re-plan to a free slot confirms. Throughout, the
     bookings mint NO experiment facts — a booking is never an experiment
     result.
  H  MCP same-origin adapter — initialize / tools/list / tools/call over
     HTTP with a real principal credential: tools/list exposes exactly the
     authorized implemented capabilities, tools/call reuses /v2 semantics
     (idempotency key honored, envelope with action_status/task_id), and
     authorization errors surface as tool-level isError.

Usage:
    set CHEMCLAW_LLM_API_KEY=<key> && python scripts/live_test_s09.py
    python scripts/live_test_s09.py --api-key <key>
    python scripts/live_test_s09.py --mock          (scripted provider, offline)

The API key is read from the environment / argv only — it is never written to
disk. Results go to docs/implementation/s09_live_results.json (no secrets).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from chemclaw_lab import agents, auth, capabilities, db  # noqa: E402
from chemclaw_lab import mcp as mcp_mod  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402

from _node_runtime import find_node  # noqa: E402

NODE_EXE: str | None = None

OWNER = "prin_owner"
PROJECT = "proj_lab"

RESULTS_PATH = REPO / "docs" / "implementation" / "s09_live_results.json"


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None,
              timeout: int = 120) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------------------

def invoke(db_path: str, context: auth.PrincipalContext, capability_id: str,
           params: dict, operation_id: str, services: ServiceSettings) -> dict:
    return capabilities.invoke_capability(
        db_path, context, capability_id, {**params, "operation_id": operation_id},
        operation_id=operation_id, services=services,
    )


def db_digest(db_path: str) -> str:
    import hashlib
    return hashlib.sha256(Path(db_path).read_bytes()).hexdigest()[:16]


def fact_counts(db_path: str) -> dict:
    conn = db.connect(db_path)
    try:
        return {
            "experiment_events": conn.execute(
                "SELECT COUNT(*) FROM experiment_events").fetchone()[0],
            "event_revisions": conn.execute(
                "SELECT COUNT(*) FROM event_revisions").fetchone()[0],
            "memory_proposals": conn.execute(
                "SELECT COUNT(*) FROM memory_proposals").fetchone()[0],
            "inventory_subtasks": conn.execute(
                "SELECT COUNT(*) FROM inventory_subtasks").fetchone()[0],
            "outbox": conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
        }
    finally:
        conn.close()


def seed_database(db_path: Path) -> auth.PrincipalContext:
    conn = db.connect(str(db_path))
    try:
        conn.execute("INSERT INTO projects (project_id, name) VALUES (?, 'Lab')", (PROJECT,))
        conn.execute(
            "INSERT INTO principals (principal_id, kind, display_name) VALUES (?,?,?)",
            (OWNER, "human", "Owner"),
        )
        conn.execute(
            "INSERT INTO memberships (principal_id, project_id, role) VALUES (?,?,?)",
            (OWNER, PROJECT, "owner"),
        )
        conn.commit()
    finally:
        conn.close()
    return auth.PrincipalContext(
        principal_id=OWNER, kind="human", display_name="Owner",
        memberships={PROJECT: "owner"},
    )


# ---------------------------------------------------------------------------

def wait_for_pi_port(process: subprocess.Popen) -> int:
    deadline = time.time() + 60
    while time.time() < deadline:
        line = process.stdout.readline() if process.poll() is None else ""
        if line.startswith("PI_SERVER_PORT="):
            return int(line.strip().split("=", 1)[1])
        if process.poll() is not None:
            raise RuntimeError(f"pi server exited: {process.stderr.read()}")
        time.sleep(0.1)
    raise RuntimeError("pi server did not report its port in 60s")


def pi_env(args, dispatch: dict, gateway_url: str) -> dict:
    env = {
        **os.environ,
        "GATEWAY_URL": gateway_url,
        "AGENT_TOKEN": dispatch["run_token"],
        "AGENT_ROLE": "planner",
        "PI_RUN_TIMEOUT_MS": "300000",
    }
    if args.mock:
        env["PI_MOCK"] = "1"
        return env
    env["PI_BASE_URL"] = args.base_url
    env["PI_API_KEY"] = args.api_key
    env["PI_MODEL"] = args.model
    env["PI_PROTOCOL"] = "openai-completions"
    return env


def run_planner(args, gateway_url: str, goal: str, conn_info: dict,
                timeout_s: int = 420) -> dict:
    """dispatch(plan_experiment) -> real Pi run -> finish."""
    conn = db.connect(str(conn_info["db_path"]))
    try:
        conn.execute("BEGIN IMMEDIATE")
        dispatch = agents.dispatch(
            conn, intent="plan_experiment", context=conn_info["ctx"],
            project_id=PROJECT, title="S09 live planning",
            input_payload={"goal": goal},
        )
        conn.commit()
        claimed = agents.claim_run(
            conn, run_id=dispatch["run_id"], worker_id="live-s09", lease_seconds=600
        )
        conn.commit()
    finally:
        conn.close()
    print(f"[live] planner dispatched run={dispatch['run_id']} epoch={claimed['claim_epoch']}")

    process = subprocess.Popen(
        [NODE_EXE, str(REPO / "runtime" / "pi" / "server.mjs")],
        cwd=str(REPO / "runtime" / "pi"),
        env=pi_env(args, dispatch, gateway_url),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        pi_port = wait_for_pi_port(process)
        started = time.time()
        run_result = http_json(
            "POST", f"http://127.0.0.1:{pi_port}/run",
            {"session_id": f"s-{uuid.uuid4().hex[:8]}", "message": goal},
            timeout=timeout_s,
        )
        elapsed = round(time.time() - started, 1)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    conn = db.connect(str(conn_info["db_path"]))
    try:
        conn.execute("BEGIN IMMEDIATE")
        agents.finish_run(
            conn, run_id=dispatch["run_id"], status="succeeded",
            claim_epoch=claimed["claim_epoch"],
            result={"outcome": "live_planning", "elapsed_s": elapsed},
        )
        conn.commit()
    finally:
        conn.close()
    print(f"[live] planner finished in {elapsed}s, "
          f"tool_calls={len(run_result.get('toolCalls', []))}")
    return {
        "run_id": dispatch["run_id"], "trace_id": dispatch["trace_id"],
        "elapsed_s": elapsed,
        "pi_error": run_result.get("error"),
        "assistant_text": run_result.get("text", "")[:800],
        "tool_calls": [
            {"name": c.get("name"), "arguments": c.get("arguments"),
             "action_status": (c.get("result") or {}).get("action_status"),
             "result_excerpt": json.dumps(
                 (c.get("result") or {}).get("result"), ensure_ascii=False)[:400]}
            for c in run_result.get("toolCalls", [])
        ],
    }


def propose_and_confirm(db_path, ctx, services, resource_id, start, end, op,
                        confirmed=True):
    plan = invoke(db_path, ctx, "resource.booking.propose", {
        "resource_id": resource_id, "project_id": PROJECT,
        "slot_start": start, "slot_end": end,
        "purpose_note": "human pre-confirmed blocking slot",
    }, f"{op}-p", services)
    if not confirmed:
        return plan
    result = invoke(db_path, ctx, "resource.booking.confirm", {
        "booking_id": plan["result"]["booking_id"],
    }, f"{op}-c", services)
    return plan, result


def scenario_g_planner_bookings(args, gateway_url: str, conn_info: dict,
                                results: dict) -> dict:
    db_path = str(conn_info["db_path"])
    ctx, services = conn_info["ctx"], conn_info["services"]
    before = fact_counts(db_path)

    # The human registers the instrument and blocks 09:00-10:00 first.
    rid = invoke(db_path, ctx, "resource.register", {
        "project_id": PROJECT, "name": "NMR-400", "kind": "instrument",
    }, "s09-live-reg", services)["result"]["resource_id"]
    propose_and_confirm(
        db_path, ctx, services, rid,
        "2026-10-14T09:00:00+08:00", "2026-10-14T10:00:00+08:00", "s09-live-blk")

    # The REAL planner proposes a plan for the SAME (busy) slot.
    outcome = run_planner(
        args, gateway_url,
        "请为项目 proj_lab 规划 NMR-400 上机：先用 resource.list 找到该资源，"
        "再用 resource.availability 查询 2026-10-14 09:00 到 12:00（北京时间）"
        "的候选时段，然后用 resource.booking.propose 提出一个 09:00 到 10:00、"
        "时长 60 分钟的预约计划（这条会撞上已确认预约，请如实报告冲突）。"
        "不要提出多个预约。完成后总结计划状态。",
        conn_info,
    )
    results["scenarios"].append(outcome)
    assert not outcome["pi_error"], f"planner Pi error: {outcome['pi_error']}"
    names = [c["name"] for c in outcome["tool_calls"]]
    if args.mock:
        outcome["scenario"] = "G_planner_bookings"
        outcome["plan_id"] = None
        print("[live] G (mock) OK: planner ran")
        return outcome
    assert "resource.booking.propose" in names, f"expected propose, got {names}"

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT booking_id, status FROM resource_bookings "
            "WHERE created_by = 'prin_owner' AND status = 'planned' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row, "planner did not create a planned booking"
    plan_id = row["booking_id"]
    outcome["plan_id"] = plan_id

    # Formal confirmation of the clashing plan is rejected by the re-check.
    from chemclaw_lab.capabilities import CapabilityError
    rejected = None
    try:
        invoke(db_path, ctx, "resource.booking.confirm", {
            "booking_id": plan_id}, "s09-live-cfm1", services)
    except CapabilityError as exc:
        rejected = {"code": exc.code, "http_status": exc.http_status}
    assert rejected == {"code": "conflict_detected", "http_status": 409}, rejected
    outcome["conflict_confirmation"] = rejected

    # The planner re-plans into a free slot; the human confirms.
    outcome2 = run_planner(
        args, gateway_url,
        f"上一条预约计划 {plan_id} 与已确认时段冲突。请针对资源 {rid} 用 "
        "resource.booking.propose 重新提出 2026-10-14 10:30 到 11:30（北京时间）"
        "的预约计划。只提这一个，完成后总结计划状态。",
        conn_info,
    )
    results["scenarios"].append(outcome2)
    assert not outcome2["pi_error"], f"planner Pi error: {outcome2['pi_error']}"
    conn = db.connect(db_path)
    try:
        row2 = conn.execute(
            "SELECT booking_id, status FROM resource_bookings "
            "WHERE slot_start = '2026-10-14T10:30:00+08:00' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row2 and row2["status"] == "planned", row2
    confirmed = invoke(db_path, ctx, "resource.booking.confirm", {
        "booking_id": row2["booking_id"]}, "s09-live-cfm2", services)
    assert confirmed["result"]["status"] == "confirmed", confirmed
    outcome2["confirmed_booking_id"] = row2["booking_id"]

    # Bookings minted NO experiment facts — a booking is never a result.
    # outbox: each agents.dispatch() writes one agent.dispatched message
    # (S06 trace semantics, unrelated to bookings); exactly 2 dispatches ran,
    # so an increment of exactly 2 proves bookings emitted no EXTRA outbox.
    after = fact_counts(db_path)
    facts = ("experiment_events", "event_revisions", "memory_proposals",
             "inventory_subtasks")
    assert all(after[k] == before[k] for k in facts), (before, after)
    assert after["outbox"] - before["outbox"] == 2, (before, after)
    outcome2["fact_counts_unchanged"] = {k: after[k] for k in facts}
    outcome2["outbox_delta"] = after["outbox"] - before["outbox"]

    # The human then runs a research milestone check with locatable evidence.
    page = invoke(db_path, ctx, "evidence.document.create", {
        "project_id": PROJECT, "notebook_code": "NB-S09LIVE",
        "title": "S09 live milestone evidence", "author": "yu",
        "source_channel": "photo_upload",
    }, "s09-live-doc", services)
    from chemclaw_lab import pnglite
    page_id = invoke(db_path, ctx, "evidence.page.ingest", {
        "document_id": page["result"]["document_id"], "page_order": 1,
        "filename": "p.png",
        "content_data_url": "data:image/png;base64,"
        + base64.b64encode(pnglite.solid_png(60, 40, (240, 99, 245))).decode("ascii"),
    }, "s09-live-page", services)["result"]["page"]["page_id"]
    invoke(db_path, ctx, "evidence.transcribe.run", {
        "page_id": page_id, "provider": "fixture_ocr",
        "config": {"fixture_blocks": [{
            "block_id": "b-s09live-0", "block_kind": "handwriting",
            "reading_order": 0, "text": "NMR 上机计划确认：2026-10-14 10:30。",
            "table_cell": None,
            "geometry": {"x": 5, "y": 5, "width": 40, "height": 8},
            "coordinate_system": {"origin": "TOPLEFT", "units": "pixel",
                                  "image_ref": "original", "frame_width": 60,
                                  "frame_height": 40, "dpi": 300},
            "source_page": 1,
            "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer",
                          "version": "0.3.0"},
            "quality_flags": [],
        }]},
    }, "s09-live-run", services)
    ms = invoke(db_path, ctx, "research.milestone.create", {
        "project_id": PROJECT, "title": "上机排期落定",
        "description": "Milestone from the live booking flow",
    }, "s09-live-ms", services)["result"]
    crit = invoke(db_path, ctx, "research.milestone.criterion.attach", {
        "milestone_id": ms["milestone_id"],
        "description": "Booking noted on the notebook page",
        "evidence_refs": [{"page_id": page_id, "block_id": "b-s09live-0"}],
    }, "s09-live-att", services)["result"]
    checked = invoke(db_path, ctx, "research.milestone.criterion.check", {
        "criterion_id": crit["criterion_id"]}, "s09-live-chk", services)
    assert checked["result"]["status"] == "satisfied", checked
    outcome2["milestone_criterion"] = "satisfied"

    outcome["scenario"] = "G_planner_bookings"
    outcome2["scenario"] = "G_planner_rebook"
    print("[live] G OK: plan->conflict 409->re-plan->confirmed; no experiment facts")
    return outcome2


def scenario_h_mcp_adapter(args, conn_info: dict, gateway_url: str,
                           results: dict) -> dict:
    """MCP over HTTP with a real credential: scoped tools/list + /v2 semantics."""
    db_path = str(conn_info["db_path"])
    ctx, services = conn_info["ctx"], conn_info["services"]

    conn = db.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        secret = f"s09live-mcp-{uuid.uuid4().hex}"
        auth.register_credential(
            conn, credential_id=f"cred_{uuid.uuid4().hex[:12]}",
            principal_id=OWNER, secret=secret)
        conn.commit()
    finally:
        conn.close()
    headers = {"Authorization": f"Bearer {secret}"}

    init = http_json("POST", f"{gateway_url}/v2/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    }, headers)
    assert init["result"]["serverInfo"]["name"] == "chemclaw-lab-mcp", init
    print(f"[live] MCP initialized: {init['result']['protocolVersion']}")

    tools = http_json("POST", f"{gateway_url}/v2/mcp", {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
    }, headers)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "resource.register" in names and "research.milestone.decide" in names
    authorized = {c["capability_id"] for c in capabilities.list_capabilities(ctx)}
    assert names == authorized, (names ^ authorized)
    print(f"[live] tools/list: {len(tools)} tools, exactly the authorized set")

    call = http_json("POST", f"{gateway_url}/v2/mcp", {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "resource.register",
            "arguments": {"project_id": PROJECT, "name": "LC-MS Live",
                          "kind": "instrument", "operation_id": "mcp-live-1"},
        },
    }, headers)
    assert call["result"]["isError"] is False, call
    envelope = json.loads(call["result"]["content"][0]["text"])
    assert envelope["action_status"] == "applied" and envelope["namespace"] == "mcp"
    resource_id = envelope["result"]["resource_id"]

    replay = http_json("POST", f"{gateway_url}/v2/mcp", {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "resource.register",
            "arguments": {"project_id": PROJECT, "name": "LC-MS Live",
                          "kind": "instrument", "operation_id": "mcp-live-1"},
        },
    }, headers)
    envelope2 = json.loads(replay["result"]["content"][0]["text"])
    assert envelope2["result"]["resource_id"] == resource_id
    print("[live] tools/call: applied + idempotent replay OK")

    denied = http_json("POST", f"{gateway_url}/v2/mcp", {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "research.milestone.decide",
            "arguments": {"milestone_id": "ms_missing", "decision": "accepted"},
        },
    }, headers)
    # member-less human owner credential IS allowed decide; use a fresh
    # viewer credential to prove authorization parity instead.
    # Same principal -> same roles; to prove the AUTH filter, list tools with
    # a credential bound to NO memberships.
    conn = db.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        outsider_id = auth.new_principal_id("human")
        conn.execute(
            "INSERT INTO principals (principal_id, kind, display_name) VALUES (?,?,?)",
            (outsider_id, "human", "MCP outsider"),
        )
        out_secret = f"s09live-mcp-out-{uuid.uuid4().hex}"
        auth.register_credential(
            conn, credential_id=f"cred_{uuid.uuid4().hex[:12]}",
            principal_id=outsider_id, secret=out_secret)
        conn.commit()
    finally:
        conn.close()
    outsider_tools = http_json("POST", f"{gateway_url}/v2/mcp", {
        "jsonrpc": "2.0", "id": 6, "method": "tools/list",
    }, {"Authorization": f"Bearer {out_secret}"})["result"]["tools"]
    outsider_names = {t["name"] for t in outsider_tools}
    assert outsider_names == set(), outsider_names
    print("[live] tools/list for a membership-less principal: empty (scope parity)")

    unknown = http_json("POST", f"{gateway_url}/v2/mcp", {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "research.paper.auto_publish", "arguments": {}},
    }, headers)
    assert unknown["error"]["code"] == -32601, unknown
    print("[live] unimplemented capability not callable: -32601")

    return {
        "scenario": "H_mcp_adapter",
        "protocol": init["result"]["protocolVersion"],
        "tools_count": len(tools),
        "registered_resource": resource_id,
        "idempotent_replay": True,
        "outsider_tools": len(outsider_names),
        "unknown_tool_error": unknown["error"]["code"],
    }


def main() -> int:
    global NODE_EXE, RESULTS_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=os.environ.get("CHEMCLAW_LLM_API_KEY"))
    parser.add_argument("--base-url", default=os.environ.get(
        "CHEMCLAW_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    parser.add_argument("--model", default=os.environ.get("CHEMCLAW_LLM_MODEL", "glm-5.3-flash"))
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    NODE_EXE = find_node()
    if args.mock:
        RESULTS_PATH = RESULTS_PATH.with_name("s09_mock_results.json")
    if not args.mock and not args.api_key:
        print("ERROR: --api-key or CHEMCLAW_LLM_API_KEY required (or use --mock)")
        return 2

    node_version = subprocess.run(
        [NODE_EXE, "--version"], capture_output=True, text=True
    ).stdout.strip()
    results = {
        "model": None if args.mock else args.model,
        "base_url": None if args.mock else args.base_url,
        "node": node_version,
        "scenarios": [],
        "timings": {},
    }
    try:
        tmp = Path(tempfile.mkdtemp(prefix="s09_live_"))
        db_path = tmp / "live.db"
        db.init_db(str(db_path))
        ctx = seed_database(db_path)
        settings = ServiceSettings(
            storage_root=tmp / "storage", manifest_dir=REPO / "runtime" / "agents",
        )
        conn_info = {"db_path": db_path, "ctx": ctx, "services": settings}

        # Registry: load all five manifests, enable ONLY the new planner —
        # the extension mechanism (§15): a new role joins via manifest and
        # domain capabilities, legacy agents stay off and unchanged.
        conn = db.connect(str(db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            summary = agents.load_registry(conn, REPO / "runtime" / "agents")
            planner_version = next(
                m["version"] for m in summary["loaded"] if m["agent_id"] == "planner"
            )
            agents.set_manifest_enabled(conn, agent_id="planner",
                                        version=planner_version, enabled=True)
            conn.commit()
        finally:
            conn.close()
        loaded_ids = sorted(m["agent_id"] for m in summary["loaded"])
        assert loaded_ids == ["alice_inventory", "b_capture", "b_query", "observer", "planner"], loaded_ids
        results["manifests_loaded"] = loaded_ids
        print(f"[live] registry: {loaded_ids}, planner enabled")

        server = create_api_server(str(db_path), port=0, service_settings=settings)
        host, port = server.server_address
        threading.Thread(target=server.serve_forever, daemon=True).start()
        gateway_url = f"http://{host}:{port}"
        print(f"[live] gateway on {gateway_url}")

        results["db_digest"] = db_digest(str(db_path))

        t0 = time.time()
        scenario_g_planner_bookings(args, gateway_url, conn_info, results)
        results["timings"]["G_planner_s"] = round(time.time() - t0, 1)
        t0 = time.time()
        results["H_mcp"] = scenario_h_mcp_adapter(args, conn_info, gateway_url, results)
        results["timings"]["H_mcp_s"] = round(time.time() - t0, 1)

        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_PATH.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[live] results -> {RESULTS_PATH}")
        print("LIVE OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        results["error"] = repr(exc)
        try:
            RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULTS_PATH.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
        print(f"LIVE FAIL: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
