"""S03 tests: ingestion, transcription, coordinates, cloud gate, degradation.

Acceptance mapping (plan §9):
- Original hashes are re-verifiable .................... test_original_hash_recheck
- Rotation/crop/DPI/TOPLEFT-BOTTOMLEFT positioning ..... tests/coordinates round-trips + pipeline mapping
- Unclear decimals/units are kept as unknown .......... test_unclear_fragments_stay_unknown
- Ingestion and querying still work when recognition is down ... test_provider_down_* (offline ingestion/query)
- The configured scope of cloud processing is inspectable ........ test_cloud_scope_inspectable / test_cloud_gate_blocks
- Re-uploading the same page does not duplicate facts .. test_reupload_idempotent
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from chemclaw_lab import auth, capabilities, coordinates, db, ingest, ocr_port, transcribe  # noqa: E402
from chemclaw_lab import pnglite  # noqa: E402
from chemclaw_lab.ocr_port import CloudPolicy, FixtureRasterTranscriber  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "s03.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO projects (project_id, name) VALUES ('proj_lab', 'Lab')"
    )
    conn.commit()
    conn.close()
    storage_root = tmp_path / "storage"

    registry = ocr_port.default_registry()
    registry.register(FixtureRasterTranscriber(name="fixture_ocr"))
    registry.register(
        FixtureRasterTranscriber(name="fixture_ocr_no_geometry", provides_geometry=False)
    )
    settings = ServiceSettings(
        storage_root=storage_root,
        registry_factory=lambda: registry,
    )
    return {
        "db_path": str(db_path),
        "storage_root": storage_root,
        "settings": settings,
        "member": auth.PrincipalContext(
            principal_id="prin_member",
            kind="user",
            display_name="Member",
            memberships={"proj_lab": "member"},
        ),
        "viewer": auth.PrincipalContext(
            principal_id="prin_viewer",
            kind="user",
            display_name="Viewer",
            memberships={"proj_lab": "viewer"},
        ),
        "outsider": auth.PrincipalContext(
            principal_id="prin_outsider",
            kind="user",
            display_name="Outsider",
            memberships={},
        ),
    }


def _invoke(env, capability, params, context=None, operation_id="op-default"):
    return capabilities.invoke_capability(
        env["db_path"],
        context or env["member"],
        capability,
        params,
        operation_id=operation_id,
        services=env["settings"],
    )


def _png_bytes(width=60, height=40):
    return pnglite.solid_png(width, height, (240, 240, 245))


def _create_document(env):
    envelope = _invoke(
        env,
        "evidence.document.create",
        {
            "project_id": "proj_lab",
            "notebook_code": "NB-2026-01",
            "title": "Synthesis notebook",
            "author": "yu",
            "source_channel": "photo_upload",
        },
        operation_id="op-doc",
    )
    assert envelope["action_status"] == "applied"
    return envelope["result"]["document_id"]


def _ingest_page(env, document_id, content=None, page_order=1, operation_id="op-page"):
    content = content or _png_bytes()
    envelope = _invoke(
        env,
        "evidence.page.ingest",
        {
            "document_id": document_id,
            "page_order": page_order,
            "paper_page_number": str(10 + page_order),
            "filename": f"page_{page_order}.png",
            "content_data_url": "data:image/png;base64,"
            + base64.b64encode(content).decode("ascii"),
        },
        operation_id=operation_id,
    )
    return envelope


# ---------------------------------------------------------------------------
# coordinate math (acceptance: exact positioning after rotate/crop/DPI/origin)


def test_roundtrip_rotate90():
    rect = coordinates.Rect(10, 5, 20, 8)
    frame = (200, 100)
    steps = [{"op": "rotate90", "angle": 90}]
    out = coordinates.forward_map_rect(rect, steps, frame)
    geom = {"x": out.x, "y": out.y, "width": out.width, "height": out.height}
    cs = {"origin": "TOPLEFT", "units": "pixel", "frame_width": 100, "frame_height": 200}
    back = coordinates.map_geometry_to_original(geom, cs, steps, original_size=frame)
    assert coordinates.almost_equal(back["x"], rect.x)
    assert coordinates.almost_equal(back["y"], rect.y)
    assert coordinates.almost_equal(back["width"], rect.width)
    assert coordinates.almost_equal(back["height"], rect.height)


def test_roundtrip_rotate270_non_square():
    rect = coordinates.Rect(30, 12, 44, 6)
    frame = (300, 120)
    steps = [{"op": "rotate90", "angle": 270}]
    out = coordinates.forward_map_rect(rect, steps, frame)
    geom = {"x": out.x, "y": out.y, "width": out.width, "height": out.height}
    cs = {"origin": "TOPLEFT", "units": "pixel", "frame_width": 120, "frame_height": 300}
    back = coordinates.map_geometry_to_original(geom, cs, steps, original_size=frame)
    assert coordinates.almost_equal(back["x"], rect.x)
    assert coordinates.almost_equal(back["y"], rect.y)
    assert coordinates.almost_equal(back["width"], rect.width)
    assert coordinates.almost_equal(back["height"], rect.height)


def test_roundtrip_crop_then_rotate_then_dpi():
    frame = (400, 200)
    steps = [
        {"op": "crop", "x": 50, "y": 40, "width": 300, "height": 150},
        {"op": "rotate90", "angle": 90},
        {"op": "rescale_dpi", "source_dpi": 300, "target_dpi": 600},
    ]
    # A rect in the FINAL derived frame; map back to the original.
    derived_w, derived_h = coordinates.derived_frame_size(frame, steps)
    target = {"x": 10.0, "y": 20.0, "width": 30.0, "height": 15.0}
    cs = {
        "origin": "TOPLEFT",
        "units": "pixel",
        "frame_width": derived_w,
        "frame_height": derived_h,
    }
    back = coordinates.map_geometry_to_original(target, cs, steps, original_size=frame)
    # Forward-map the result; it must land on the same derived rect.
    out = coordinates.forward_map_rect(
        coordinates.Rect(back["x"], back["y"], back["width"], back["height"]), steps, frame
    )
    assert coordinates.almost_equal(out.x, target["x"])
    assert coordinates.almost_equal(out.y, target["y"])
    assert coordinates.almost_equal(out.width, target["width"])
    assert coordinates.almost_equal(out.height, target["height"])


def test_bottomleft_origin_mapping():
    frame = (100, 80)
    steps: list[dict] = []
    geom = {"x": 10, "y": 30, "width": 20, "height": 10}
    cs = {"origin": "BOTTOMLEFT", "units": "pixel", "frame_width": 100, "frame_height": 80}
    back = coordinates.map_geometry_to_original(geom, cs, steps, original_size=frame)
    # BOTTOMLEFT y=30 with height=10 → TOPLEFT y = 80-30-10 = 40
    assert coordinates.almost_equal(back["y"], 40)
    assert back["origin"] == "TOPLEFT"


def test_deform_expand_is_honestly_unmappable():
    geom = {"x": 1, "y": 1, "width": 2, "height": 2}
    cs = {"origin": "TOPLEFT", "units": "pixel", "frame_width": 10, "frame_height": 10}
    steps = [{"op": "deform_expand", "invertible": False}]
    with pytest.raises(coordinates.UnmappableGeometry):
        coordinates.map_geometry_to_original(geom, cs, steps, original_size=(10, 10))


# ---------------------------------------------------------------------------
# ingestion


def test_ingest_and_hash_recheck(env):
    document_id = _create_document(env)
    content = _png_bytes()
    envelope = _ingest_page(env, document_id, content)
    assert envelope["action_status"] == "applied"
    page = envelope["result"]["page"]
    assert page["sha256"] == hashlib.sha256(content).hexdigest()
    assert page["preview"]["materialized"] is True

    conn = db.connect(env["db_path"])
    try:
        report = ingest.verify_original_hash(conn, page["page_id"])
    finally:
        conn.close()
    assert report["match"] is True and report["file_present"] is True


def test_reupload_idempotent(env):
    document_id = _create_document(env)
    content = _png_bytes()
    first = _ingest_page(env, document_id, content, operation_id="op-a")
    again = _ingest_page(env, document_id, content, operation_id="op-b")
    assert first["result"]["page"]["page_id"] == again["result"]["page"]["page_id"]
    assert again["result"]["page"]["already_present"] is True
    assert again["warnings"] == ["page_already_present"]
    conn = db.connect(env["db_path"])
    try:
        count = conn.execute("SELECT COUNT(*) FROM evidence_pages").fetchone()[0]
        evidence_count = conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0]
    finally:
        conn.close()
    assert count == 1 and evidence_count == 1


def test_page_conflict_on_different_content(env):
    document_id = _create_document(env)
    _ingest_page(env, document_id, _png_bytes(), operation_id="op-a")
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _ingest_page(env, document_id, _png_bytes(width=61), operation_id="op-b")
    assert excinfo.value.code == "page_conflict"


def test_page_chain_links(env):
    document_id = _create_document(env)
    p1 = _ingest_page(env, document_id, page_order=1, operation_id="op-p1")["result"]["page"]
    p2 = _ingest_page(env, document_id, page_order=2, operation_id="op-p2")["result"]["page"]
    p3 = _ingest_page(env, document_id, page_order=3, operation_id="op-p3")["result"]["page"]
    # Re-read p1/p2: the chain is maintained as pages arrive.
    p1_now = _invoke(env, "evidence.page.read", {"page_id": p1["page_id"]}, operation_id="op-p1r")["result"]["page"]
    p2_now = _invoke(env, "evidence.page.read", {"page_id": p2["page_id"]}, operation_id="op-p2r")["result"]["page"]
    assert p1_now["prev_page_id"] is None and p1_now["next_page_id"] == p2["page_id"]
    assert p2_now["prev_page_id"] == p1["page_id"] and p2_now["next_page_id"] == p3["page_id"]
    assert p3["prev_page_id"] == p2["page_id"] and p3["next_page_id"] is None


def test_mime_sniffing_and_size_cap(env):
    with pytest.raises(ingest.IngestError) as type_err:
        ingest.sniff_mime(b"\x00\x01\xff\xfe-not-a-known-type\x00")
    assert type_err.value.code == "unsupported_file_type"
    with pytest.raises(ingest.IngestError) as cap_err:
        ingest.ingest_page(
            sqlite3.connect(":memory:"),
            document_id="x",
            content=b"x" * (ingest.MAX_ORIGINAL_BYTES + 1),
            filename="big.bin",
            page_order=1,
            storage_root=env["storage_root"],
        )
    assert cap_err.value.code == "file_too_large"


def test_path_boundary_violation(env, tmp_path):
    root = ingest.resolve_storage_root(env["storage_root"])
    with pytest.raises(ingest.IngestError) as excinfo:
        ingest._ensure_within(root, tmp_path.parent / "outside.png")
    assert excinfo.value.code == "path_boundary_violation"


# ---------------------------------------------------------------------------
# transcription pipeline


def _run_transcribe(env, page_id, provider="fixture_ocr", config=None, operation_id="op-run"):
    return _invoke(
        env,
        "evidence.transcribe.run",
        {"page_id": page_id, "provider": provider, "config": config or {}},
        operation_id=operation_id,
    )


def test_transcription_run_persists_blocks_and_records(env):
    document_id = _create_document(env)
    content = _png_bytes()
    page = _ingest_page(env, document_id, content, operation_id="op-p1")["result"]["page"]
    blocks = [
        {
            "block_id": "b-para-0",
            "block_kind": "paragraph",
            "reading_order": 0,
            "text": "Sample A-1 0.25 g stirred at 25 °C",
            "table_cell": None,
            "geometry": {"x": 5, "y": 6, "width": 40, "height": 8},
            "coordinate_system": {
                "origin": "TOPLEFT",
                "units": "pixel",
                "image_ref": "original",
                "frame_width": 60,
                "frame_height": 40,
                "dpi": 300,
            },
            "source_page": 1,
            "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer", "version": "0.3.0"},
            "quality_flags": [],
        },
        {
            "block_id": "b-para-1",
            "block_kind": "handwriting",
            "reading_order": 1,
            "text": "yield ?? %",
            "table_cell": None,
            "geometry": {"x": 5, "y": 20, "width": 30, "height": 8},
            "coordinate_system": {
                "origin": "BOTTOMLEFT",
                "units": "pixel",
                "image_ref": "original",
                "frame_width": 60,
                "frame_height": 40,
                "dpi": 300,
            },
            "source_page": 1,
            "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer", "version": "0.3.0"},
            "quality_flags": ["unclear_number"],
            "competing_readings": [
                {"candidate_text": "yield 78 %", "confidence": 0.4},
                {"candidate_text": "yield 73 %", "confidence": 0.4},
            ],
        },
    ]
    envelope = _run_transcribe(env, page["page_id"], config={"fixture_blocks": blocks})
    assert envelope["action_status"] == "applied"
    run = envelope["result"]
    assert run["status"] == "succeeded"
    assert run["input_sha256"] == hashlib.sha256(content).hexdigest()
    assert run["provider"] == "fixture_ocr"
    assert len(run["blocks"]) == 2
    assert run["raw_output"]["kind"] == "fixture"

    conn = db.connect(env["db_path"])
    try:
        fragments = transcribe.list_risk_fragments(conn, run_id=run["run_id"])
        readings = conn.execute(
            "SELECT candidate_text, chosen FROM competing_readings ORDER BY candidate_text"
        ).fetchall()
        page_row = conn.execute(
            "SELECT status FROM evidence_pages WHERE page_id = ?", (page["page_id"],)
        ).fetchone()
    finally:
        conn.close()
    by_kind = {f["kind"]: f for f in fragments}
    assert by_kind["sample_id"]["status"] == "ok"
    # The fixture has BOTH a clean number (0.25) and an unresolvable one
    # ("??"): the resolved fragment is ok, the unreadable one is unknown.
    number_statuses = {f["status"] for f in fragments if f["kind"] == "number"}
    assert number_statuses == {"ok", "unknown"}
    assert by_kind["unit"]["status"] == "ok"
    assert "negation" not in by_kind  # no negation tokens in fixture text
    assert len(readings) == 2 and all(r["chosen"] is None for r in readings)
    assert page_row["status"] == "transcribed"

    # Read path: page transcript with risk fragments
    read = _invoke(env, "evidence.page.read", {"page_id": page["page_id"]}, context=env["viewer"], operation_id="op-read")
    transcript = read["result"]["page"]["transcript"]
    assert transcript["active_revision"] == 1
    assert transcript["blocks"][0]["original_geometry"]["origin"] == "TOPLEFT"


def test_unclear_fragments_stay_unknown(env):
    document_id = _create_document(env)
    page = _ingest_page(env, document_id, operation_id="op-p1")["result"]["page"]
    blocks = [
        {
            "block_id": "b-unclear",
            "block_kind": "handwriting",
            "reading_order": 0,
            "text": "mass ??.?? g in ?? mL",
            "table_cell": None,
            "geometry": {"x": 1, "y": 2, "width": 10, "height": 5},
            "coordinate_system": {
                "origin": "TOPLEFT",
                "units": "pixel",
                "image_ref": "original",
                "frame_width": 60,
                "frame_height": 40,
            },
            "source_page": 1,
            "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer", "version": "0.3.0"},
            "quality_flags": ["unclear_number", "unclear_unit"],
        }
    ]
    run = _run_transcribe(env, page["page_id"], config={"fixture_blocks": blocks})["result"]
    conn = db.connect(env["db_path"])
    try:
        fragments = transcribe.list_risk_fragments(conn, run_id=run["run_id"], status="unknown")
    finally:
        conn.close()
    kinds = sorted(f["kind"] for f in fragments)
    assert "number" in kinds and "unit" in kinds
    assert all(f["normalized_value"] is None for f in fragments)


def test_untrusted_geometry_preserves_text_and_pending_fragment_review(env):
    """Usable text stays active; untrusted coordinates never confirm key fields."""
    document_id = _create_document(env)
    page = _ingest_page(env, document_id, operation_id="op-p1")["result"]["page"]
    blocks = [
        {
            "block_id": "b-no-geo",
            "block_kind": "paragraph",
            "reading_order": 0,
            "text": "Sample B-2 1.5 mg",
            "table_cell": None,
            "geometry": {"x": 1, "y": 1, "width": 9, "height": 4},
            "coordinate_system": {
                "origin": "TOPLEFT",
                "units": "pixel",
                "image_ref": "original",
                "frame_width": 60,
                "frame_height": 40,
            },
            "source_page": 1,
            "extractor": {"provider": "fixture_ocr_no_geometry", "model": None, "version": "0.3.0"},
            "quality_flags": [],
        }
    ]
    run = _run_transcribe(
        env, page["page_id"], provider="fixture_ocr_no_geometry", config={"fixture_blocks": blocks}
    )["result"]
    assert run["status"] == "succeeded"
    assert run["blocks"][0]["text"] == blocks[0]["text"]
    # Retain the supplied coordinates as untrusted metadata, without inventing
    # a new box or silently removing the source's quality warning.
    assert run["blocks"][0]["geometry"] == blocks[0]["geometry"]
    assert run["blocks"][0]["coordinate_system"] == blocks[0]["coordinate_system"]
    assert run["blocks"][0]["quality_flags"] == ["geometry_untrusted"]
    conn = db.connect(env["db_path"])
    try:
        fragments = transcribe.list_risk_fragments(conn, run_id=run["run_id"])
        page_status = conn.execute("SELECT status FROM evidence_pages WHERE page_id=?",
                                   (page["page_id"],)).fetchone()["status"]
    finally:
        conn.close()
    assert page_status == "transcribed"
    assert fragments and {f["kind"] for f in fragments} >= {"sample_id", "number", "unit"}
    assert all(f["status"] == "pending_confirm" and f["normalized_value"] is None for f in fragments)


def test_provider_down_ingestion_and_query_survive(env):
    """Acceptance: OCR downtime must not block ingestion or querying."""
    document_id = _create_document(env)
    page = _ingest_page(env, document_id, operation_id="op-p1")["result"]["page"]
    # Provider down → stable error envelope, AND the failed run is persisted
    # as an audit trail (retryable, plan §8).
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _run_transcribe(env, page["page_id"], provider="failing_ocr", operation_id="op-run1")
    assert excinfo.value.code == "provider_down"

    conn = db.connect(env["db_path"])
    try:
        runs = conn.execute(
            "SELECT run_id, status, error_code FROM transcript_runs WHERE page_id = ?",
            (page["page_id"],),
        ).fetchall()
        page_status = conn.execute(
            "SELECT status FROM evidence_pages WHERE page_id = ?", (page["page_id"],)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed" and runs[0]["error_code"] == "provider_down"
    assert page_status == "failed"

    # Query still works while the provider is down.
    read = _invoke(env, "evidence.page.read", {"page_id": page["page_id"]}, context=env["viewer"], operation_id="op-read")
    assert read["result"]["page"]["page_id"] == page["page_id"]
    assert read["result"]["page"]["transcript"]["active_revision"] == 0

    # Retrying the failing provider without changing anything still fails,
    # and the ORIGINAL stays queryable afterwards.
    with pytest.raises(capabilities.CapabilityError):
        _invoke(env, "evidence.transcription.retry", {"run_id": runs[0]["run_id"]}, operation_id="op-retry1")
    read2 = _invoke(env, "evidence.page.read", {"page_id": page["page_id"]}, context=env["viewer"], operation_id="op-read2")
    assert read2["action_status"] == "applied"

    # A second provider attempt lands as a NEW run row (append-only).
    ok_run = _run_transcribe(env, page["page_id"], provider="fixture_ocr", config={"fixture_blocks": []}, operation_id="op-run2")
    assert ok_run["result"]["run_id"] != runs[0]["run_id"]
    conn = db.connect(env["db_path"])
    try:
        total_runs = conn.execute(
            "SELECT COUNT(*) FROM transcript_runs WHERE page_id = ?", (page["page_id"],)
        ).fetchone()[0]
        page_status_after = conn.execute(
            "SELECT status FROM evidence_pages WHERE page_id = ?", (page["page_id"],)
        ).fetchone()["status"]
    finally:
        conn.close()
    # 3 runs: initial failure + failed retry + successful second provider.
    assert total_runs == 3
    assert page_status_after == "transcribed"


def test_human_correction_creates_revision(env):
    document_id = _create_document(env)
    page = _ingest_page(env, document_id, operation_id="op-p1")["result"]["page"]
    _run_transcribe(env, page["page_id"], config={"fixture_blocks": []}, operation_id="op-run1")
    corrected_blocks = [
        {
            "block_id": "b-human-0",
            "block_kind": "handwriting",
            "reading_order": 0,
            "text": "Sample A-1 mass 0.2503 g",
            "table_cell": None,
            "geometry": {"x": 5, "y": 6, "width": 40, "height": 8},
            "coordinate_system": {
                "origin": "TOPLEFT",
                "units": "pixel",
                "image_ref": "original",
                "frame_width": 60,
                "frame_height": 40,
            },
            "source_page": 1,
            "extractor": {"provider": "human_transcriber", "model": None, "version": "1.0.0"},
            "quality_flags": [],
        }
    ]
    corrected = _invoke(
        env,
        "evidence.transcript.correct",
        {
            "page_id": page["page_id"],
            "blocks": corrected_blocks,
            "expected_revision": 1,
            "reason": "OCR misread the third decimal",
        },
        operation_id="op-fix",
    )
    assert corrected["action_status"] == "applied"
    assert corrected["result"]["origin"] == "human_correction"

    read = _invoke(env, "evidence.page.read", {"page_id": page["page_id"]}, operation_id="op-read")
    assert read["result"]["page"]["transcript"]["active_revision"] == 2
    assert read["result"]["page"]["transcript"]["provider"] == "human_transcriber"

    # Old runs remain queryable (append-only, no overwrite).
    conn = db.connect(env["db_path"])
    try:
        run_count = conn.execute(
            "SELECT COUNT(*) FROM transcript_runs WHERE page_id = ?", (page["page_id"],)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT message_type, payload_json FROM outbox WHERE message_type = 'transcript.revised'"
        ).fetchall()
    finally:
        conn.close()
    assert run_count == 2
    assert len(events) >= 2

    # Stale expected_revision is rejected (optimistic concurrency).
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(
            env,
            "evidence.transcript.correct",
            {
                "page_id": page["page_id"],
                "blocks": corrected_blocks,
                "expected_revision": 1,
                "reason": "stale",
            },
            operation_id="op-fix2",
        )
    assert excinfo.value.code == "revision_conflict"


def test_derived_version_transform_roundtrip(env):
    """Materialized rotate/crop artifacts keep a verifiable transform chain."""
    document_id = _create_document(env)
    content = pnglite.stamp_region_png(
        60, 40, (240, 240, 245), rect=(10, 10, 20, 12), ink=(20, 20, 20)
    )
    page = _ingest_page(env, document_id, content, operation_id="op-p1")["result"]["page"]

    conn = db.connect(env["db_path"])
    try:
        rotated = ingest.create_derived_version(
            conn,
            page_id=page["page_id"],
            storage_root=env["storage_root"],
            transform={"steps": [{"op": "rotate90", "angle": 90}]},
        )
        cropped = ingest.create_derived_version(
            conn,
            page_id=page["page_id"],
            storage_root=env["storage_root"],
            transform={"steps": [{"op": "crop", "x": 5, "y": 5, "width": 30, "height": 20}]},
        )
        conn.commit()
    finally:
        conn.close()
    assert rotated["width"] == 40 and rotated["height"] == 60
    assert cropped["width"] == 30 and cropped["height"] == 20

    # Pixels really moved: the ink rectangle survives in the rotated artifact.
    rotated_bytes = Path(_artifact_path(env, rotated))
    w, h, rows = pnglite.decode_png(rotated_bytes.read_bytes())
    assert (w, h) == (40, 60)
    dark = [
        (x, y)
        for y in range(h)
        for x in range(w)
        if rows[y][x * 3] < 50
    ]
    assert dark, "rotated artifact lost the ink region"


def _artifact_path(env, artifact):
    conn = db.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT storage_path FROM derived_artifacts WHERE artifact_id = ?",
            (artifact["artifact_id"],),
        ).fetchone()
    finally:
        conn.close()
    return row["storage_path"]


def test_geometry_mapped_through_derived_chain(env):
    """Blocks recognized on a derived (rotated) image map back to the original."""
    document_id = _create_document(env)
    content = _png_bytes()
    page = _ingest_page(env, document_id, content, operation_id="op-p1")["result"]["page"]
    conn = db.connect(env["db_path"])
    try:
        derived = ingest.create_derived_version(
            conn,
            page_id=page["page_id"],
            storage_root=env["storage_root"],
            transform={"steps": [{"op": "rotate90", "angle": 90}]},
        )
        conn.commit()
    finally:
        conn.close()
    derived_w, derived_h = derived["width"], derived["height"]
    blocks = [
        {
            "block_id": "b-derived",
            "block_kind": "paragraph",
            "reading_order": 0,
            "text": "Sample C-1 2.0 mg",
            "table_cell": None,
            "geometry": {"x": 10, "y": 12, "width": 20, "height": 6},
            "coordinate_system": {
                "origin": "TOPLEFT",
                "units": "pixel",
                "image_ref": f"derived:{derived['artifact_id']}",
                "frame_width": derived_w,
                "frame_height": derived_h,
                "transform": {"steps": [{"op": "rotate90", "angle": 90}]},
            },
            "source_page": 1,
            "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer", "version": "0.3.0"},
            "quality_flags": [],
        }
    ]
    run = _run_transcribe(env, page["page_id"], config={"fixture_blocks": blocks})["result"]
    conn = db.connect(env["db_path"])
    try:
        transcript = transcribe.get_page_transcript(conn, page_id=page["page_id"], original_size=(60, 40))
    finally:
        conn.close()
    block = transcript["blocks"][0]
    assert block["positioning"]["mode"] == "mapped"
    og = block["original_geometry"]
    # Forward round-trip must reproduce the derived-frame coordinates.
    out = coordinates.forward_map_rect(
        coordinates.Rect(og["x"], og["y"], og["width"], og["height"]),
        [{"op": "rotate90", "angle": 90}],
        (60, 40),
    )
    assert coordinates.almost_equal(out.x, 10)
    assert coordinates.almost_equal(out.y, 12)
    _ = run


def test_scope_enforcement_on_evidence(env):
    document_id = _create_document(env)
    page = _ingest_page(env, document_id, operation_id="op-p1")["result"]["page"]
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "evidence.page.read", {"page_id": page["page_id"]}, context=env["outsider"], operation_id="op-x1")
    assert excinfo.value.code == "forbidden_capability"  # outsider has no role at all
    # Viewer can read but not run transcription.
    read = _invoke(env, "evidence.page.read", {"page_id": page["page_id"]}, context=env["viewer"], operation_id="op-x2")
    assert read["action_status"] == "applied"
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(
            env,
            "evidence.transcribe.run",
            {"page_id": page["page_id"], "provider": "fixture_ocr"},
            context=env["viewer"],
            operation_id="op-x3",
        )
    assert excinfo.value.code == "forbidden_capability"


def test_cloud_scope_inspectable_and_gated(env):
    listing = _invoke(env, "evidence.provider.list", {}, context=env["viewer"], operation_id="op-plist")
    result = listing["result"]
    assert result["cloud_policy"]["allow_cloud"] is False
    provider_names = {p["name"] for p in result["providers"]}
    assert "cloud_vision_stub" in provider_names

    document_id = _create_document(env)
    page = _ingest_page(env, document_id, operation_id="op-p1")["result"]["page"]
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _run_transcribe(env, page["page_id"], provider="cloud_vision_stub", operation_id="op-cloud1")
    assert excinfo.value.code == "cloud_disabled"

    # Operator explicitly allows the provider with a declared scope — then the
    # scope is inspectable and the gate opens (stub still needs 'approved').
    settings = env["settings"]
    settings.cloud_policy = CloudPolicy(
        allow_cloud=True,
        allowed_data_classes=("page_images",),
        providers=("cloud_vision_stub",),
        notes="test scope",
    )
    listing2 = _invoke(env, "evidence.provider.list", {}, context=env["viewer"], operation_id="op-plist2")
    assert listing2["result"]["cloud_policy"]["allow_cloud"] is True
    assert listing2["result"]["cloud_policy"]["providers"] == ["cloud_vision_stub"]


def test_text_upload_transcription(env):
    document_id = _create_document(env)
    text = b"Note: sample D-1 3.2 mL added at 09:00 2026-10-01\nnot verified\n"
    envelope = _invoke(
        env,
        "evidence.page.ingest",
        {
            "document_id": document_id,
            "page_order": 1,
            "filename": "notes.txt",
            "content_data_url": "data:text/plain;base64,"
            + base64.b64encode(text).decode("ascii"),
        },
        operation_id="op-textpage",
    )
    page = envelope["result"]["page"]
    assert page["mime"] == "text/plain"
    run = _run_transcribe(env, page["page_id"], provider="text_input", operation_id="op-textrun")["result"]
    assert run["status"] == "succeeded"
    assert len(run["blocks"]) == 2
    texts = [b["text"] for b in run["blocks"]]
    assert any("not" in t for t in texts)
    conn = db.connect(env["db_path"])
    try:
        fragments = transcribe.list_risk_fragments(conn, run_id=run["run_id"])
    finally:
        conn.close()
    kinds = {f["kind"] for f in fragments}
    assert "sample_id" in kinds and "negation" in kinds and "date" in kinds
