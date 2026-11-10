"""Independent handoff transport, replayable evidence, tool and secret bounds."""
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chemclaw_lab import external_handoff as runner
from chemclaw_lab.clients import ReadOnlyQueryClient


def _json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def inputs(tmp_path):
    config = _json(tmp_path / "reader.json", {"base_url": "http://127.0.0.1:9999", "project_id": "P",
        "principal_id": "service_test", "api_key": "reader-secret-value"})
    task = tmp_path / "task.md"
    task.write_text("分析样品 S；给出记录依据及未知项。", encoding="utf-8")
    return config, task, tmp_path / "run"


def test_prepare_exports_only_four_shared_read_contracts_and_no_credentials(inputs):
    config, task, out = inputs
    runner.prepare_run(config, task, out)
    specs = json.loads((out / "tools.json").read_text(encoding="utf-8"))
    assert set(specs["name_map"].values()) == set(runner.TOOL_NAMES)
    assert "query.answer.render" not in json.dumps(specs)
    for tool in specs["tools"]:
        key = specs["name_map"][tool["function"]["name"]]
        assert tool["function"]["parameters"] == runner.READ_INPUT_SCHEMAS[key]
    exported = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert "reader-secret-value" not in exported
    assert "api_key" not in exported
    with pytest.raises(runner.HandoffError):
        runner.prepare_run(config, task, out)


def test_manual_execution_binds_scope_records_full_results_and_finishes(inputs, monkeypatch):
    config, task, out = inputs
    runner.prepare_run(config, task, out)
    seen = []
    def invoke(client, name, params, **kw):
        seen.append((name, params))
        return {"action_status": "applied", "result": {"sample": "S", "text": "reader-secret-value"}}
    monkeypatch.setattr(ReadOnlyQueryClient, "invoke", invoke)
    response = runner.execute_tool(out, config, {"name": "query__samples__list", "arguments": {}})
    assert seen == [("query.samples.list", {"project_ids": ["P"], "view": "catalog"})]
    assert response["response"]["result"]["text"] == "[redacted]"
    assert json.loads(Path(response["response_file"]).read_text(encoding="utf-8")) == response["response"]
    invalid = runner.execute_tool(out, config, {"name": "memory.proposal.apply", "arguments": {}})
    assert invalid["response"]["action_status"] == "failed"
    wrong_project = runner.execute_tool(out, config, {"name": "query.sample.context", "arguments": {
        "project_id": "OTHER", "sample_id": "S"}})
    assert wrong_project["response"]["action_status"] == "failed"
    assert len(seen) == 1
    answer = out.parent / "answer.md"
    answer.write_text("样品 S 有记录，产率未知。", encoding="utf-8")
    summary = runner.finish_run(out, answer, actor="independent-test-agent")
    assert summary["tool_calls"] == 3
    assert summary["usage"] is None
    assert summary["model_calls"] is None and summary["model_calls_status"] == "externally_managed"
    assert summary["internal_model_calls"] == 0
    assert (out / "summary.json").is_file()
    with pytest.raises(runner.HandoffError):
        runner.execute_tool(out, config, {"name": "query.samples.list", "arguments": {}})


def test_no_tools_condition_does_not_contact_core(inputs, monkeypatch):
    config, task, out = inputs
    runner.prepare_run(config, task, out, tools="none")
    def forbidden(*args, **kw):
        pytest.fail("No Core request is permitted in no-tools condition")
    monkeypatch.setattr(ReadOnlyQueryClient, "invoke", forbidden)
    response = runner.execute_tool(out, config, {"name": "query.samples.list", "arguments": {}})
    assert response["response"]["action_status"] == "failed"


def test_reader_does_not_accept_embedded_url_credentials(inputs):
    config, task, out = inputs
    data = json.loads(config.read_text())
    data["base_url"] = "https://user:secret@example.invalid/v2"
    _json(config, data)
    with pytest.raises(runner.HandoffError):
        runner.prepare_run(config, task, out)


