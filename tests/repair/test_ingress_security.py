"""R08/F10-F11: one attachment path, identity gate, edit/recall ownership."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import urllib.request

import pytest

from chemclaw_lab import channels, db
from chemclaw_lab.capabilities import CapabilityError
from chemclaw_lab.pnglite import solid_png


def _account(lab, *, platform="webhook_test", secret_ref=None):
    owner = lab.context("hum_ch_owner", memberships={"P1": "owner"})
    params = {"platform": platform, "display_name": "repair channel"}
    if secret_ref:
        params["verify_secret_ref"] = secret_ref
    return lab.invoke(
        owner,
        "channel.account.register",
        params,
        operation_id="op-ch-reg",
    )["result"]


def _bind(lab, account_id, platform_user_id, principal_id="hum_sender"):
    owner = lab.context("hum_ch_owner", memberships={"P1": "owner"})
    lab.context(principal_id, memberships={"P1": "member"})
    return lab.invoke(
        owner,
        "channel.identity.bind",
        {
            "channel_account_id": account_id,
            "platform_user_id": platform_user_id,
            "target_principal_id": principal_id,
        },
        operation_id=f"op-bind-{platform_user_id}",
    )["result"]


def _ingest_raw(lab, account_id, event):
    conn = db.connect(lab.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = channels.ingest_channel_event(
            conn,
            channel_account_id=account_id,
            event=event,
            storage_root=lab.settings.storage_root,
        )
        conn.commit()
        return result
    finally:
        conn.close()


def test_attachment_bytes_are_decoded_once_and_preserved(lab):
    account = _account(lab)
    _bind(lab, account["channel_account_id"], "wx-1")
    data = solid_png(3, 3, (10, 20, 30))
    result = _ingest_raw(
        lab,
        account["channel_account_id"],
        {
            "platform_message_id": "att-1",
            "kind": "message",
            "platform_user_id": "wx-1",
            "text": "photo",
            "attachments": [
                {
                    "name": "page.png",
                    "mime": "image/png",
                    "data_b64": base64.b64encode(data).decode("ascii"),
                }
            ],
        },
    )
    meta = json.loads(result["attachments_json"])[0]
    path = __import__("pathlib").Path(meta["path"])
    assert path.read_bytes() == data
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
    assert meta["bytes"] == len(data)
    assert meta["download_status"] == "ready"


def test_empty_and_invalid_attachments_never_become_empty_files(lab):
    account = _account(lab)
    _bind(lab, account["channel_account_id"], "wx-1", "hum_sender2")
    with pytest.raises(channels.ChannelError) as excinfo:
        _ingest_raw(
            lab,
            account["channel_account_id"],
            {
                "platform_message_id": "bad-1",
                "kind": "message",
                "platform_user_id": "wx-1",
                "attachments": [{"name": "x.png", "mime": "image/png", "data_b64": ""}],
            },
        )
    assert excinfo.value.code == "empty_attachment"
    with pytest.raises(channels.ChannelError) as excinfo:
        _ingest_raw(
            lab,
            account["channel_account_id"],
            {
                "platform_message_id": "bad-2",
                "kind": "message",
                "platform_user_id": "wx-1",
                "attachments": [{"name": "x.png", "mime": "image/png", "data_b64": "@@@@@"}],
            },
        )
    assert excinfo.value.code == "invalid_attachment_encoding"


def test_edit_and_recall_require_the_original_author(lab):
    account = _account(lab)
    _bind(lab, account["channel_account_id"], "wx-a", "hum_a")
    _bind(lab, account["channel_account_id"], "wx-b", "hum_b")
    original = _ingest_raw(
        lab,
        account["channel_account_id"],
        {
            "platform_message_id": "m-original",
            "kind": "message",
            "platform_user_id": "wx-a",
            "text": "A's message",
        },
    )
    edited = _ingest_raw(
        lab,
        account["channel_account_id"],
        {
            "platform_message_id": "m-edit-forged",
            "kind": "edited",
            "platform_user_id": "wx-b",
            "edits_original_id": original["platform_message_id"],
            "text": "forged edit",
        },
    )
    assert edited["status"] == "rejected"
    assert edited["error_code"] == "author_mismatch"
    recalled = _ingest_raw(
        lab,
        account["channel_account_id"],
        {
            "platform_message_id": "m-recall-forged",
            "kind": "recalled",
            "platform_user_id": "wx-b",
            "recall_of_message_id": original["platform_message_id"],
        },
    )
    assert recalled["status"] == "rejected"
    assert recalled["error_code"] == "author_mismatch"
    # The victim's original is still untouched.
    assert lab.scalar(
        "SELECT status FROM channel_messages WHERE platform_message_id = 'm-original'"
    ) == "processed"


def test_production_platform_without_official_verifier_is_rejected(lab):
    account = _account(lab, platform="qq_bot")
    with pytest.raises(channels.ChannelError) as excinfo:
        channels.verify_webhook_signature(
            account, body=b"{}", signature="deadbeef"
        )
    # Do not pretend a local HMAC is the official QQ signature scheme.
    assert excinfo.value.code == "not_implemented"
    assert excinfo.value.http_status == 501


def test_http_webhook_uses_the_same_attachment_decoder(lab, monkeypatch):
    from chemclaw_lab.api import ChemClawHTTPServer

    monkeypatch.setenv("REPAIR_WEBHOOK_SECRET", "top-secret")
    account = _account(lab, secret_ref="REPAIR_WEBHOOK_SECRET")
    _bind(lab, account["channel_account_id"], "wx-http", "hum_http")
    httpd = ChemClawHTTPServer(
        ("127.0.0.1", 0), lab.db_path, api_key=None, service_settings=lab.settings
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        data = solid_png(2, 2, (1, 2, 3))
        body = json.dumps(
            {
                "platform_message_id": "http-att-1",
                "kind": "message",
                "platform_user_id": "wx-http",
                "text": "http photo",
                "attachments": [
                    {
                        "name": "http.png",
                        "mime": "image/png",
                        "data_b64": base64.b64encode(data).decode("ascii"),
                    }
                ],
            }
        ).encode("utf-8")
        signature = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/v2/channels/"
            f"{account['channel_account_id']}/webhook",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Channel-Signature": signature,
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    meta = json.loads(result["attachments_json"])[0]
    assert __import__("pathlib").Path(meta["path"]).read_bytes() == data
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
