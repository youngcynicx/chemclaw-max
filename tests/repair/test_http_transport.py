"""Real Node fetch bursts must reach authenticated Core reads without retries."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from chemclaw_lab import agents, db
from chemclaw_lab.api import ChemClawHTTPServer


NODE_BURST = r"""
let input = "";
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
const results = await Promise.all(Array.from({ length: config.count }, async (_, i) => {
  try {
    const response = await fetch(config.url, {
      method: "POST", signal: AbortSignal.timeout(8000),
      headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json",
        "X-Operation-Id": `transport-read-${i}` },
      body: JSON.stringify({ params: { event_id: config.event_id } }),
    });
    const payload = await response.json();
    return { ok: response.ok && payload.action_status === "applied"
        && payload.result.entity_id === config.event_id && payload.result.revision === 1,
      status: response.status };
  } catch (error) {
    // Deliberately omit error.message: transport errors can contain URL secrets.
    return { ok: false, name: error.name, code: error.cause?.code || null };
  }
}));
console.log(JSON.stringify({ requests: results.length,
  successes: results.filter(result => result.ok).length,
  failures: results.filter(result => !result.ok) }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for native fetch coverage")
def test_parallel_authenticated_reads_survive_brief_accept_pressure(lab):
    actor = lab.context("transport_reader", memberships={"P1": "member"})
    connection = db.connect(lab.db_path)
    try:
        registry = agents.load_registry(connection, Path(__file__).resolve().parents[2] / "runtime" / "agents")
        assert not registry["invalid"]
        for manifest in registry["loaded"]:
            agents.set_manifest_enabled(connection, agent_id=manifest["agent_id"],
                                        version=manifest["version"], enabled=True)
        connection.commit()
    finally:
        connection.close()
    run = lab.invoke(actor, "assistant.agent.start", {
        "project_id": "P1", "text": "Read a synthetic transport fixture", "session_id": "transport-test",
    }, operation_id="transport-start")["result"]
    event_id = lab.add_event(project_id="P1", raw_text="Synthetic transport observation")

    class BrieflyBusyServer(ChemClawHTTPServer):
        accepted = 0

        def get_request(self):
            # Model brief scheduling pressure on the accept loop, not a slow
            # business handler. All requests still use the real Core gateway.
            time.sleep(0.02)
            accepted = super().get_request()
            self.accepted += 1
            return accepted

    server = BrieflyBusyServer(("127.0.0.1", 0), lab.db_path, service_settings=lab.settings)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    count = 24
    try:
        # Keep the synthetic run token off command lines and failure messages.
        client = subprocess.run([shutil.which("node"), "-e", NODE_BURST],
            input=json.dumps({"url": f"http://127.0.0.1:{server.server_port}/v2/agent/tools/memory.event.read",
                              "token": run["run_token"], "event_id": event_id, "count": count}),
            capture_output=True, text=True, timeout=15)
        assert client.returncode == 0, "Local Node client did not return its sanitized report"
        report = json.loads(client.stdout)
        assert report == {"requests": count, "successes": count, "failures": []}, report
        assert server.accepted == count
        assert lab.scalar("SELECT used_tool_calls FROM agent_run_tokens WHERE run_id=?", (run["run_id"],)) == count
        assert lab.scalar("SELECT count(*) FROM event_revisions") == 1
        assert lab.scalar("SELECT count(*) FROM inventory_transactions") == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
