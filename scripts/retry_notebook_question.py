"""Retry one notebook question using a copy of its frozen OCR/session fixture."""
from pathlib import Path
import argparse
import http.client
import json
import secrets
import sqlite3
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
    parser.add_argument("--from-run", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    previous = json.loads((args.from_run / "results.json").read_text(encoding="utf-8"))
    if previous.get("mode") != "blind-durable-product-evaluation" or previous.get("active_tokens") != 0:
        raise SystemExit("Source must be a completed isolated notebook evaluation with no active tokens.")
    turn = next((t for t in previous["turns"] if t["id"] == args.id), None)
    if not turn or args.id.startswith("ingest-"):
        raise SystemExit("Select an existing question turn.")
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    database = root / "evaluation.db"
    if database.exists():
        raise SystemExit("Retry database already exists; choose a new directory.")
    source = sqlite3.connect(f"file:{(args.from_run / 'evaluation.db').resolve().as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(database)
    source.backup(target)
    target.close(); source.close()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    model = bridge.validate_model_config({"baseUrl": config["modelBase"], "model": config["model"],
        "apiKey": config["modelKey"], "visionModel": config.get("visionModel"),
        "maxTokens": config.get("modelMaxTokens", 16384), "contextWindow": config.get("modelContextWindow", 128000),
        **({"reasoningEffort": config["modelReasoningEffort"]} if config.get("modelReasoningEffort") else {})}, 4174)
    key = secrets.token_urlsafe(32)
    conn = db.connect(database)
    owner = conn.execute("SELECT principal_id,project_id FROM assistant_jobs WHERE job_id=?", (turn["job_id"],)).fetchone()
    auth.register_credential(conn, credential_id="retry_" + secrets.token_hex(8), principal_id=owner["principal_id"], secret=key)
    conn.commit(); conn.close()
    configure(database, list(DEFAULT_ROLES))
    core = create_api_server(str(database), port=0, service_settings=ServiceSettings(storage_root=(args.from_run / "storage").resolve()))
    connection = bridge.CoreConnection(f"http://127.0.0.1:{core.server_port}", key, owner["project_id"])
    front = bridge.BridgeServer(("127.0.0.1", 0), REPO / "frontend/dist", default_connection=connection, default_model=model)
    for server in (core, front):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("127.0.0.1", front.server_port, timeout=60)
    output = {"mode": "fixed-ocr-durable-retry", "question_id": args.id, "prompt": turn["prompt"],
        "original_status": turn["status"], "fixture": "copy of original isolated OCR and session; no reference answers",
        "pages": previous["pages"], "events": []}
    try:
        client.request("GET", "/api/session")
        response = client.getresponse(); browser = json.loads(response.read())
        headers = {"Cookie": response.getheader("Set-Cookie").split(";", 1)[0], "X-ChemClaw-Session": browser["csrfToken"],
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{front.server_port}"}
        payload = {"text": turn["prompt"], "sessionId": turn["session_id"],
            "requestId": "retry-" + secrets.token_hex(16), "attachments": []}
        client.request("POST", "/api/core/jobs", json.dumps(payload), headers)
        response = client.getresponse(); submitted = json.loads(response.read())
        if response.status != 202:
            raise RuntimeError(f"Retry submission HTTP {response.status}")
        job_id = submitted["job"]["job_id"]
        output["job_id"] = job_id
        start, seq, notice = time.monotonic(), 0, 0
        while time.monotonic() - start < 600:
            client.request("GET", f"/api/core/jobs/{job_id}?after={seq}", headers=headers)
            response = client.getresponse(); body = json.loads(response.read())
            if response.status != 200:
                raise RuntimeError(f"Retry polling HTTP {response.status}")
            job = body["job"]
            output["events"].extend(job.get("events", [])); seq = job.get("last_event_seq", seq)
            output.update(status=job["status"], seconds=round(time.monotonic() - start, 2), result=job.get("result"))
            atomic_save(root / "results.json", public_export(output, (key, model.api_key)))
            if time.monotonic() - notice > 45:
                print(f"RETRY {args.id} {job['status']} {round(time.monotonic()-start)}s", flush=True); notice = time.monotonic()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(2)
        else:
            client.request("POST", f"/api/core/jobs/{job_id}/cancel", "{}", headers)
            client.getresponse().read()
            raise RuntimeError("Retry timed out")
    finally:
        client.close(); front.shutdown(); front.server_close(); core.shutdown(); core.server_close()
        conn = db.connect(database)
        output["counts"] = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("evidence_pages", "page_transcripts", "memory_proposals", "experiment_events", "samples")}
        output["active_tokens"] = conn.execute("SELECT COUNT(*) FROM agent_run_tokens WHERE status='active'").fetchone()[0]
        conn.close()
        atomic_save(root / "results.json", public_export(output, (key, model.api_key)))
    print(f"RETRY FINISHED {output['status']} {output['seconds']}s", flush=True)


if __name__ == "__main__":
    main()
