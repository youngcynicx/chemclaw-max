"""Blind images -> durable agents -> questions evaluation in an isolated lab.

Questions JSON contains only {ingest_prompt,questions:[{id,prompt}]}; reference
answers are deliberately not an input. No formal facts are auto-confirmed.
"""
from pathlib import Path
import argparse
import base64
import hashlib
import http.client
import json
import secrets
import sys
import threading
import time
import re
import os
import tempfile

_PRIVATE_KEYS = {"run_token", "agent_token", "api_key", "apikey", "authorization", "token", "token_hash",
                 "access_token", "refresh_token", "password", "secret", "data_b64", "thinking", "reasoning_content", "thinkingsignature"}
_OMIT = object()


def public_export(value, secrets=()):
    """Export public receipts, never provider thinking or credential fields."""
    if isinstance(value, dict):
        if value.get("type") == "thinking":
            return _OMIT
        result = {}
        for key, child in value.items():
            if key.lower() in _PRIVATE_KEYS:
                continue
            output_key = key
            if key in {"result_json", "items_json"}:
                output_key = key[:-5]
                try:
                    child = json.loads(child) if isinstance(child, str) else child
                except (ValueError, TypeError):
                    child = {"export_error": "invalid_json_receipt"}
            cleaned = public_export(child, secrets)
            if cleaned is not _OMIT:
                result[output_key] = cleaned
        return result
    if isinstance(value, list):
        return [cleaned for child in value if (cleaned := public_export(child, secrets)) is not _OMIT]
    if isinstance(value, str):
        for secret in filter(None, secrets):
            value = value.replace(secret, "[redacted]")
    return value


