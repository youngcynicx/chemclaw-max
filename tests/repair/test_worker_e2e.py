"""R11/E01: real ingress -> resident worker -> Core -> outbox -> adapter.

This is an L1 local acceptance test. It starts from the channel ingress
ledger (the product entry), not from directly calling an internal business
function. No LLM/OCR is involved; that boundary is reported separately.
"""

from __future__ import annotations

from pathlib import Path

from chemclaw_lab import channels, db, worker


def _channel(lab):
    owner = lab.context("hum_worker_owner", memberships={"P1": "owner"})
    lab.context("hum_worker_sender", memberships={"P1": "member"})
    account = lab.invoke(
        owner,
        "channel.account.register",
        {"platform": "webhook_test", "display_name": "worker"},
        operation_id="op-worker-account",
    )["result"]
    lab.invoke(
        owner,
        "channel.identity.bind",
        {
            "channel_account_id": account["channel_account_id"],
            "platform_user_id": "wx-worker",
            "target_principal_id": "hum_worker_sender",
        },
        operation_id="op-worker-bind",
    )
    return account


def _ingest(lab, account, *, message_id, text, project_id="P1"):
    conn = db.connect(lab.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = channels.ingest_channel_event(
            conn,
            channel_account_id=account["channel_account_id"],
            event={
                "platform_message_id": message_id,
                "kind": "message",
                "platform_user_id": "wx-worker",
                "platform_thread_id": "dm-worker",
                "text": text,
                "project_id": project_id,
            },
            storage_root=lab.settings.storage_root,
        )
        conn.commit()
        return result
    finally:
        conn.close()


def test_inventory_text_entry_runs_through_worker_and_reply(lab):
    account = _channel(lab)
    inbound = _ingest(
        lab,
        account,
        message_id="msg-inv-1",
        text="Used 5 g of Sodium chloride",
    )
    assert inbound["status"] == "processed"
    assert lab.stock() == (100.0, 0)

    stats = worker.process_inbound_once(lab.db_path, services=lab.settings)
    assert stats.inbound_processed == 1
    assert lab.stock() == (95.0, 1)
    inbound_row = lab.rows(
        "SELECT status FROM inbound_events WHERE inbound_id = ?",
        (inbound["inbound_id"],),
    )[0]
    assert inbound_row["status"] == "processed"
    outbox = lab.rows(
        "SELECT status, payload_json FROM outbox WHERE aggregate_id = ?",
        (inbound["inbound_id"],),
    )
    assert len(outbox) == 1 and outbox[0]["status"] == "pending"

    delivered = worker.process_outbox_once(lab.db_path)
    assert delivered["delivered"] == 1
    assert lab.scalar(
        "SELECT status FROM outbox WHERE aggregate_id = ?", (inbound["inbound_id"],)
    ) == "sent"
    delivery = lab.rows(
        "SELECT state, channel_account_id, platform_thread_id FROM deliveries "
        "WHERE outbox_id = (SELECT outbox_id FROM outbox WHERE aggregate_id = ?)",
        (inbound["inbound_id"],),
    )[0]
    assert delivery["state"] == "delivered"
    assert delivery["channel_account_id"] == account["channel_account_id"]
    assert delivery["platform_thread_id"] == "dm-worker"


def test_worker_restart_does_not_double_apply_or_duplicate_reply(lab):
    account = _channel(lab)
    inbound = _ingest(
        lab, account, message_id="msg-inv-2", text="Used 5 g of Sodium chloride"
    )
    worker.process_inbound_once(lab.db_path, services=lab.settings)
    assert lab.stock() == (95.0, 1)
    assert lab.scalar(
        "SELECT COUNT(*) FROM outbox WHERE aggregate_id = ?", (inbound["inbound_id"],)
    ) == 1

    # Simulate a crash after the business commit but before the inbound row
    # was marked processed: the worker re-runs the same logical operation.
    lab.execute(
        "UPDATE inbound_events SET status = 'queued', processed_at = NULL "
        "WHERE inbound_id = ?",
        (inbound["inbound_id"],),
    )
    worker.process_inbound_once(lab.db_path, services=lab.settings)
    assert lab.stock() == (95.0, 1)
    assert lab.scalar(
        "SELECT COUNT(*) FROM inventory_transactions"
    ) == 1
    assert lab.scalar(
        "SELECT COUNT(*) FROM outbox WHERE aggregate_id = ?", (inbound["inbound_id"],)
    ) == 1


def test_revoked_identity_stops_queued_work(lab):
    account = _channel(lab)
    inbound = _ingest(
        lab, account, message_id="msg-revoked", text="Used 5 g of Sodium chloride"
    )
    # Revoke the binding BEFORE the worker consumes the queued inbound row.
    binding_id = lab.rows(
        "SELECT binding_id FROM channel_identities "
        "WHERE channel_account_id = ? AND platform_user_id = 'wx-worker'",
        (account["channel_account_id"],),
    )[0]["binding_id"]
    owner = lab.context("hum_worker_owner", memberships={"P1": "owner"})
    lab.invoke(
        owner,
        "channel.identity.revoke",
        {"binding_id": binding_id},
        operation_id="op-worker-revoke",
    )
    stats = worker.process_inbound_once(lab.db_path, services=lab.settings)
    assert stats.inbound_clarified == 1
    assert lab.stock() == (100.0, 0)
    row = lab.rows(
        "SELECT status, payload_json FROM inbound_events WHERE inbound_id = ?",
        (inbound["inbound_id"],),
    )[0]
    assert row["status"] == "failed"
    assert "identity_revoked" in row["payload_json"]


def test_query_text_entry_returns_a_cited_reply(lab):
    account = _channel(lab)
    lab.execute(
        "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_w', 'P1', 'S-1')"
    )
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["实际取用 0.5 g S-1"])
    lab.add_event(
        project_id="P1",
        sample_id="smp_w",
        amount=0.5,
        unit="g",
        raw_text="实际取用 0.5 g S-1",
        evidence_refs=[
            {
                "page_id": page["page_id"],
                "transcript_revision": 1,
                "block_id": transcript["block_ids"][0],
            }
        ],
    )
    inbound = _ingest(
        lab, account, message_id="msg-q-1", text="S-1 的用量是多少？"
    )
    worker.process_inbound_once(lab.db_path, services=lab.settings)
    reply_row = lab.rows(
        "SELECT payload_json FROM outbox WHERE aggregate_id = ?",
        (inbound["inbound_id"],),
    )[0]
    import json

    reply = json.loads(reply_row["payload_json"])
    assert "0.5" in reply["text"]
    assert "snapshot" in reply["text"] or "实际取用" in reply["text"]


