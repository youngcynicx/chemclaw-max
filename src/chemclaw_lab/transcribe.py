"""S03: transcription pipeline — runs, blocks, revisions, risk fragments.

Plan S03 §3-§8 implemented as one pipeline:

- Every recognition/human-transcription execution is an APPEND-ONLY
  ``transcript_runs`` row (provider/model/version, input sha256, status,
  raw output). Raw OCR output and the normalized block list are both kept.
- Blocks are validated against the frozen ``transcript_block_v1`` schema
  before storage. The page's active transcript is a pointer
  (``page_transcripts``) with a monotonic revision; corrections append a new
  run (origin ``human_correction``) and bump the revision — they never
  overwrite earlier runs and never auto-write business facts (S04 decides).
- Risky fragments (sample ids, numbers, units, dates, negations, states) are
  marked individually; unclear ones are recorded as ``unknown``/
  ``pending_confirm`` while independent fragments are still saved.
- Competing readings (struck values, insertion arrows) are kept side by side
  with the chosen one NULL — the plan forbids guessing a unique number.
- A failed run keeps the original ingestable and queryable; transcription
  can be retried with a fresh run (§8: OCR failure ≠ ingestion failure).
- Every successful run or correction emits a messaging outbox event
  (``transcript.revised``) so downstream proposal flows can re-check.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from . import ingest, ocr_port
from .ocr_port import CloudPolicy, ProviderRegistry, TranscriberError
from .validation import SchemaViolation


class TranscriptionError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def run_transcription(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    provider_name: str,
    registry: ProviderRegistry,
    cloud_policy: CloudPolicy,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one transcription run over the page's ORIGINAL content.

    Offline-by-design: the original bytes are read (and hash-verified) from
    local storage; the provider runs against them. A provider failure marks
    the run FAILED and leaves ingestion/query untouched.
    """
    content, mime = ingest.read_original_bytes(conn, page_id)
    input_sha256 = hashlib.sha256(content).hexdigest()

    provider = registry.get(provider_name)
    info = provider.info()
    cloud_policy.check_provider(info)  # raises for disallowed cloud providers
    if mime not in info.supported_mimes:
        raise TranscriberError(
            "unsupported_mime",
            f"Provider {provider_name!r} does not support {mime}.",
            http_status=415,
        )

    page = ingest.get_page(conn, page_id)
    page_meta = {
        "page_id": page_id,
        "document_id": page["document_id"],
        "page_order": page["page_order"],
    }

    run_id = f"run_{hashlib.sha256(f'{page_id}|{provider_name}|{input_sha256}'.encode()).hexdigest()[:12]}_{_next_run_seq(conn, page_id):04d}"
    conn.execute(
        """
        INSERT INTO transcript_runs
          (run_id, page_id, provider, model, provider_version, config_json,
           input_sha256, status, origin)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)
        """,
        (
            run_id,
            page_id,
            info.name,
            info.model,
            info.version,
            json.dumps(config or {}, ensure_ascii=False),
            input_sha256,
            "ocr" if info.kind != "human" else "human",
        ),
    )
    conn.execute(
        "UPDATE evidence_pages SET status = 'transcribing' WHERE page_id = ?",
        (page_id,),
    )

    try:
        result = provider.transcribe(
            content=content, mime=mime, page_meta=page_meta, config=config
        )
        blocks = ocr_port.validate_blocks(result.get("blocks", []))
    except TranscriberError as exc:
        conn.execute(
            """
            UPDATE transcript_runs
            SET status = 'failed', error_code = ?, finished_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (exc.code, run_id),
        )
        conn.execute(
            "UPDATE evidence_pages SET status = 'failed' WHERE page_id = ?",
            (page_id,),
        )
        raise TranscriptionError(exc.code, exc.message, http_status=exc.http_status) from exc
    except SchemaViolation as exc:
        conn.execute(
            """
            UPDATE transcript_runs
            SET status = 'failed', error_code = 'invalid_block_schema',
                finished_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (run_id,),
        )
        conn.execute(
            "UPDATE evidence_pages SET status = 'failed' WHERE page_id = ?",
            (page_id,),
        )
        raise TranscriptionError(
            "invalid_block_schema", f"Provider returned schema-invalid blocks: {exc}"
        ) from exc

    # Text is usable by default. Geometry/unclear-field risks remain attached
    # to individual blocks and can be corrected later; they are not a gate.
    needs_human = False
    _persist_blocks(conn, run_id, blocks, geometry_trusted=info.provides_geometry)

    raw_output = result.get("raw_output")
    conn.execute(
        """
        UPDATE transcript_runs
        SET status = ?, raw_output_json = ?, finished_at = CURRENT_TIMESTAMP
        WHERE run_id = ?
        """,
        (
            "needs_human" if needs_human else "succeeded",
            json.dumps(raw_output, ensure_ascii=False),
            run_id,
        ),
    )
    _activate_run(conn, page_id, run_id)
    conn.execute(
        """
        UPDATE evidence_pages SET status = ?
        WHERE page_id = ?
        """,
        ("needs_human" if needs_human else "transcribed", page_id),
    )
    _emit_revised_event(conn, page_id, run_id, "run_completed")
    return get_run(conn, run_id)


