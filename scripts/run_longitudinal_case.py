"""Frozen three-stage text simulation via existing direct/single/multi paths.

Development harness only. No semantic parser, answer file, automatic model retry
or corrective continuation. --resume-harness repairs explicit readback-only
failures and continues unattempted stages. --validate-only never starts a model.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

import run_handoff_case as pilot
from check_handoff_views import compare_views
from chemclaw_lab.clients import ReadOnlyQueryClient

REPO = pilot.REPO
PROJECT = "simulated_film_longitudinal_v2"
# The legacy Condition's initialization and snapshot use its module constant.
# This isolated process uses a new project throughout; the old file is unchanged.
pilot.PROJECT = PROJECT
bridge = pilot.bridge


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_check(root):
    """Structural checks only; evaluation contents are never model input."""
    manifest = json.loads((root / "freeze_manifest.json").read_text(encoding="utf-8-sig"))
    for name, expected in manifest["sha256"].items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.parts[0] not in ("input", "evaluation", "external-tasks"):
            raise ValueError("Invalid frozen fixture path")
        if digest(root / name) != expected:
            raise ValueError(f"Frozen fixture changed: {name}")
    files = {str(path.relative_to(root)).replace("\\", "/")
             for folder in ("input", "evaluation", "external-tasks")
             for path in (root / folder).rglob("*") if path.is_file()}
    if files != set(manifest["sha256"]):
        raise ValueError("Fixture files differ from frozen manifest")
    spec = json.loads((root / "input/tasks.json").read_text(encoding="utf-8-sig"))
    if spec["project_id"] != PROJECT or len(spec["stages"]) != 3:
        raise ValueError("Expected v2 independent project and three stages")
    stages, sources, task_ids = set(), set(), set()
    tasks = {item["id"]: item for item in spec["tasks"]}
    if len(tasks) != len(spec["tasks"]):
        raise ValueError("Duplicate task identity")
    for stage in spec["stages"]:
        stage_id = stage["id"]
        if stage_id in stages or stage_id == "correction" or not stage_id.replace("_", "").isalnum():
            raise ValueError("Stage identity must be unique and avoid the legacy correction branch")
        stages.add(stage_id)
        if len(stage["sources"]) != 2 or not stage["organize"].strip() or not stage["task_ids"]:
            raise ValueError("Each stage needs two pages and a research task")
        for name in stage["sources"]:
            if Path(name).name != name or name in sources or not name.endswith(".txt"):
                raise ValueError("Pages must be distinct plain text filenames")
            text = (root / "input" / name).read_text(encoding="utf-8-sig")
            if not text.startswith("【文字模拟记录；非真实湿实验；非扫描件或 OCR】"):
                raise ValueError("Simulation disclosure missing")
            sources.add(name)
        for task_id in stage["task_ids"]:
            if task_id in task_ids or tasks[task_id]["stage"] != stage_id or not tasks[task_id]["prompt"].strip():
                raise ValueError("Task must belong to exactly one stage")
            task_ids.add(task_id)
    if task_ids != set(tasks) or len(sources) != 6:
        raise ValueError("Unexpected tasks or pages")
    return spec


def code_hashes():
    """Freeze maintained product code/prompts plus harness dependencies, no keys."""
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
    return {str(path.relative_to(REPO)).replace("\\", "/"): digest(path) for path in sorted(paths)
            if "__pycache__" not in path.parts}


def fixture_hashes(root):
    return {str(path.relative_to(root)).replace("\\", "/"): digest(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def difference(before, after):
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
            "changed": sorted(name for name in set(before) & set(after) if before[name] != after[name])}


def save(path, data, hidden=()):
    pilot.atomic_save(path, pilot.public_export(data, tuple(hidden)))


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def readback_only_failure(turn):
    return (turn.get("status") == "harness_failed" and bool(turn.get("readback_error"))
            and turn.get("status_before_readback_failure") == "completed")


def validate_attempt(turn, kind):
    """Reject all uncertain starts/results; this never invokes or retries a model."""
    if kind == "direct":
        if turn.get("status") != "completed" or not isinstance(turn.get("text"), str):
            raise RuntimeError("Direct attempted stage is not definitely completed; refusing resume")
        return
    if (turn.get("status") != "completed" and not readback_only_failure(turn)):
        raise RuntimeError("Only completed models with explicit readback-only failures may resume")
    if (not turn.get("run_id") or not turn.get("session_id")
            or turn.get("result", {}).get("status") != "completed"
            or turn.get("finish", {}).get("status") != "completed"
            or turn.get("finish", {}).get("run_id") != turn["run_id"]):
        raise RuntimeError("Model run/result/finish uncertain; refusing resume")


def resume_check(root, fixture, spec, selected, *, check_attempts=True):
    """Read-only validation: original freeze, ordered attempts, and real run state."""
    original = json.loads((root / "preflight-freeze.json").read_text(encoding="utf-8"))
    current = {"product_and_harness": code_hashes(), "original_fixture": fixture_hashes(fixture),
               "frozen_fixture": fixture_hashes(root / "frozen")}
    changes = {name: difference(original[name], current[name]) for name in original}
    for name in ("original_fixture", "frozen_fixture"):
        if any(changes[name].values()):
            raise RuntimeError("Original/frozen fixture changed; refusing resume")
    code_changes = changes["product_and_harness"]
    if (code_changes["added"] or code_changes["removed"]
            or set(code_changes["changed"]) - {"scripts/run_longitudinal_case.py"}):
        raise RuntimeError("Product/prompt/role/dependency changes exceed observation harness repair")
    report = json.loads((root / "run.json").read_text(encoding="utf-8"))
    if report["case_id"] != spec["case_id"] or report["project_id"] != PROJECT:
        raise RuntimeError("Resume case identity mismatch")
    ordered = [stage["id"] for stage in spec["stages"]]
    checked = {}
    for kind in (selected if check_attempts else []):
        retained = report["conditions"].get(kind)
        if retained is None:
            raise RuntimeError("Resume may only continue previously created conditions")
        condition = retained if kind == "direct" else json.loads((root / kind / "results.json").read_text(encoding="utf-8"))
        turns = condition["turns"]
        if [turn["stage"] for turn in turns] != ordered[:len(turns)] or len(turns) > len(ordered):
            raise RuntimeError("Retained attempts are not a unique stage prefix")
        for turn in turns:
            validate_attempt(turn, kind)
        if kind != "direct":
            connection = sqlite3.connect((root / kind / "case.db").resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if connection.execute("SELECT count(*) FROM agent_runs WHERE status IN ('queued','running')").fetchone()[0]:
                    raise RuntimeError("Retained agent runs are active; refusing resume")
                for turn in turns:
                    row = connection.execute("SELECT status,result_json FROM agent_runs WHERE run_id=?", (turn["run_id"],)).fetchone()
                    if not row or row[0] != "succeeded" or json.loads(row[1] or "{}").get("status") != "completed":
                        raise RuntimeError("Stored run is not definitely completed; refusing resume")
            finally:
                connection.close()
        checked[kind] = {"attempted_stages": [turn["stage"] for turn in turns],
                         "original_turns_sha256": content_hash(turns),
                         "readback_only_stages": [turn["stage"] for turn in turns if readback_only_failure(turn)]}
    return {"original_freeze_differences": changes, "model_product_prompts_unchanged": True,
            "observation_harness_repair": {"path": "scripts/run_longitudinal_case.py",
                "original_sha256": original["product_and_harness"]["scripts/run_longitudinal_case.py"],
                "current_sha256": current["product_and_harness"]["scripts/run_longitudinal_case.py"]},
            "checked_conditions": checked}


class LongitudinalCondition(pilot.Condition):
    """Reuse old isolation/runtime/backup; upload two separate pages per turn."""
    def __init__(self, root, kind, model, hidden, *, resume=False):
        super().__init__(root, kind, model, hidden, resume=resume)
        if not resume:
            conn = pilot.db.connect(self.database)
            try:
                conn.execute("UPDATE projects SET name=? WHERE project_id=?",
                             ("SIMULATED film longitudinal handoff v2", PROJECT))
                conn.commit()
            finally:
                conn.close()
        self.report.update(model_calls=None, token_usage=None, case_type="text-only simulation; no OCR or wet-lab evidence",
                           trace_policy="Unabridged Pi result toolCalls/events/delegations retained in results.json and agent_runs.result_json in stage databases")
        self.save()

    def observe(self, stage):
        snapshot = self.snapshot(stage)
        config = json.loads(Path(self.report["reader_config"]).read_text(encoding="utf-8-sig"))
        if config["project_id"] != PROJECT:
            raise RuntimeError("Observation reader belongs to a different project")
        reader = ReadOnlyQueryClient(config["base_url"], config["api_key"], timeout=60)
        contexts, comparisons = [], []
        for card in snapshot["cards"].get("samples", []):
            context = reader.get_sample_context(PROJECT, card["sample_id"], budget={"max_bytes": 1048576})
            contexts.append(context)
            comparisons.append(compare_views(card, context))
        snapshot.update(contexts=contexts, view_comparisons=comparisons,
                        observation="Sequential actual readback; may be incomplete if a failed runtime left work active.")
        save(self.root / f"{stage}-readback.json", snapshot, self.hidden)
        return snapshot

    def recover_observation(self, turn):
        validate_attempt(turn, self.kind)
        if not readback_only_failure(turn):
            return
        recoveries = self.report.setdefault("observation_recoveries", [])
        original_hash = content_hash(turn)
        if any(item.get("stage") == turn["stage"] and item.get("status") == "completed"
               and item.get("original_turn_sha256") == original_hash for item in recoveries):
            return
        name = f"{turn['stage']}-observation-recovery-{len(recoveries) + 1}"
        if (self.root / f"{name}.db").exists():
            raise RuntimeError("Recovery observation path exists; refusing overwrite")
        entry = {"stage": turn["stage"], "id": name, "status": "running", "original_turn_sha256": original_hash,
                 "model_calls": 0, "token_usage": 0, "note": "Read-only observation recovery; original model turn is unchanged."}
        recoveries.append(entry)
        self.save()
        try:
            entry["snapshot"] = self.observe(name)
            entry["status"] = "completed"
        except Exception as error:
            entry.update(status="failed", error=str(error))
            raise
        finally:
            entry["original_turn_unchanged"] = original_hash == content_hash(turn)
            self.save()

    def turn_pages(self, stage, pages, prompt):
        if any(turn["stage"] == stage for turn in self.report["turns"]):
            raise RuntimeError("Stage already attempted; refusing any automatic repeat")
        entry = {"stage": stage, "prompt": prompt, "sources": pages, "status": "running",
                 "model_calls": None, "token_usage": None}
        self.report["turns"].append(entry)
        self.save()
        started_at = time.monotonic()
        print(f"START {self.kind} {stage}", flush=True)
        try:
            attachments = [{"name": page["name"], "mime": "text/plain",
                            "data_b64": base64.b64encode(page["text"].encode("utf-8")).decode("ascii")}
                           for page in pages]
            staged = bridge.stage_agent_attachments(self.connection, self.model, attachments)
            entry["staged"] = staged
            self.save()
            if staged["failed_attachments"] or staged["warnings"] or len(staged["pages"]) != len(pages):
                raise RuntimeError("Page staging incomplete; no model started")
            # Persist start intent before the request; even uncertain starts are never retried.
            entry["start_requested"] = True
            self.save()
            started = bridge.invoke_internal_write(self.connection, "assistant.agent.start", {
                "project_id": PROJECT, "text": prompt, "session_id": self.session_id,
                "context": {"pages": staged["pages"], "object_refs": staged["object_refs"]}})
            self.session_id = started["session_id"]
            entry.update(session_id=self.session_id, run_id=started["run_id"])
            self.save()
            context = {"project_id": PROJECT, "task_id": started["task_id"], "run_id": started["run_id"],
                       "pages": staged["pages"], "object_refs": staged["object_refs"]}
            runner = pilot.single_pi if self.kind == "single" else bridge.run_pi_agent
            result = runner(self.connection, self.model, started, prompt, context)
            result["data"] = {"pages": staged["pages"]}
            result["object_refs"] = staged["object_refs"] + result.get("object_refs", [])
            entry["result"] = result
            self.save()
            finished = bridge.invoke_internal_write(self.connection, "assistant.agent.finish", {
                "project_id": PROJECT, "run_id": started["run_id"], "claim_epoch": started["claim_epoch"], "result": result})
            entry.update(status=result.get("status", "unknown"), finish=finished)
        except Exception as error:
            entry.update(status="harness_failed", error=str(error))
        entry["seconds"] = round(time.monotonic() - started_at, 2)
        try:
            entry["snapshot"] = self.observe(stage)
        except Exception as error:
            entry["readback_error"] = str(error)
            entry["status_before_readback_failure"] = entry["status"]
            entry["status"] = "harness_failed"
        self.save()
        print(f"FINISH {self.kind} {stage} {entry['status']}", flush=True)
        return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", type=Path, default=REPO / "benchmark/handoff_v2")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--conditions", nargs="+", choices=("direct", "single", "multi"), default=("direct", "single", "multi"))
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--keep-serving", action="store_true")
    parser.add_argument("--resume-harness", action="store_true",
                        help="Repair explicit observation-only failures and continue never-attempted stages; never repeat model calls")
    args = parser.parse_args()
    fixture = args.fixture_dir.resolve()
    spec = fixture_check(fixture)
    selected = list(dict.fromkeys(args.conditions))
    resume_info = None
    if args.resume_harness:
        if not args.run_dir:
            parser.error("--resume-harness requires --run-dir")
        spec = fixture_check(args.run_dir.resolve() / "frozen")
        resume_info = resume_check(args.run_dir.resolve(), fixture, spec, selected)
    if args.validate_only:
        print(json.dumps({"valid": True, "case_id": spec["case_id"], "stages": 3, "pages": 6,
                          "model_started": False, "model_calls": 0, "product_files_hashed": len(code_hashes()),
                          "resume_check": resume_info}))
        return 0
    if not args.config or not args.run_dir:
        parser.error("--config and --run-dir are required for actual model runs")
    # Exclusive creation prevents overwrites, resume and ambiguous replay after any failure.
    root = args.run_dir.resolve()
    if root == fixture or fixture in root.parents:
        parser.error("--run-dir must be outside the fixture directory")
    if not args.resume_harness:
        root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(fixture, root / "frozen")
        spec = fixture_check(root / "frozen")
    before = {"product_and_harness": code_hashes(), "original_fixture": fixture_hashes(fixture),
              "frozen_fixture": fixture_hashes(root / "frozen")}
    if not args.resume_harness:
        save(root / "preflight-freeze.json", before)
    settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
    model = bridge.validate_model_config({"baseUrl": settings["modelBase"], "model": settings["model"],
        "apiKey": settings.get("modelKey", ""), "protocol": settings.get("modelProtocol", "openai-chat"),
        "reasoningFormat": settings.get("modelReasoningFormat", "auto"),
        "reasoningEffort": settings.get("modelReasoningEffort") or "auto",
        "maxTokens": settings.get("modelMaxTokens", 16384), "contextWindow": settings.get("modelContextWindow", 128000)}, 0)
    hidden = list(filter(None, (settings.get("modelKey"), settings.get("visionKey"))))
    del settings
    report = {"case_id": spec["case_id"], "project_id": PROJECT, "conditions": {}, "requested_conditions": selected,
              "status": "running", "model": model.model, "reasoning_format": model.reasoning_format,
              "reasoning_effort": model.reasoning_effort, "max_tokens_per_response": model.max_tokens,
              "context_window": model.context_window, "model_calls": None, "token_usage": None,
              "usage_note": "Complete provider calls/tokens including delegates are not exposed by existing receipts; null, not zero.",
              "input_policy": "Only frozen input pages and natural tasks are sent to models. Rubric is retained for separate developer evaluation and never read as a model message.",
              "limitations": ["One text-only simulated case; no OCR, wet-lab evidence or statistical superiority claim.",
                              "Same model and response ceiling, not equal aggregate inference budget.",
                              "Direct sees cumulative original text without prior answers or persistent memory.",
                              "Original failures retained; no automatic retry or corrective model continuation."]}
    postcheck_name = "source-hash-postcheck.json"
    if args.resume_harness:
        retained = json.loads((root / "run.json").read_text(encoding="utf-8"))
        for name in ("model", "reasoning_format", "reasoning_effort", "max_tokens_per_response", "context_window"):
            if retained.get(name) != report[name]:
                raise RuntimeError(f"Model configuration changed: {name}")
        report = retained
        number = len(report.get("harness_resumptions", [])) + 1
        recovery_name = f"harness-resume-{number}"
        archive = root / f"{recovery_name}-original"
        archive.mkdir(exist_ok=False)
        for name in ("run.json", "source-hash-postcheck.json"):
            shutil.copyfile(root / name, archive / name)
        for kind in selected:
            if kind != "direct":
                (archive / kind).mkdir()
                shutil.copyfile(root / kind / "results.json", archive / kind / "results.json")
        if (root / "stop").exists():
            shutil.copyfile(root / "stop", archive / "stop")
            (root / "stop").unlink()
        report.setdefault("harness_resumptions", []).append({"id": recovery_name, "conditions": selected,
            "validation": resume_info, "original_artifacts": str(archive),
            "policy": "Readback-only recovery preserves original failed turns; no started model stage is repeated."})
        report["status"] = "resuming_harness"
        save(root / f"{recovery_name}-preflight.json", {"before": before, "validation": resume_info})
        postcheck_name = f"source-hash-postcheck-{recovery_name}.json"
    servers = []
    failed = False
    save(root / "run.json", report, hidden)
    try:
        tasks = {item["id"]: item for item in spec["tasks"]}
        inputs = []
        for stage in spec["stages"]:
            pages = [{"name": name, "text": (root / "frozen/input" / name).read_text(encoding="utf-8-sig")}
                     for name in stage["sources"]]
            questions = "\n".join(tasks[item]["prompt"] for item in stage["task_ids"])
            inputs.append((stage["id"], pages, stage["organize"] + "\n\n" + questions, questions))
        for kind in selected:
            if (root / "stop").exists():
                report["status"] = "stopped_between_calls"
                break
            if kind == "direct":
                condition = report["conditions"].get(kind) or {"condition": kind, "turns": [], "persistent_state": False,
                             "model_calls": None, "token_usage": None, "tools": []}
                report["conditions"][kind] = condition
                original = []
                for stage, pages, _, questions in inputs:
                    if (root / "stop").exists():
                        break
                    original.extend(page["text"] for page in pages)
                    if any(turn["stage"] == stage for turn in condition["turns"]):
                        continue
                    message = "以下是目前收到的全部文字模拟笔记。请据原文回答研究交接问题。\n\n" + "\n\n".join(original) + "\n\n" + questions
                    entry = {"stage": stage, "prompt": message, "status": "running", "start_requested": True,
                             "model_calls": None, "token_usage": None}
                    condition["turns"].append(entry)
                    save(root / "run.json", report, hidden)
                    start = time.monotonic()
                    print(f"START direct {stage}", flush=True)
                    try:
                        entry.update(text=bridge.call_model(model, [{"role": "user", "content": message}], timeout=300), status="completed")
                    except Exception as error:
                        entry.update(status="failed", error=str(error))
                        failed = True
                    entry["seconds"] = round(time.monotonic() - start, 2)
                    save(root / "run.json", report, hidden)
                    print(f"FINISH direct {stage} {entry['status']}", flush=True)
                    if entry["status"] != "completed":
                        break
            else:
                condition = LongitudinalCondition(root, kind, model, hidden, resume=args.resume_harness)
                servers.append(condition.server)
                report["conditions"][kind] = condition.report
                for stage, pages, prompt, _ in inputs:
                    if (root / "stop").exists():
                        break
                    prior = next((turn for turn in condition.report["turns"] if turn["stage"] == stage), None)
                    if prior:
                        condition.recover_observation(prior)
                        save(root / "run.json", report, hidden)
                        continue
                    entry = condition.turn_pages(stage, pages, prompt)
                    save(root / "run.json", report, hidden)
                    if entry["status"] != "completed":
                        failed = True
                        # A later scheduled source stage is not a retry or repair.
                        # Uncertain harness failures stop this condition instead.
                        if entry["status"] == "harness_failed":
                            break
        report["status"] = "stopped_between_calls" if (root / "stop").exists() else ("finished_with_failures" if failed else "finished")
        save(root / "run.json", report, hidden)
    except Exception as error:
        failed = True
        report.update(status="harness_failed", error=str(error))
        save(root / "run.json", report, hidden)
    finally:
        after = {"product_and_harness": code_hashes(), "original_fixture": fixture_hashes(fixture),
                 "frozen_fixture": fixture_hashes(root / "frozen")}
        changes = {name: difference(before[name], after[name]) for name in before}
        unchanged = all(not entries for group in changes.values() for entries in group.values())
        postcheck = {"unchanged": unchanged, "differences": changes, "after": after}
        if args.resume_harness:
            try:
                postcheck["original_freeze_validation"] = resume_check(root, fixture, spec, selected, check_attempts=False)
                report["model_product_prompts_unchanged_from_original"] = True
            except RuntimeError as error:
                failed = True
                postcheck["original_freeze_error"] = str(error)
                report["model_product_prompts_unchanged_from_original"] = False
            report["resume_freeze_unchanged"] = unchanged
        else:
            report["freeze_unchanged"] = unchanged
        save(root / postcheck_name, postcheck)
        save(root / "run.json", report, hidden)
        if args.keep_serving and servers:
            print("SERVING_UNTIL_STOP_FILE " + str(root / "stop"), flush=True)
            try:
                while not (root / "stop").exists():
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        for server in servers:
            server.shutdown()
            server.server_close()
    return 1 if failed or not unchanged else 0


if __name__ == "__main__":
    raise SystemExit(main())
