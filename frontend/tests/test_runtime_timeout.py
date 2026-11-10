"""Verified Core manifest controls host/HTTP deadlines."""
import io
import gzip
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_server as bridge


class RuntimeTimeoutTests(unittest.TestCase):
    def test_manifest_timeout_controls_actual_process_env_and_http_with_legacy_fallback(self):
        connection = bridge.CoreConnection("http://127.0.0.1:1", "fixture-core-key", "proj_test")
        config = bridge.ModelConfig("https://provider.invalid/v1", "fixture-model", "fixture-model-key")
        # Resumed descriptors carry the same validated manifest and must not
        # silently fall back to the old hardcoded 240-second root deadline.
        for manifest, resumed, expected_ms, expected_http in [
            ({"timeout_s": 900}, False, "900000", 915),
            ({"timeout_s": 900}, True, "900000", 915),
            ({"timeout_s": 300}, False, "300000", 315),
            ({"timeout_s": 300}, True, "300000", 315),
            ({"timeout_s": 150.25}, False, "150250", 165.25),
            (None, False, "240000", 255),
            ({}, False, "240000", 255),
        ]:
            with self.subTest(manifest=manifest, resumed=resumed):
                started = {"run_token": "fixture-run-token", "session_id": "sess_fixture"}
                if manifest is not None:
                    started["manifest"] = manifest
                resume = {"runs": {"run_fixture": {"manifest": manifest}}} if resumed else None
                process = MagicMock()
                process.stdout = io.StringIO("PI_SERVER_PORT=43129\n")
                process.wait.return_value = 0
                response = MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps({"status": "completed", "text": "fixture reply"}).encode()
                opener = MagicMock()
                opener.open.return_value = response
                with patch.object(bridge, "_pi_node", return_value="fixture-node"), \
                     patch.object(bridge.subprocess, "Popen", return_value=process) as popen, \
                     patch.object(bridge, "build_opener", return_value=opener):
                    result = bridge.run_pi_agent(connection, config, started, "fixture", {}, durable=True, resume=resume)
                self.assertEqual(result["text"], "fixture reply")
                self.assertEqual(popen.call_args.kwargs["env"]["PI_RUN_TIMEOUT_MS"], expected_ms)
                self.assertEqual(opener.open.call_args.kwargs["timeout"], expected_http)
                body = json.loads(opener.open.call_args.args[0].data)
                self.assertIsNone(opener.open.call_args.args[0].get_header("Content-encoding"))
                self.assertEqual(body.get("resume"), resume)
                self.assertNotIn("fixture-core-key", json.dumps(body))
                self.assertNotIn("fixture-model-key", json.dumps(body))
                process.terminate.assert_called_once()

    def test_default_enabled_roles_share_the_900_second_run_budget(self):
        roles = ("orchestrator", "b_capture", "b_query", "alice_inventory", "planner")
        directory = Path(__file__).resolve().parents[2] / "runtime" / "agents"
        for role in roles:
            with self.subTest(role=role):
                manifest = json.loads((directory / (role + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(manifest["agent_id"], role)
                self.assertEqual(manifest["timeout_s"], 900)

    def test_large_unicode_resume_is_gzipped_losslessly_for_local_runtime_only(self):
        connection = bridge.CoreConnection("http://127.0.0.1:1", "fixture-core-key", "proj_test")
        config = bridge.ModelConfig("https://provider.invalid/v1", "fixture-model", "fixture-model-key")
        resume = {"checkpoints": {"run_fixture": {"messages": [{"role": "user", "content": "跨页原文与回执" * 30000}],
            "tool_calls": [{"name": "example.read", "state": "completed", "result": {"text": "已保存来源" * 10000}}]}}}
        process = MagicMock()
        process.stdout = io.StringIO("PI_SERVER_PORT=43129\n")
        process.wait.return_value = 0
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"completed","text":"fixture reply"}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(bridge, "_pi_node", return_value="fixture-node"), \
             patch.object(bridge.subprocess, "Popen", return_value=process), \
             patch.object(bridge, "build_opener", return_value=opener):
            bridge.run_pi_agent(connection, config, {"run_token": "fixture-run-token", "session_id": "sess_fixture"},
                "fixture", {}, durable=True, resume=resume)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Content-encoding"), "gzip")
        decoded = gzip.decompress(request.data)
        self.assertGreater(len(decoded), 256 * 1024)
        self.assertLess(len(request.data), len(decoded))
        self.assertEqual(json.loads(decoded)["resume"], resume)
        self.assertNotIn(b"fixture-model-key", decoded)
        self.assertNotIn(b"fixture-core-key", decoded)

    def test_invalid_supplied_manifest_timeout_cannot_launch_or_silently_fallback(self):
        connection = bridge.CoreConnection("http://127.0.0.1:1", "fixture-core-key", "proj_test")
        config = bridge.ModelConfig("https://provider.invalid/v1", "fixture-model", "fixture-model-key")
        for invalid in (True, 0, -1, "300", float("nan"), float("inf")):
            with self.subTest(timeout_type=type(invalid).__name__), \
                 patch.object(bridge, "_pi_node", return_value="fixture-node"), \
                 patch.object(bridge.subprocess, "Popen") as popen, \
                 self.assertRaises(bridge.BridgeError) as raised:
                bridge.run_pi_agent(connection, config, {"run_token": "fixture-run-token", "session_id": "sess_fixture",
                    "manifest": {"timeout_s": invalid}}, "fixture", {})
            self.assertEqual(raised.exception.code, "invalid_agent_timeout")
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
