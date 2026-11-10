"""A checkpoint transport fault must leave the same durable run recoverable.

Uses an isolated real Core; only the Pi child process and its local HTTP reply
are faked. No model service, live benchmark, or user configuration is accessed.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO / "frontend"), str(REPO / "scripts")]
from chemclaw_lab import auth, agents, db, orchestration  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402
from configure_agent_runtime import configure, DEFAULT_ROLES  # noqa: E402
import dev_server as bridge  # noqa: E402


class CheckpointTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="chemclaw_checkpoint_transport_")
        folder = Path(cls.tmp.name)
        cls.database = folder / "test.db"
        db.init_db(cls.database)
        key = secrets.token_urlsafe(24)
        conn = db.connect(cls.database)
        try:
            conn.execute("INSERT INTO projects(project_id,name) VALUES('proj_test','Checkpoint transport fixture')")
            auth.register_principal(conn, principal_id="human_test", kind="human", display_name="Test researcher")
            auth.grant_membership(conn, principal_id="human_test", project_id="proj_test", role="member")
            auth.register_credential(conn, credential_id="test_key", principal_id="human_test", secret=key)
            conn.commit()
        finally:
            conn.close()
        configure(cls.database, list(DEFAULT_ROLES))
        cls.core = create_api_server(str(cls.database), port=0,
            service_settings=ServiceSettings(storage_root=folder / "storage"))
        cls.connection = bridge.CoreConnection(f"http://127.0.0.1:{cls.core.server_port}", key, "proj_test")
        cls.model = bridge.ModelConfig("https://provider.invalid/v1", "fixture-model", "fixture-model-key")
        threading.Thread(target=cls.core.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.core.shutdown()
        cls.core.server_close()
        cls.tmp.cleanup()

    def worker(self, worker_id="fixture_worker"):
        # Run _execute deterministically without an automatic scheduler retry.
        worker = bridge.AgentJobWorker.__new__(bridge.AgentJobWorker)
        worker.worker_id = worker_id
        worker.stopping = threading.Event()
        worker.lock = threading.Lock()
        worker.contexts = {self.connection: self.model}
        worker.active = {}
        return worker

    def claim(self, identifier):
        submitted = bridge.invoke_job(self.connection, "submit", request_id=identifier,
            text="Checkpoint regression fixture", context={"pages": [], "object_refs": []})
        return bridge.invoke_job(self.connection, "claim", job_id=submitted["job"]["job_id"],
            worker_id="fixture_worker", lease_seconds=60)

    def host_reply(self, status, payload):
        process = MagicMock()
        process.stdout = io.StringIO("PI_SERVER_PORT=43129\n")
        process.wait.return_value = 0
        actual_opener = bridge.build_opener

        class RoutedOpener:
            def __init__(self, opener):
                self.opener = opener

            def open(self, request, *args, **kwargs):
                if request.full_url == "http://127.0.0.1:43129/run":
                    raw = json.dumps(payload).encode()
                    if status >= 400:
                        raise HTTPError(request.full_url, status, "fixture HTTP fault", {}, io.BytesIO(raw))
                    response = MagicMock()
                    response.__enter__.return_value.read.return_value = raw
                    return response
                return self.opener.open(request, *args, **kwargs)

        return process, patch.object(bridge, "_pi_node", return_value="fixture-node"), \
            patch.object(bridge.subprocess, "Popen", return_value=process), \
            patch.object(bridge, "build_opener", side_effect=lambda *handlers: RoutedOpener(actual_opener(*handlers)))

    def test_503_checkpoint_failure_is_typed_and_worker_keeps_run_receipts_for_resume(self):
        claim = self.claim("transport-checkpoint-503")
        job_id, run_id = claim["job"]["job_id"], claim["started"]["run_id"]
        checkpoint = {"run_id": run_id, "messages": [], "toolCalls": [{
            "name": "inventory.query.list", "operation_id": "fixture-receipt-before-fault",
            "state": "completed", "result": {"status": "applied", "items": []}}]}
        conn = db.connect(self.database)
        try:
            conn.execute("INSERT INTO assistant_runtime_checkpoints(run_id,checkpoint_json,claim_epoch) VALUES(?,?,?)",
                (run_id, json.dumps(checkpoint), claim["started"]["claim_epoch"]))
            conn.commit()
        finally:
            conn.close()
        failure = {"error": {"code": "checkpoint_unavailable", "message": "fixture-private-upstream-detail"}}
        process, node, popen, opener = self.host_reply(503, failure)
        with node, popen, opener:
            with self.assertRaises(bridge.BridgeError) as raised:
                bridge.run_pi_agent(self.connection, self.model, claim["started"], "fixture", {}, durable=True)
        self.assertEqual(raised.exception.code, "checkpoint_unavailable")
        self.assertEqual(raised.exception.status, 503)
        self.assertNotIn("fixture-private-upstream-detail", raised.exception.message)
        process.terminate.assert_called_once()

        process, node, popen, opener = self.host_reply(503, failure)
        worker = self.worker()
        with node, popen, opener:
            worker._execute(self.connection, self.model, claim)
        after = bridge.invoke_job(self.connection, "read", job_id=job_id)["job"]
        self.assertEqual(after["status"], "running")
        self.assertIsNone(after["result"], "Transport fault was incorrectly finalized as a business result")
        self.assertEqual(after["run_id"], run_id)
        self.assertEqual(worker.active, {})
        conn = db.connect(self.database)
        try:
            row = conn.execute("SELECT status,result_json FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertIsNone(row["result_json"])
            self.assertEqual(json.loads(conn.execute("SELECT checkpoint_json FROM assistant_runtime_checkpoints WHERE run_id=?", (run_id,)).fetchone()[0]), checkpoint)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM typed_messages WHERE message_type='assistant.agent.reply' AND correlation_id=?", (after["session_id"],)).fetchone()[0], 0)
            conn.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now','-1 second') WHERE job_id=?", (job_id,))
            conn.execute("UPDATE agent_runs SET lease_expires_at=datetime('now','-1 second') WHERE run_id=?", (run_id,))
            conn.commit()
        finally:
            conn.close()
        resumed = bridge.invoke_job(self.connection, "claim", job_id=job_id,
            worker_id="replacement_worker", lease_seconds=60)
        self.assertEqual(resumed["started"]["run_id"], run_id)
        self.assertGreater(resumed["started"]["claim_epoch"], claim["started"]["claim_epoch"])
        self.assertEqual(resumed["resume"]["checkpoints"][run_id], checkpoint)

    def test_200_business_failure_is_finalized_and_not_misclassified_as_transport_retry(self):
        claim = self.claim("transport-ordinary-business-failure")
        result = {"status": "failed", "text": "Business validation could not satisfy the requested task.",
            "warnings": ["business_validation_failed"]}
        _, node, popen, opener = self.host_reply(200, result)
        with node, popen, opener:
            self.worker()._execute(self.connection, self.model, claim)
        after = bridge.invoke_job(self.connection, "read", job_id=claim["job"]["job_id"])["job"]
        self.assertEqual(after["status"], "failed")
        self.assertEqual(after["result"]["text"], result["text"])
        self.assertIn("business_validation_failed", after["result"]["warnings"])
        self.assertIsNone(after["lease_expires_at"])
        with self.assertRaises(bridge.BridgeError):
            bridge.invoke_job(self.connection, "claim", job_id=after["job_id"],
                worker_id="replacement_worker", lease_seconds=60)

    def save_receipt(self, claim, *, child=False):
        run_id = claim["started"]["run_id"]
        sample_id = "saved_" + secrets.token_hex(6)
        receipt = {"name": "memory.sample.create", "toolCallId": "already-committed",
            "operation_id": run_id + "/already-committed", "state": "completed", "isError": False,
            "result": {"action_status": "applied", "object_refs": [{"kind": "sample", "id": sample_id}]}}
        checkpoint = {"version": 3, "run_id": run_id, "messages": [], "tool_calls": [receipt]}
        conn = db.connect(self.database)
        try:
            conn.execute("INSERT INTO samples(sample_id,project_id,label) VALUES(?,'proj_test',?)", (sample_id, sample_id))
            conn.execute("INSERT INTO assistant_runtime_checkpoints(run_id,checkpoint_json,claim_epoch) VALUES(?,?,?)",
                (run_id, json.dumps(checkpoint), claim["started"]["claim_epoch"]))
            child_run = None
            if child:
                token = agents.resolve_run_token(conn, claim["started"]["run_token"])
                child_run = orchestration.delegate(conn, token, {"agent_id": "b_query", "message": "Unfinished fixture child."})
            conn.commit()
        finally:
            conn.close()
        claim["resume"]["checkpoints"][run_id] = checkpoint
        return checkpoint, receipt, sample_id, child_run

    def test_nonretryable_checkpoint_limit_completes_partial_preserving_receipts_and_cancelling_child(self):
        claim = self.claim("terminal-checkpoint-limit")
        checkpoint, receipt, sample_id, child = self.save_receipt(claim, child=True)
        result = {"status": "partial", "text": "The checkpoint cannot fit; committed receipts are retained.",
            "error_code": "checkpoint_size_limit", "runtime_error": {"code": "checkpoint_size_limit", "retryable": False},
            "toolCalls": [receipt], "object_refs": receipt["result"]["object_refs"]}
        payload = {"error": {"code": "checkpoint_size_limit", "retryable": False}, "result": result}
        _, node, popen, opener = self.host_reply(422, payload)
        with node, popen, opener:
            self.worker()._execute(self.connection, self.model, claim)
        after = bridge.invoke_job(self.connection, "read", job_id=claim["job"]["job_id"])["job"]
        self.assertEqual(after["status"], "partial")
        self.assertEqual(after["result"]["error_code"], "checkpoint_size_limit")
        self.assertFalse(after["result"]["runtime_error"]["retryable"])
        self.assertEqual(after["result"]["toolCalls"], [receipt])
        self.assertIsNone(after["lease_expires_at"])
        conn = db.connect(self.database)
        try:
            self.assertEqual(conn.execute("SELECT label FROM samples WHERE sample_id=?", (sample_id,)).fetchone()[0], sample_id)
            self.assertEqual(json.loads(conn.execute("SELECT checkpoint_json FROM assistant_runtime_checkpoints WHERE run_id=?", (claim["started"]["run_id"],)).fetchone()[0]), checkpoint)
            self.assertEqual(conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (child["run_id"],)).fetchone()[0], "cancelled")
            self.assertEqual(conn.execute("SELECT status FROM agent_run_tokens WHERE run_id=?", (child["run_id"],)).fetchone()[0], "revoked")
        finally:
            conn.close()
        with self.assertRaises(bridge.BridgeError):
            bridge.invoke_job(self.connection, "claim", job_id=after["job_id"], worker_id="another_worker", lease_seconds=60)

    def test_deterministic_errors_without_valid_result_fail_once_and_reference_original_checkpoint(self):
        cases = ((422, "checkpoint_size_limit", None), (422, "checkpoint_invalid", {"status": "completed", "text": "invalid promotion"}),
                 (413, "runtime_request_size_limit", None))
        for status, code, invalid in cases:
            with self.subTest(code=code):
                claim = self.claim("terminal-fallback-" + code)
                checkpoint, _, sample_id, _ = self.save_receipt(claim)
                failure = {"error": {"code": code, "retryable": False, "message": "private-host-detail"}, "result": invalid}
                _, node, popen, opener = self.host_reply(status, failure)
                with node, popen, opener:
                    with self.assertRaises(bridge.TerminalRuntimeError) as raised:
                        bridge.run_pi_agent(self.connection, self.model, claim["started"], "fixture", {}, durable=True)
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(raised.exception.retryable)
                self.assertNotIn("private-host-detail", raised.exception.message)
                _, node, popen, opener = self.host_reply(status, failure)
                with node, popen, opener:
                    self.worker()._execute(self.connection, self.model, claim)
                after = bridge.invoke_job(self.connection, "read", job_id=claim["job"]["job_id"])["job"]
                self.assertEqual(after["status"], "failed")
                self.assertEqual(after["result"]["error_code"], code)
                self.assertEqual(after["result"]["runtime_error"]["retained_checkpoint_run_ids"], [claim["started"]["run_id"]])
                self.assertIsNone(after["lease_expires_at"])
                self.assertNotIn("private-host-detail", json.dumps(after["result"]))
                conn = db.connect(self.database)
                try:
                    self.assertIsNotNone(conn.execute("SELECT sample_id FROM samples WHERE sample_id=?", (sample_id,)).fetchone())
                    self.assertEqual(json.loads(conn.execute("SELECT checkpoint_json FROM assistant_runtime_checkpoints WHERE run_id=?", (claim["started"]["run_id"],)).fetchone()[0]), checkpoint)
                finally:
                    conn.close()

    def test_cancelled_or_reclaimed_epoch_cannot_be_completed_by_late_terminal_error(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                claim = self.claim("terminal-late-" + str(cancelled))
                job_id = claim["job"]["job_id"]
                if cancelled:
                    bridge.invoke_job(self.connection, "cancel", job_id=job_id)
                else:
                    conn = db.connect(self.database)
                    try:
                        conn.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now','-1 second') WHERE job_id=?", (job_id,))
                        conn.commit()
                    finally:
                        conn.close()
                    bridge.invoke_job(self.connection, "claim", job_id=job_id, worker_id="new_owner", lease_seconds=60)
                with patch.object(bridge, "run_pi_agent", side_effect=bridge.TerminalRuntimeError("checkpoint_invalid", "fixture", 422)):
                    self.worker()._execute(self.connection, self.model, claim)
                after = bridge.invoke_job(self.connection, "read", job_id=job_id)["job"]
                self.assertEqual(after["status"], "cancelled" if cancelled else "running")
                if not cancelled:
                    self.assertIsNone(after["result"])

    def test_rejected_terminal_summary_finishes_partial_with_only_bounded_core_references(self):
        claim = self.claim("terminal-summary-core-rejection")
        checkpoint, _, sample_id, child = self.save_receipt(claim, child=True)
        unsafe_ref = "sk-terminal-summary-invalid-0123456789"
        result = {"status": "partial", "text": "host-only-detail",
            "error_code": "checkpoint_size_limit",
            "runtime_error": {"code": "checkpoint_size_limit", "retryable": False},
            "object_refs": [{"kind": "sample", "id": unsafe_ref}],
            "data": {"host_detail": "must-not-survive"},
            "warnings": ["host-only-warning"], "toolCalls": []}
        payload = {"error": {"code": "checkpoint_size_limit", "retryable": False}, "result": result}
        actual_invoke, completions, rejections = bridge.invoke_job, [], []

        def observed_complete(connection, operation, **params):
            if operation == "complete":
                completions.append(params)
            try:
                return actual_invoke(connection, operation, **params)
            except bridge.BridgeError as exc:
                if operation == "complete":
                    rejections.append((getattr(exc, "core_status", None), getattr(exc, "core_code", None)))
                raise

        _, node, popen, opener = self.host_reply(422, payload)
        with node, popen as process_mock, opener, patch.object(bridge, "invoke_job", side_effect=observed_complete):
            self.worker()._execute(self.connection, self.model, claim)
        self.assertEqual(process_mock.call_count, 1, "A completion fallback must not replay the host/model")
        self.assertEqual(rejections, [(400, "schema_violation")])
        self.assertEqual(len(completions), 2)
        for name in ("job_id", "worker_id", "claim_epoch"):
            self.assertEqual(completions[0][name], completions[1][name])
        fallback = completions[1]["result"]
        encoded = json.dumps(fallback, ensure_ascii=False).encode()
        self.assertLess(len(encoded), 8192)
        self.assertEqual(fallback["object_refs"], [{"kind": "agent_run", "id": claim["started"]["run_id"]}])
        self.assertEqual(fallback["runtime_error"]["retained_checkpoint_run_ids"], [claim["started"]["run_id"]])
        for omitted in (unsafe_ref, "host-only-detail", "must-not-survive", "host-only-warning", self.model.model,
                        self.model.base_url):
            self.assertNotIn(omitted, encoded.decode())
        self.assertNotIn("model", fallback)
        self.assertNotIn("modelUsed", fallback)
        self.assertNotIn("toolCalls", fallback)
        after = actual_invoke(self.connection, "read", job_id=claim["job"]["job_id"])["job"]
        self.assertEqual(after["status"], "partial")
        self.assertIsNone(after["lease_expires_at"])
        self.assertFalse(after["result"]["runtime_error"]["retryable"])
        conn = db.connect(self.database)
        try:
            self.assertIsNotNone(conn.execute("SELECT sample_id FROM samples WHERE sample_id=?", (sample_id,)).fetchone())
            self.assertEqual(json.loads(conn.execute("SELECT checkpoint_json FROM assistant_runtime_checkpoints WHERE run_id=?",
                (claim["started"]["run_id"],)).fetchone()[0]), checkpoint)
            self.assertEqual(conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (child["run_id"],)).fetchone()[0], "cancelled")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM typed_messages WHERE message_type='assistant.agent.reply' AND correlation_id=?",
                (after["session_id"],)).fetchone()[0], 1)
        finally:
            conn.close()
        with self.assertRaises(bridge.BridgeError):
            actual_invoke(self.connection, "claim", job_id=after["job_id"], worker_id="replacement_worker", lease_seconds=60)

    def test_rejected_nonterminal_result_is_not_converted_to_terminal_fallback(self):
        for index, terminal in enumerate((None, {"code": "checkpoint_size_limit", "retryable": True},
                {"code": "unknown_failure", "retryable": False}, {"code": "checkpoint_size_limit", "retryable": 0})):
            with self.subTest(terminal=terminal):
                claim = self.claim("ordinary-summary-core-rejection-" + str(index))
                result = {"status": "partial", "text": "Ordinary host result.",
                    "object_refs": [{"kind": "sample", "id": "sk-invalid-reference-0123456789"}]}
                if terminal is not None:
                    result["runtime_error"] = terminal
                _, node, popen, opener = self.host_reply(200, result)
                with node, popen, opener, patch.object(bridge, "invoke_job", wraps=bridge.invoke_job) as calls:
                    self.worker()._execute(self.connection, self.model, claim)
                self.assertEqual(len([call for call in calls.call_args_list if call.args[1] == "complete"]), 1)
                after = bridge.invoke_job(self.connection, "read", job_id=claim["job"]["job_id"])["job"]
                self.assertEqual(after["status"], "running")
                self.assertIsNone(after["result"])

    def test_terminal_completion_does_not_retry_uncertain_transport_or_conflict(self):
        for index, (status, code) in enumerate(((503, "schema_violation"), (409, "schema_violation"),
                (400, "other_rejection"), (None, None))):
            with self.subTest(status=status, code=code):
                claim = self.claim("terminal-completion-uncertain-" + str(index))
                runtime = {"status": "partial", "text": "Terminal runtime result.",
                    "runtime_error": {"code": "checkpoint_invalid", "retryable": False}}
                actual_invoke, completions = bridge.invoke_job, []

                def refused_complete(connection, operation, **params):
                    if operation == "complete":
                        completions.append(params)
                        error = bridge.BridgeError("core_unreachable", "Uncertain completion response.", 502)
                        error.core_status, error.core_code = status, code
                        raise error
                    return actual_invoke(connection, operation, **params)

                with patch.object(bridge, "run_pi_agent", return_value=runtime), \
                        patch.object(bridge, "invoke_job", side_effect=refused_complete):
                    self.worker()._execute(self.connection, self.model, claim)
                self.assertEqual(len(completions), 1)
                after = actual_invoke(self.connection, "read", job_id=claim["job"]["job_id"])["job"]
                self.assertEqual(after["status"], "running")
                self.assertIsNone(after["result"])

    def test_safe_fallback_after_rejection_still_obeys_cancel_and_reclaimed_epoch(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                claim = self.claim("terminal-fallback-fence-" + str(cancelled))
                job_id = claim["job"]["job_id"]
                runtime = {"status": "partial", "text": "Terminal runtime result.",
                    "runtime_error": {"code": "checkpoint_invalid", "retryable": False},
                    "object_refs": [{"kind": "sample", "id": "sk-invalid-reference-0123456789"}]}
                actual_invoke, completions, rejections = bridge.invoke_job, [], []

                def fenced_complete(connection, operation, **params):
                    if operation == "complete":
                        completions.append(params)
                    try:
                        return actual_invoke(connection, operation, **params)
                    except bridge.BridgeError as exc:
                        if operation == "complete":
                            rejections.append(getattr(exc, "core_status", None))
                            if len(completions) == 1:
                                self.assertEqual(getattr(exc, "core_code", None), "schema_violation")
                                if cancelled:
                                    actual_invoke(connection, "cancel", job_id=job_id)
                                else:
                                    conn = db.connect(self.database)
                                    try:
                                        conn.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now','-1 second') WHERE job_id=?", (job_id,))
                                        conn.commit()
                                    finally:
                                        conn.close()
                                    actual_invoke(connection, "claim", job_id=job_id, worker_id="new_owner", lease_seconds=60)
                        raise

                with patch.object(bridge, "run_pi_agent", return_value=runtime), \
                        patch.object(bridge, "invoke_job", side_effect=fenced_complete):
                    self.worker()._execute(self.connection, self.model, claim)
                self.assertEqual(len(completions), 2)
                self.assertEqual(rejections, [400, 409])
                self.assertEqual(completions[0]["claim_epoch"], completions[1]["claim_epoch"])
                after = actual_invoke(self.connection, "read", job_id=job_id)["job"]
                self.assertEqual(after["status"], "cancelled" if cancelled else "running")
                if not cancelled:
                    self.assertEqual(after["worker_id"], "new_owner")
                    self.assertIsNone(after["result"])

    def test_terminal_fallback_bounds_checkpoint_locators_and_drops_invalid_identifiers(self):
        root_id = "run_" + "a" * 12
        checkpoints = {"run_" + format(index, "012x"): {} for index in range(1000)}
        checkpoints["sk-invalid-reference-0123456789"] = {"text": "untrusted"}
        fallback = bridge._terminal_runtime_fallback("checkpoint_invalid", {"run_id": root_id},
            {"checkpoints": checkpoints}, status="partial")
        ids = fallback["runtime_error"]["retained_checkpoint_run_ids"]
        self.assertEqual(ids[0], root_id)
        self.assertEqual(len(ids), 16)
        self.assertLess(len(json.dumps(fallback, ensure_ascii=False).encode()), 8192)
        self.assertNotIn("sk-", json.dumps(fallback))
        self.assertNotIn("untrusted", json.dumps(fallback))


if __name__ == "__main__":
    unittest.main()
