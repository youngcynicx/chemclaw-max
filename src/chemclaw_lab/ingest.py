"""S03: controlled evidence ingestion.

Plan S03 §1-2 and §8:
- Photos, scanned PDFs, and text are ingested under control: the ORIGINAL is
  stored unchanged (content-addressed by sha256, re-checkable), together with
  document identity (notebook), page order, paper page number, cross-page
  relations, author, and message source.
- File type (magic bytes), size, and path boundaries are checked BEFORE
  anything is written; attachments are never exposed as public static files —
  access goes through scope-checked reads only.
- Previews/derived versions are stored SEPARATELY from the original with
  their full transform chain recorded. PNG originals get real materialized
  derivatives (rotate/crop/preview) via the stdlib codec; JPEG/PDF get an
  honest ``materialized=0`` record and quality flag ``preview_pending``
  instead of a fabricated preview.
- Re-uploading the same content or the same (document, page_order) is
  idempotent: no duplicate evidence objects, no duplicate pages, no
  duplicate facts.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import pnglite
from .validation import SchemaViolation, load_named_schema, validate_payload_against_schema


TRANSFORM_SCHEMA_NAME = "transform_v1.json"

MAX_ORIGINAL_BYTES = 20 * 1024 * 1024  # 20 MB hard cap per original file
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

MAGIC_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/gif", b"GIF87a"),
    ("image/gif", b"GIF89a"),
    ("image/bmp", b"BM"),
    ("image/tiff", b"II*\x00"),
    ("image/tiff", b"MM\x00*"),
    ("application/pdf", b"%PDF"),
    ("application/zip", b"PK\x03\x04"),
    ("application/gzip", b"\x1f\x8b"),
    ("application/x-hdf5", b"\x89HDF\r\n\x1a\n"),
    ("application/x-netcdf", b"CDF\x01"),
    ("application/x-netcdf", b"CDF\x02"),
    ("application/vnd.isac.fcs", b"FCS"),
    ("application/x-bam", b"BAM\x01"),
)

# Broad research-file support. The pipeline always preserves the original;
# only text/plain and text/markdown are auto-transcribed today. Other types
# are stored with an honest "parser not configured" warning.
EXTENSION_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".webp": "image/webp", ".heic": "image/heic",
    ".pdf": "application/pdf",
    ".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json", ".jsonl": "application/x-ndjson",
    ".xml": "application/xml", ".html": "text/html", ".htm": "text/html",
    ".yaml": "application/yaml", ".yml": "application/yaml",
    ".tex": "text/x-tex", ".rtf": "application/rtf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".zip": "application/zip", ".gz": "application/gzip",
    ".tar": "application/x-tar", ".7z": "application/x-7z-compressed",
    ".cif": "chemical/x-cif", ".pdb": "chemical/x-pdb",
    ".mol": "chemical/x-mdl-molfile", ".sdf": "chemical/x-mdl-sdfile",
    ".smi": "chemical/x-daylight-smiles",
    ".fasta": "chemical/x-fasta", ".fa": "chemical/x-fasta",
    ".fastq": "chemical/x-fastq", ".fq": "chemical/x-fastq",
    ".vcf": "text/x-vcf", ".bam": "application/x-bam",
    ".mzml": "application/x-mzml", ".mzxml": "application/x-mzxml",
    ".jdx": "text/x-jcamp-dx", ".dx": "text/x-jcamp-dx",
    ".fcs": "application/vnd.isac.fcs",
    ".cdf": "application/x-netcdf", ".nc": "application/x-netcdf",
    ".h5": "application/x-hdf5", ".hdf5": "application/x-hdf5",
    ".ab1": "application/x-ab1", ".spc": "application/x-spc",
    ".raw": "application/octet-stream", ".dta": "application/octet-stream",
    ".wiff": "application/octet-stream", ".baf": "application/octet-stream",
}

TEXT_MIME_ALIASES = {
    "text/plain", "text/markdown", "text/csv", "text/tab-separated-values",
    "application/json", "application/x-ndjson", "application/xml",
    "text/xml", "text/html", "application/yaml", "text/x-tex",
    "chemical/x-cif", "chemical/x-pdb", "chemical/x-mdl-molfile",
    "chemical/x-mdl-sdfile", "chemical/x-daylight-smiles",
    "chemical/x-fasta", "chemical/x-fastq", "text/x-vcf",
    "application/x-mzml", "application/x-mzxml", "text/x-jcamp-dx",
}


class IngestError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def sniff_mime(
    content: bytes,
    declared_mime: str | None = None,
    filename: str | None = None,
) -> str:
    """Return a stable MIME type for a research file.

    Order:
    1. magic bytes for common binary formats;
    2. UTF-8 text detection;
    3. filename extension for the broad research-file allowlist;
    4. an explicit non-generic declared MIME from the platform.

    Unknown binary files are still rejected: "支持更多类型" means a curated
    allowlist, not accepting arbitrary bytes.
    """
    declared = (declared_mime or "").strip().lower() or None
    if declared == "image/jpg":
        declared = "image/jpeg"
    suffix = ""
    if filename:
        suffix = Path(filename).suffix.lower()

    detected: str | None = None
    for mime, magic in MAGIC_SIGNATURES:
        if content.startswith(magic):
            detected = mime
            break

    # ZIP-based office/OpenDocument files share the ZIP magic; use the
    # extension to refine them before the mismatch check.
    if detected == "application/zip" and suffix in EXTENSION_MIME:
        detected = EXTENSION_MIME[suffix]

    if detected is None:
        try:
            text = content.decode("utf-8")
            # Arbitrary binary can be valid UTF-8 (NUL/control bytes). Treat
            # only printable text as text; otherwise fall through to the
            # extension allowlist / unsupported error.
            if all(ord(ch) >= 32 or ch in "\n\r\t" for ch in text):
                detected = "text/plain"
        except UnicodeDecodeError:
            detected = None

    if detected is not None and suffix in EXTENSION_MIME:
        ext_mime = EXTENSION_MIME[suffix]
        if detected == "text/plain" and ext_mime in TEXT_MIME_ALIASES:
            detected = ext_mime
        elif detected == "application/octet-stream":
            detected = ext_mime

    if detected is None and suffix in EXTENSION_MIME:
        detected = EXTENSION_MIME[suffix]
    declared_allowlist = {
        value for value in EXTENSION_MIME.values()
        if value != "application/octet-stream"
    }
    if detected is None and declared in declared_allowlist:
        detected = declared
    if detected is None:
        raise IngestError(
            "unsupported_file_type",
            "File type not recognizable and not in the supported research-file allowlist.",
            http_status=415,
        )

    if declared and declared not in {
        "application/octet-stream", "binary/octet-stream",
    } and declared != detected:
        compatible = (
            detected in TEXT_MIME_ALIASES and declared in TEXT_MIME_ALIASES
        ) or (detected == "application/zip" and declared.startswith("application/"))
        if not compatible:
            raise IngestError(
                "mime_mismatch",
                f"Declared type {declared!r} does not match content ({detected}).",
                http_status=415,
            )
    return detected


def _safe_component(name: str) -> str:
    cleaned = SAFE_FILENAME.sub("_", name).strip("._") or "upload"
    return cleaned[:64]


def resolve_storage_root(root: str | Path) -> Path:
    """Resolve and create the storage root; returns an absolute path."""
    path = Path(root).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_within(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if root != resolved and root not in resolved.parents:
        raise IngestError(
            "path_boundary_violation",
            "Resolved path escapes the configured storage root.",
            http_status=400,
        )
    return resolved


def save_original(
    storage_root: str | Path,
    content: bytes,
    *,
    filename: str,
) -> tuple[Path, str]:
    """Store the original content-addressed; returns (path, sha256).

    Idempotent: identical content reuses the existing file (hash-addressed).
    """
    root = resolve_storage_root(storage_root)
    digest = hashlib.sha256(content).hexdigest()
    safe_name = _safe_component(filename)
    target = _ensure_within(root, root / "originals" / digest[:2] / f"{digest}__{safe_name}")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(target)  # atomic within the same directory
    return target, digest


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- document / page identity -------------------------------------------------


def create_document(
    conn: sqlite3.Connection,
    *,
    project_id: str | None,
    notebook_code: str,
    title: str | None = None,
    author: str | None = None,
    source_channel: str | None = None,
) -> dict[str, Any]:
    """Create (or return existing) notebook/document identity."""
    existing = conn.execute(
        """
        SELECT * FROM evidence_documents
        WHERE project_id IS ? AND notebook_code = ?
        """,
        (project_id, notebook_code),
    ).fetchone()
    if existing is not None:
        return _document_row(existing)
    if project_id is not None and conn.execute(
        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone() is None:
        raise IngestError("unknown_project", f"Unknown project_id: {project_id}.", http_status=404)
    document_id = f"doc_{hashlib.sha256(f'{project_id}|{notebook_code}'.encode()).hexdigest()[:16]}"
    conn.execute(
        """
        INSERT INTO evidence_documents
          (document_id, project_id, notebook_code, title, author, source_channel)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (document_id, project_id, notebook_code, title, author, source_channel),
    )
    row = conn.execute(
        "SELECT * FROM evidence_documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    return _document_row(row)


def _document_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "project_id": row["project_id"],
        "notebook_code": row["notebook_code"],
        "title": row["title"],
        "author": row["author"],
        "source_channel": row["source_channel"],
    }