def correct_transcript(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    blocks: list[dict[str, Any]],
    expected_revision: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Human correction: a NEW revision, previous runs stay queryable.

    Plan S03 §7: corrections create a new revision, trigger related-record
    re-proposal via the outbox, and never auto-overwrite business facts.
    Optimistic concurrency via ``expected_revision`` (S02-style).
    """
    if not actor or not reason:
        raise TranscriptionError(
            "missing_actor_or_reason", "Corrections require actor and reason."
        )
    current = conn.execute(
        "SELECT active_revision, active_run_id FROM page_transcripts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    active_revision = current["active_revision"] if current else 0
    if active_revision != expected_revision:
        raise TranscriptionError(
            "revision_conflict",
            f"Expected revision {expected_revision}, page is at {active_revision}.",
            http_status=409,
        )
    blocks = ocr_port.validate_blocks(blocks)

    run_id = f"run_{page_id}_human_{active_revision + 1:04d}"
    page_row = conn.execute(
        "SELECT evidence_id FROM evidence_pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if page_row is None:
        raise TranscriptionError("unknown_page", f"Unknown page {page_id!r}.", http_status=404)
    digest = conn.execute(
        "SELECT sha256 FROM evidence_objects WHERE evidence_id = ?", (page_row["evidence_id"],)
    ).fetchone()["sha256"]
    conn.execute(
        """
        INSERT INTO transcript_runs
          (run_id, page_id, provider, model, provider_version, config_json,
           input_sha256, status, origin, finished_at)
        VALUES (?, ?, 'human_transcriber', NULL, '1.0.0', ?, ?, 'succeeded',
                'human_correction', CURRENT_TIMESTAMP)
        """,
        (
            run_id,
            page_id,
            json.dumps({"actor": actor, "reason": reason}, ensure_ascii=False),
            digest,
        ),
    )
    _persist_blocks(conn, run_id, blocks, geometry_trusted=False)  # human fixes get trusted geometry only via manual locate
    _activate_run(conn, page_id, run_id)
    conn.execute("UPDATE evidence_pages SET status='transcribed' WHERE page_id=?", (page_id,))
    _emit_revised_event(conn, page_id, run_id, "human_correction")
    return get_run(conn, run_id)


def retry_transcription(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    registry: ProviderRegistry,
    cloud_policy: CloudPolicy,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-run a failed transcription with the same provider (fresh run row)."""
    run = get_run(conn, run_id)
    if run["status"] not in ("failed", "needs_human"):
        raise TranscriptionError(
            "not_retryable",
            f"Run {run_id!r} is {run['status']!r}; only failed/needs_human runs retry.",
            http_status=409,
        )
    config = dict(run.get("config") or {})
    if config_override:
        config.update(config_override)
    return run_transcription(
        conn,
        page_id=run["page_id"],
        provider_name=run["provider"],
        registry=registry,
        cloud_policy=cloud_policy,
        config=config,
    )


# --- reads ---------------------------------------------------------------


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM transcript_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise TranscriptionError("unknown_run", f"Unknown run {run_id!r}.", http_status=404)
    blocks = [
        _block_row(block_row)
        for block_row in conn.execute(
            "SELECT * FROM transcript_blocks WHERE run_id = ? ORDER BY reading_order",
            (run_id,),
        ).fetchall()
    ]
    return {
        "run_id": row["run_id"],
        "page_id": row["page_id"],
        "provider": row["provider"],
        "model": row["model"],
        "provider_version": row["provider_version"],
        "config": json.loads(row["config_json"] or "{}"),
        "input_sha256": row["input_sha256"],
        "status": row["status"],
        "error_code": row["error_code"],
        "origin": row["origin"],
        "raw_output": json.loads(row["raw_output_json"]) if row["raw_output_json"] else None,
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "blocks": blocks,
    }


def import_transcript(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    blocks: list[dict[str, Any]],
    provider: str,
    model: str | None,
    version: str | None,
    raw_output: Any = None,
    expected_revision: int | None = None,
    needs_human: bool = False,
) -> dict[str, Any]:
    """Import an external OCR/vision result as a NEW immutable run.

    Used by the local bridge after a vision model extracts text. The original
    bytes are untouched; text is adopted by default without claiming human verification.
    API keys/secrets must not be placed in provider/model/config.
    """
    current = conn.execute(
        "SELECT active_revision FROM page_transcripts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    active_revision = current["active_revision"] if current else 0
    if expected_revision is not None and active_revision != expected_revision:
        raise TranscriptionError(
            "revision_conflict",
            f"Expected revision {expected_revision}, page is at {active_revision}.",
            http_status=409,
        )
    try:
        blocks = ocr_port.validate_blocks(blocks)
    except SchemaViolation as exc:
        raise TranscriptionError(
            "invalid_block_schema", f"Imported blocks are schema-invalid: {exc}"
        ) from exc
    page_row = conn.execute(
        "SELECT evidence_id FROM evidence_pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if page_row is None:
        raise TranscriptionError("unknown_page", f"Unknown page {page_id!r}.", http_status=404)
    digest_row = conn.execute(
        "SELECT sha256 FROM evidence_objects WHERE evidence_id = ?",
        (page_row["evidence_id"],),
    ).fetchone()
    digest = digest_row["sha256"] if digest_row else ""
    seq = _next_run_seq(conn, page_id)
    run_id = (
        f"run_{hashlib.sha256(f'{page_id}|{provider}|{model}|{seq}'.encode()).hexdigest()[:12]}"
        f"_{seq:04d}"
    )
    conn.execute(
        """
        INSERT INTO transcript_runs
          (run_id, page_id, provider, model, provider_version, config_json,
           input_sha256, status, origin, raw_output_json, finished_at)
        VALUES (?, ?, ?, ?, ?, '{}', ?, ?, 'ocr', ?, CURRENT_TIMESTAMP)
        """,
        (
            run_id,
            page_id,
            provider,
            model,
            version,
            digest,
            "needs_human" if needs_human else "succeeded",
            json.dumps(raw_output, ensure_ascii=False),
        ),
    )
    _persist_blocks(conn, run_id, blocks, geometry_trusted=False)
    _activate_run(conn, page_id, run_id)
    conn.execute(
        "UPDATE evidence_pages SET status = ? WHERE page_id = ?",
        ("needs_human" if needs_human else "transcribed", page_id),
    )
    _emit_revised_event(conn, page_id, run_id, "external_ocr_import")
    result = get_run(conn, run_id)
    result["active_revision"] = active_revision + 1
    return result


def get_page_transcript(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    original_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Active transcript with blocks mapped back to ORIGINAL image pixels.

    Blocks recognized on derived images carry a ``transform`` chain inside
    their coordinate_system; mapping to the original is computed on read.
    When the chain is non-invertible (deform_expand) the response presents
    the DERIVED image reference and a located range — never fabricated
    original coordinates (plan S03 §3).
    """
    page = ingest.get_page(conn, page_id)
    active = conn.execute(
        "SELECT active_run_id, active_revision FROM page_transcripts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    if active is None or active["active_run_id"] is None:
        return {
            "page_id": page_id,
            "active_revision": 0,
            "active_run_id": None,
            "blocks": [],
            "risk_fragments": [],
            "status": page["status"],
        }
    run = get_run(conn, active["active_run_id"])
    mapped_blocks = []
    for block in run["blocks"]:
        mapped_blocks.append(
            _map_block_to_original(block, conn, original_size=original_size)
        )
    fragments = [
        dict(row)
        for row in conn.execute(
            """
            SELECT fragment_id, run_id, block_id, kind, value_text,
                   normalized_value, status
            FROM risk_fragments WHERE run_id = ? ORDER BY fragment_id
            """,
            (run["run_id"],),
        ).fetchall()
    ]
    return {
        "page_id": page_id,
        "active_revision": active["active_revision"],
        "active_run_id": run["run_id"],
        "provider": run["provider"],
        "status": page["status"],
        "blocks": mapped_blocks,
        "risk_fragments": fragments,
    }


def list_risk_fragments(
    conn: sqlite3.Connection, *, run_id: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    query = "SELECT * FROM risk_fragments WHERE 1=1"
    params: list[Any] = []
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY run_id, fragment_id"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


# --- internals ------------------------------------------------------------


def _next_run_seq(conn: sqlite3.Connection, page_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM transcript_runs WHERE page_id = ?", (page_id,)
    ).fetchone()
    return int(row["n"]) + 1


def _persist_blocks(
    conn: sqlite3.Connection,
    run_id: str,
    blocks: list[dict[str, Any]],
    *,
    geometry_trusted: bool,
) -> None:
    for block in blocks:
        conn.execute(
            """
            INSERT INTO transcript_blocks
              (block_id, run_id, block_kind, reading_order, block_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                block["block_id"],
                run_id,
                block["block_kind"],
                int(block["reading_order"]),
                json.dumps(block, ensure_ascii=False),
            ),
        )
        for fragment in _extract_risk_fragments(block, geometry_trusted=geometry_trusted):
            seed = (
                f"{block['block_id']}|{fragment['kind']}|"
                f"{fragment.get('value_text')}|{fragment.get('match_start')}"
            )
            fragment_id = f"fr_{hashlib.sha256(seed.encode()).hexdigest()[:16]}"
            conn.execute(
                """
                INSERT INTO risk_fragments
                  (fragment_id, run_id, block_id, kind, value_text,
                   normalized_value, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fragment_id,
                    run_id,
                    block["block_id"],
                    fragment["kind"],
                    fragment.get("value_text"),
                    fragment.get("normalized_value"),
                    fragment["status"],
                ),
            )
        for reading in block.get("competing_readings", []) or []:
            candidate = reading["candidate_text"]
            seed = f"{block['block_id']}|{candidate}"
            reading_id = f"cr_{hashlib.sha256(seed.encode()).hexdigest()[:16]}"
            conn.execute(
                """
                INSERT INTO competing_readings
                  (reading_id, block_id, candidate_text, confidence, chosen)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (
                    reading_id,
                    block["block_id"],
                    candidate,
                    reading.get("confidence"),
                ),
            )


def _activate_run(conn: sqlite3.Connection, page_id: str, run_id: str) -> None:
    row = conn.execute(
        "SELECT active_revision FROM page_transcripts WHERE page_id = ?", (page_id,)
    ).fetchone()
    next_revision = (row["active_revision"] if row else 0) + 1
    conn.execute(
        """
        INSERT INTO page_transcripts (page_id, active_run_id, active_revision, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(page_id) DO UPDATE SET
          active_run_id = excluded.active_run_id,
          active_revision = excluded.active_revision,
          updated_at = CURRENT_TIMESTAMP
        """,
        (page_id, run_id, next_revision),
    )


def _emit_revised_event(
    conn: sqlite3.Connection, page_id: str, run_id: str, reason: str
) -> None:
    """Outbox event so downstream proposal flows can re-check (S04 consumes)."""
    from . import tasks  # local import: avoid module-level cycle

    tasks.enqueue_outbox(
        conn,
        aggregate_type="evidence_page",
        aggregate_id=page_id,
        message_type="transcript.revised",
        payload={"page_id": page_id, "run_id": run_id, "reason": reason},
    )


RISK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("sample_id", r"\b[A-Z]{1,3}[-\s]?\d{1,4}\b"),
    ("number", r"\b\d+(?:\.\d+)?\b"),
    # Unreadable digits the recognizer could not resolve (e.g. "??.??").
    # No \b anchors: '?' is a non-word character, so word boundaries never
    # match around it.
    ("number", r"\?\?(?:\.\?\?)?"),
    ("unit", r"\b(?:g|mg|kg|mL|L|µL|mM|M|mol|°C|K|s|min|h)\b"),
    ("date", r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"),
    ("negation", r"\b(?:not|no|none|未|无|不加|失败|澄清)\b"),
)

UNKNOWN_TOKENS = ("?.", "??", "?.??", "??.", "unclear", "illegible")


def _extract_risk_fragments(
    block: dict[str, Any], *, geometry_trusted: bool
) -> list[dict[str, Any]]:
    """Mark risky fragments. Unclear ones hang as unknown; others save fine."""
    text = block.get("text") or ""
    if not text:
        return []
    flags = set(block.get("quality_flags") or [])
    fragments: list[dict[str, Any]] = []

    for kind, pattern in RISK_PATTERNS:
        for match in re.finditer(pattern, text):
            value_text = match.group(0)
            unclear = any(token in value_text for token in UNKNOWN_TOKENS) or (
                kind == "number" and "unclear_number" in flags
            ) or (
                kind == "unit" and "unclear_unit" in flags
            )
            if unclear:
                status = "unknown"
            elif not geometry_trusted:
                status = "pending_confirm"
            else:
                status = "ok"
            fragments.append(
                {
                    "kind": kind,
                    "value_text": value_text,
                    "normalized_value": None,
                    "status": status,
                    # Same kind+value can legitimately occur several times in
                    # one block ("1 mL ... S-1"); the position keeps the
                    # deterministic fragment_id unique.
                    "match_start": match.start(),
                }
            )
    return fragments


def _block_row(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["block_json"])


def _map_block_to_original(
    block: dict[str, Any], conn: sqlite3.Connection, *, original_size: tuple[int, int] | None
) -> dict[str, Any]:
    """Attach ``original_geometry`` to a block copy (or an honest fallback)."""
    from . import coordinates

    out = dict(block)
    geometry = block.get("geometry")
    cs = block.get("coordinate_system")
    if geometry is None or cs is None:
        out["original_geometry"] = None
        out["positioning"] = {"mode": "none", "reason": "block has no raster geometry"}
        return out
    transform = cs.get("transform") or {"steps": []}
    steps = coordinates.parse_chain(transform)
    if not steps:
        # Geometry already on the original image; still normalize origin/units.
        try:
            mapped = coordinates.map_geometry_to_original(
                geometry,
                cs,
                [],
                original_size=(float(cs.get("frame_width")), float(cs.get("frame_height"))),
            )
            out["original_geometry"] = mapped
            out["positioning"] = {"mode": "direct", "coordinate_system": cs.get("origin")}
        except coordinates.GeometryError as exc:
            out["original_geometry"] = None
            out["positioning"] = {"mode": "error", "reason": str(exc)}
        return out
    if original_size is None:
        out["original_geometry"] = None
        out["positioning"] = {
            "mode": "derived_only",
            "reason": "original_size unknown; derived-image coordinates retained",
            "image_ref": cs.get("image_ref"),
        }
        return out
    try:
        mapped = coordinates.map_geometry_to_original(
            geometry, cs, steps, original_size=original_size
        )
        out["original_geometry"] = mapped
        out["positioning"] = {"mode": "mapped", "chain": coordinates.chain_summary(steps)}
    except coordinates.UnmappableGeometry as exc:
        out["original_geometry"] = None
        out["positioning"] = {
            "mode": "derived_range_only",
            "reason": str(exc),
            "image_ref": cs.get("image_ref"),
            "derived_geometry": geometry,
        }
    except coordinates.GeometryError as exc:
        out["original_geometry"] = None
        out["positioning"] = {"mode": "error", "reason": str(exc)}
    return out


__all__ = [
    "TranscriptionError",
    "correct_transcript",
    "get_page_transcript",
    "get_run",
    "list_risk_fragments",
    "retry_transcription",
    "run_transcription",
]
