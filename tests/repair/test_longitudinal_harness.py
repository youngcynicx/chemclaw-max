"""Observation/resume regression: real reader HTTP, never a model invocation."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_longitudinal_case as harness


def completed_readback_failure():
    return {"stage": "arrival", "status": "harness_failed", "readback_error": "old bridge whitelist",
            "status_before_readback_failure": "completed", "run_id": "run-one", "session_id": "session-one",
            "result": {"status": "completed"}, "finish": {"status": "completed", "run_id": "run-one"}}


def test_observe_uses_scoped_reader_http_without_bridge_context(tmp_path, monkeypatch):
    requests = []
    card = {"sample_id": "sample-one", "label": "fictional", "records": [], "current_state": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"result": {"status": "complete", "events": [], "current_state": []}}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "reader.json"
        config.write_text(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "fixture-only-secret", "project_id": harness.PROJECT}), encoding="utf-8")
        condition = object.__new__(harness.LongitudinalCondition)
        condition.root, condition.hidden = tmp_path, ["fixture-only-secret"]
        condition.report = {"reader_config": str(config)}
        condition.snapshot = lambda stage: {"cards": {"samples": [card]}}

        def forbidden_bridge(*args, **kwargs):
            raise AssertionError("Context observation must use dedicated reader, not frontend bridge")

        monkeypatch.setattr(harness.bridge, "invoke_read", forbidden_bridge)
        observed = condition.observe("observation-only")
        assert observed["view_comparisons"][0]["consistent"]
        assert requests[0][0] == "/v2/capabilities/query.sample.context/invoke"
        assert requests[0][1] == "Bearer fixture-only-secret"
        assert requests[0][2]["params"]["project_id"] == harness.PROJECT
        assert requests[0][2]["params"]["sample_id"] == "sample-one"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("change", [
    {"status": "running"},
    {"status": "harness_failed", "readback_error": ""},
    {"status_before_readback_failure": "running"},
    {"run_id": None},
    {"session_id": None},
    {"result": {}},
    {"result": {"status": "failed"}},
    {"finish": {}},
    {"finish": {"status": "completed", "run_id": "another-run"}},
])
def test_resume_rejects_uncertain_attempt_without_model_retry(change):
    turn = {**completed_readback_failure(), **change}
    with pytest.raises(RuntimeError):
        harness.validate_attempt(turn, "single")


def test_readback_recovery_preserves_original_turn_and_never_repeats(tmp_path):
    turn = completed_readback_failure()
    original = deepcopy(turn)
    condition = object.__new__(harness.LongitudinalCondition)
    condition.root, condition.kind, condition.hidden = tmp_path, "single", []
    condition.report = {"turns": [turn]}
    observed = []
    condition.observe = lambda name: observed.append(name) or {"actual_fixture": True}
    condition.recover_observation(turn)
    condition.recover_observation(turn)
    assert turn == original
    assert len(observed) == 1
    recovery = condition.report["observation_recoveries"][0]
    assert recovery["model_calls"] == 0 and recovery["original_turn_unchanged"]
    assert "observation-recovery" in observed[0]
