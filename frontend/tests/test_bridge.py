"""Contract/security tests: these use an isolated fake Core, never a lab DB."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "dev_server.py"
SPEC = importlib.util.spec_from_file_location("chemclaw_frontend_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class FakeCoreHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.calls.append(("GET", self.path, None, dict(self.headers)))
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/must-not-follow")
            self.end_headers()
            return
        if self.path == "/health":
            return self.send_json({"status": "ok", "version": "0.4.0"})
        if self.headers.get("Authorization") != "Bearer test-key-secret":
            return self.send_json({"error": "unauthorized"}, 401)
        if self.path == "/v2/capabilities":
            read_caps = [
                "inventory.query.list", "query.events.search", "query.samples.list",
                "query.sample.timeline", "query.answer.build", "memory.event.read",
                "memory.sample.search", "memory.proposal.read",
                "evidence.page.read", "evidence.page.original", "evidence.page.preview",
                "evidence.pages.list", "evidence.provider.list", "resource.list",
                "resource.booking.status",
            ]
            return self.send_json({
                "capabilities": [
                    {"capability_id": cap, "mutates": False} for cap in read_caps
                ] + [
                    {"capability_id": "memory.event.correct", "mutates": True},
                    {"capability_id": "assistant.task.process", "mutates": True},
                    {"capability_id": "assistant.agent.start", "mutates": True},
                    {"capability_id": "assistant.agent.finish", "mutates": True},
                ]
            })
        if self.path == "/v2/tasks":
            return self.send_json({"tasks": [{"task_id": "one", "project_id": "p1"},
                                            {"task_id": "two", "project_id": "p2"},
                                            {"task_id": "global", "project_id": None}]})
        self.send_json({"error": "unknown"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(("POST", self.path, body, dict(self.headers)))
        params = body["params"]
        if self.path == "/v2/capabilities/assistant.agent.start/invoke":
            return self.send_json({"action_status": "proposed", "result": {
                "session_id": params.get("session_id") or "sess_agent",
                "task_id": "task_agent", "run_id": "run_agent", "claim_epoch": 1,
                "run_token": "private-test-run-token", "trace_id": "trace_agent", "history": [],
            }})
        if self.path == "/v2/capabilities/assistant.agent.finish/invoke":
            return self.send_json({"action_status": "proposed", "result": {
                **params["result"], "session_id": "sess_agent", "message_id": "reply_agent",
                "task_id": "task_agent", "run_id": "run_agent", "intent": "assistant",
            }})
        if self.path == "/v2/capabilities/inventory.query.list/invoke":
            if params["kind"] == "transactions" and params["project_id"] != "p1":
                return self.send_json({"action_status": "rejected", "error": {"code": "forbidden_scope"}})
            return self.send_json({"action_status": "applied", "result": {
                "kind": params["kind"], "items": [{"chemical_id": "chem1", "name": "Test chemical", "amount": 2, "unit": "g"}]}})
        if self.path == "/v2/capabilities/query.events.search/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "candidates": [{"object_id": "event1", "chunk": "source text"}], "snapshot_version": "snap"}})
        if self.path == "/v2/capabilities/query.samples.list/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "samples": [{"sample_id": "smp1", "project_id": "p1", "label": "S-1",
                             "event_count": 1}], "scope": ["p1"]}})
        if self.path == "/v2/capabilities/query.sample.timeline/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "sample": {"sample_id": params.get("sample_id")},
                "timeline": [{"event_id": "event1", "revision": 1}]}})
        if self.path == "/v2/capabilities/query.answer.build/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "status": "answered", "question": params.get("question"),
                "claims": [{"claim_id": "c1", "value": 0.5, "unit": "g",
                            "statement": "实际取用 0.5 g", "verify": {"source_supports": True}}],
                "gaps": [], "rendered_text": "实际取用 0.5 g [c1]"}})
        if self.path == "/v2/capabilities/memory.event.read/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "event_id": params.get("event_id"),
                "revisions": [{"revision": 1, "field_json": {"fields": {}}}]}})
        if self.path == "/v2/capabilities/evidence.page.read/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "page": {"page_id": params.get("page_id"), "mime": "text/plain",
                         "transcript": {"blocks": []}}}})
        if self.path == "/v2/capabilities/resource.list/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "resources": [{"resource_id": "res1", "name": "NMR", "kind": "instrument",
                               "project_id": "p1", "status": "active", "capacity": 1}]}})
        if self.path == "/v2/capabilities/resource.booking.status/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "bookings": [{"booking_id": "bkg1", "resource_id": "res1",
                              "project_id": "p1", "status": "confirmed",
                              "slot_start": "2026-10-18T09:00:00+08:00",
                              "slot_end": "2026-10-18T10:00:00+08:00"}]}})
        if self.path == "/v2/capabilities/evidence.provider.list/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "providers": [{"name": "text_input", "kind": "local"}]}})
        if self.path == "/v2/capabilities/assistant.task.process/invoke":
            return self.send_json({"action_status": "applied", "result": {
                "session_id": params.get("session_id") or "sess_fixture",
                "message_id": "tmsg_fixture",
                "intent": "query_answer",
                "agent": "b_query",
                "status": "applied",
                "text": "verified answer",
                "data": {"status": "answered", "claims": []},
            }})
        self.send_json({"error": "unknown"}, 404)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        Path(cls.directory.name, "index.html").write_text("<!doctype html><title>Demo</title>", encoding="utf-8")
        for name in ("app.js", "module.mjs", "styles.css", "notebook.svg", "display.ttf", "display.woff2"):
            Path(cls.directory.name, name).write_text("/* fixture */", encoding="utf-8")
        cls.core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        cls.core.calls = []
        cls.core.redirect = False
        cls.frontend = bridge.BridgeServer(("127.0.0.1", 0), Path(cls.directory.name))
        cls.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (cls.core, cls.frontend)]
        for thread in cls.threads:
            thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.frontend, cls.core):
            server.shutdown()
            server.server_close()
        cls.directory.cleanup()

    def setUp(self):
        self.core.calls.clear()
        self.core.redirect = False
        self.cookie, self.csrf = self.new_session()

    def request(self, method, path, body=None, *, cookie=True, csrf=True, headers=None):
        merged = {"Origin": f"http://127.0.0.1:{self.frontend.server_port}"}
        if cookie:
            merged["Cookie"] = self.cookie
        if csrf:
            merged["X-ChemClaw-Session"] = self.csrf
        if body is not None:
            merged["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        merged.update(headers or {})
        client = http.client.HTTPConnection("127.0.0.1", self.frontend.server_port, timeout=4)
        client.request(method, path, body=body, headers=merged)
        response = client.getresponse()
        raw = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        client.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw.decode()
        return status, data, result_headers

    def new_session(self):
        client = http.client.HTTPConnection("127.0.0.1", self.frontend.server_port)
        client.request("GET", "/api/session")
        response = client.getresponse()
        data = json.loads(response.read())
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        client.close()
        return cookie, data["csrfToken"]

    def connect(self, **overrides):
        config = {"endpoint": f"http://127.0.0.1:{self.core.server_port}",
                  "apiKey": "test-key-secret", "projectId": "p1"}
        config.update(overrides)
        return self.request("POST", "/api/connect", config)

    def test_connect_authenticates_and_validates_project(self):
        status, body, _ = self.connect()
        self.assertEqual(status, 200)
        self.assertTrue(body["connected"])
        self.assertTrue(body["readOnly"])
        self.assertEqual(body["inventoryScope"], "lab_shared")
        self.assertNotIn("test-key-secret", json.dumps(body))
        call = self.core.calls[-1]
        self.assertEqual(call[2], {"params": {"kind": "transactions", "project_id": "p1"}})
        self.assertTrue(call[3]["X-Operation-Id"].startswith("frontend_read_"))

    def test_bad_project_does_not_connect(self):
        status, body, _ = self.connect(projectId="p2")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "core_capability_error")
        self.assertEqual(self.request("GET", "/api/core/inventory")[0], 409)

    def test_bad_key_does_not_connect(self):
        status, body, _ = self.connect(apiKey="wrong")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "core_unauthorized")

    def test_inventory_mapping_and_explicit_shared_scope(self):
        self.connect()
        status, body, _ = self.request("GET", "/api/core/inventory")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"][0]["chemical_id"], "chem1")
        self.assertEqual(body["scope"], "lab_shared")
        self.assertEqual(self.core.calls[-1][2]["params"]["project_id"], "p1")

    def test_tasks_are_filtered_to_selected_project(self):
        self.connect()
        status, body, _ = self.request("GET", "/api/core/tasks")
        self.assertEqual(status, 200)
        self.assertEqual([row["task_id"] for row in body["tasks"]], ["one"])

    def test_search_is_scoped_and_preserves_core_candidates(self):
        self.connect()
        status, body, _ = self.request("GET", "/api/core/search?q=sample")
        self.assertEqual(status, 200)
        self.assertEqual(body["candidates"][0]["object_id"], "event1")
        self.assertEqual(self.core.calls[-1][2]["params"], {"project_ids": ["p1"], "text": "sample", "limit": 50})

    def test_search_rejects_extra_scope_and_duplicate_parameters(self):
        self.connect()
        for query in ("q=x&project_id=p2", "q=x&q=y", "q=", "q=" + "a" * 513):
            with self.subTest(query=query[:30]):
                self.assertEqual(self.request("GET", "/api/core/search?" + query)[0], 400)

    def test_unified_session_message_routes_to_core_write_capability(self):
        self.connect()
        status, body, _ = self.request(
            "POST", "/api/core/session/message",
            {"text": "S-1 实际取用多少？", "attachments": [], "sessionId": "sess-browser"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["agent"], "b_query")
        self.assertEqual(body["session_id"], "sess-browser")
        self.assertFalse(body["readOnly"])
        call = self.core.calls[-1]
        self.assertEqual(call[0], "POST")
        self.assertEqual(call[1], "/v2/capabilities/assistant.task.process/invoke")
        self.assertEqual(call[2]["params"]["project_id"], "p1")
        self.assertEqual(call[2]["params"]["text"], "S-1 实际取用多少？")
        self.assertEqual(call[2]["params"]["session_id"], "sess-browser")

    def test_session_message_rejects_bad_payload_and_before_connect(self):
        status, body, _ = self.request(
            "POST", "/api/core/session/message", {"text": "hello"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "not_connected")

        self.connect()
        for payload in (
            {},
            {"text": ""},
            {"text": "x" * 4001},
            {"text": "ok", "attachments": [{"data_b64": ""}]},
            {"text": "ok", "attachments": [{"data_b64": "x"}] * 6},
            {"text": "ok", "extra": True},
        ):
            with self.subTest(payload=str(payload)[:40]):
                status, body, _ = self.request(
                    "POST", "/api/core/session/message", payload
                )
                self.assertEqual(status, 400, body)

    def test_model_config_validation_and_status(self):
        with patch.object(bridge, "call_model", return_value="OK"):
            status, body, _ = self.request("POST", "/api/model/configure", {
                "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-5.3-flash",
                "apiKey": "test-model-key",
            })
            self.assertEqual(status, 200, body)
            self.assertTrue(body["configured"])
            self.assertEqual(body["test"], "ok")
            status, session, _ = self.request("GET", "/api/session")
            self.assertEqual(status, 200)
            self.assertTrue(session["modelConfigured"])
            self.assertEqual(session["modelName"], "glm-5.3-flash")
            status, status_body, _ = self.request("GET", "/api/model/status")
            self.assertTrue(status_body["configured"])
        status, body, _ = self.request("POST", "/api/model/configure", {
            "baseUrl": "http://example.com/api", "model": "x", "apiKey": "y",
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_model_config")
        status, body, _ = self.request("POST", "/api/model/clear", {})
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])

    def test_model_session_runs_agent_and_persists_receipts(self):
        self.connect()
        with patch.object(bridge, "call_model", return_value="OK"):
            self.assertEqual(self.request("POST", "/api/model/configure", {
                "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-5.3-flash",
                "apiKey": "test-model-key",
            })[0], 200)
        with patch.object(bridge, "_pi_node", return_value="node"), \
             patch.object(bridge, "run_pi_agent", return_value={
                 "text": "库存与样品记录已核对", "status": "completed",
                 "object_refs": [{"kind": "evidence_page", "id": "page1"}],
                 "events": [{"type": "tool_result_received", "tool": "inventory.query.list"}],
                 "delegations": [{"agent": "alice_inventory", "status": "completed"}],
             }) as run:
            status, body, _ = self.request(
                "POST", "/api/core/session/message",
                {"text": "S-1 的余量？", "attachments": []},
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "库存与样品记录已核对")
        self.assertTrue(body["modelUsed"])
        self.assertEqual(body["runtime"], "pi")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(len(body["delegations"]), 1)
        self.assertEqual(self.core.calls[-1][1], "/v2/capabilities/assistant.agent.finish/invoke")
        self.assertFalse(any("assistant.task.process" in c[1] for c in self.core.calls))
        self.assertEqual(run.call_args.args[4]["project_id"], "p1")
        self.assertNotIn("private-test-run-token", json.dumps(body))

    def test_agent_failure_is_persisted_without_replaying_legacy_write(self):
        self.connect()
        with patch.object(bridge, "call_model", return_value="OK"):
            self.request("POST", "/api/model/configure", {
                "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-5.3-flash",
                "apiKey": "test-model-key",
            })
        with patch.object(bridge, "_pi_node", return_value="node"), \
             patch.object(bridge, "run_pi_agent", side_effect=bridge.BridgeError(
                 "agent_runtime_failed", "timeout", 502,
             )):
            status, body, _ = self.request(
                "POST", "/api/core/session/message",
                {"text": "S-1 的余量？", "attachments": []},
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "failed")
        self.assertNotIn("error", body)
        self.assertEqual(body["runtime_error"], "agent_runtime_failed")
        self.assertIn("agent_runtime_failed", body["warnings"])
        self.assertFalse(any("assistant.task.process" in c[1] for c in self.core.calls))
        self.assertEqual(self.core.calls[-1][2]["params"]["result"]["status"], "failed")

    def test_agent_credentials_are_removed_before_persistence_and_response(self):
        self.connect()
        with patch.object(bridge, "call_model", return_value="OK"):
            self.request("POST", "/api/model/configure", {
                "baseUrl": "https://example.com/v1", "model": "test", "apiKey": "private-model-secret",
            })
        with patch.object(bridge, "_pi_node", return_value="node"), \
             patch.object(bridge, "run_pi_agent", return_value={
                 "text": "private-model-secret private-test-run-token", "status": "completed",
                 "delegations": [{"run_token": "child-private", "text": "safe"}],
             }):
            status, body, _ = self.request("POST", "/api/core/session/message", {"text": "检查记录"})
        self.assertEqual(status, 200, body)
        for result in (body, self.core.calls[-1][2]):
            encoded = json.dumps(result)
            self.assertNotIn("private-model-secret", encoded)
            self.assertNotIn("private-test-run-token", encoded)
            self.assertNotIn("child-private", encoded)

    def test_agent_attachment_staging_does_not_create_or_confirm_proposals(self):
        connection = bridge.CoreConnection("http://127.0.0.1:8876", "core-key", "p1")
        config = bridge.ModelConfig("https://example.com/v1", "test", "model-key")
        with patch.object(bridge, "invoke_internal_write", side_effect=[
            {"document_id": "doc1"}, {"page": {"page_id": "page1", "mime": "text/plain"}},
            {"status": "succeeded"},
        ]) as invoke:
            result = bridge.stage_agent_attachments(connection, config, [{
                "name": "note.txt", "mime": "text/plain", "data_b64": "c291cmNl",
            }])
        self.assertEqual([c.args[1] for c in invoke.call_args_list], [
            "evidence.document.create", "evidence.page.ingest", "evidence.transcribe.run",
        ])
        self.assertEqual(result["pages"][0]["page_id"], "page1")
        self.assertNotIn("c291cmNl", json.dumps(result))

    def test_agent_staging_distinguishes_unsaved_upload_from_saved_ocr_failure(self):
        connection = bridge.CoreConnection("http://127.0.0.1:8876", "core-key", "p1")
        config = bridge.ModelConfig("https://example.com/v1", "test", "model-key")
        with patch.object(bridge, "invoke_internal_write", side_effect=[
            {"document_id": "doc1"},
            bridge.BridgeError("unsupported_media", "cannot save", 415),
            {"page": {"page_id": "saved_image", "mime": "image/png"}},
        ]), patch.object(bridge, "vision_ocr_page", side_effect=bridge.ModelError("ocr_failed", "timeout")):
            staged = bridge.stage_agent_attachments(connection, config, [
                {"name": "bad.bin", "mime": "application/octet-stream", "data_b64": "AA=="},
                {"name": "note.png", "mime": "image/png", "data_b64": "AA=="},
            ])
        self.assertEqual(staged["failed_attachments"], [{"index": 0, "name": "bad.bin", "code": "unsupported_media"}])
        self.assertEqual(staged["pages"][0]["page_id"], "saved_image")
        self.assertTrue(any("note.png" in warning for warning in staged["warnings"]))

    def test_bootstrap_with_server_defaults_is_auto_connected(self):
        server = bridge.BridgeServer(
            ("127.0.0.1", 0),
            Path(self.directory.name),
            default_connection=bridge.CoreConnection(
                f"http://127.0.0.1:{self.core.server_port}", "test-key-secret", "p1"
            ),
            default_model=bridge.ModelConfig(
                "https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash", "test-key"
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.request("GET", "/api/session")
            response = client.getresponse()
            session = json.loads(response.read())
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            self.assertTrue(session["connected"])
            self.assertTrue(session["modelConfigured"])
            self.assertEqual(session["modelName"], "glm-5.3-flash")
            client.request("GET", "/api/bootstrap", headers={
                "Cookie": cookie,
                "X-ChemClaw-Session": session["csrfToken"],
            })
            response = client.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200, body)
            self.assertTrue(body["connected"])
            self.assertEqual(body["projectId"], "p1")
            self.assertTrue(body["features"]["task"])
            self.assertEqual(body["modelName"], "glm-5.3-flash")
            client.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_no_arbitrary_upstream_route_or_business_writes(self):
        self.connect()
        count = len(self.core.calls)
        for method, path in (("GET", "/api/core/../../v1/inventory/events"),
                             ("POST", "/api/core/inventory"),
                             ("POST", "/api/capabilities/inventory.adjustment.apply")):
            with self.subTest(path=path):
                self.assertIn(self.request(method, path, {} if method == "POST" else None)[0], (403, 404))
        self.assertEqual(len(self.core.calls), count)

    def test_internal_capability_allowlist(self):
        connection = bridge.CoreConnection("http://127.0.0.1:1", "secret", "p1")
        with self.assertRaises(bridge.BridgeError) as error:
            bridge.invoke_read(connection, "inventory.adjustment.apply", {})
        self.assertEqual(error.exception.status, 403)

    def test_session_cookie_and_token_both_required(self):
        self.connect()
        self.assertEqual(self.request("GET", "/api/core/inventory", cookie=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/core/inventory", csrf=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/core/inventory", headers={"X-ChemClaw-Session": "wrong"})[0], 403)

    def test_sessions_do_not_share_connection_or_csrf(self):
        self.connect()
        other_cookie, other_csrf = self.new_session()
        status = self.request("GET", "/api/core/inventory", headers={"Cookie": other_cookie, "X-ChemClaw-Session": other_csrf})[0]
        self.assertEqual(status, 409)
        status = self.request("GET", "/api/core/inventory", headers={"Cookie": other_cookie})[0]
        self.assertEqual(status, 403)

    def test_cross_origin_same_site_and_invalid_host_rejected(self):
        for headers in ({"Origin": "https://outside.example"},
                        {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site"},
                        {"Host": f"evil.example:{self.frontend.server_port}"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/connect", {}, headers=headers)[0], 403)

    def test_disconnect_drops_connection(self):
        self.connect()
        self.assertEqual(self.request("POST", "/api/disconnect", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/core/health")[0], 409)

    def test_no_redirects_followed(self):
        self.core.redirect = True
        status, body, _ = self.connect()
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "core_redirect")

    def test_remote_endpoints_and_path_userinfo_are_rejected(self):
        for endpoint in ("http://example.com:8000", "http://127.0.0.2:8000", "file:///tmp/core",
                         "http://user:pass@127.0.0.1:8000", "http://127.0.0.1:8000/path",
                         "http://127.0.0.1:8000?q=x", "http://127.0.0.1:8000#frag",
                         f"http://127.0.0.1:{self.frontend.server_port}"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.connect(endpoint=endpoint)[0], 400)

    def test_localhost_is_pinned_and_keys_cannot_inject_headers(self):
        config = {"endpoint": "http://localhost:8000/", "apiKey": "abc", "projectId": "p1"}
        self.assertEqual(bridge.validate_connection(config, 4173).endpoint, "http://127.0.0.1:8000")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_connection({**config, "apiKey": "abc\r\nX-Bad: hi"}, 4173)

    def test_body_limit(self):
        self.assertEqual(self.request("POST", "/api/connect", {"large": "x" * 9000})[0], 413)

    def test_static_files_and_traversal_boundary(self):
        status, body, headers = self.request("GET", "/", cookie=False, csrf=False)
        self.assertEqual(status, 200)
        self.assertIn("Demo", body)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/../dev_server.py", "/%2e%2e/dev_server.py", "/.openai/hosting.json", "/..%5cdev_server.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_server_refuses_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            bridge.BridgeServer(("0.0.0.0", 0), Path(self.directory.name))

    def test_static_browser_asset_mime_types_ignore_windows_registry(self):
        expected = {"app.js": "text/javascript; charset=utf-8",
                    "module.mjs": "text/javascript; charset=utf-8",
                    "styles.css": "text/css; charset=utf-8",
                    "notebook.svg": "image/svg+xml; charset=utf-8",
                    "display.ttf": "font/ttf", "display.woff2": "font/woff2"}
        with patch.object(bridge.mimetypes, "guess_type", return_value=("text/plain", None)):
            for name, content_type in expected.items():
                with self.subTest(name=name):
                    status, _, headers = self.request("GET", "/" + name, cookie=False, csrf=False)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Type"], content_type)
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")


    def test_extended_read_routes_and_feature_flags(self):
        status, body, _ = self.connect()
        self.assertEqual(status, 200)
        self.assertTrue(body["features"]["samples"])
        self.assertTrue(body["features"]["answer"])
        self.assertEqual(body["missingReadCapabilities"], [])
        self.assertTrue(all(cap.get("mutates") is False for cap in body["capabilities"]))
        checks = {
            "/api/core/samples": "samples",
            "/api/core/timeline?sample_id=smp1": "timeline",
            "/api/core/event?event_id=event1&history=1": "revisions",
            "/api/core/page?page_id=pg1": "page",
            "/api/core/answer?q=how%20much": "claims",
            "/api/core/resources": "resources",
            "/api/core/bookings?status=confirmed": "bookings",
            "/api/core/providers": "providers",
        }
        for path, key in checks.items():
            with self.subTest(path=path):
                status, data, _ = self.request("GET", path)
                self.assertEqual(status, 200, data)
                self.assertIn(key, data)
        # scope is enforced at the bridge, not just in the UI
        status, data, _ = self.request("GET", "/api/core/timeline?sample_id=x&sample_id=y")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_search")

    def test_extended_routes_are_not_available_before_connect(self):
        for path in ("/api/core/samples", "/api/core/answer?q=x",
                     "/api/core/resources", "/api/core/bookings"):
            with self.subTest(path=path):
                status, body, _ = self.request("GET", path)
                self.assertEqual(status, 409)
                self.assertEqual(body["error"]["code"], "not_connected")


if __name__ == "__main__":
    unittest.main()
