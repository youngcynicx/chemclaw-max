"""Exercise browser bridge -> real Pi -> Core delegated runs in an isolated lab.

Default uses a scripted model through the actual Pi loop. --live uses an API
key from CHEMCLAW_LLM_API_KEY, or --config reads an existing local UI config.
Neither credentials nor experimental data from that config's lab are copied.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "frontend"), str(REPO / "scripts")]

from chemclaw_lab import agents, auth, db  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402
import dev_server as bridge  # noqa: E402
from configure_agent_runtime import configure, DEFAULT_ROLES  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--jobs", action="store_true", help="Exercise durable background jobs and reload recovery")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--base-url", default="https://open.bigmodel.cn/api/paas/v4")
    parser.add_argument("--model", default="glm-5.3-flash")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config else {}
    model_key = settings.get("modelKey") or os.environ.get("CHEMCLAW_LLM_API_KEY", "")
    if args.live and not model_key:
        raise SystemExit("Live test needs CHEMCLAW_LLM_API_KEY or a local --config file.")
    model = bridge.ModelConfig(settings.get("modelBase", args.base_url),
        settings.get("model", args.model), model_key or "offline-only",
        reasoning_effort=settings.get("modelReasoningEffort"),
        max_tokens=settings.get("modelMaxTokens", 16384),
        context_window=settings.get("modelContextWindow", 128000))
    result_report = {"mode": "live" if args.live else "scripted", "model": model.model,
                     "transport": "jobs" if args.jobs else "synchronous"}
    with tempfile.TemporaryDirectory(prefix="chemclaw_agents_") as directory:
        tmp = Path(directory)
        database = tmp / "test.db"
        db.init_db(database)
        core_key = secrets.token_urlsafe(24)
        conn = db.connect(database)
        try:
            conn.execute("INSERT INTO projects (project_id,name) VALUES ('proj_test','Isolated test')")
            auth.register_principal(conn, principal_id="human_test", kind="human", display_name="Test researcher")
            auth.grant_membership(conn, principal_id="human_test", project_id="proj_test", role="member")
            auth.register_credential(conn, credential_id="test_key", principal_id="human_test", secret=core_key)
            conn.execute("INSERT INTO samples (sample_id,project_id,label) VALUES ('sample_test','proj_test','S-1')")
            conn.execute("""INSERT INTO chemicals (chemical_id,name,formula,amount,unit,threshold,hazard_level,max_auto_delta,revision)
                VALUES ('chem_test','Sodium chloride','NaCl',100.0,'g',5.0,'low',10.0,0)""")
            conn.commit()
        finally:
            conn.close()
        configure(database, list(DEFAULT_ROLES))
        core = create_api_server(str(database), port=0,
            service_settings=ServiceSettings(storage_root=tmp / "storage"))
        connection = bridge.CoreConnection(f"http://127.0.0.1:{core.server_port}", core_key, "proj_test")
        frontend = bridge.BridgeServer(("127.0.0.1", 0), REPO / "frontend" / "dist",
            default_connection=connection, default_model=model)
        for server in (core, frontend):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        actual_run = bridge.run_pi_agent

        def run_scoped(*a, **kw):
            return actual_run(*a, **kw, mock=not args.live)

        client = http.client.HTTPConnection("127.0.0.1", frontend.server_port, timeout=300)
        try:
            client.request("GET", "/api/session")
            response = client.getresponse()
            session = json.loads(response.read())
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            headers = {"Content-Type": "application/json", "Cookie": cookie,
                "Origin": f"http://127.0.0.1:{frontend.server_port}", "X-ChemClaw-Session": session["csrfToken"]}
            note = "样品 S-1 实际取用 0.5 g Sodium chloride。未记录干燥温度。"

            def submit(payload):
                request_id = secrets.token_hex(16)
                body = {**payload, **({"requestId": request_id} if args.jobs else {})}
                route = "/api/core/jobs" if args.jobs else "/api/core/session/message"
                submitted_at = time.monotonic()
                client.request("POST", route, json.dumps(body), headers)
                response = client.getresponse()
                body = json.loads(response.read())
                assert response.status == (202 if args.jobs else 200), body
                if args.jobs:
                    assert time.monotonic() - submitted_at < 15, "Submit waited for model"
                    job_id = body["job"]["job_id"]
                    client.request("POST", route, json.dumps({**payload, "requestId": request_id}), headers)
                    duplicate_response = client.getresponse()
                    duplicate = json.loads(duplicate_response.read())
                    assert duplicate_response.status == 202 and duplicate["job"]["job_id"] == job_id, duplicate
                    # Simulate a fresh browser session: recovery comes from Core,
                    # with no task IDs, attachment bytes or model keys in storage.
                    client.request("GET", "/api/session")
                    refreshed = client.getresponse()
                    session_info = json.loads(refreshed.read())
                    headers["Cookie"] = refreshed.getheader("Set-Cookie").split(";", 1)[0]
                    headers["X-ChemClaw-Session"] = session_info["csrfToken"]
                    client.request("GET", "/api/core/jobs", headers=headers)
                    listed_response = client.getresponse()
                    listed = json.loads(listed_response.read())
                    assert any(j["job_id"] == job_id for j in listed["jobs"]), listed
                    deadline, seq, progress = time.monotonic() + 360, 0, []
                    while time.monotonic() < deadline:
                        client.request("GET", f"/api/core/jobs/{job_id}?after={seq}", headers=headers)
                        polled = client.getresponse()
                        snapshot = json.loads(polled.read())
                        assert polled.status == 200, snapshot
                        job = snapshot["job"]
                        progress.extend(job.get("events", []))
                        seq = job.get("last_event_seq", seq)
                        if job["status"] not in ("queued", "running"):
                            body = job["result"]
                            break
                        time.sleep(.25)
                    else:
                        raise AssertionError("Background task did not finish")
                    assert any(e["type"] == "tool_call_requested" for e in progress), progress
                    result_report.setdefault("jobs", []).append({"job_id": job_id,
                        "events": len(progress), "duplicate": True, "reload_recovered": True})
                assert body.get("runtime") == "pi", body
                diagnostic = {"status": body.get("status"), "runtime_error": body.get("runtime_error"),
                    "text": body.get("text", "")[:900], "warnings": body.get("warnings"),
                    "delegations": [{"agent": d.get("agent_id"), "status": d.get("status"),
                        "error": d.get("error")} for d in body.get("delegations", [])]}
                if body.get("error") or body.get("runtime_error") or body.get("status") == "failed":
                    if args.report:
                        args.report.parent.mkdir(parents=True, exist_ok=True)
                        args.report.write_text(json.dumps({"ok": False, **diagnostic}, ensure_ascii=False, indent=2), encoding="utf-8")
                    raise AssertionError(diagnostic)
                encoded = json.dumps(body)
                assert core_key not in encoded and model.api_key not in encoded
                assert "run_token" not in encoded
                return body

            with patch.object(bridge, "run_pi_agent", side_effect=run_scoped):
                first = submit({"text": "请整理附件实验记录，生成待核验提案，并查询当前 Sodium chloride 库存。缺失条件保留未知，等我核对。",
                    "attachments": [{"name": "note.txt", "mime": "text/plain",
                        "data_b64": base64.b64encode(note.encode()).decode()}]})
                print(f"First turn: {first['status']}, delegated {len(first.get('delegations', []))} role(s)", flush=True)
                assert first.get("delegations"), first
                assert first["data"]["pages"], first
                role_receipts = [{"agent": d.get("agent_id"), "status": d.get("status"),
                    "calls": [{"name": call["name"], "action_status": (call.get("result") or {}).get("action_status"),
                        "error": (call.get("result") or {}).get("error") if (call.get("result") or {}).get("ok") is False else None}
                        for call in d.get("toolCalls", [])]}
                    for d in first.get("delegations", [])]
                if args.live:
                    assert first["status"] in ("needs_review", "clarification_needed"), role_receipts
                    assert not any(d.get("status") in ("failed", "partial") for d in first["delegations"]), role_receipts
                    assert any(r["agent"] == "alice_inventory" and any(c["name"] == "inventory.query.list"
                        and c["action_status"] == "applied" for c in r["calls"]) for r in role_receipts), role_receipts
                second = submit({"text": "继续上一轮：列出刚才上传页面的 ID，以及仍需核对或补充的事项。保持待核验状态。",
                    "sessionId": first["session_id"], "attachments": []})
            conn = db.connect(database)
            try:
                runs = [dict(row) for row in conn.execute("SELECT agent_id,status FROM agent_runs")]
                active = conn.execute("SELECT COUNT(*) FROM agent_run_tokens WHERE status='active'").fetchone()[0]
                facts = conn.execute("SELECT COUNT(*) FROM experiment_events").fetchone()[0]
                proposals = conn.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0]
                stock = conn.execute("SELECT amount FROM chemicals WHERE chemical_id='chem_test'").fetchone()[0]
                messages = conn.execute("SELECT COUNT(*) FROM typed_messages WHERE correlation_id=?", (first["session_id"],)).fetchone()[0]
                assert active == 0 and facts == 0 and stock == 100.0
                assert messages == 4
                if args.live:
                    assert proposals >= 1, first
                    items = [item for row in conn.execute("SELECT items_json FROM memory_proposals") for item in json.loads(row[0])]
                    assert any(item.get("unknown") is True for item in items), "Missing explicit unknown item"
                    assert any(item.get("event_kind") == "operation" and not item.get("unknown")
                        and (item.get("fields") or {}).get("amount", {}).get("normalized") == 0.5
                        for item in items), "Known usage must remain independently reviewable"
                    assert first["data"]["pages"][0]["page_id"] in second["text"], second
                result_report.update({"ok": True, "first_status": first["status"], "second_status": second["status"],
                    "runs": runs, "active_tokens": active, "experiment_facts": facts, "proposals": proposals,
                    "stock": stock, "session_messages": messages,
                    "delegated_roles": [d["agent_id"] for d in first["delegations"]],
                    "role_receipts": role_receipts,
                    "first_text": first["text"], "second_text": second["text"]})
            finally:
                conn.close()
        finally:
            client.close()
            for server in (frontend, core):
                server.shutdown()
                server.server_close()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result_report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Summary intentionally excludes keys, full model prompts and raw tool receipts.
    print(json.dumps({k: v for k, v in result_report.items() if not k.endswith("_text")}, ensure_ascii=False))
    print("AGENT ORCHESTRATION SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
