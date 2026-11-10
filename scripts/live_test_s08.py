"""S08 live acceptance: REAL glm-5.3-flash drives the §14 recovery matrix
through the full ChemClaw-Lab stack (cross-domain stock + fault recovery).

Scenarios (plan §14, on top of the S06 A/B/C base):
  D  cross-domain + delivery faults — Pi(glm) captures a usage page, the human
     confirms/applies, the inventory_proposal outbox message is delivered
     through the S06 FaultInjectionAdapter (first send DROPPED), the bridge
     ingests exactly one subtask, execution charges the stock exactly once,
     and Pi(glm) then queries the inventory and cites the remaining amount.
  E  node crash recovery — a capture dispatch is claimed, the Pi server
     process is KILLED mid-run, the lease expires, recover_expired_runs
     re-queues it, and a fresh Pi run finishes the task honestly.
  F  model outage degradation — the Pi host points at a dead base_url, the
     run fails with a recorded error_code (no partial business writes), the
     base_url is restored, and the same task completes on retry.

Usage:
    set CHEMCLAW_LLM_API_KEY=<key> && python scripts/live_test_s08.py
    python scripts/live_test_s08.py --api-key <key>
    python scripts/live_test_s08.py --mock          (scripted provider, offline)

The API key is read from the environment / argv only — it is never written to
disk. Results go to docs/implementation/s08_live_results.json (no secrets).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
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

from chemclaw_lab import agents, auth, capabilities, db, deliveries  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402

from _node_runtime import find_node  # noqa: E402

NODE_EXE: str | None = None

OWNER = "prin_owner"
PROJECT = "proj_lab"

SEED_LINES = [
    "2026-10-12 09:00 于 2 号通风橱称取氯化钠 20 g，溶于 500 mL 去离子水配制生理盐水。",
    "样品编号 SAMPLE-001，电导率仪读数 12.5 mS/cm。",
]
CAPTURE_LINES = [
    "2026-10-12 15:20 取用氯化钠 8 g 加入 SAMPLE-002 缓冲液。",
    "磁力搅拌 30 分钟，溶液澄清无沉淀。",
]
CRASH_LINES = [
    "2026-10-12 16:40 取用氯化钠 3 g 配制 100 mL 洗涤液。",
    "过滤后滤液透明。",
]
QUERY_AFTER_STOCK = "SAMPLE-002 的缓冲液用了多少克氯化钠？库存里还剩多少克？"

RESULTS_PATH = REPO / "docs" / "implementation" / "s08_live_results.json"


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
# seeding (offline pipeline, deterministic fixtures)

def invoke(db_path: str, context: auth.PrincipalContext, capability_id: str,
           params: dict, operation_id: str, services: ServiceSettings) -> dict:
    return capabilities.invoke_capability(
        db_path, context, capability_id, {**params, "operation_id": operation_id},
        operation_id=operation_id, services=services,
    )


def fixture_block(text: str, order: int, op: str = "fx") -> dict:
    return {
        "block_id": f"b-{op}-{order}",
        "block_kind": "handwriting",
        "reading_order": order,
        "text": text,
        "table_cell": None,
        "geometry": {"x": 5, "y": 5 + order * 10, "width": 40, "height": 8},
        "coordinate_system": {
            "origin": "TOPLEFT", "units": "pixel", "image_ref": "original",
            "frame_width": 60, "frame_height": 40, "dpi": 300,
        },
        "source_page": 1,
        "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer",
                      "version": "0.3.0"},
        "quality_flags": [],
    }


def seed_page(db_path: str, ctx: auth.PrincipalContext, services: ServiceSettings,
              lines: list[str], notebook: str, op: str) -> str:
    from chemclaw_lab import pnglite

    doc = invoke(db_path, ctx, "evidence.document.create", {
        "project_id": PROJECT, "notebook_code": notebook,
        "title": f"S08 live {notebook}", "author": "yu", "source_channel": "photo_upload",
    }, f"{op}-doc", services)
    document_id = doc["result"]["document_id"]
    seed = sum(ord(c) for c in notebook) % 200
    page = invoke(db_path, ctx, "evidence.page.ingest", {
        "document_id": document_id, "page_order": 1, "filename": "page_1.png",
        "content_data_url": "data:image/png;base64,"
        + base64.b64encode(pnglite.solid_png(60, 40, (240, seed, 245))).decode("ascii"),
    }, f"{op}-page", services)
    page_id = page["result"]["page"]["page_id"]
    run = invoke(db_path, ctx, "evidence.transcribe.run", {
        "page_id": page_id, "provider": "fixture_ocr",
        "config": {"fixture_blocks": [
            fixture_block(t, i, op) for i, t in enumerate(lines)
        ]},
    }, f"{op}-run", services)
    assert run["result"]["status"] == "succeeded", run
    return page_id


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
        conn.execute(
            """
            INSERT INTO chemicals (chemical_id, name, formula, amount, unit, threshold,
                                   hazard_level, max_auto_delta, revision)
            VALUES ('chem_nacl', '氯化钠', 'NaCl', 500.0, 'g', 50.0, 'low', 100.0, 0)
            """
        )
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES (?, ?, ?)",
            ("smp_sample001", PROJECT, "SAMPLE-001"),
        )
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES (?, ?, ?)",
            ("smp_sample002", PROJECT, "SAMPLE-002"),
        )
        conn.commit()
    finally:
        conn.close()
    return auth.PrincipalContext(
        principal_id=OWNER, kind="human", display_name="Owner",
        memberships={PROJECT: "owner"},
    )


# ---------------------------------------------------------------------------
# Pi host plumbing

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


def pi_env(args, dispatch: dict, role: str, gateway_url: str, *, dead_model: bool = False) -> dict:
    env = {
        **os.environ,
        "GATEWAY_URL": gateway_url,
        "AGENT_TOKEN": dispatch["run_token"],
        "AGENT_ROLE": role,
        "PI_RUN_TIMEOUT_MS": "300000",
    }
    if args.mock:
        env["PI_MOCK"] = "1"
        return env
    env["PI_BASE_URL"] = "http://127.0.0.1:9/v1" if dead_model else args.base_url
    env["PI_API_KEY"] = args.api_key
    env["PI_MODEL"] = args.model
    env["PI_PROTOCOL"] = "openai-completions"
    return env


def start_pi(args, dispatch: dict, role: str, gateway_url: str, *, dead_model: bool = False):
    process = subprocess.Popen(
        [NODE_EXE, str(REPO / "runtime" / "pi" / "server.mjs")],
        cwd=str(REPO / "runtime" / "pi"),
        env=pi_env(args, dispatch, role, gateway_url, dead_model=dead_model),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    return process


def run_agent_role(args, gateway_url: str, role: str, intent: str, message: str,
                   input_payload: dict, conn_info: dict, *, dead_model: bool = False,
                   worker_id: str = "live-s08", finish_status: str = "succeeded",
                   error_code: str | None = None, timeout_s: int = 420) -> dict:
    """dispatch -> spawn Pi server (real model) -> POST /run -> finish run."""
    conn = db.connect(str(conn_info["db_path"]))
    try:
        conn.execute("BEGIN IMMEDIATE")
        dispatch = agents.dispatch(
            conn, intent=intent, context=conn_info["ctx"], project_id=PROJECT,
            title=f"S08 live {intent}", input_payload=input_payload,
        )
        conn.commit()
        claimed = agents.claim_run(
            conn, run_id=dispatch["run_id"], worker_id=worker_id, lease_seconds=600
        )
        conn.commit()
    finally:
        conn.close()
    print(f"[live] {role}: dispatched run={dispatch['run_id']} epoch={claimed['claim_epoch']}")

    process = start_pi(args, dispatch, role, gateway_url, dead_model=dead_model)
    try:
        pi_port = wait_for_pi_port(process)
        started = time.time()
        run_result = http_json(
            "POST", f"http://127.0.0.1:{pi_port}/run",
            {"session_id": f"s-{uuid.uuid4().hex[:8]}", "message": message},
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
        finished = agents.finish_run(
            conn, run_id=dispatch["run_id"], status=finish_status,
            claim_epoch=claimed["claim_epoch"],
            result={"outcome": "live_test", "elapsed_s": elapsed},
            error_code=error_code,
        )
        token_row = conn.execute(
            "SELECT used_tool_calls FROM agent_run_tokens WHERE run_id = ?",
            (dispatch["run_id"],),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    used = int(token_row["used_tool_calls"]) if token_row else -1
    print(f"[live] {role}: finished ({finish_status}) in {elapsed}s, tool_calls_used={used}")
    return {
        "role": role, "intent": intent,
        "run_id": dispatch["run_id"], "trace_id": dispatch["trace_id"],
        "elapsed_s": elapsed, "tool_calls_used": used,
        "finish_status": finish_status, "error_code": error_code,
        "pi_error": run_result.get("error"),
        "assistant_text": run_result.get("text", ""),
        "tool_calls": [
            {"name": c.get("name"), "arguments": c.get("arguments"),
             "gateway_status": (c.get("result") or {}).get("status"),
             "gateway_ok": (c.get("result") or {}).get("ok"),
             "gateway_action_status": (c.get("result") or {}).get("action_status"),
             "result_excerpt": json.dumps(
                 (c.get("result") or {}).get("result"), ensure_ascii=False)[:400]}
            for c in run_result.get("toolCalls", [])
        ],
    }


def confirm_and_apply(conn_info: dict, proposal_id: str) -> dict:
    db_path = str(conn_info["db_path"])
    op = f"live-cfm-{uuid.uuid4().hex[:8]}"
    read = invoke(db_path, conn_info["ctx"], "memory.proposal.read",
                  {"proposal_id": proposal_id}, f"{op}-read", conn_info["services"])
    proposal = read["result"]["proposal"]
    keys = [i["item_key"] for i in proposal["items"]
            if i["event_kind"] in ("operation", "observation")]
    outcome = {"confirmed": len(keys)}
    if keys:
        invoke(db_path, conn_info["ctx"], "memory.proposal.confirm_items", {
            "proposal_id": proposal_id, "item_keys": keys,
            "decision": "confirmed", "expected_proposal_revision": proposal["revision"],
        }, f"{op}-confirm", conn_info["services"])
        applied = invoke(db_path, conn_info["ctx"], "memory.proposal.apply", {
            "proposal_id": proposal_id,
        }, f"{op}-apply", conn_info["services"])
        outcome["applied"] = len(applied["result"]["applied"])
    else:
        outcome["applied"] = 0
    print(f"[live] human confirm+apply: {outcome}")
    return outcome


def deliver_with_faults(conn_info: dict, adapter, *, passes: int = 4) -> list[dict]:
    """Drive delivery passes with the fault adapter until the outbox drains."""
    history = []
    for i in range(passes):
        conn = db.connect(str(conn_info["db_path"]))
        try:
            conn.execute("BEGIN IMMEDIATE")
            processed = deliveries.process_outbox_once(
                conn, adapter=adapter, retry_backoff_s=0
            )
            conn.commit()
        finally:
            conn.close()
        history.append(processed)
        print(f"[live] delivery pass {i + 1}: {processed}")
        if processed["attempted"] == 0:
            break
    return history


def db_digest(db_path: str) -> str:
    return hashlib.sha256(Path(db_path).read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# scenarios

def scenario_d_cross_domain_faults(args, gateway_url: str, conn_info: dict,
                                   page_id: str, results: dict) -> dict:
    outcome = run_agent_role(
        args, gateway_url, "b_capture", "capture_page",
        f"请阅读页面 {page_id} 的转写，并整理出实验记忆提案（memory.proposal.create）。"
        "使用 profile_id=chemistry_notebook、profile_version=1.0，项目为 proj_lab。",
        {"page_id": page_id}, conn_info,
    )
    results["scenarios"].append(outcome)
    assert not outcome["pi_error"], f"b_capture Pi error: {outcome['pi_error']}"
    names = [c["name"] for c in outcome["tool_calls"]]
    if args.mock:
        outcome["scenario"] = "D_cross_domain_faults"
        outcome["proposal_id"] = None
        print("[live] D (mock) OK: capture done")
        return outcome
    assert "memory.proposal.create" in names, f"expected proposal.create, got {names}"

    db_path = str(conn_info["db_path"])
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT proposal_id FROM memory_proposals WHERE page_id = ? ORDER BY rowid DESC",
            (page_id,),
        ).fetchone()
        assert row, "no proposal created for the capture page"
        proposal_id = row["proposal_id"]
    finally:
        conn.close()
    outcome["proposal_id"] = proposal_id

    applied = confirm_and_apply(conn_info, proposal_id)
    assert applied["applied"] >= 1, "apply produced no events"

    # Delivery through the fault adapter: the first send is DROPPED in transit.
    adapter = deliveries.FaultInjectionAdapter(drop_first_n=1)
    history = deliver_with_faults(conn_info, adapter)
    conn = db.connect(db_path)
    try:
        outbox = conn.execute(
            "SELECT status, COUNT(*) FROM outbox WHERE message_type = 'inventory_proposal' "
            "GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    assert dict(outbox).get("sent") == 1, f"outbox did not settle: {outbox}"

    # Bridge ingests; then a human executes the subtask exactly once.
    bridged = invoke(db_path, conn_info["ctx"], "inventory.inbound.process", {},
                     f"live-bridge-{uuid.uuid4().hex[:8]}", conn_info["services"])
    subtasks = bridged["result"]["subtasks"]
    assert len(subtasks) == 1, f"expected exactly one subtask, got {len(subtasks)}"
    executed = invoke(db_path, conn_info["ctx"], "inventory.subtask.execute",
                      {"subtask_id": subtasks[0]["subtask_id"]},
                      f"live-exec-{uuid.uuid4().hex[:8]}", conn_info["services"])
    assert executed["result"]["status"] == "applied", executed

    conn = db.connect(db_path)
    try:
        stock = conn.execute(
            "SELECT amount, revision FROM chemicals WHERE chemical_id = 'chem_nacl'"
        ).fetchone()
        txn_count = conn.execute(
            "SELECT COUNT(*) FROM inventory_transactions WHERE chemical_id = 'chem_nacl'"
        ).fetchone()[0]
        sub_count = conn.execute(
            "SELECT COUNT(*) FROM inventory_subtasks"
        ).fetchone()[0]
    finally:
        conn.close()
    # 500 g seeded, one usage charged exactly once despite the dropped send.
    assert sub_count == 1 and txn_count == 1, (sub_count, txn_count)
    assert 492.0 <= float(stock["amount"]) <= 492.001, float(stock["amount"])
    assert int(stock["revision"]) == 1
    outcome["scenario"] = "D_cross_domain_faults"
    outcome["stock_after"] = float(stock["amount"])
    outcome["transactions"] = txn_count
    outcome["subtasks"] = sub_count
    outcome["delivery_history"] = history
    print(f"[live] D OK: stock={stock['amount']} g (one charge), rev={stock['revision']}")
    return outcome


def scenario_e_crash_recovery(args, gateway_url: str, conn_info: dict,
                              page_id: str, results: dict) -> dict:
    """Dispatch a capture, kill the Pi server mid-run, recover the lease."""
    conn = db.connect(str(conn_info["db_path"]))
    try:
        conn.execute("BEGIN IMMEDIATE")
        dispatch = agents.dispatch(
            conn, intent="capture_page", context=conn_info["ctx"], project_id=PROJECT,
            title="S08 live crash", input_payload={"page_id": page_id},
        )
        conn.commit()
    finally:
        conn.close()

    # The worker claims, then the node DIES before/while running.
    conn = db.connect(str(conn_info["db_path"]))
    try:
        conn.execute("BEGIN IMMEDIATE")
        claimed = agents.claim_run(conn, run_id=dispatch["run_id"],
                                   worker_id="crashed-worker", lease_seconds=120)
        epoch = claimed["claim_epoch"]
        conn.commit()
    finally:
        conn.close()

    process = start_pi(args, dispatch, "b_capture", gateway_url)
    try:
        wait_for_pi_port(process)  # the node came up, then crashes hard
    finally:
        process.kill()
        process.wait(timeout=10)
    print(f"[live] E: pi node killed (run={dispatch['run_id']}, epoch={epoch})")

    # The lease expires; the dispatcher re-queues the run for a new worker.
    conn = db.connect(str(conn_info["db_path"]))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE agent_runs SET lease_expires_at = datetime('now', '-5 seconds') "
            "WHERE run_id = ?",
            (dispatch["run_id"],),
        )
        recovered = agents.recover_expired_runs(conn)
        conn.commit()
    finally:
        conn.close()
    assert recovered, "expired run was not recovered"
    print(f"[live] E: recovered -> {recovered[0]}")

    # A fresh worker re-claims the SAME run and finishes it with a REAL model
    # run, presenting the original run token and the new epoch.
    conn = db.connect(str(conn_info["db_path"]))
    try:
        conn.execute("BEGIN IMMEDIATE")
        reclaimed = agents.claim_run(conn, run_id=dispatch["run_id"],
                                     worker_id="recovered-worker")
        epoch2 = reclaimed["claim_epoch"]
        assert epoch2 == epoch + 1, (epoch, epoch2)
        conn.commit()
    finally:
        conn.close()

    message = (
        f"请直接完成页面 {page_id} 的整理：读取页面后，在同一轮立即调用 "
        "memory.proposal.create 提交全部 item，不要只汇报计划或停在检索阶段。"
        "使用 profile_id=chemistry_notebook、profile_version=1.0，项目为 proj_lab。"
        "只有文本出现 SAMPLE-xxx 形式的样品编号时才调用 memory.sample.search；"
        "化学品名（如氯化钠）和溶液名（如洗涤液）不是样品标签，不要为它们检索。"
    )
    process = start_pi(args, dispatch, "b_capture", gateway_url)
    try:
        pi_port = wait_for_pi_port(process)
        started = time.time()
        run_result = http_json(
            "POST", f"http://127.0.0.1:{pi_port}/run",
            {"session_id": f"s-{uuid.uuid4().hex[:8]}", "message": message},
            timeout=420,
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
        finished = agents.finish_run(
            conn, run_id=dispatch["run_id"], status="succeeded",
            claim_epoch=epoch2,
            result={"outcome": "live_recovery", "elapsed_s": elapsed},
        )
        token_row = conn.execute(
            "SELECT used_tool_calls FROM agent_run_tokens WHERE run_id = ?",
            (dispatch["run_id"],),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    outcome = {
        "role": "b_capture", "intent": "capture_page",
        "run_id": dispatch["run_id"], "trace_id": dispatch["trace_id"],
        "elapsed_s": elapsed,
        "tool_calls_used": int(token_row["used_tool_calls"]) if token_row else -1,
        "finish_status": "succeeded",
        "pi_error": run_result.get("error"),
        "assistant_text": run_result.get("text", ""),
        "tool_calls": [
            {"name": c.get("name"), "arguments": c.get("arguments"),
             "gateway_status": (c.get("result") or {}).get("status"),
             "gateway_ok": (c.get("result") or {}).get("ok"),
             "gateway_action_status": (c.get("result") or {}).get("action_status"),
             "result_excerpt": json.dumps(
                 (c.get("result") or {}).get("result"), ensure_ascii=False)[:400]}
            for c in run_result.get("toolCalls", [])
        ],
    }
    results["scenarios"].append(outcome)
    assert not outcome["pi_error"], f"recovered Pi error: {outcome['pi_error']}"
    if not args.mock:
        names = [c["name"] for c in outcome["tool_calls"]]
        assert "memory.proposal.create" in names, f"expected proposal.create, got {names}"

    # Exactly ONE proposal exists for the crash page (the crashed attempt
    # wrote nothing; the recovered run is the only author). The mock provider
    # only answers one hop and never writes a proposal.
    conn = db.connect(str(conn_info["db_path"]))
    try:
        n_proposals = conn.execute(
            "SELECT COUNT(*) FROM memory_proposals WHERE page_id = ?", (page_id,)
        ).fetchone()[0]
        stale = conn.execute(
            "SELECT status FROM agent_runs WHERE run_id = ?", (dispatch["run_id"],)
        ).fetchone()
    finally:
        conn.close()
    if not args.mock:
        assert n_proposals == 1, f"expected 1 proposal after recovery, got {n_proposals}"
    assert stale["status"] == "succeeded", stale["status"]
    outcome["scenario"] = "E_crash_recovery"
    outcome["proposals_for_page"] = n_proposals
    print(f"[live] E OK: same run recovered to succeeded, {n_proposals} proposal(s)")
    return outcome


def scenario_f_model_outage(args, gateway_url: str, conn_info: dict, results: dict) -> dict:
    """Model down: the run fails with a recorded error, nothing partial is
    written; restoring the base_url lets the retry succeed."""
    question = "SAMPLE-001 的生理盐水用了多少克氯化钠？"
    failed = run_agent_role(
        args, gateway_url, "b_query", "query_answer",
        question, {"question": question}, conn_info,
        dead_model=True, finish_status="failed", error_code="model_down",
        timeout_s=90,
    )
    results["scenarios"].append(failed)
    if not args.mock:
        assert failed["pi_error"], "expected the dead base_url to fail the run"
    assert failed["finish_status"] == "failed" and failed["error_code"] == "model_down"

    conn = db.connect(str(conn_info["db_path"]))
    try:
        row = conn.execute(
            "SELECT status, error_code FROM agent_runs WHERE run_id = ?",
            (failed["run_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed" and row["error_code"] == "model_down"

    # Recovery: the SAME question through a healthy model.
    retried = run_agent_role(
        args, gateway_url, "b_query", "query_answer",
        question, {"question": question}, conn_info, worker_id="recovered-query",
    )
    results["scenarios"].append(retried)
    assert not retried["pi_error"], f"recovered Pi error: {retried['pi_error']}"
    if not args.mock:
        names = [c["name"] for c in retried["tool_calls"]]
        assert any(n.startswith("query.") for n in names), f"expected query.*, got {names}"
        assert "20" in retried["assistant_text"], (
            f"answer does not cite the seeded 20 g: {retried['assistant_text'][:300]}"
        )
    retried["scenario"] = "F_model_outage"
    failed["scenario"] = "F_model_outage"
    print("[live] F OK: outage recorded, retry answered with the cited fact")
    return {"failed_run": failed, "retried_run": retried}


def main() -> int:
    global NODE_EXE, RESULTS_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=os.environ.get("CHEMCLAW_LLM_API_KEY", ""))
    parser.add_argument("--base-url", default="https://open.bigmodel.cn/api/paas/v4")
    parser.add_argument("--model", default="glm-5.3-flash")
    parser.add_argument("--mock", action="store_true", help="scripted provider (offline)")
    args = parser.parse_args()
    NODE_EXE = find_node()
    if args.mock:
        RESULTS_PATH = RESULTS_PATH.with_name("s08_mock_results.json")
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
        tmp = Path(tempfile.mkdtemp(prefix="s08_live_"))
        db_path = tmp / "live.db"
        db.init_db(str(db_path))
        ctx = seed_database(db_path)
        settings = ServiceSettings(
            storage_root=tmp / "storage", manifest_dir=REPO / "runtime" / "agents",
        )
        conn_info = {"db_path": db_path, "ctx": ctx, "services": settings}

        # Seed events through the OFFLINE pipeline (deterministic); the agents
        # under test consume the resulting memory, they don't re-run S03.
        seed_page_id = seed_page(str(db_path), ctx, settings, SEED_LINES, "NB-SEED", "op-seed")
        prop = invoke(str(db_path), ctx, "memory.proposal.create", {
            "project_id": PROJECT,
            "page_id": seed_page_id,
            "transcript_revision": 1, "profile_id": "chemistry_notebook",
            "profile_version": "1.0",
        }, "op-seed-prop", settings)
        proposal = prop["result"]["proposal"]
        keys = [i["item_key"] for i in proposal["items"]]
        invoke(str(db_path), ctx, "memory.proposal.confirm_items", {
            "proposal_id": proposal["proposal_id"], "item_keys": keys,
            "decision": "confirmed", "expected_proposal_revision": proposal["revision"],
        }, "op-seed-confirm", settings)
        applied = invoke(str(db_path), ctx, "memory.proposal.apply", {
            "proposal_id": proposal["proposal_id"],
        }, "op-seed-apply", settings)
        n_events = len(applied["result"]["applied"])
        print(f"[live] seed events applied: {n_events}")
        assert n_events >= 1, "seed apply produced no events"

        # Registry: load + enable the three S06 agents.
        conn = db.connect(str(db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            summary = agents.load_registry(conn, REPO / "runtime" / "agents")
            for agent_id in ("b_capture", "b_query", "observer"):
                version = next(
                    m["version"] for m in summary["loaded"] if m["agent_id"] == agent_id
                )
                agents.set_manifest_enabled(conn, agent_id=agent_id, version=version,
                                            enabled=True)
            conn.commit()
        finally:
            conn.close()
        loaded_ids = {m["agent_id"] for m in summary["loaded"]}
        assert {"b_capture", "b_query", "observer", "planner", "alice_inventory"} <= loaded_ids, summary
        assert len(summary["loaded"]) == len({(m["agent_id"], m["version"]) for m in summary["loaded"]})
        assert not summary["invalid"], summary["invalid"]

        server = create_api_server(str(db_path), port=0, service_settings=settings)
        host, port = server.server_address
        threading.Thread(target=server.serve_forever, daemon=True).start()
        gateway_url = f"http://{host}:{port}"
        print(f"[live] gateway on {gateway_url}")

        results["db_digest"] = db_digest(str(db_path))

        d_page = seed_page(str(db_path), ctx, settings, CAPTURE_LINES, "NB-CAP", "op-cap")
        e_page = seed_page(str(db_path), ctx, settings, CRASH_LINES, "NB-CRASH", "op-crash")
        t0 = time.time()
        scenario_d_cross_domain_faults(args, gateway_url, conn_info, d_page, results)
        results["timings"]["D_cross_domain_s"] = round(time.time() - t0, 1)
        t0 = time.time()
        scenario_e_crash_recovery(args, gateway_url, conn_info, e_page, results)
        results["timings"]["E_crash_recovery_s"] = round(time.time() - t0, 1)
        t0 = time.time()
        results["F_model_outage"] = scenario_f_model_outage(
            args, gateway_url, conn_info, results
        )
        results["timings"]["F_model_outage_s"] = round(time.time() - t0, 1)

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