def atomic_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
        delays = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)
        for attempt in range(len(delays) + 1):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def main():
    REPO = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    ROOT = args.run_dir.resolve()
    ROOT.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(REPO / "src"), str(REPO / "frontend"), str(REPO / "scripts")]
    from chemclaw_lab import auth, db
    from chemclaw_lab.api import create_api_server
    from chemclaw_lab.service_config import ServiceSettings
    from configure_agent_runtime import configure, DEFAULT_ROLES
    import dev_server as bridge

    IMAGE_DIR = args.images.resolve()
    DATABASE = ROOT / "evaluation.db"
    if DATABASE.exists():
        raise SystemExit("Evaluation database already exists; use a new run directory.")
    spec = json.loads(args.questions.read_text(encoding="utf-8"))
    questions = spec.get("questions")
    if (set(spec) - {"ingest_prompt", "questions"} or not isinstance(spec.get("ingest_prompt"), str)
            or not isinstance(questions, list) or not questions
            or any(not isinstance(q, dict) or set(q) != {"id", "prompt"}
                   or not isinstance(q["id"], str) or not isinstance(q["prompt"], str) for q in questions)):
        raise SystemExit("Questions input must contain prompts only; no answer/reference fields.")
    identifiers = [q["id"] for q in questions]
    if (len(set(identifiers)) != len(identifiers) or any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", i)
            or i.startswith("ingest-") for i in identifiers)):
        raise SystemExit("Question IDs must be unique valid identifiers and cannot start with ingest-.")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    model = bridge.ModelConfig(config["modelBase"], config["model"], config["modelKey"],
        vision_model=config.get("visionModel") or config["model"],
        reasoning_effort=config.get("modelReasoningEffort"),
        max_tokens=config.get("modelMaxTokens", 16384),
        context_window=config.get("modelContextWindow", 128000))
    db.init_db(DATABASE)
    key = secrets.token_urlsafe(32)
    c = db.connect(DATABASE)
    c.execute("INSERT INTO projects(project_id,name) VALUES('notebook_benchmark','Isolated image evaluation')")
    auth.register_principal(c, principal_id="notebook_evaluator", kind="human", display_name="Benchmark tester")
    auth.grant_membership(c, principal_id="notebook_evaluator", project_id="notebook_benchmark", role="member")
    auth.register_credential(c, credential_id="benchmark_key", principal_id="notebook_evaluator", secret=key)
    c.commit(); c.close()
    configure(DATABASE, list(DEFAULT_ROLES))
    core = create_api_server(str(DATABASE), port=0, service_settings=ServiceSettings(storage_root=ROOT / "storage"))
    connection = bridge.CoreConnection(f"http://127.0.0.1:{core.server_port}", key, "notebook_benchmark")
    front = bridge.BridgeServer(("127.0.0.1", 0), REPO / "frontend/dist", default_connection=connection, default_model=model)
    for server in (core, front):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("127.0.0.1", front.server_port, timeout=120)
    headers = {}
    results = {"mode": "blind-durable-product-evaluation", "model": model.model,
        "vision_model": model.vision_model, "ocr_max_tokens": 1024,
            "max_tokens": model.max_tokens, "context_window": model.context_window, "protocol": "openai-completions",
            "requested_reasoning_effort": model.reasoning_effort,
            "effective_reasoning_effort": model.reasoning_effort or ("low" if bridge._native_glm53(model.base_url, model.model) else None),
            "model_profile": "glm-5.3-native" if bridge._native_glm53(model.base_url, model.model) else "compatible",
        "input": "image files and questions only; no reference answers",
        "fixture": "empty project; no seeded samples, facts, inventory or reference answers",
        "images": [], "turns": []}

    def safe(value):
        return public_export(value, (key, model.api_key))

    def save():
        atomic_save(ROOT / "results.json", safe(results))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            atomic_save(args.report, safe(results))

    def request(method, path, payload=None):
        client.request(method, path, None if payload is None else json.dumps(payload), headers)
        response = client.getresponse()
        body = json.loads(response.read())
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}: {safe(body)}")
        return body, response

    session_id = None
    def run_turn(identifier, text, images=()):
        nonlocal session_id
        payload = {"text": text, "requestId": "notebook-evaluation-" + identifier, "attachments": []}
        if session_id:
            payload["sessionId"] = session_id
        for image in images:
            raw = image.read_bytes()
            mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
            payload["attachments"].append({"name": image.name, "mime": mime, "data_b64": base64.b64encode(raw).decode()})
            results["images"].append({"name": image.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        start = time.monotonic()
        submitted, _ = request("POST", "/api/core/jobs", payload)
        job = submitted["job"]
        session_id = job["session_id"]
        turn = {"id": identifier, "prompt": text, "job_id": job["job_id"],
                "session_id": session_id, "submit_seconds": round(time.monotonic() - start, 3), "events": [],
                "context": job.get("context")}
        results["turns"].append(turn)
        save()
        print(f"SUBMITTED {identifier} {turn['submit_seconds']}s", flush=True)
        seq, last_notice = 0, 0
        while time.monotonic() - start < 1200:
            snapshot, _ = request("GET", f"/api/core/jobs/{job['job_id']}?after={seq}")
            job = snapshot["job"]
            turn["events"].extend(job.get("events", []))
            seq = job.get("last_event_seq", seq)
            if time.monotonic() - last_notice > 45:
                print(f"PROGRESS {identifier} {job['status']} {round(time.monotonic()-start)}s events={len(turn['events'])}", flush=True)
                last_notice = time.monotonic()
                save()
            if job["status"] not in ("queued", "running"):
                turn.update({"status": job["status"], "seconds": round(time.monotonic()-start, 2), "result": job.get("result"), "context": job.get("context")})
                save()
                print(f"FINISHED {identifier} {job['status']} {turn['seconds']}s", flush=True)
                return
            time.sleep(2)
        turn["status"] = "timeout"
        save()
        request("POST", f"/api/core/jobs/{job['job_id']}/cancel", {})
        raise RuntimeError(f"Evaluation turn {identifier} timed out")

    try:
        client.request("GET", "/api/session")
        response = client.getresponse()
        browser = json.loads(response.read())
        headers.update({"Cookie": response.getheader("Set-Cookie").split(";", 1)[0],
            "X-ChemClaw-Session": browser["csrfToken"], "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{front.server_port}"})
        images = sorted(p for p in IMAGE_DIR.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
        if not images:
            raise RuntimeError("No images found")
        for start in range(0, len(images), args.batch_size):
            run_turn(f"ingest-{start // args.batch_size + 1}", spec["ingest_prompt"], images[start:start+args.batch_size])
        for question in questions:
            run_turn(question["id"], question["prompt"])
    finally:
        client.close()
        front.shutdown(); front.server_close()
        core.shutdown(); core.server_close()
        c = db.connect(DATABASE)
        pages = []
        filenames = {page["page_id"]: page["filename"] for turn in results["turns"]
                     for page in (turn.get("context") or {}).get("pages", [])}
        for (context_json,) in c.execute("SELECT context_json FROM assistant_jobs"):
            for page in json.loads(context_json or "{}").get("pages", []):
                filenames.setdefault(page["page_id"], page.get("filename"))
        original_names = {image["sha256"]: image["name"] for image in results["images"]}
        for row in c.execute("SELECT p.page_id,p.page_order,p.status,e.sha256 FROM evidence_pages p "
                             "JOIN evidence_objects e ON e.evidence_id=p.evidence_id ORDER BY p.rowid"):
            page = bridge._agent_public(dict(row), ())
            page["filename"] = filenames.get(page["page_id"]) or original_names.get(page["sha256"])
            from chemclaw_lab import transcribe
            page["transcript"] = transcribe.get_page_transcript(c, page_id=row["page_id"])
            pages.append(page)
        results["pages"] = pages
        results["proposals"] = [dict(row) for row in c.execute("SELECT proposal_id,status,items_json FROM memory_proposals ORDER BY rowid")]
        results["runs"] = [dict(row) for row in c.execute("SELECT agent_id,status,result_json FROM agent_runs ORDER BY rowid")]
        results["counts"] = {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("evidence_pages", "page_transcripts", "memory_proposals", "experiment_events", "samples")}
        results["active_tokens"] = c.execute("SELECT COUNT(*) FROM agent_run_tokens WHERE status='active'").fetchone()[0]
        c.close()
        save()
        print("EVALUATION SAVED", flush=True)


if __name__ == "__main__":
    main()
