"""One retained six-image development regression through production jobs.

No evaluator/gold inputs, paid preflight, model retry or terminal-job replay.
Run directory creation is exclusive. An interrupted harness needs explicit
read-only reconciliation; this script deliberately refuses a second launch.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[3]
RUN = ROOT / "run"
PROJECT = "simulated_mof_english_fast_20261029"
sys.path[:0] = [str(REPO / folder) for folder in ("src", "frontend", "scripts")]
from benchmark_phase_guard import (WAITING, advance_phase_cursor, phase_gate,
    pause_for_provider_resource, confirm_provider_resource_recovery)
from benchmark_notebook_images import atomic_save, public_export

TERMINAL = {"completed", "partial", "failed", "cancelled", "clarification_needed", "needs_review"}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes():
    """Same product scope as longitudinal code_hashes, including new modules."""
    paths = set()
    for folder, pattern in (("src", "*.py"), ("src", "*.json"), ("scripts", "*.py")):
        paths.update((REPO / folder).rglob(pattern))
    paths.update(path for path in (REPO / "frontend/dist").rglob("*") if path.is_file())
    paths.update((REPO / "frontend").glob("*.py"))
    paths.update((REPO / "runtime/pi").glob("*.mjs"))
    paths.update((REPO / "runtime/pi/prompts").rglob("*.md"))
    paths.update((REPO / "runtime/pi").glob("*.json"))
    paths.update(path for path in (REPO / "runtime/agents").rglob("*") if path.is_file())
    paths.update((REPO / "agents").rglob("*.md"))
    for name in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "uv.lock"):
        if (REPO / name).is_file():
            paths.add(REPO / name)
    return {path.relative_to(REPO).as_posix(): digest(path) for path in sorted(paths)
            if "__pycache__" not in path.parts}


class PipelineStopped(Exception):
    pass


class SubmissionUncertain(PipelineStopped):
    pass


class OnePassPipeline:
    """Durable phase control, independently testable without a model/server."""
    def __init__(self, manifest, report, save, request, *, project_id=PROJECT,
                 root=ROOT, sleep=time.sleep, monotonic=time.monotonic):
        self.manifest, self.report = manifest, report
        self._save, self.request = save, request
        self.project_id, self.root = project_id, Path(root)
        self.sleep, self.monotonic = sleep, monotonic
        self.lock = threading.RLock()
        self.session_id = manifest.get("session_id")

    def replace_manifest(self, updated):
        self.manifest.clear()
        self.manifest.update(updated)

    def save(self):
        with self.lock:
            self._save()

    def guard(self, index, operation="submit"):
        if self.manifest.get("status") not in {"running", WAITING}:
            raise PipelineStopped("reconcile_harness_state_before_submission")
        decision = phase_gate(self.manifest, index, operation=operation)
        if not decision["allowed"]:
            raise PipelineStopped(decision["action"])
        return decision

    def submitted(self):
        return [{"step_index": turn["step_index"], "step_id": turn["id"],
                 "request_id": turn["request_id"], "job_id": turn.get("job_id"),
                 "page_ids": [p["page_id"] for p in (turn.get("context") or {}).get("pages", [])],
                 "status": turn["status"]} for turn in self.report["turns"]]

    def cursor(self):
        self.replace_manifest(advance_phase_cursor(self.manifest,
            submitted_steps=self.submitted(), next_unsubmitted_step=len(self.report["turns"])))

    def pause_if_quota(self, job):
        updated = pause_for_provider_resource(self.manifest, job, project_id=self.project_id,
            session_id=self.session_id, submitted_steps=self.submitted(),
            next_unsubmitted_step=len(self.report["turns"]))
        if updated is not None:
            self.replace_manifest(updated)
            self.save()
            return True
        return False

    def wait_for_quota_confirmation(self):
        """No provider probe. Only a human-authorized release can advance."""
        if self.manifest.get("status") != WAITING:
            return
        print("WAITING provider_resource; no next POST/OCR; inspect durable manifest", flush=True)
        release = self.root / "confirm-provider-recovery.json"
        while self.manifest.get("status") == WAITING:
            self.check_stop()
            if release.is_file():
                receipt = json.loads(release.read_text(encoding="utf-8-sig"))
                if receipt.get("confirmed") is not True or receipt.get("project_id") != self.project_id or receipt.get("session_id") != self.session_id:
                    raise PipelineStopped("Invalid provider recovery acknowledgement")
                self.replace_manifest(confirm_provider_resource_recovery(self.manifest,
                    project_id=self.project_id, session_id=self.session_id,
                    confirmed_by=receipt.get("confirmed_by", "operator")))
                self.manifest["provider_resource_guard"]["confirmation_file_sha256"] = digest(release)
                self.save()
                destination = self.root / ("used-provider-recovery-" + secrets.token_hex(5) + ".json")
                release.rename(destination)
                return
            self.sleep(1)

    def check_stop(self):
        if (self.root / "stop-run").is_file():
            raise PipelineStopped("operator_stop_requested")

    def before_ocr(self, page_id):
        """Fence real image preprocessing without making an old job repeatable.

        The phase gates for submit AND OCR were checked before its sole POST.
        This callback additionally refuses quota waits and a second actual OCR
        attempt for the same page. It is not a new submission/continuation.
        """
        with self.lock:
            if self.manifest.get("status") == WAITING:
                raise PipelineStopped("provider_quota_exhausted")
            attempts = self.manifest.setdefault("ocr_attempts", [])
            if any(item["page_id"] == page_id for item in attempts):
                raise PipelineStopped("repeated_ocr_not_permitted")
            attempts.append({"page_id": page_id, "state": "intent_saved", "at_utc": now()})
            self.save()

    def _find_accepted_job(self, request_id):
        listing, _ = self.request("GET", "/api/core/jobs")
        matches = [job for job in listing.get("jobs", []) if job.get("request_id") == request_id
                   and job.get("project_id") == self.project_id]
        if len(matches) > 1:
            raise SubmissionUncertain("Multiple jobs for one submission; inspect state")
        return matches[0] if matches else None

    def run_turn(self, identifier, text, *, image=None, image_hash=None, observer_seconds=1500):
        with self.lock:
            index = len(self.report["turns"])
            if any(turn["id"] == identifier for turn in self.report["turns"]):
                raise SubmissionUncertain("Existing turn is read-only; never submit it again")
            self.guard(index)
            if image:
                self.guard(index, "ocr")
            entry = {"step_index": index, "id": identifier, "prompt": text,
                "request_id": "mof-fast-20261029-" + identifier,
                "status": "submitting", "started_at_utc": now(), "events": [], "poll_errors": []}
            payload = {"text": text, "requestId": entry["request_id"], "attachments": []}
            if self.session_id:
                payload["sessionId"] = self.session_id
            if image:
                path = self.root / "input" / image
                if digest(path) != image_hash:
                    raise PipelineStopped("Input image changed")
                payload["attachments"] = [{"name": image, "mime": "image/jpeg",
                    "data_b64": base64.b64encode(path.read_bytes()).decode("ascii")}]
                entry.update(image=image, image_sha256=image_hash)
            self.report["turns"].append(entry)
            self.cursor()
            self.save()  # Intent/cursor MUST survive before possibly accepted POST.
        start = self.monotonic()
        print("SUBMIT " + identifier, flush=True)
        try:
            submitted, _ = self.request("POST", "/api/core/jobs", payload)
            job = submitted["job"]
            with self.lock:
                entry["submit_receipt"] = submitted
        except Exception as error:
            with self.lock:
                entry.update(status="submission_unknown", submission_error_type=type(error).__name__)
                self.manifest["status"] = "waiting_for_submission_reconciliation"
                self.cursor()
                self.save()
            # GET-only reconciliation. A lost acknowledgement never causes a
            # second POST, even if the list is temporarily empty.
            try:
                job = self._find_accepted_job(entry["request_id"])
            except Exception:
                job = None
            if job is None:
                raise SubmissionUncertain("Unknown POST outcome; inspect request_id and saved originals; do not rerun") from None
            with self.lock:
                entry["reconciled_existing_job"] = True
                self.manifest["status"] = "running"
        with self.lock:
            self.session_id = job["session_id"]
            self.manifest["session_id"] = self.session_id
            entry.update(job_id=job["job_id"], session_id=self.session_id,
                status=job["status"], context=job.get("context"), submit_seconds=round(self.monotonic() - start, 3))
            self.cursor()
            self.save()
        seq = 0
        while self.monotonic() - start < observer_seconds:
            self.check_stop()
            try:
                current, _ = self.request("GET", f"/api/core/jobs/{job['job_id']}?after={seq}")
            except (OSError, http.client.HTTPException) as error:
                with self.lock:
                    entry["poll_errors"].append({"at_utc": now(), "error_type": type(error).__name__})
                    self.save()
                if len(entry["poll_errors"]) > 3:
                    raise PipelineStopped("Repeated read-only poll failure") from None
                self.sleep(2)
                continue
            job = current["job"]
            if job["job_id"] != entry["job_id"] or job["session_id"] != self.session_id:
                raise PipelineStopped("Job ownership changed unexpectedly")
            with self.lock:
                entry["events"].extend(job.get("events", []))
                seq = job.get("last_event_seq", seq)
                entry.update(status=job["status"], last_event_seq=seq, context=job.get("context"))
                self.cursor()
                if job["status"] in TERMINAL:
                    entry.update(seconds=round(self.monotonic() - start, 2), finished_at_utc=now(), result=job.get("result"))
                    self.pause_if_quota(job)
                self.save()
            if job["status"] in TERMINAL:
                print(f"FINISH {identifier} {entry['status']} {entry['seconds']}s", flush=True)
                return entry
            self.sleep(2)
        entry["observer_error"] = "harness_deadline_exceeded_no_resubmit"
        self.manifest["status"] = "waiting_for_running_job_observation"
        self.save()
        raise PipelineStopped("Observer deadline exceeded; no second submission")

    def barrier(self, name):
        resource_wait = self.manifest.get("status") == WAITING
        if not resource_wait:
            self.manifest["status"] = "waiting_for_external_reader"
        self.manifest["external_barrier"] = name
        entry = {"name": name, "status": "waiting", "started_at_utc": now()}
        self.report["barriers"].append(entry)
        self.save()
        print("BARRIER " + name, flush=True)
        started = self.monotonic()
        while not (self.root / name).is_file():
            self.check_stop()
            self.sleep(1)
        entry.update(status="released", finished_at_utc=now(), seconds=round(self.monotonic() - started, 2))
        self.manifest["status"] = WAITING if resource_wait else "running"
        self.manifest["external_barrier"] = None
        self.save()


def main():
    if RUN.exists():
        raise SystemExit("Existing run directory: refusing replay; inspect its durable manifest")
    RUN.mkdir()
    # Set before importing frontend/provider integrations or starting hosts.
    os.environ["CHEMCLAW_PROVIDER_AUDIT_PATH"] = str(ROOT / "provider-requests.jsonl")
    try:
        from chemclaw_lab import auth, db, transcribe
        from chemclaw_lab.api import ChemClawHTTPServer
        from chemclaw_lab.clients import ReadOnlyQueryClient
        from chemclaw_lab.service_config import ServiceSettings
        from configure_agent_runtime import configure, DEFAULT_ROLES
        from create_external_reader import create_external_reader
        from check_handoff_views import compare_views
        import dev_server as bridge
    except Exception as error:
        atomic_save(RUN / "startup-failure.json", {"status": "startup_failed", "at_utc": now(),
            "error_type": type(error).__name__, "paid_calls": 0, "servers_started": False})
        raise SystemExit("Startup import failed; inspect startup-failure.json") from None

    hidden, servers, live_servers = [], [], []
    database = RUN / "evaluation.db"
    manifest = {"started_at_utc": now(), "status": "preparing", "project_id": PROJECT,
        "provider_audit_path": "../provider-requests.jsonl", "paid_preflight": "not_tested",
        "preflight_reason": "Existing provider/configuration retained; omit paid synthetic probes. First actual OCR is observed.",
        "model_retry": False, "terminal_job_replay": False, "business_facts_seeded": False,
        "external_search": False, "cross_test_memory": False, "repetitions": 1}
    report = {"project_id": PROJECT, "turns": [], "snapshots": [], "barriers": [], "observation_errors": []}
    def save():
        atomic_save(RUN / "manifest.json", public_export(manifest, hidden))
        atomic_save(RUN / "results.json", public_export(report, hidden))
    save()
    pipeline, reader, image_hashes, before = None, None, {}, None

    def snapshot(identifier, *, live=True):
        record = {"id": identifier, "created_at_utc": now(), "live": live}
        try:
            cards, contexts, comparisons, catalog = None, [], [], []
            if live:
                cards = reader.invoke("query.samples.list", {"project_ids": [PROJECT]}).get("result", {})
                for card in cards.get("samples", []):
                    context = reader.get_sample_context(PROJECT, card["sample_id"], budget={"max_bytes": 1048576})
                    contexts.append(context)
                    comparisons.append(compare_views(card, context))
                offset = 0
                while True:
                    listed = reader.list_pages(project_ids=[PROJECT], limit=100, offset=offset)
                    catalog.extend(listed["pages"])
                    if not listed["pagination"]["has_more"]:
                        break
                    offset = listed["pagination"]["next_offset"]
            conn = db.connect_read_only(database)
            try:
                conn.execute("BEGIN")
                tables = ("samples", "event_revisions", "memory_proposals", "evidence_pages", "page_transcripts",
                    "event_confirmations", "inventory_transactions", "chemicals", "inventory_subtasks",
                    "inventory_adjustment_proposals", "resources", "resource_bookings", "bookings", "agent_runs", "assistant_jobs")
                counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
                active = {table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE status IN ('queued','running','cancelling')").fetchone()[0]
                          for table in ("assistant_jobs", "agent_runs")}
                tokens = conn.execute("SELECT COUNT(*) FROM agent_run_tokens WHERE status='active'").fetchone()[0]
                pages = []
                for row in conn.execute("SELECT p.page_id,p.page_order,p.status,o.sha256 FROM evidence_pages p JOIN evidence_objects o ON o.evidence_id=p.evidence_id ORDER BY p.rowid"):
                    page = dict(row)
                    page["filename"] = next((name for name, hash_ in image_hashes.items() if hash_ == page["sha256"]), None)
                    page["transcript"] = transcribe.get_page_transcript(conn, page_id=page["page_id"])
                    page["transcript_sha256"] = hashlib.sha256(json.dumps(page["transcript"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    pages.append(page)
                target = sqlite3.connect(RUN / f"{identifier}.db")
                try:
                    conn.backup(target)
                finally:
                    target.close()
            finally:
                conn.close()
            payload = {"id": identifier, "live_api_readback": live, "counts": counts, "cards": cards,
                "contexts": contexts, "view_comparisons": comparisons, "source_catalog": catalog,
                "pages": pages, "active": active, "active_run_tokens": tokens}
            atomic_save(RUN / f"{identifier}-readback.json", public_export(payload, hidden))
            record.update(status="completed", counts=counts, active=active, active_run_tokens=tokens,
                readback=f"{identifier}-readback.json", database=f"{identifier}.db", database_sha256=digest(RUN / f"{identifier}.db"))
        except Exception as error:
            record.update(status="observation_failed", error_type=type(error).__name__)
            report["observation_errors"].append(deepcopy(record))
        report["snapshots"].append(record)
        save()
        return record

    try:
        spec = json.loads((ROOT / "protocol-public.json").read_text(encoding="utf-8"))
        frozen = json.loads((ROOT / "preparation-inputs.json").read_text(encoding="utf-8"))
        image_hashes = {item["name"]: item["sha256"] for item in frozen["images"]}
        if len(image_hashes) != 6 or not all(digest(ROOT / "input" / name) == hash_ for name, hash_ in image_hashes.items()):
            raise PipelineStopped("Image integrity failed")
        if digest(ROOT / "protocol-public.json") != frozen["public_protocol"]["sha256"]:
            raise PipelineStopped("Public protocol changed")
        if any((ROOT / name).exists() for name in ("continue-S3", "finish-evaluation", "confirm-provider-recovery.json", "stop-run")):
            raise PipelineStopped("Stale release/stop markers must not preauthorize a fresh run")
        config = json.loads(Path("D:/Chem/ChemClaw-Lab-S10-Repair-test/ui-config.json").read_text(encoding="utf-8-sig"))
        hidden.extend(value for key, value in config.items() if isinstance(value, str) and ("key" in key.lower() or "token" in key.lower()) and value)
        model = bridge.validate_model_config({"baseUrl": config["modelBase"], "model": "glm-5.3-flash",
            "apiKey": config.get("modelKey", ""), "protocol": config.get("modelProtocol", "openai-chat"),
            "visionModel": "glm-5.3-flash", "visionBaseUrl": config["modelBase"], "visionApiKey": config.get("modelKey", ""),
            "reasoningFormat": "zai", "reasoningEffort": "low", "maxTokens": 16384, "contextWindow": 128000}, 0)
        del config
        assert bridge._reasoning_settings(model) == bridge._reasoning_settings(bridge._vision_config(model)) == ("zai", "low")
        assert bridge._vision_output_budget(model) == 16384
        host_key = secrets.token_urlsafe(32)
        hidden.extend([host_key, model.api_key, model.vision_api_key or ""])
        db.init_db(database)
        conn = db.connect(database)
        try:
            conn.execute("INSERT INTO projects(project_id,name) VALUES(?,?)", (PROJECT, "SIMULATED English MOF visual benchmark"))
            auth.register_principal(conn, principal_id="researchloop_operator", kind="human", display_name="Simulated regression operator")
            auth.grant_membership(conn, principal_id="researchloop_operator", project_id=PROJECT, role="member")
            auth.register_credential(conn, credential_id="researchloop_host", principal_id="researchloop_operator", secret=host_key)
            conn.commit()
        finally:
            conn.close()
        configure(database, list(DEFAULT_ROLES))
        conn = db.connect_read_only(database)
        try:
            timeouts = {row["agent_id"]: json.loads(row["manifest_json"])["timeout_s"] for row in conn.execute("SELECT agent_id,manifest_json FROM agent_manifests WHERE enabled=1")}
        finally:
            conn.close()
        assert set(timeouts) == set(DEFAULT_ROLES) and all(value == 900 for value in timeouts.values())
        before = code_hashes()
        required = {"runtime/pi/provider-observability.mjs", "runtime/pi/task-continuation.mjs", "scripts/benchmark_phase_guard.py"}
        assert required <= before.keys(), "Freeze must include new provider/continuation/guard code"
        for name in before:
            destination = RUN / "source-snapshot" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, destination)
        shutil.copyfile(__file__, RUN / "runner-frozen.py")
        shutil.copyfile(ROOT / "protocol-public.json", RUN / "protocol-public.frozen.json")
        manifest.update(status="running", product_before=before, runner_sha256=digest(__file__),
            protocol_sha256=digest(ROOT / "protocol-public.json"), input_archive_sha256=frozen["archive_sha256"],
            images=frozen["images"], role_timeouts_s=timeouts, enabled_roles=list(DEFAULT_ROLES),
            tree_deadline_s=900, ocr_timeout_s=240, observer_harness_deadline_s=1500,
            inputs="Only six original JPEGs and English public task text; no gold/authoring/reference transcription.",
            model={"name": model.model, "vision_model": model.vision_model, "base_url": model.base_url,
                "protocol": model.protocol, "reasoning_format": "zai", "reasoning_effort": "low",
                "ocr_reasoning_format": "zai", "ocr_reasoning_effort": "low", "max_tokens": 16384,
                "ocr_max_tokens": 16384, "context_window": 128000},
            external_handoff="Not requested in this four-question suite; post-run scoring is separate; current assistant, not independent blind evaluation.")
        save()
        core = ChemClawHTTPServer(("127.0.0.1", 0), database, service_settings=ServiceSettings(storage_root=RUN / "storage"))
        servers.append(core)
        connection = bridge.CoreConnection(f"http://127.0.0.1:{core.server_port}", host_key, PROJECT)
        reader_path = RUN / "reader-private.json"
        create_external_reader(database, PROJECT, reader_path, connection.endpoint)
        private = json.loads(reader_path.read_text(encoding="utf-8"))
        hidden.append(private["api_key"])
        reader = ReadOnlyQueryClient(connection.endpoint, private["api_key"], timeout=60)
        del private
        headers, front = {}, None
        session_last_used = [time.monotonic()]
        def request(method, path, payload=None):
            # Barriers may exceed the frontend's 30-minute browser TTL. Renew
            # only the free browser session before a subsequent jobs request;
            # no model probe, job submission or checkpoint replay is involved.
            if headers and path != "/api/session" and time.monotonic() - session_last_used[0] > 600:
                fresh, fresh_cookie = request("GET", "/api/session")
                headers["X-ChemClaw-Session"] = fresh["csrfToken"]
                if fresh_cookie:
                    headers["Cookie"] = fresh_cookie.split(";", 1)[0]
            client = http.client.HTTPConnection("127.0.0.1", front.server_port, timeout=120)
            try:
                body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                client.request(method, path, body, headers)
                response = client.getresponse()
                result = json.loads(response.read())
                if response.status >= 300:
                    raise PipelineStopped("HTTP " + str(response.status))
                session_last_used[0] = time.monotonic()
                return result, response.getheader("Set-Cookie")
            finally:
                client.close()
        pipeline = OnePassPipeline(manifest, report, save, request)
        original_ocr = bridge.vision_ocr_page
        def guarded_ocr(connection_, model_, page_id, **kwargs):
            try:
                pipeline.before_ocr(page_id)
            except PipelineStopped as error:
                raise bridge.BridgeError("benchmark_paid_phase_blocked", str(error), 409) from None
            return original_ocr(connection_, model_, page_id, **kwargs)
        bridge.vision_ocr_page = guarded_ocr
        front = bridge.BridgeServer(("127.0.0.1", 0), REPO / "frontend/dist", default_connection=connection, default_model=model)
        servers.insert(0, front)
        for server in reversed(servers):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            live_servers.append(server)
        manifest.update(core_endpoint=connection.endpoint, frontend_url=f"http://127.0.0.1:{front.server_port}/")
        save()
        browser, cookie = request("GET", "/api/session")
        headers.update(Cookie=cookie.split(";", 1)[0], **{"X-ChemClaw-Session": browser["csrfToken"],
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{front.server_port}"})
        baseline = snapshot("baseline")
        if baseline["status"] != "completed" or any(baseline.get("active", {}).values()):
            raise PipelineStopped("Baseline incomplete; no image submitted")
        for stage in spec["stages"]:
            for filename in stage["new_images"]:
                pipeline.wait_for_quota_confirmation()
                if code_hashes() != before or digest(__file__) != manifest["runner_sha256"]:
                    raise PipelineStopped("Frozen code changed before paid submission")
                text = spec["public_instruction"] + "\n\nThis is the next notebook page in chronological order. Read and organize this page and carry forward the existing project state. Later pages will be supplied individually."
                entry = pipeline.run_turn("upload-" + Path(filename).stem, text, image=filename, image_hash=image_hashes[filename])
                observed = snapshot(entry["id"])
                if observed["status"] != "completed" or any(observed.get("active", {}).values()):
                    raise PipelineStopped("Readback incomplete or previous run active")
            pipeline.wait_for_quota_confirmation()
            if code_hashes() != before or digest(__file__) != manifest["runner_sha256"]:
                raise PipelineStopped("Frozen code changed before paid stage submission")
            entry = pipeline.run_turn(stage["id"], spec["public_instruction"] + "\n\n" + stage["task"])
            observed = snapshot(entry["id"])
            if observed["status"] != "completed" or any(observed.get("active", {}).values()):
                raise PipelineStopped("Stage readback incomplete or previous run active")
            if stage["id"] == "S2":
                pipeline.barrier("continue-S3")
        pipeline.barrier("finish-evaluation")
        manifest["provider_resource_unresolved"] = manifest.get("status") == WAITING
        manifest["status"] = "completed"
    except BaseException as error:
        if manifest.get("status") not in {WAITING, "waiting_for_submission_reconciliation", "waiting_for_running_job_observation"}:
            manifest["status"] = "harness_failed"
        manifest.update(error_type=type(error).__name__, error=public_export(str(error), hidden))
        save()
        print("HARNESS STOP " + manifest["status"] + " " + type(error).__name__, flush=True)
    finally:
        if database.is_file() and reader is not None and live_servers:
            snapshot("final-live")
        for server in servers:
            if server in live_servers:
                server.shutdown()
            server.server_close()
        if database.is_file():
            final = snapshot("final", live=False)
            if final["status"] != "completed" and manifest["status"] == "completed":
                manifest["status"] = "observation_failed"
            elif manifest["status"] == "completed" and (any(final.get("active", {}).values()) or final.get("active_run_tokens", 0)):
                manifest["status"] = "cleanup_incomplete"
        if before is not None:
            after = code_hashes()
            manifest.update(product_after=after, product_unchanged=after == before,
                runner_unchanged=digest(__file__) == manifest["runner_sha256"],
                public_protocol_unchanged=digest(ROOT / "protocol-public.json") == manifest["protocol_sha256"])
        manifest.update(finished_at_utc=now(), servers_stopped=True,
            inputs_unchanged=all(digest(ROOT / "input" / name) == hash_ for name, hash_ in image_hashes.items()))
        audit_path = ROOT / "provider-requests.jsonl"
        manifest["provider_audit_sha256"] = digest(audit_path) if audit_path.is_file() else None
        save()
        print("SAVED " + manifest["status"], flush=True)
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