def test_bad_business_message_is_archived_and_worker_continues(lab, monkeypatch):
    account = _channel(lab)
    bad = _ingest(
        lab, account, message_id="msg-bad", text="Used 5 g of Unobtainium"
    )
    good = _ingest(
        lab, account, message_id="msg-good", text="Used 5 g of Sodium chloride"
    )
    from chemclaw_lab import capabilities

    original = capabilities.invoke_capability

    def flaky(db_path, context, capability, params, **kwargs):
        if "Unobtainium" in str(params.get("raw_text", "")):
            raise capabilities.CapabilityError(
                "policy_reject", "simulated deterministic rejection"
            )
        return original(db_path, context, capability, params, **kwargs)

    monkeypatch.setattr(worker.capabilities, "invoke_capability", flaky)
    stats = worker.process_inbound_once(lab.db_path, services=lab.settings)
    assert stats.inbound_clarified == 1
    assert stats.inbound_processed == 1
    assert lab.stock() == (95.0, 1)
    assert lab.scalar(
        "SELECT status FROM inbound_events WHERE inbound_id = ?", (bad["inbound_id"],)
    ) == "failed"
    assert lab.scalar(
        "SELECT status FROM inbound_events WHERE inbound_id = ?", (good["inbound_id"],)
    ) == "processed"


def test_unclear_project_is_clarified_not_guessed(lab):
    account = _channel(lab)
    lab.execute(
        "INSERT INTO memberships (principal_id, project_id, role) "
        "VALUES ('hum_worker_sender', 'P2', 'member')"
    )
    inbound = _ingest(
        lab,
        account,
        message_id="msg-amb-1",
        text="Used 5 g of Sodium chloride",
        project_id="",
    )
    stats = worker.process_inbound_once(lab.db_path, services=lab.settings)
    assert stats.inbound_clarified == 1
    assert lab.stock() == (100.0, 0)
    reply = lab.rows(
        "SELECT payload_json FROM outbox WHERE aggregate_id = ?",
        (inbound["inbound_id"],),
    )[0]["payload_json"]
    assert "项目" in reply