def get_page(conn: sqlite3.Connection, page_id: str) -> dict[str, Any]:
    """Public page record read (used by the transcription pipeline and API)."""
    return _page_row(conn, page_id)


def get_document(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM evidence_documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise IngestError("unknown_document", f"Unknown document {document_id!r}.", http_status=404)
    return _document_row(row)


def ingest_page(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    content: bytes,
    filename: str,
    page_order: int,
    paper_page_number: str | None = None,
    storage_root: str | Path,
    project_id: str | None = None,
    source_channel: str | None = None,
    declared_mime: str | None = None,
) -> dict[str, Any]:
    """Ingest one page image/document: validate, store original, register.

    Returns the page record. Idempotent on (document_id, page_order): a
    re-upload of the SAME page returns the existing page with
    ``already_present=True`` and never creates a second page or evidence row.
    Re-upload with DIFFERENT content is rejected (page identity conflict) —
    a different photo of the same sheet is a new derived-version problem,
    not a silent overwrite.
    """
    if page_order < 1:
        raise IngestError("invalid_page_order", "page_order must be >= 1.")
    if len(content) > MAX_ORIGINAL_BYTES:
        raise IngestError(
            "file_too_large",
            f"Original file exceeds the {MAX_ORIGINAL_BYTES} byte limit.",
            http_status=413,
        )
    mime = sniff_mime(content, declared_mime, filename)
    get_document(conn, document_id)  # 404 if missing

    existing = conn.execute(
        "SELECT * FROM evidence_pages WHERE document_id = ? AND page_order = ?",
        (document_id, page_order),
    ).fetchone()
    if existing is not None:
        # Idempotent re-upload: same content → same page, no duplicates.
        stored = conn.execute(
            "SELECT sha256, project_id FROM evidence_objects WHERE evidence_id = ?",
            (existing["evidence_id"],),
        ).fetchone()
        digest = hashlib.sha256(content).hexdigest()
        if stored is not None and stored["sha256"] == digest:
            page = _page_row(conn, existing["page_id"])
            page["already_present"] = True
            return page
        raise IngestError(
            "page_conflict",
            f"Page order {page_order} in document already holds different content.",
            http_status=409,
        )

    original_path, digest = save_original(storage_root, content, filename=filename)
    evidence_id = f"ev_{digest[:16]}"
    already_registered = conn.execute(
        "SELECT 1 FROM evidence_objects WHERE evidence_id = ?", (evidence_id,)
    ).fetchone() is not None
    if not already_registered:
        conn.execute(
            """
            INSERT INTO evidence_objects
              (evidence_id, project_id, storage_path, sha256, mime, page_count, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                project_id,
                str(original_path),
                digest,
                mime,
                1,
                source_channel or "ingest",
            ),
        )
    elif project_id is not None:
        stored_project = conn.execute(
            "SELECT project_id FROM evidence_objects WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if stored_project["project_id"] not in (None, project_id):
            raise IngestError(
                "scope_conflict",
                "Content hash already registered under a different project.",
                http_status=409,
            )

    page_id = f"pg_{digest[:12]}_{page_order:04d}"
    # The same content can legitimately appear in two different notebooks
    # (e.g. a shared protocol page). page_id is global, so disambiguate with
    # a stable document suffix instead of failing the second ingest.
    collision = conn.execute(
        "SELECT document_id FROM evidence_pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if collision is not None and collision["document_id"] != document_id:
        suffix = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:8]
        page_id = f"{page_id}_{suffix}"
    conn.execute(
        """
        INSERT INTO evidence_pages
          (page_id, document_id, evidence_id, page_order, paper_page_number, status)
        VALUES (?, ?, ?, ?, ?, 'ingested')
        """,
        (page_id, document_id, evidence_id, page_order, paper_page_number),
    )
    _relink_page_chain(conn, document_id)
    page = _page_row(conn, page_id)
    page["already_present"] = False
    page["mime"] = mime
    page["sha256"] = digest
    return page


def _relink_page_chain(conn: sqlite3.Connection, document_id: str) -> None:
    """Maintain prev/next links from page_order (cross-page relations)."""
    rows = conn.execute(
        """
        SELECT page_id FROM evidence_pages
        WHERE document_id = ? ORDER BY page_order
        """,
        (document_id,),
    ).fetchall()
    ordered = [row["page_id"] for row in rows]
    for index, page_id in enumerate(ordered):
        prev_id = ordered[index - 1] if index > 0 else None
        next_id = ordered[index + 1] if index + 1 < len(ordered) else None
        conn.execute(
            "UPDATE evidence_pages SET prev_page_id = ?, next_page_id = ? WHERE page_id = ?",
            (prev_id, next_id, page_id),
        )


def add_page_relation(
    conn: sqlite3.Connection,
    *,
    from_page_id: str,
    to_page_id: str,
    relation_kind: str,
) -> dict[str, Any]:
    if relation_kind not in ("continuation", "supplement", "insert"):
        raise IngestError(
            "invalid_relation_kind", "relation_kind must be continuation|supplement|insert."
        )
    for page_id in (from_page_id, to_page_id):
        if conn.execute("SELECT 1 FROM evidence_pages WHERE page_id = ?", (page_id,)).fetchone() is None:
            raise IngestError("unknown_page", f"Unknown page {page_id!r}.", http_status=404)
    if from_page_id == to_page_id:
        raise IngestError("invalid_relation", "A page cannot relate to itself.")
    relation_id = (
        f"rel_{hashlib.sha256(f'{from_page_id}|{to_page_id}|{relation_kind}'.encode()).hexdigest()[:16]}"
    )
    try:
        conn.execute(
            """
            INSERT INTO page_relations (relation_id, from_page_id, to_page_id, relation_kind)
            VALUES (?, ?, ?, ?)
            """,
            (relation_id, from_page_id, to_page_id, relation_kind),
        )
    except sqlite3.IntegrityError as exc:
        raise IngestError(
            "invalid_state", "Page relation already exists.", http_status=409
        ) from exc
    return {"relation_id": relation_id, "from_page_id": from_page_id,
            "to_page_id": to_page_id, "relation_kind": relation_kind}


# --- reads ---------------------------------------------------------------


def _page_row(conn: sqlite3.Connection, page_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM evidence_pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if row is None:
        raise IngestError("unknown_page", f"Unknown page {page_id!r}.", http_status=404)
    doc = conn.execute(
        "SELECT * FROM evidence_documents WHERE document_id = ?", (row["document_id"],)
    ).fetchone()
    ev = conn.execute(
        "SELECT * FROM evidence_objects WHERE evidence_id = ?", (row["evidence_id"],)
    ).fetchone()
    active = conn.execute(
        "SELECT active_run_id, active_revision FROM page_transcripts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    preview = conn.execute(
        """
        SELECT artifact_id, storage_path, sha256, transform_json, materialized
        FROM derived_artifacts WHERE page_id = ? AND kind = 'preview'
        ORDER BY created_at DESC LIMIT 1
        """,
        (page_id,),
    ).fetchone()
    return {
        "page_id": row["page_id"],
        "document_id": row["document_id"],
        "notebook_code": doc["notebook_code"] if doc else None,
        "project_id": doc["project_id"] if doc else None,
        "evidence_id": row["evidence_id"],
        "page_order": row["page_order"],
        "paper_page_number": row["paper_page_number"],
        "status": row["status"],
        "prev_page_id": row["prev_page_id"],
        "next_page_id": row["next_page_id"],
        "sha256": ev["sha256"] if ev else None,
        "mime": ev["mime"] if ev else None,
        "storage_path": ev["storage_path"] if ev else None,
        "original_bytes": ev and Path(ev["storage_path"]).stat().st_size if ev and Path(ev["storage_path"]).exists() else None,
        "active_revision": active["active_revision"] if active else 0,
        "active_run_id": active["active_run_id"] if active else None,
        "preview": (
            {
                "artifact_id": preview["artifact_id"],
                "sha256": preview["sha256"],
                "materialized": bool(preview["materialized"]),
                "transform": _load_transform(preview["transform_json"]),
            }
            if preview
            else {"materialized": False, "reason": "preview_pending"}
        ),
    }


def _load_transform(transform_json: str | None) -> dict[str, Any]:
    if not transform_json:
        return {"steps": []}
    try:
        return dict(json.loads(transform_json))
    except Exception:  # noqa: BLE001 - corrupt record surfaces as empty
        return {"steps": []}


def list_document_pages(conn: sqlite3.Connection, document_id: str) -> list[dict[str, Any]]:
    get_document(conn, document_id)
    rows = conn.execute(
        """
        SELECT page_id FROM evidence_pages WHERE document_id = ? ORDER BY page_order
        """,
        (document_id,),
    ).fetchall()
    return [_page_row(conn, row["page_id"]) for row in rows]


def read_original_bytes(conn: sqlite3.Connection, page_id: str) -> tuple[bytes, str]:
    """Scope-checked by callers; returns (content, mime) of the ORIGINAL."""
    page = _page_row(conn, page_id)
    path = Path(page["storage_path"])
    if not path.exists():
        raise IngestError(
            "original_missing",
            "Original file is missing from storage.",
            http_status=500,
        )
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != page["sha256"]:
        raise IngestError(
            "hash_mismatch",
            f"Stored original hash {digest} does not match registry "
            f"{page['sha256']}; file was modified or corrupted.",
            http_status=500,
        )
    return content, page["mime"] or "application/octet-stream"


def verify_original_hash(conn: sqlite3.Connection, page_id: str) -> dict[str, Any]:
    page = _page_row(conn, page_id)
    path = Path(page["storage_path"])
    exists = path.exists()
    actual = hash_file(path) if exists else None
    return {
        "page_id": page_id,
        "evidence_id": page["evidence_id"],
        "expected_sha256": page["sha256"],
        "actual_sha256": actual,
        "match": bool(actual == page["sha256"]),
        "file_present": exists,
    }


# --- derived artifacts ---------------------------------------------------


def create_derived_version(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    storage_root: str | Path,
    transform: dict[str, Any],
    label: str = "derived",
) -> dict[str, Any]:
    """Materialize a derived version of a PNG page with its transform chain.

    JPEG/PDF originals are NOT re-encoded (no stdlib codec) — the call fails
    with ``derived_unsupported_format`` instead of pretending. The transform
    chain is validated against ``transform_v1`` and stored verbatim.
    """
    schema = load_named_schema(TRANSFORM_SCHEMA_NAME)
    try:
        validate_payload_against_schema(transform, schema)
    except SchemaViolation as exc:
        raise IngestError("invalid_transform", str(exc)) from exc
    steps = transform.get("steps", [])
    if not steps:
        raise IngestError("invalid_transform", "Transform chain is empty.")

    content, mime = read_original_bytes(conn, page_id)
    root = resolve_storage_root(storage_root)

    if mime != "image/png":
        raise IngestError(
            "derived_unsupported_format",
            f"Derived versions require PNG originals (got {mime}); the "
            "original stays unchanged and un-re-encoded.",
            http_status=415,
        )

    width, height, rows = pnglite.decode_png(content)
    current_w, current_h = width, height
    for step in steps:
        op = step["op"]
        if op == "rotate90":
            current_w, current_h, rows = pnglite.rotate_rows(
                rows, int(step["angle"]), current_w, current_h
            )
        elif op == "crop":
            rows = pnglite.crop_rows(
                rows,
                int(step["x"]),
                int(step["y"]),
                int(step["width"]),
                int(step["height"]),
                current_w,
            )
            current_w, current_h = int(step["width"]), int(step["height"])
        elif op == "rescale_dpi":
            # Pixel-array rescale is not a faithful DPI re-render; record the
            # chain but do not fabricate pixels. Block geometry math still
            # handles rescale_dpi exactly (coordinates.py).
            raise IngestError(
                "derived_unsupported_format",
                "Pixel rescaling is not supported by the stdlib codec; record "
                "the chain with rotate/crop only or use an external renderer.",
                http_status=415,
            )
        elif op == "deform_expand":
            raise IngestError(
                "invalid_transform",
                "deform_expand is not materializable by this codec.",
            )

    payload = pnglite.encode_png(current_w, current_h, rows)
    artifact_id = f"da_{hashlib.sha256(f'{page_id}|{label}|{payload[:64].hex()}'.encode()).hexdigest()[:16]}"
    artifact_path = _ensure_within(
        root, root / "derived" / page_id[:6] / f"{artifact_id}.png"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if not artifact_path.exists():
        tmp = artifact_path.with_suffix(".part")
        tmp.write_bytes(payload)
        tmp.replace(artifact_path)
    conn.execute(
        """
        INSERT INTO derived_artifacts
          (artifact_id, page_id, kind, storage_path, sha256, transform_json, materialized)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (
            artifact_id,
            page_id,
            _kind_of(steps),
            str(artifact_path),
            hashlib.sha256(payload).hexdigest(),
            json.dumps(transform, ensure_ascii=False),
        ),
    )
    return {
        "artifact_id": artifact_id,
        "page_id": page_id,
        "kind": _kind_of(steps),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "width": current_w,
        "height": current_h,
        "transform": transform,
        "materialized": True,
    }


def _pillow_preview(content: bytes, target_width: int) -> tuple[bytes, int, int] | None:
    """Render a PNG preview with Pillow when the optional imaging extra exists."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.seek(0)
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width > target_width:
                ratio = target_width / float(width)
                rgb = rgb.resize((target_width, max(1, int(height * ratio))))
                width, height = rgb.size
            output = io.BytesIO()
            rgb.save(output, format="PNG", optimize=True)
            return output.getvalue(), width, height
    except Exception:
        return None


def create_preview(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    storage_root: str | Path,
    target_width: int = 400,
) -> dict[str, Any]:
    """Generate a downscaled PNG preview.

    PNG uses the zero-dependency pnglite path. JPEG/TIFF/BMP/WebP/GIF use
    Pillow when the optional imaging extra is installed; otherwise the
    preview is honestly marked pending.
    """
    content, mime = read_original_bytes(conn, page_id)
    root = resolve_storage_root(storage_root)
    if mime != "image/png":
        rendered = _pillow_preview(content, target_width) if mime.startswith("image/") else None
        if rendered is not None:
            payload, width, height = rendered
            artifact_id = (
                f"da_{hashlib.sha256(f'{page_id}|preview|{width}'.encode()).hexdigest()[:16]}"
            )
            existing = conn.execute(
                "SELECT artifact_id, sha256, materialized FROM derived_artifacts "
                "WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "page_id": page_id,
                    "artifact_id": artifact_id,
                    "materialized": bool(existing["materialized"]),
                    "width": width,
                    "height": height,
                    "sha256": existing["sha256"],
                    "already_present": True,
                }
            artifact_path = _ensure_within(
                root, root / "derived" / page_id[:6] / f"{artifact_id}.png"
            )
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            if not artifact_path.exists():
                temp = artifact_path.with_suffix(".part")
                temp.write_bytes(payload)
                temp.replace(artifact_path)
            conn.execute(
                """
                INSERT INTO derived_artifacts
                  (artifact_id, page_id, kind, storage_path, sha256, transform_json, materialized)
                VALUES (?, ?, 'preview', ?, ?, '{}', 1)
                """,
                (artifact_id, page_id, str(artifact_path), hashlib.sha256(payload).hexdigest()),
            )
            return {
                "page_id": page_id,
                "artifact_id": artifact_id,
                "materialized": True,
                "width": width,
                "height": height,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        artifact_id = (
            f"da_{hashlib.sha256(f'{page_id}|preview-pending'.encode()).hexdigest()[:16]}"
        )
        existing = conn.execute(
            "SELECT artifact_id FROM derived_artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO derived_artifacts
                  (artifact_id, page_id, kind, storage_path, sha256, transform_json, materialized)
                VALUES (?, ?, 'preview', ?, ?, '{}', 0)
                """,
                (artifact_id, page_id, "pending://preview", ""),
            )
        return {
            "page_id": page_id,
            "artifact_id": artifact_id,
            "materialized": False,
            "reason": (
                f"preview_pending: imaging codec unavailable for {mime}; "
                "install Pillow or use an image preview endpoint"
            ),
        }
    width, height, rows = pnglite.decode_png(content)
    if width <= target_width:
        # Already preview-sized: the original bytes ARE the preview (no
        # re-encode, hash equals the original).
        payload = content
    else:
        tw, th, preview_rows = pnglite.downscale_rows(rows, width, height, target_width)
        payload = pnglite.encode_png(tw, th, preview_rows)
        width, height = tw, th
    artifact_id = f"da_{hashlib.sha256(f'{page_id}|preview|{width}'.encode()).hexdigest()[:16]}"
    existing = conn.execute(
        "SELECT artifact_id, sha256, materialized FROM derived_artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if existing is not None:
        return {
            "page_id": page_id,
            "artifact_id": artifact_id,
            "materialized": bool(existing["materialized"]),
            "width": width,
            "height": height,
            "sha256": existing["sha256"],
            "already_present": True,
        }
    artifact_path = _ensure_within(root, root / "derived" / page_id[:6] / f"{artifact_id}.png")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if not artifact_path.exists():
        tmp = artifact_path.with_suffix(".part")
        tmp.write_bytes(payload)
        tmp.replace(artifact_path)
    conn.execute(
        """
        INSERT INTO derived_artifacts
          (artifact_id, page_id, kind, storage_path, sha256, transform_json, materialized)
        VALUES (?, ?, 'preview', ?, ?, '{}', 1)
        """,
        (artifact_id, page_id, str(artifact_path), hashlib.sha256(payload).hexdigest()),
    )
    return {
        "page_id": page_id,
        "artifact_id": artifact_id,
        "materialized": True,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _kind_of(steps: list[dict[str, Any]]) -> str:
    ops = [step["op"] for step in steps]
    if ops == ["rotate90"]:
        return "rotated"
    if ops == ["crop"]:
        return "cropped"
    return "enhanced"


__all__ = [
    "IngestError",
    "MAX_ORIGINAL_BYTES",
    "add_page_relation",
    "create_derived_version",
    "create_document",
    "create_preview",
    "get_document",
    "hash_file",
    "ingest_page",
    "list_document_pages",
    "read_original_bytes",
    "resolve_storage_root",
    "save_original",
    "sniff_mime",
    "verify_original_hash",
]
