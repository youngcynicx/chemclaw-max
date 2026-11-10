"""Health evidence never probes providers or invents a pre-work baseline."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import BadStatusLine
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/benchmark_health_guard.py"
spec = importlib.util.spec_from_file_location("benchmark_health_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture
def service():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.calls.append((self.command, self.path))
            if self.path == "/healthz":
                self.send_response(302)
                self.send_header("Location", "/provider-probe")
            else:
                self.send_response(self.server.status)
            self.end_headers()
            try:
                self.wfile.write(b"SECRET_BODY_DO_NOT_SAVE")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self):
            self.server.calls.append((self.command, self.path))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls, server.status = [], 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def stamp(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


def work_stamp(snapshot):
    # Windows' wall-clock precision can give adjacent captures the same stamp.
    # Require a genuinely later work boundary rather than weakening the guard.
    completed = datetime.fromisoformat(snapshot["completed_at_utc"])
    current = datetime.now(timezone.utc)
    while current <= completed:
        time.sleep(0.001)
        current = datetime.now(timezone.utc)
    return current.isoformat()


def test_only_get_status_evidence_no_body_proxy_or_provider_call(service, monkeypatch):
    server, base = service
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    result = guard.capture_health({"core": base + "/health", "frontend": base + "/"})
    assert result["provider_calls"] == 0
    assert server.calls == [("GET", "/health"), ("GET", "/")]
    assert all(row["state"] == "healthy" and row["http_status"] == 200 for row in result["endpoints"].values())
    assert "SECRET_BODY_DO_NOT_SAVE" not in json.dumps(result)
    assert "Authorization" not in json.dumps(result)


def test_redirect_never_reaches_other_endpoint(service):
    server, base = service
    result = guard.capture_health({"core": base + "/healthz"})
    assert server.calls == [("GET", "/healthz")]
    assert result["endpoints"]["core"]["http_status"] == 302
    assert result["endpoints"]["core"]["state"] == "http_error"
    assert "SECRET_BODY_DO_NOT_SAVE" not in json.dumps(result)


@pytest.mark.parametrize("bad", [
    "https://example.org/health", "http://127.0.0.1/api/model/probe",
    "http://127.0.0.1/health?api_key=secret", "http://user:secret@localhost/health",
    "http://127.0.0.1/health#token", "file:///health", "http://localhost:0/health",
    "http://127.0.0.1/health\n", "http://127.0.0.2/health", "http://127.0.0.1:bad/health",
])
def test_invalid_endpoint_rejected_before_any_partial_network_call(service, bad):
    server, base = service
    with pytest.raises(ValueError):
        guard.capture_health({"first": base + "/health", "bad": bad})
    assert server.calls == []


def test_connection_failure_retains_error_class_not_exception_message():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    result = guard.capture_health({"core": f"http://127.0.0.1:{port}/health"}, timeout_s=0.1)
    row = result["endpoints"]["core"]
    assert row["state"] == "unavailable" and row["http_status"] is None
    assert row["error_class"] in {"URLError", "TimeoutError", "ConnectionRefusedError"}
    assert set(row) == {"url", "state", "http_status", "error_class", "duration_ms"}


def test_malformed_local_http_is_safe_unavailable_not_a_body_leak(monkeypatch):
    class BrokenHTTP:
        def open(self, *args, **kwargs):
            raise BadStatusLine("SECRET_BODY_DO_NOT_SAVE")

    monkeypatch.setattr(guard, "build_opener", lambda *handlers: BrokenHTTP())
    result = guard.capture_health({"core": "http://127.0.0.1/health"})
    assert result["endpoints"]["core"]["state"] == "unavailable"
    assert result["endpoints"]["core"]["error_class"] == "BadStatusLine"
    assert "SECRET_BODY_DO_NOT_SAVE" not in json.dumps(result)


def test_health_record_is_exclusive_and_duplicate_does_not_even_probe(service, tmp_path):
    server, base = service
    path = tmp_path / "before.json"
    guard.record_health(path, {"core": base + "/health"}, phase="before")
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        guard.record_health(path, {"core": base + "/health"}, phase="before")
    assert path.read_bytes() == original and server.calls == [("GET", "/health")]


def test_known_late_baseline_is_not_created_or_probed(service, tmp_path):
    server, base = service
    path = tmp_path / "late.json"
    with pytest.raises(ValueError, match="after test work"):
        guard.record_health(path, {"core": base + "/health"}, phase="before", work_started_at_utc=stamp(-1))
    assert not path.exists() and server.calls == []


def test_probe_that_finishes_after_work_cannot_be_saved_as_pre_work(service, tmp_path, monkeypatch):
    server, base = service
    started = datetime.now(timezone.utc)
    ticks = iter([started, started, started + timedelta(seconds=2)])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    monkeypatch.setattr(guard, "datetime", Clock)
    path = tmp_path / "overlap.json"
    with pytest.raises(ValueError, match="did not finish"):
        guard.record_health(path, {"core": base + "/health"}, phase="before",
            work_started_at_utc=(started + timedelta(seconds=1)).isoformat())
    assert not path.exists() and server.calls == [("GET", "/health")]


def test_before_after_success_and_degradation_are_observations_not_causation(service):
    server, base = service
    before = guard.capture_health({"core": base + "/health"}, phase="before")
    work_at = work_stamp(before)
    after = guard.capture_health({"core": base + "/health"}, phase="after")
    healthy = guard.compare_health(before, after, work_started_at_utc=work_at)
    assert healthy["classification"] == "healthy_before_and_after" and healthy["baseline_validated"]
    server.status = 503
    after = guard.capture_health({"core": base + "/health"}, phase="after")
    degraded = guard.compare_health(before, after, work_started_at_utc=work_at)
    assert degraded["classification"] == "health_degraded_observed"
    assert degraded["causality"] == "not_established"
    assert "SECRET_BODY_DO_NOT_SAVE" not in json.dumps(degraded)


def test_missing_unanchored_and_late_baselines_cannot_become_regressions(service):
    _, base = service
    work_at = stamp(-1)
    before = guard.capture_health({"core": base + "/health"}, phase="before")
    after = guard.capture_health({"core": base + "/health"}, phase="after")
    assert guard.compare_health(None, after, work_started_at_utc=work_at)["classification"] == "baseline_missing"
    assert guard.compare_health(before, after)["classification"] == "baseline_time_unverified"
    late = guard.compare_health(before, after, work_started_at_utc=work_at)
    assert late["classification"] == "baseline_recorded_after_work" and late["baseline_validated"] is False


def test_unavailable_before_is_preserved_even_when_after_unavailable(service):
    server, base = service
    server.status = 503
    before = guard.capture_health({"core": base + "/health"}, phase="before")
    work_at = work_stamp(before)
    after = guard.capture_health({"core": base + "/health"}, phase="after")
    result = guard.compare_health(before, after, work_started_at_utc=work_at)
    assert result["classification"] == "baseline_unavailable"
    server.status = 200
    recovered = guard.capture_health({"core": base + "/health"}, phase="after")
    assert guard.compare_health(before, recovered, work_started_at_utc=work_at)["classification"] == "recovered_observed"


def test_changed_endpoint_or_process_identity_is_incomparable(service):
    _, base = service
    identities = [{"pid": 123, "created_at_utc": stamp(-20), "name": "python.exe"}]
    before = guard.capture_health({"core": base + "/health"}, phase="before", process_identities=identities)
    work_at = work_stamp(before)
    after = guard.capture_health({"core": base + "/health"}, phase="after", process_identities=identities)
    changed = deepcopy(after)
    changed["process_identities"][0]["created_at_utc"] = stamp(-10)  # Same PID, different actual process.
    assert guard.compare_health(before, changed, work_started_at_utc=work_at)["classification"] == "incomparable_process_identities"
    changed = deepcopy(after)
    changed["endpoints"]["core"]["url"] = base + "/"
    assert guard.compare_health(before, changed, work_started_at_utc=work_at)["classification"] == "incomparable_endpoints"


def test_wrong_phase_or_after_before_work_is_not_a_valid_comparison(service):
    _, base = service
    before = guard.capture_health({"core": base + "/health"}, phase="before")
    after = guard.capture_health({"core": base + "/health"}, phase="after")
    wrong = deepcopy(before)
    wrong["phase"] = "after"
    assert guard.compare_health(wrong, after, work_started_at_utc=stamp())["classification"] == "incomparable_phase"
    assert guard.compare_health(before, after, work_started_at_utc=stamp(10))["classification"] == "incomparable_time_order"


@pytest.mark.parametrize("identity", [
    {"pid": 1, "created_at_utc": stamp(), "name": "python.exe", "command": "secret"},
    {"pid": True, "created_at_utc": stamp(), "name": "python.exe"},
    {"pid": 1, "created_at_utc": "2026-10-29", "name": "python.exe"},
    {"pid": 1, "created_at_utc": stamp(), "name": "C:/private/python.exe"},
])
def test_process_identity_cannot_copy_command_lines_paths_or_naive_times(service, identity):
    server, base = service
    with pytest.raises(ValueError):
        guard.capture_health({"core": base + "/health"}, process_identities=[identity])
    assert server.calls == []


def test_cli_writes_missing_baseline_as_missing_not_green(service, tmp_path):
    server, base = service
    after_path, comparison_path = tmp_path / "after.json", tmp_path / "comparison.json"
    first = subprocess.run([sys.executable, str(SCRIPT), "capture", "--output", str(after_path),
        "--phase", "after", "--endpoint", "core=" + base + "/health"], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, str(SCRIPT), "compare", "--before", str(tmp_path/"absent.json"),
        "--after", str(after_path), "--output", str(comparison_path), "--work-started-at-utc", stamp(-1)],
        capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    report = json.loads(comparison_path.read_text(encoding="utf-8"))
    assert report["classification"] == "baseline_missing" and report["baseline_validated"] is False
    assert server.calls == [("GET", "/health")]
