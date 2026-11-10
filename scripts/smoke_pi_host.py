"""S06 smoke: the REAL Pi host process (node + pi-agent-core 0.86.0) drives a
full run against the ChemClaw-Lab Core gateway with a one-time run token.

Flow: init db -> serve HTTP API -> load+enable manifests -> task.dispatch ->
spawn runtime/pi/server.mjs (PI_MOCK=1, AGENT_TOKEN, AGENT_ROLE) -> POST /run ->
assert the scripted tool call hit the gateway -> finish run (token revoked).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from chemclaw_lab import agents, auth, capabilities, db  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402

from _node_runtime import find_node  # noqa: E402  (script directory is sys.path[0])

NODE_EXE: str | None = None


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def main() -> int:
    global NODE_EXE
    NODE_EXE = find_node()
    tmp = Path(tempfile.mkdtemp(prefix="s06_pi_smoke_"))
    db_path = tmp / "smoke.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("INSERT INTO projects (project_id, name) VALUES ('proj_lab','Lab')")
    conn.execute(
        "INSERT INTO principals (principal_id, kind, display_name) VALUES ('prin_owner','human','Owner')"
    )
    conn.execute(
        "INSERT INTO memberships (principal_id, project_id, role) VALUES ('prin_owner','proj_lab','owner')"
    )
    conn.execute(
        """
        INSERT INTO chemicals (chemical_id, name, formula, amount, unit, threshold,
                               hazard_level, max_auto_delta, revision)
        VALUES ('chem_nacl', '氯化钠', 'NaCl', 500.0, 'g', 50.0, 'low', 100.0, 0)
        """
    )
    conn.commit()
    context = auth.PrincipalContext(
        principal_id="prin_owner", kind="human", display_name="Owner",
        memberships={"proj_lab": "owner"},
    )
    settings = ServiceSettings(storage_root=tmp / "storage", manifest_dir=REPO / "runtime" / "agents")
    conn.execute("BEGIN IMMEDIATE")
    summary = agents.load_registry(conn, REPO / "runtime" / "agents")
    agents.set_manifest_enabled(conn, agent_id="alice_inventory", version="1.0.0", enabled=True)
    conn.commit()
    loaded_ids = {item["agent_id"] for item in summary["loaded"]}
    expected_ids = {"alice_inventory", "b_capture", "b_query", "observer", "planner"}
    assert expected_ids <= loaded_ids, (expected_ids - loaded_ids, summary)
    identities = [(item["agent_id"], item["version"]) for item in summary["loaded"]]
    assert len(identities) == len(set(identities)), "duplicate agent@version loaded"
    assert not summary["invalid"], summary["invalid"]
    dispatch = agents.dispatch(
        conn, intent="record_inventory", context=context, project_id="proj_lab"
    )
    conn.commit()
    claimed = agents.claim_run(
        conn, run_id=dispatch["run_id"], worker_id="pi-smoke", lease_seconds=300
    )
    conn.commit()
    conn.close()
    print(f"[smoke] dispatched run={dispatch['run_id']} "
          f"epoch={claimed['claim_epoch']} trace={dispatch['trace_id']}")

    server = create_api_server(str(db_path), port=0, service_settings=settings)
    host, port = server.server_address
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gateway_url = f"http://{host}:{port}"

    pi_dir = REPO / "runtime" / "pi"
    env = {
        **os.environ,
        "GATEWAY_URL": gateway_url,
        "AGENT_TOKEN": dispatch["run_token"],
        "AGENT_ROLE": "alice_inventory",
        "PI_MOCK": "1",
    }
    process = subprocess.Popen(
        [NODE_EXE, str(pi_dir / "server.mjs")],
        cwd=str(pi_dir), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        pi_port = None
        deadline = time.time() + 30
        while time.time() < deadline:
            line = process.stdout.readline() if process.poll() is None else ""
            if line.startswith("PI_SERVER_PORT="):
                pi_port = int(line.strip().split("=", 1)[1])
                break
            if process.poll() is not None:
                raise RuntimeError(f"pi server exited: {process.stderr.read()}")
            time.sleep(0.1)
        assert pi_port, "pi server did not report its port"
        print(f"[smoke] pi server on 127.0.0.1:{pi_port} (mock)")

        run_result = http_json(
            "POST", f"http://127.0.0.1:{pi_port}/run",
            {"session_id": f"s-{uuid.uuid4().hex[:8]}", "message": "查询一下当前氯化钠库存。"},
        )
        assert not run_result.get("error"), run_result
        names = [call["name"] for call in run_result["toolCalls"]]
        assert "inventory.query.list" in names, names
        tool_result = next(c for c in run_result["toolCalls"] if c["name"] == "inventory.query.list")
        assert tool_result["result"]["action_status"] == "applied", tool_result
        rows = tool_result["result"]["result"]["items"]
        assert rows and rows[0]["chemical_id"] == "chem_nacl", rows
        print(f"[smoke] pi tool call OK: {names} -> {len(rows)} chemical row(s)")
        print(f"[smoke] assistant text: {run_result['text'][:120]}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        server.shutdown()
        server.server_close()

    conn = db.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        finished = agents.finish_run(
            conn, run_id=dispatch["run_id"], status="succeeded",
            claim_epoch=claimed["claim_epoch"],
            result={"outcome": "mock_run_ok"},
        )
        token_row = conn.execute(
            "SELECT status FROM agent_run_tokens WHERE run_id = ?", (dispatch["run_id"],)
        ).fetchone()
        used = conn.execute(
            "SELECT used_tool_calls FROM agent_run_tokens WHERE run_id = ?", (dispatch["run_id"],)
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    assert finished["status"] == "succeeded"
    assert token_row["status"] == "revoked", token_row
    assert used["used_tool_calls"] >= 1
    print(f"[smoke] run finished; token revoked; tool_calls_used={used['used_tool_calls']}")
    print("SMOKE OK")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
