"""End-to-end frontend bridge <-> repaired Core read smoke.

This is an L1/local integration check: a real repaired Core HTTP server, a
real frontend BridgeServer, synthetic data in a temp SQLite database, and
HTTP requests through the same /api/core/* routes the browser uses.

No real lab database, model, OCR provider or messaging account is used.
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(FRONTEND))

from chemclaw_lab import auth, db  # noqa: E402
from chemclaw_lab.api import create_api_server  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402
import dev_server as bridge  # noqa: E402


def seed(db_path: Path) -> None:
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO projects (project_id, name) VALUES ('P1','alpha'), ('P2','beta')")
        conn.execute("INSERT INTO principals (principal_id, kind, display_name) VALUES ('hum_member','human','Member')")
        conn.execute("INSERT INTO memberships (principal_id, project_id, role) VALUES ('hum_member','P1','member')")
        conn.execute(
            "INSERT INTO auth_credentials (credential_id, principal_id, secret_hash) "
            "VALUES ('cred','hum_member',?)",
            (auth._hash_secret("front-secret"),),
        )
        conn.execute(
            "INSERT INTO chemicals (chemical_id,name,formula,location,amount,unit,threshold,"
            "hazard_level,max_auto_delta) VALUES ('chm_nacl','Sodium chloride','NaCl','A1',"
            "95.0,'g',10.0,'low',100.0)"
        )
        conn.execute(
            "INSERT INTO tasks (task_id,task_type,title,principal_id,project_id,status,revision) "
            "VALUES ('task_p1','agent.capture_page','P1 task','hum_member','P1','queued',0)"
        )
        conn.execute(
            "INSERT INTO tasks (task_id,task_type,title,principal_id,project_id,status,revision) "
            "VALUES ('task_p2','agent.capture_page','P2 task','hum_member','P2','queued',0)"
        )
        conn.execute(
            "INSERT INTO evidence_objects (evidence_id,project_id,storage_path,sha256,mime) "
            "VALUES ('evd1','P1','fixture.txt',?,'text/plain')",
            ("0" * 64,),
        )
        conn.execute(
            "INSERT INTO evidence_documents (document_id,project_id,notebook_code,title) "
            "VALUES ('doc1','P1','NB1','Notebook')"
        )
        conn.execute(
            "INSERT INTO evidence_pages (page_id,document_id,evidence_id,page_order,status) "
            "VALUES ('pg1','doc1','evd1',1,'transcribed')"
        )
        conn.execute(
            "INSERT INTO transcript_runs (run_id,page_id,provider,status,input_sha256,origin) "
            "VALUES ('run1','pg1','text_input','succeeded','h','ocr')"
        )
        conn.execute(
            "INSERT INTO transcript_blocks (block_id,run_id,block_kind,reading_order,block_json) "
            "VALUES ('b1','run1','paragraph',0,?)",
            (json.dumps({"text": "实际取用 0.5 g S-1"}, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO page_transcripts (page_id,active_run_id,active_revision) "
            "VALUES ('pg1','run1',1)"
        )
        conn.execute(
            "INSERT INTO samples (sample_id,project_id,label) VALUES ('smp1','P1','S-1')"
        )
        conn.execute(
            "INSERT INTO event_revisions (revision_id,entity_type,entity_id,revision,"
            "field_json,knowledge_as_of,evidence_refs_json,provenance_json,created_by) "
            "VALUES ('r1','experiment_event','evt1',1,?,CURRENT_TIMESTAMP,?, '{}','hum_member')",
            (
                json.dumps(
                    {
                        "event_kind": "operation",
                        "raw_text": "实际取用 0.5 g S-1",
                        "fields": {
                            "amount": {"normalized": 0.5, "unit": "g"},
                            "unit": {"raw": "g", "normalized": "g"},
                            "sample_label": {"normalized": "S-1"},
                        },
                        "sample_id": "smp1",
                        "page_id": "pg1",
                        "project_id": "P1",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    [{"page_id": "pg1", "transcript_revision": 1, "block_id": "b1"}]
                ),
            ),
        )
        conn.execute(
            "INSERT INTO resources (resource_id,project_id,name,kind,capacity,status) "
            "VALUES ('res1','P1','NMR','instrument',1,'active')"
        )
        conn.execute(
            "INSERT INTO resource_bookings (booking_id,resource_id,project_id,status,"
            "slot_start,slot_end,slot_start_utc,slot_end_utc,time_normalization) "
            "VALUES ('bkg1','res1','P1','confirmed','2026-10-18T09:00:00+08:00',"
            "'2026-10-18T10:00:00+08:00',1,2,'utc')"
        )
        conn.commit()
    finally:
        conn.close()


def call(client: http.client.HTTPConnection, method: str, path: str, *, headers=None, body=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    client.request(method, path, body=body, headers=headers or {})
    response = client.getresponse()
    raw = response.read().decode("utf-8")
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {"raw": raw}
    return response.status, payload, response.getheader("Set-Cookie")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="frontend_core_smoke_"))
    db_path = tmp / "core.db"
    seed(db_path)
    settings = ServiceSettings(storage_root=tmp / "storage")
    core = create_api_server(str(db_path), port=0, service_settings=settings)
    core_port = core.server_address[1]
    threading.Thread(target=core.serve_forever, daemon=True).start()
    front = bridge.BridgeServer(("127.0.0.1", 0), FRONTEND / "dist")
    front_port = front.server_port
    threading.Thread(target=front.serve_forever, daemon=True).start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", front_port, timeout=15)
        host = f"127.0.0.1:{front_port}"
        status, session, cookie_header = call(
            client, "GET", "/api/session", headers={"Host": host}
        )
        assert status == 200 and session["bridge"] == "chemclaw_read_bridge_v1"
        cookie = cookie_header.split(";", 1)[0]
        headers = {
            "Host": host,
            "Cookie": cookie,
            "X-ChemClaw-Session": session["csrfToken"],
            "Origin": f"http://{host}",
            "Content-Type": "application/json",
        }
        status, connected, _ = call(
            client,
            "POST",
            "/api/connect",
            headers=headers,
            body=json.dumps(
                {
                    "endpoint": f"http://127.0.0.1:{core_port}",
                    "apiKey": "front-secret",
                    "projectId": "P1",
                }
            ),
        )
        assert status == 200 and connected["connected"] is True, connected
        assert connected["coreVersion"] == "0.4.1", connected
        assert connected["features"]["samples"] is True
        assert connected["features"]["answer"] is True
        assert connected["features"]["resources"] is True
        assert connected["missingReadCapabilities"] == [], connected["missingReadCapabilities"]

        results = {}
        for name, path in {
            "health": "/api/core/health",
            "inventory": "/api/core/inventory",
            "tasks": "/api/core/tasks",
            "samples": "/api/core/samples",
            "timeline": "/api/core/timeline?sample_id=smp1",
            "event": "/api/core/event?event_id=evt1&history=1",
            "page": "/api/core/page?page_id=pg1",
            "search": "/api/core/search?q=%E5%AE%9E%E9%99%85",
            "answer": "/api/core/answer?q=S-1%20%E5%AE%9E%E9%99%85%E5%8F%96%E7%94%A8%E5%A4%9A%E5%B0%91%EF%BC%9F",
            "resources": "/api/core/resources",
            "bookings": "/api/core/bookings?status=confirmed",
            "providers": "/api/core/providers",
        }.items():
            status, payload, _ = call(client, "GET", path, headers=headers)
            assert status == 200, (name, status, payload)
            results[name] = payload

        assert results["health"]["health"]["version"] == "0.4.1"
        assert any(item["chemical_id"] == "chm_nacl" for item in results["inventory"]["items"])
        assert [task["task_id"] for task in results["tasks"]["tasks"]] == ["task_p1"]
        assert any(sample["sample_id"] == "smp1" for sample in results["samples"]["samples"])
        assert any(event["event_id"] == "evt1" for event in results["timeline"]["timeline"])
        assert results["event"]["revisions"][0]["revision"] == 1
        assert results["page"]["page"]["page_id"] == "pg1"
        assert any(c["object_id"] == "evt1" for c in results["search"]["candidates"])
        assert results["answer"]["status"] == "answered", results["answer"]
        assert any(c.get("value") == 0.5 for c in results["answer"]["claims"])
        assert any(r["resource_id"] == "res1" for r in results["resources"]["resources"])
        assert any(b["booking_id"] == "bkg1" for b in results["bookings"]["bookings"])
        assert isinstance(results["providers"].get("providers"), list)

        # Unified task session: natural language -> Core classifier -> B query.
        status, session_reply, _ = call(
            client,
            "POST",
            "/api/core/session/message",
            headers=headers,
            body=json.dumps({
                "text": "S-1 实际取用多少？",
                "attachments": [],
                "sessionId": "smoke-session",
            }, ensure_ascii=False),
        )
        assert status == 200, session_reply
        assert session_reply["agent"] == "b_query", session_reply
        assert "0.5" in session_reply["text"], session_reply

        # Read-only proof: inventory and event counts did not change.
        snapshot = {}
        conn = db.connect(db_path)
        try:
            for table in ("inventory_transactions", "event_revisions", "tasks", "resource_bookings"):
                snapshot[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()
        assert snapshot == {
            "inventory_transactions": 0,
            "event_revisions": 1,
            "tasks": 2,
            "resource_bookings": 1,
        }, snapshot
        print(json.dumps(
            {
                "status": "OK",
                "coreVersion": connected["coreVersion"],
                "features": connected["features"],
                "routes": {name: "ok" for name in results},
                "readOnlySnapshot": snapshot,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    finally:
        core.shutdown(); core.server_close()
        front.shutdown(); front.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
