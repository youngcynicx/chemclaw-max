"""Async bridge integration against a temporary real Core, with a controlled host.

No user database, runtime configuration, model service, or real credential is read.
Run: python -m unittest discover -s frontend/tests -p test_jobs_bridge.py -v
"""
from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO / "frontend"), str(REPO / "scripts")]
from chemclaw_lab import auth, db  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402
from configure_agent_runtime import configure, DEFAULT_ROLES  # noqa: E402
import dev_server as bridge  # noqa: E402


class JobsBridgeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Same isolated setup used by smoke_agent_orchestration.py; initialize once.
        cls.tmp = tempfile.TemporaryDirectory(prefix="chemclaw_job_bridge_")
        directory = Path(cls.tmp.name)
        cls.database = directory / "test.db"
        db.init_db(cls.database)
        key = secrets.token_urlsafe(24)
        conn = db.connect(cls.database)
        try:
            conn.execute("INSERT INTO projects(project_id,name) VALUES('proj_test','Async fixture')")
            auth.register_principal(conn, principal_id="human_test", kind="human", display_name="Test researcher")
            auth.grant_membership(conn, principal_id="human_test", project_id="proj_test", role="member")
            auth.register_credential(conn, credential_id="test_key", principal_id="human_test", secret=key)
            conn.commit()
        finally:
            conn.close()
        configure(cls.database, list(DEFAULT_ROLES))
        cls.core = create_api_server(str(cls.database), port=0,
            service_settings=ServiceSettings(storage_root=directory / "storage"))
        cls.connection = bridge.CoreConnection(f"http://127.0.0.1:{cls.core.server_port}", key, "proj_test")
        cls.model = bridge.ModelConfig("http://127.0.0.1:1/v1", "offline-fixture", "fixture-only-model-key")
        cls.controls = {}
        cls.controls_lock = threading.Lock()

        def fake_run(connection, model, started, message, context, **kwargs):
            with cls.controls_lock:
                control = cls.controls[message]
                control["calls"].append({"run_id": started["run_id"], "epoch": started["claim_epoch"],
                    "token": started["run_token"], "resume": kwargs.get("resume")})
                call_number = len(control["calls"])
            control["started"].set()
            cancel = kwargs["cancel_event"]
            if control.get("resume") and call_number > 1:
                return {"status": "completed", "text": "resumed successfully"}
            deadline = time.monotonic() + 15
            while not control["release"].wait(0.02):
                if cancel.is_set():
                    control["cancelled"].set()
                    # Deliberately return a late success; worker must discard it.
                    return {"status": "completed", "text": "late cancelled result"}
                if time.monotonic() > deadline:
                    raise OSError("test host timed out")
            return {"status": "completed", "text": "finished successfully",
                "knowledge_delivery": {"version": 1, "state": "not_written",
                    "notice": "No knowledge writes were requested in this fixture."}}

        cls.host_patch = patch.object(bridge, "run_pi_agent", side_effect=fake_run)
        cls.node_patch = patch.object(bridge, "_pi_node", return_value="fixture-node")
        cls.host_patch.start()
        cls.node_patch.start()
        cls.frontend = bridge.BridgeServer(("127.0.0.1", 0), REPO / "frontend" / "dist",
            default_connection=cls.connection, default_model=cls.model)
        for server in (cls.core, cls.frontend):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.headers = cls.new_browser()

    @classmethod
    def tearDownClass(cls):
        for control in cls.controls.values():
            control["release"].set()
        cls.frontend.shutdown()
        cls.frontend.server_close()
        cls.core.shutdown()
        cls.core.server_close()
        cls.node_patch.stop()
        cls.host_patch.stop()
        cls.tmp.cleanup()

    @classmethod
    def request(cls, method, path, payload=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", cls.frontend.server_port, timeout=10)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            client.request(method, path, body, headers or {})
            response = client.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            client.close()

    @classmethod
    def new_browser(cls):
        status, result, response_headers = cls.request("GET", "/api/session")
        assert status == 200, result
        cookie = response_headers["Set-Cookie"].split(";", 1)[0]
        return {"Cookie": cookie, "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{cls.frontend.server_port}",
            "X-ChemClaw-Session": result["csrfToken"]}

    def control(self, text, **flags):
        control = {"started": threading.Event(), "release": threading.Event(),
            "cancelled": threading.Event(), "calls": [], **flags}
        self.controls[text] = control
        self.addCleanup(control["release"].set)
        return control

    def post(self, payload):
        return self.request("POST", "/api/core/jobs", payload, self.headers)

    def read(self, job_id, headers=None):
        status, result, _ = self.request("GET", f"/api/core/jobs/{job_id}?after=0", headers=headers or self.headers)
        self.assertEqual(status, 200, result)
        return result["job"]

    def wait_status(self, job_id, expected):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job = self.read(job_id)
            if job["status"] == expected:
                return job
            time.sleep(0.04)
        self.fail(f"Job did not reach {expected}: {job['status']}")

    def test_01_fast_ack_dedup_conflicts_and_new_cookie_restore(self):
        text = "save isolated note"
        control = self.control(text)
        attachment = {"name": "note.txt", "mime": "text/plain",
            "data_b64": base64.b64encode(b"sample S-1; temperature unknown").decode()}
        payload = {"text": text, "requestId": "request-dedup", "attachments": [attachment]}
        began = time.monotonic()
        status, response, _ = self.post(payload)
        self.assertEqual(status, 202, response)
        self.assertLess(time.monotonic() - began, 3, "Acknowledgement waited for host completion")
        job_id = response["job"]["job_id"]
        self.assertFalse(control["release"].is_set())
        self.assertTrue(control["started"].wait(5), "Worker did not claim accepted job")
        status, duplicate, _ = self.post(payload)
        self.assertEqual(status, 202, duplicate)
        self.assertEqual(duplicate["job"]["job_id"], job_id)
        conn = db.connect(self.database)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_pages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM assistant_jobs WHERE request_id='request-dedup'").fetchone()[0], 1)
        finally:
            conn.close()
        for altered in ({**payload, "text": "different intent"},
                        {**payload, "attachments": [{**attachment, "data_b64": base64.b64encode(b"changed bytes").decode()}]}):
            status, conflict, _ = self.post(altered)
            self.assertEqual(status, 409, conflict)
            self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        fresh = self.new_browser()
        self.assertNotEqual(fresh["Cookie"], self.headers["Cookie"])
        status, listed, _ = self.request("GET", "/api/core/jobs", headers=fresh)
        self.assertEqual(status, 200, listed)
        self.assertIn(job_id, [job["job_id"] for job in listed["jobs"]])
        restored = self.read(job_id, fresh)
        self.assertEqual(restored["session_id"], response["job"]["session_id"])
        self.assertTrue(restored["events"])
        control["release"].set()
        finished = self.wait_status(job_id, "completed")
        self.assertEqual(finished["result"]["text"], "finished successfully")
        self.assertEqual(finished["result"]["knowledge_delivery"]["state"], "not_written")
        self.assertEqual(len(control["calls"]), 1)

    def test_02_cancel_aborts_host_and_rejects_late_finish(self):
        control = self.control("cancel isolated job")
        status, response, _ = self.post({"text": "cancel isolated job", "requestId": "request-cancel", "attachments": []})
        self.assertEqual(status, 202, response)
        job_id = response["job"]["job_id"]
        self.assertTrue(control["started"].wait(5))
        running = self.read(job_id)
        status, cancelled, _ = self.request("POST", f"/api/core/jobs/{job_id}/cancel", {}, self.headers)
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["job"]["status"], "cancelled")
        self.assertTrue(control["cancelled"].wait(3), "Cancellation never reached active host")
        with self.assertRaises(bridge.BridgeError):
            bridge.invoke_job(self.connection, "complete", job_id=job_id, worker_id=running["worker_id"],
                claim_epoch=running["claim_epoch"], result={"text": "late overwrite", "status": "completed"})
        self.assertEqual(self.read(job_id)["status"], "cancelled")
        self.assertNotIn("late", self.read(job_id)["result"]["text"])

    def test_03_worker_recreated_resumes_same_run_with_new_epoch(self):
        control = self.control("resume isolated job", resume=True)
        status, response, _ = self.post({"text": "resume isolated job", "requestId": "request-resume", "attachments": []})
        self.assertEqual(status, 202, response)
        job_id = response["job"]["job_id"]
        self.assertTrue(control["started"].wait(5))
        before = self.read(job_id)
        self.frontend.jobs.close()
        self.assertTrue(control["cancelled"].wait(3))
        conn = db.connect(self.database)
        try:
            conn.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now','-1 second') WHERE job_id=?", (job_id,))
            conn.execute("UPDATE agent_runs SET lease_expires_at=datetime('now','-1 second') WHERE run_id=?", (before["run_id"],))
            conn.commit()
        finally:
            conn.close()
        self.frontend.jobs = bridge.AgentJobWorker()
        self.frontend.jobs.register(self.connection, self.model)
        finished = self.wait_status(job_id, "completed")
        self.assertEqual(finished["run_id"], before["run_id"])
        self.assertGreater(finished["claim_epoch"], before["claim_epoch"])
        self.assertEqual(finished["result"]["text"], "resumed successfully")
        self.assertEqual(len(control["calls"]), 2)
        self.assertEqual(control["calls"][0]["run_id"], control["calls"][1]["run_id"])
        self.assertNotEqual(control["calls"][0]["token"], control["calls"][1]["token"])
        self.assertIn(before["run_id"], control["calls"][1]["resume"]["runs"])
        conn = db.connect(self.database)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_runs WHERE run_id=?", (before["run_id"],)).fetchone()[0], 1)
        finally:
            conn.close()

    def test_04_clear_model_pauses_running_and_queued_until_reconfigured(self):
        active = self.control("pause active job", resume=True)
        queued = self.control("pause queued job")
        status, first, _ = self.post({"text": "pause active job", "requestId": "request-pause-active", "attachments": []})
        self.assertEqual(status, 202, first)
        self.assertTrue(active["started"].wait(5))
        status, second, _ = self.post({"text": "pause queued job", "requestId": "request-pause-queued", "attachments": []})
        self.assertEqual(status, 202, second)
        status, cleared, _ = self.request("POST", "/api/model/clear", {}, self.headers)
        self.assertEqual(status, 200, cleared)
        self.assertTrue(active["cancelled"].wait(3))
        self.assertFalse(queued["started"].wait(0.3), "Cleared model still claimed queued work")
        status, history, _ = self.request("GET", "/api/core/jobs", headers=self.headers)
        self.assertEqual(status, 200, history)
        self.assertIn(first["job"]["job_id"], [job["job_id"] for job in history["jobs"]])
        # Shorten the abandoned lease instead of waiting a minute after pause.
        conn = db.connect(self.database)
        try:
            conn.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now','-1 second') WHERE job_id=?", (first["job"]["job_id"],))
            conn.commit()
        finally:
            conn.close()
        active["release"].set()
        queued["release"].set()
        with patch.object(bridge, "call_model", return_value="OK"):
            status, configured, _ = self.request("POST", "/api/model/configure", {
                "baseUrl": self.model.base_url, "model": self.model.model, "apiKey": self.model.api_key}, self.headers)
        self.assertEqual(status, 200, configured)
        self.wait_status(first["job"]["job_id"], "completed")
        self.wait_status(second["job"]["job_id"], "completed")


if __name__ == "__main__":
    unittest.main()
