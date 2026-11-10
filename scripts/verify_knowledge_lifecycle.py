"""Opt-in live acceptance over two synthetic pages in a new, retained lab.

Uses only credentials/model settings from --config, never the configured lab.
No OCR or benchmark answers are involved. Each turn runs once; reports retain
actual public receipts for independent semantic inspection, without retrying
until an answer passes. Run with the repository virtualenv and --help.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import hashlib
import json
from pathlib import Path
import secrets
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "frontend"), str(REPO / "scripts")]

from chemclaw_lab import auth, db
from chemclaw_lab.api import create_api_server
from chemclaw_lab.service_config import ServiceSettings
from configure_agent_runtime import configure, DEFAULT_ROLES
from benchmark_notebook_images import atomic_save, public_export
import dev_server as bridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Recover this script's inactive retained lab; never repeat completed model turns")
    parser.add_argument("--verify-timelines", action="store_true", help="One additional read-only turn plus card/timeline consistency check")
    args = parser.parse_args()
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    database = root / "lifecycle.db"
    if database.exists() and not args.resume:
        raise SystemExit("Use a fresh run directory; existing lab will not be modified.")
    prior = None
    if args.resume:
        prior = json.loads((root / "results.json").read_text(encoding="utf-8"))
        if not database.exists() or prior.get("mode") != "live-synthetic-lifecycle":
            raise SystemExit("Resume requires this script's existing isolated lab and report.")
    settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
    model = bridge.validate_model_config({
        "baseUrl": settings["modelBase"], "model": settings["model"], "apiKey": settings.get("modelKey", ""),
        "protocol": settings.get("modelProtocol", "openai-chat"),
        "reasoningFormat": settings.get("modelReasoningFormat", "auto"),
        "reasoningEffort": settings.get("modelReasoningEffort") or "auto",
        "maxTokens": settings.get("modelMaxTokens", 16384),
        "contextWindow": settings.get("modelContextWindow", 128000),
    }, 0)
    hidden = tuple(filter(None, [settings.get("modelKey"), settings.get("visionKey")]))
    del settings
    db.init_db(database)
    key = secrets.token_urlsafe(32)
    hidden += (key,)
    conn = db.connect(database)
    try:
        if prior:
            if conn.execute("SELECT count(*) FROM assistant_jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]:
                raise SystemExit("Resume only after the previous harness has stopped and jobs are terminal.")
        else:
            conn.execute("INSERT INTO projects(project_id,name) VALUES('lifecycle','Synthetic lifecycle acceptance')")
            auth.register_principal(conn, principal_id="tester", kind="human", display_name="Acceptance researcher")
            auth.grant_membership(conn, principal_id="tester", project_id="lifecycle", role="member")
        auth.register_credential(conn, credential_id="acceptance-" + secrets.token_hex(8), principal_id="tester", secret=key)
        conn.commit()
    finally:
        conn.close()
    configure(database, list(DEFAULT_ROLES))
    core = create_api_server(str(database), port=0, service_settings=ServiceSettings(storage_root=root / "storage"))
    connection = bridge.CoreConnection(f"http://127.0.0.1:{core.server_port}", key, "lifecycle")
    front = bridge.BridgeServer(("127.0.0.1", 0), REPO / "frontend/dist",
        default_connection=connection, default_model=model)
    for server in (core, front):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    report = prior or {"mode": "live-synthetic-lifecycle", "model": model.model,
        "protocol": model.protocol, "status": "running", "turns": [],
        "scope": "Two synthetic text pages; no main database, OCR, handoff or benchmark reference input."}
    if prior:
        report.setdefault("harness_recoveries", []).append({"error": report.pop("error", None),
            "note": "Read completed jobs from Core; no model turn resubmitted."})
        report["status"] = "running"
    client = http.client.HTTPConnection("127.0.0.1", front.server_port, timeout=60)
    headers = {}

    def save():
        atomic_save(root / "results.json", public_export(report, hidden))

    def request(method, route, payload=None):
        client.request(method, route, None if payload is None else json.dumps(payload, ensure_ascii=False).encode(), headers)
        response = client.getresponse()
        body = json.loads(response.read())
        if response.status not in (200, 202):
            raise RuntimeError(f"HTTP {response.status}: {public_export(body, hidden)}")
        return body, response

    def snapshot():
        conn = db.connect(database)
        try:
            counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("samples", "event_revisions", "memory_proposals", "event_confirmations", "inventory_transactions")}
            counts["active_tokens"] = conn.execute("SELECT count(*) FROM agent_run_tokens WHERE status='active'").fetchone()[0]
        finally:
            conn.close()
        cards = bridge.invoke_read(connection, "query.samples.list", {"project_ids": ["lifecycle"]})
        return {"counts": counts, "cards": cards}

    session_id = None

    def turn(identifier, text, note=None):
        nonlocal session_id
        start = time.monotonic()
        payload = {"text": text, "requestId": "lifecycle-" + identifier, "attachments": []}
        if session_id:
            payload["sessionId"] = session_id
        if note:
            payload["attachments"] = [{"name": identifier + ".txt", "mime": "text/plain",
                "data_b64": base64.b64encode(note.encode()).decode()}]
        existing = next((entry for entry in report["turns"] if entry["id"] == identifier), None)
        if existing and (existing["prompt"] != text or existing["source_text"] != note):
            raise RuntimeError("Resume input differs from retained turn; refusing to rerun.")
        if existing and existing.get("snapshot"):
            session_id = existing["session_id"]
            return existing
        job = (request("GET", "/api/core/jobs/" + existing["job_id"])[0]["job"] if existing
            else request("POST", "/api/core/jobs", payload)[0]["job"])
        session_id = job["session_id"]
        entry = existing or {"id": identifier, "prompt": text, "source_text": note, "job_id": job["job_id"],
            "session_id": session_id, "status": job["status"]}
        if not existing:
            tracked = [*sorted((REPO / "runtime/pi/prompts").glob("*.md")), REPO / "runtime/pi/knowledge-delivery.mjs",
                REPO / "src/chemclaw_lab/sample_cards.py", REPO / "src/chemclaw_lab/query.py"]
            entry["source_hashes_at_start"] = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
            report["turns"].append(entry)
        save()
        print(f"START {identifier}", flush=True)
        while time.monotonic() - start < 600:
            job = request("GET", "/api/core/jobs/" + job["job_id"])[0]["job"]
            if job["status"] not in ("queued", "running", "cancelling"):
                entry.update(status=job["status"], seconds=None if existing else round(time.monotonic() - start, 2),
                    result=job.get("result"), snapshot=snapshot())
                save()
                state = (job.get("result") or {}).get("knowledge_delivery", {}).get("state")
                print(f"FINISH {identifier}: {job['status']}, {state}", flush=True)
                if job["status"] != "completed":
                    raise RuntimeError(f"{identifier} did not complete; inspect retained receipts.")
                return entry
            time.sleep(2)
        request("POST", "/api/core/jobs/" + job["job_id"] + "/cancel", {})
        raise RuntimeError(f"{identifier} exceeded watchdog; cancellation requested.")

    try:
        session, response = request("GET", "/api/session")
        headers.update({"Content-Type": "application/json", "Cookie": response.getheader("Set-Cookie").split(";", 1)[0],
            "Origin": f"http://127.0.0.1:{front.server_port}", "X-ChemClaw-Session": session["csrfToken"]})
        first = turn("initial", "请将附件模拟资料整理到知识与样品。分别归卡，保留共同操作的关联、阶段、原始来源和未知项；完成后说明真实保存结果。",
            "模拟实验资料，不是真实实验。\n2026-10-21，样品LC-A和LC-B共同进行干燥：先80℃2小时，再100℃3小时。\nLC-A得到白色粉末20 mg；LC-B得到淡黄色粉末18 mg。两者产率均未记录。")
        appended = turn("append", "请将第二页新增资料补入已有样品卡，并结合上一页给出当前时间线。不要重复保存已有事实，区分初次处理、返工和计划。",
            "模拟实验资料续页，不是真实实验。\n2026-10-22，仅LC-A返工：110℃1小时。返工后颜色仍白色，未重新称重。\n计划2026-10-24对LC-A和LC-B做对比测量，尚未执行；测量结果未知。")
        queried = turn("query", "只读查询LC-A与LC-B现有样品卡，用表格列出各自阶段条件、产物质量、观察、计划和未知项；标明来源。不要新增或修改知识。")
        cards = queried["snapshot"]["cards"]["samples"]
        sample = next(card for card in cards if card["label"] == "LC-A")
        candidates = [record for record in sample["records"] if "20" in record["raw_text"]
            and isinstance(record["fields"].get("product_mass"), dict)]
        if len(candidates) != 1:
            raise RuntimeError("Cannot uniquely locate synthetic mass event; inspect report without guessing.")
        record = candidates[0]
        mass = {**record["fields"]["product_mass"], "raw": "22 mg", "normalized": 22, "unit": "mg"}
        correction = {"eventId": record["event_id"], "expectedRevision": record["revision"],
            "reason": "模拟人工纠错：原页质量20 mg有误，正确为22 mg。",
            "changes": {"raw_text": record["raw_text"].replace("20", "22"), "fields": {"product_mass": mass}}}
        if "correction" not in report:
            report["correction"] = {"request": correction, "receipt": request("POST", "/api/core/event/correct", correction)[0],
                "snapshot": snapshot()}
        save()
        corrected = turn("after-correction", "我已通过纠错接口更正了一条质量记录。请重新核对这两页是否已完整归入样品；已有事实复用，不要重新导入。给出LC-A和LC-B的当前质量、阶段和后续研究交接，保留计划/实做与未知；人工修订和原页不一致时明确说明依据。")
        counts = lambda entry: entry["snapshot"]["counts"]
        c1, c2, c3, c4 = map(counts, [first, appended, queried, corrected])
        human = report["correction"]["snapshot"]["counts"]
        current = next(r for s in report["correction"]["snapshot"]["cards"]["samples"]
            for r in s["records"] if r["event_id"] == record["event_id"])
        report["structural_checks"] = {
            "two_samples_reused": all(c["samples"] == 2 for c in (c1, c2, c3, c4)),
            "append_added_knowledge": c2["event_revisions"] > c1["event_revisions"],
            "query_did_not_write": all(c3[t] == c2[t] for t in ("samples", "event_revisions", "memory_proposals")),
            "human_revision_preserved_without_reimport": all(c4[t] == human[t] for t in ("samples", "event_revisions", "memory_proposals")),
            "human_correction_applied": current["revision"] == record["revision"] + 1
                and current["fields"]["product_mass"]["normalized"] == 22,
            "no_approval_or_inventory_effect": all(c4[t] == 0 for t in ("event_confirmations", "inventory_transactions", "active_tokens")),
            "delivery_states": [t["result"].get("knowledge_delivery", {}).get("state") for t in report["turns"][:4]]
                == ["verified", "verified", "read_only", "read_only"],
        }
        report["structural_passed"] = all(report["structural_checks"].values())
        if args.verify_timelines:
            checked = turn("timeline-check", "只读查询LC-A和LC-B的当前样品时间线。列出两者的实际操作、质量及共同计划，逐项保留来源和当前版本；未知项保留未知。不要创建、修改或补录记录。")
            cards = checked["snapshot"]["cards"]["samples"]
            timelines = {card["sample_id"]: bridge.invoke_read(connection, "query.sample.timeline",
                {"sample_id": card["sample_id"]}) for card in cards}
            report["timeline_check"] = {"timelines": timelines,
                "matches_card_event_ids": all({r["event_id"] for r in card["records"]}
                    == {r["event_id"] for r in timelines[card["sample_id"]]["timeline"]} for card in cards),
                "no_writes": all(counts(checked)[t] == c4[t] for t in ("samples", "event_revisions", "memory_proposals")),
                "read_only_receipt": checked["result"].get("knowledge_delivery", {}).get("state") == "read_only"}
            report["structural_passed"] &= all(report["timeline_check"][k]
                for k in ("matches_card_event_ids", "no_writes", "read_only_receipt"))
        report["semantic_review"] = "Required: inspect final text, fields, sample assignments, revisions and sources; structural checks do not prove completeness or chemistry correctness."
        report["status"] = "finished"
        save()
        print("STRUCTURAL " + str(report["structural_passed"]), flush=True)
        return 0 if report["structural_passed"] else 1
    except Exception as error:
        report.update(status="failed", error=str(error))
        save()
        print(str(public_export({"error": str(error)}, hidden)), flush=True)
        return 1
    finally:
        client.close()
        for server in (front, core):
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
