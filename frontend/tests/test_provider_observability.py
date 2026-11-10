"""No real provider traffic: preserve transport semantics and audit only allowlists."""
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_server as bridge


class ProviderObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "audit.jsonl"
        self.env = patch.dict(os.environ, {"CHEMCLAW_PROVIDER_AUDIT_PATH": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = bridge.ModelConfig("https://provider.example/v1", "custom", "fixture-private-key")

    def rows(self):
        return bridge._provider_rows([json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()])

    def request(self, payload, *, status=200, config=None):
        response = BytesIO(json.dumps(payload).encode())
        response.status = status
        with patch.object(bridge, "build_opener") as factory:
            factory.return_value.open.return_value = response
            result = bridge.call_model(config or self.config,
                [{"role": "user", "content": "fixture-private-prompt"}],
                audit_context={"page_id": "page_test", "run_id": "run_test"})
            self.assertEqual(factory.return_value.open.call_count, 1)
            return result

    def test_success_real_usage_and_secrets_are_not_recorded(self):
        result = self.request({"choices": [{"finish_reason": "stop", "message": {"content": "OK", "reasoning_content": "fixture-private-thinking"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29,
                "prompt_tokens_details": {"cached_tokens": 4}, "completion_tokens_details": {"reasoning_tokens": 3},
                "unknown": "fixture-private-key"}})
        self.assertEqual(result, "OK")
        row = self.rows()[0]
        self.assertEqual(row["usage"]["total_tokens"], 29)
        self.assertEqual(row["usage"]["cache_read_tokens"], 4)
        self.assertEqual(row["usage"]["reasoning_tokens"], 3)
        self.assertEqual(row["http_status"], 200)
        self.assertIsNone(row["cost"])
        self.assertEqual(row["run_id"], "run_test")
        for secret in ["fixture-private-key", "fixture-private-prompt", "fixture-private-thinking"]:
            self.assertNotIn(secret, self.path.read_text())
        events = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["phase"] for row in events], ["started", "finished"])
        self.assertEqual(events[0]["request_id"], events[1]["request_id"])
        self.assertIsNone(events[0]["usage"]["total_tokens"])

    def test_missing_usage_is_null_and_truncation_preserves_usage(self):
        self.request({"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]})
        self.assertTrue(all(value is None for value in self.rows()[0]["usage"].values()))
        with self.assertRaises(bridge.ModelError) as error:
            self.request({"choices": [{"finish_reason": "length", "message": {"content": "unfinished"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 12, "total_tokens": 20}})
        self.assertEqual(error.exception.code, "model_output_truncated")
        self.assertEqual(self.rows()[1]["usage"]["total_tokens"], 20)

    def test_quota_1113_distinct_from_rate_limit_and_persisted_on_error(self):
        for code, expected in [(1113, "provider_quota_exhausted"), ("rate_limit_exceeded", "provider_rate_limited")]:
            exc = HTTPError("https://provider.example", 429, "", {}, BytesIO(json.dumps({"error": {
                "code": code, "message": "fixture-private-key"}}).encode()))
            with patch.object(bridge, "build_opener") as factory:
                factory.return_value.open.side_effect = exc
                with self.assertRaises(bridge.ModelError) as caught:
                    bridge.call_model(self.config, [])
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(factory.return_value.open.call_count, 1)
                self.assertEqual(caught.exception.provider_observation["http_status"], 429)
        self.assertEqual(self.rows()[0]["error_class"], "provider_quota_exhausted")
        self.assertNotIn("fixture-private-key", self.path.read_text())

    def test_responses_incomplete_usage_and_explicit_provider_cost(self):
        config = bridge.replace(self.config, protocol="openai-responses")
        with self.assertRaises(bridge.ModelError):
            self.request({"status": "incomplete", "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11,
                "cost": 0.02, "cost_currency": "CNY"}}, config=config)
        self.assertEqual(self.rows()[0]["stop_reason"], "incomplete")
        self.assertEqual(self.rows()[0]["cost"], 0.02)

    def test_transport_failure_is_one_record_no_retry(self):
        with patch.object(bridge, "build_opener") as factory:
            factory.return_value.open.side_effect = URLError("fixture-private-key")
            with self.assertRaises(bridge.ModelError):
                bridge.call_model(self.config, [])
            self.assertEqual(factory.return_value.open.call_count, 1)
        self.assertEqual(self.rows()[0]["error_class"], "model_unreachable")
        self.assertIsNone(self.rows()[0]["usage"]["total_tokens"])

    def test_quota_result_never_claims_resumable_terminal_job_or_loses_refs(self):
        staged = {"pages": [{"page_id": "page_a"}], "object_refs": [{"kind": "evidence_page", "id": "page_a"}], "warnings": []}
        result = bridge._provider_quota_result(staged)
        self.assertTrue(result["needs_provider_quota"])
        self.assertNotIn("resumable", result)
        self.assertEqual(result["object_refs"], staged["object_refs"])
        self.assertEqual(result["classification"], "requires_resource_recovery")

    def test_audit_disk_failure_does_not_replay_or_lose_success(self):
        with patch.dict(os.environ, {"CHEMCLAW_PROVIDER_AUDIT_PATH": self.tmp.name}):
            self.assertEqual(self.request({"choices": [{"message": {"content": "OK"}}]}), "OK")

    def test_worker_quota_preserves_page_references_and_skips_pi_and_next_ocr(self):
        connection = bridge.CoreConnection("http://127.0.0.1:1", "fixture-core", "project_a")
        worker = bridge.AgentJobWorker.__new__(bridge.AgentJobWorker)
        worker.worker_id, worker.stopping = "fixture_worker", threading.Event()
        worker.lock, worker.contexts, worker.active = threading.Lock(), {connection: self.config}, {}
        pages = [{"page_id": "page_a", "mime": "image/png"}, {"page_id": "page_b", "mime": "image/png"}]
        refs = [{"kind": "evidence_page", "id": "page_a"}, {"kind": "evidence_page", "id": "page_b"}]
        claim = {"job": {"job_id": "job_a", "text": "organize", "context": {"pages": pages, "object_refs": refs}},
            "started": {"run_id": "run_a", "task_id": "task_a", "claim_epoch": 1, "run_token": "fixture-run-token"}}
        error = bridge.ModelError("provider_quota_exhausted", "quota")
        with patch.object(bridge, "invoke_read", return_value={"page": {"transcript": {}}}), \
                patch.object(bridge, "vision_ocr_page", side_effect=error) as ocr, \
                patch.object(bridge, "run_pi_agent") as pi, patch.object(bridge, "invoke_job") as invoke:
            worker._execute(connection, self.config, claim)
        self.assertEqual(ocr.call_count, 1)
        pi.assert_not_called()
        self.assertEqual(invoke.call_count, 1)
        args, kwargs = invoke.call_args
        self.assertEqual(args[1], "complete")
        self.assertEqual(kwargs["result"]["object_refs"], refs)
        self.assertEqual(kwargs["result"]["data"]["pages"], pages)
        self.assertTrue(kwargs["result"]["needs_provider_quota"])
        self.assertNotIn("resumable", kwargs["result"])

    def test_legacy_stage_saves_remaining_originals_without_next_paid_request(self):
        attachments = [{"name": f"image{i}.png", "mime": "image/png", "data_b64": "AA=="} for i in range(2)]
        connection = bridge.CoreConnection("http://127.0.0.1:1", "fixture-core", "project_a")
        def written(_connection, capability, _params, **_kwargs):
            if capability == "evidence.document.create":
                return {"document_id": "doc_a"}
            return {"page": {"page_id": "page_" + str(_params["page_order"]), "mime": "image/png"}}
        with patch.object(bridge, "invoke_internal_write", side_effect=written), \
                patch.object(bridge, "vision_ocr_page", side_effect=bridge.ModelError("provider_quota_exhausted", "quota")) as ocr:
            staged = bridge.stage_agent_attachments(connection, self.config, attachments)
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual(len(staged["pages"]), 2)

    def test_union_deduplicates_delegation_and_keeps_unknown_usage(self):
        row = {"request_id": "one", "retry_index": 0, "usage": bridge._provider_usage({"prompt_tokens": 5,
            "completion_tokens": 3, "total_tokens": 8}), "cost": None, "cost_currency": None}
        summary = bridge._provider_summary([row, row])
        self.assertEqual(summary["request_count"], 1)
        self.assertEqual(summary["usage"]["total_tokens"], 8)
        missing = {**row, "request_id": "two", "usage": bridge._provider_usage(None)}
        summary = bridge._provider_summary([row, missing])
        self.assertEqual(summary["request_count"], 2)
        self.assertIsNone(summary["usage"]["total_tokens"])
        self.assertIsNone(summary["cost"])

    def test_not_sent_excluded_from_actual_request_usage_cost_retry_and_quota_totals(self):
        finished = {"request_id": "finished", "phase": "finished", "retry_index": 0,
            "usage": bridge._provider_usage({"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}),
            "cost": 0.02, "cost_currency": "CNY", "error_class": None}
        started = {"request_id": "started", "phase": "started", "retry_index": 1,
            "usage": bridge._provider_usage(None), "cost": None, "cost_currency": None}
        not_sent = {"request_id": "not_sent", "phase": "not_sent", "retry_index": 99,
            "usage": bridge._provider_usage({"prompt_tokens": 999, "completion_tokens": 999, "total_tokens": 1998}),
            "cost": 100, "cost_currency": "USD", "error_class": "provider_quota_exhausted"}
        earlier = {**finished, "phase": "started", "usage": bridge._provider_usage(None), "cost": None}
        summary = bridge._provider_summary([earlier, not_sent, started, finished, not_sent])
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["request_intent_count"], 3)
        self.assertEqual(summary["not_sent_requests"], 1)
        self.assertEqual(summary["unfinished_requests"], 1)
        self.assertEqual(summary["retry_count"], 1)
        self.assertEqual(summary["usage_observed_requests"], 1)
        self.assertEqual(summary["quota_exhausted_requests"], 0)
        self.assertIsNone(summary["usage"]["total_tokens"])
        self.assertIsNone(summary["cost"])
        known = bridge._provider_summary([earlier, not_sent, finished, not_sent])
        self.assertEqual(known["request_count"], 1)
        self.assertEqual(known["usage"]["total_tokens"], 8)
        self.assertEqual(known["cost"], 0.02)
        self.assertEqual(known["cost_currency"], "CNY")
        self.assertEqual(known["retry_count"], 0)


if __name__ == "__main__":
    unittest.main()