def test_chat_loop_performs_actual_http_model_round_trip_and_read_call(inputs, monkeypatch):
    config, task, out = inputs
    requests = []
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "reasoning_content": "PRIVATE_REASONING",
                    "tool_calls": [{"id": "call-model-1", "type": "function", "function": {
                        "name": "query__sample__context", "arguments": json.dumps({"sample_id": "S", "project_id": "P"})}}]}
            else:
                assert body["messages"][-1]["role"] == "tool"
                message = {"role": "assistant", "content": "记录显示产率未知 [event E revision 1]；model-secret-value"}
            payload = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model_config = _json(out.parent / "model.json", {"baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
        "model": "test-chat", "apiKey": "model-secret-value", "reasoningFormat": "none"})
    def invoke(client, name, params, **kw):
        assert name == "query.sample.context"
        assert params["project_id"] == "P"
        return {"action_status": "applied", "result": {"events": [{"event_id": "E", "revision": 1}],
            "coverage": {"retrieval_complete": True}}}
    monkeypatch.setattr(ReadOnlyQueryClient, "invoke", invoke)
    try:
        summary = runner.run_model(config, model_config, task, out)
    finally:
        server.shutdown()
        server.server_close()
    assert summary["model_calls"] == 2 and summary["tool_calls"] == 1
    assert summary["usage"]["total_tokens"] == 30
    assert requests[0]["tool_choice"] == "auto"
    exported = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert "model-secret-value" not in exported
    assert "reader-secret-value" not in exported
    assert "PRIVATE_REASONING" not in exported
    assert "[redacted]" in (out / "final.md").read_text(encoding="utf-8")


def test_turn_exhaustion_is_failed_not_a_fake_answer(inputs, monkeypatch):
    config, task, out = inputs
    model_config = _json(out.parent / "model.json", {"modelBase": "http://127.0.0.1:1234/v1",
        "model": "test", "modelKey": "", "modelProtocol": "openai-chat"})
    monkeypatch.setattr(runner, "request_chat", lambda *a: ({"role": "assistant", "content": None,
        "tool_calls": [{"id": "c", "function": {"name": "disallowed", "arguments": "{}"}}]}, None))
    with pytest.raises(runner.HandoffError, match="turn budget"):
        runner.run_model(config, model_config, task, out, max_turns=1)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert not (out / "final.md").exists()


def test_unsupported_model_protocol_is_explicit(inputs):
    config, task, out = inputs
    model_config = _json(out.parent / "model.json", {"baseUrl": "http://127.0.0.1:1234/v1",
        "model": "test", "apiKey": "", "protocol": "openai-responses"})
    with pytest.raises(runner.HandoffError, match="chat only"):
        runner.run_model(config, model_config, task, out)
    assert not out.exists()


def test_execute_uses_real_http_and_keeps_complete_response_in_artifacts(inputs):
    config, task, out = inputs
    received = []
    class Core(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            received.append((self.path, self.headers["Authorization"],
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            payload = json.dumps({"action_status": "applied", "result": {"text": "完整原文，不能用摘要替代"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Core)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    value = json.loads(config.read_text())
    value["base_url"] = f"http://127.0.0.1:{server.server_port}"
    _json(config, value)
    runner.prepare_run(config, task, out)
    try:
        result = runner.execute_tool(out, config, {"name": "memory.event.read", "arguments": {"event_id": "E", "revision": 1}})
    finally:
        server.shutdown()
        server.server_close()
    assert result["response"]["result"]["text"] == "完整原文，不能用摘要替代"
    assert received[0][0] == "/v2/capabilities/memory.event.read/invoke"
    assert received[0][1] == "Bearer reader-secret-value"
    assert received[0][2]["params"]["revision"] == 1
    exported = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert "完整原文，不能用摘要替代" in exported
    assert "reader-secret-value" not in exported
