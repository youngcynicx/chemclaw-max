"""S03: OCR/transcriber port, provider registry, and cloud-processing gate.

The plan (S03 §3) requires a replaceable recognition port — local, cloud
vision, or human — where every run records provider/model/version, the input
hash, and output block locations. Two honesty rules are enforced HERE, not
left to providers:

- ``provides_geometry=False`` providers mark every block
  ``geometry_untrusted``; downstream, key fields from such runs must go
  through manual locate/confirm (no fake bboxes for coverage).
- Cloud providers run only when :class:`CloudPolicy` allows them, and the
  exact configured scope (what may leave the host) is inspectable through
  ``describe_cloud_policy`` — acceptance: "确认云端处理的配置范围可检查".

Bundled providers are deterministic and offline: a plain-text transcriber and
a fixture raster transcriber used by tests/eval to exercise the full pipeline
(real handwriting OCR needs pilot material — boundary documented in the S03
report).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import pnglite
from .validation import validate_payload_against_schema, load_named_schema


BLOCK_SCHEMA_NAME = "transcript_block_v1.json"


class TranscriberError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    kind: str  # 'local' | 'cloud' | 'human'
    model: str | None
    version: str
    provides_geometry: bool
    cloud_scope: dict[str, Any] | None = None
    supported_mimes: frozenset[str] = field(default_factory=frozenset)


@dataclass
class CloudPolicy:
    """Configured boundary for cloud-processed recognition (inspectable)."""

    allow_cloud: bool = False
    allowed_data_classes: tuple[str, ...] = ()  # e.g. ('redacted_text',)
    require_explicit_confirmation: bool = True
    providers: tuple[str, ...] = ()
    notes: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "allow_cloud": self.allow_cloud,
            "allowed_data_classes": list(self.allowed_data_classes),
            "require_explicit_confirmation": self.require_explicit_confirmation,
            "providers": list(self.providers),
            "notes": self.notes,
        }

    def check_provider(self, info: ProviderInfo) -> None:
        if info.kind != "cloud":
            return
        if not self.allow_cloud:
            raise TranscriberError(
                "cloud_disabled",
                f"Cloud provider {info.name!r} is configured but cloud "
                "processing is disabled by policy.",
                http_status=403,
            )
        if info.name not in self.providers:
            raise TranscriberError(
                "cloud_provider_not_allowed",
                f"Cloud provider {info.name!r} is not in the allowed list.",
                http_status=403,
            )


class TranscriberPort(Protocol):
    """Contract every recognition backend implements."""

    def info(self) -> ProviderInfo: ...

    def transcribe(
        self,
        *,
        content: bytes,
        mime: str,
        page_meta: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return {'raw_output': dict, 'blocks': [block...], 'needs_human': bool}.

        Every block must satisfy ``transcript_block_v1``.
        """
        ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, TranscriberPort] = {}

    def register(self, provider: TranscriberPort) -> None:
        info = provider.info()
        if info.name in self._providers:
            raise TranscriberError(
                "provider_conflict", f"Provider {info.name!r} already registered.", http_status=409
            )
        self._providers[info.name] = provider

    def get(self, name: str) -> TranscriberPort:
        provider = self._providers.get(name)
        if provider is None:
            raise TranscriberError(
                "unknown_provider", f"Unknown transcription provider {name!r}.", http_status=404
            )
        return provider

    def list(self) -> list[dict[str, Any]]:
        return [self._describe(p.info()) for p in self._providers.values()]

    @staticmethod
    def _describe(info: ProviderInfo) -> dict[str, Any]:
        return {
            "name": info.name,
            "kind": info.kind,
            "model": info.model,
            "version": info.version,
            "provides_geometry": info.provides_geometry,
            "cloud_scope": info.cloud_scope,
            "supported_mimes": sorted(info.supported_mimes),
        }


class TextInputTranscriber:
    """Offline transcriber for text uploads (.txt/.md): one block per line.

    ``provides_geometry=True``: this provider never emits raster geometry
    (blocks carry ``geometry=None``), so there is no positioning ambiguity
    to distrust — plain-text pages are exact and must NOT be forced into
    manual locate/confirm (plan §5 targets providers whose region location
    is unreliable, not text sources where geometry does not apply).
    """

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="text_input",
            kind="local",
            model=None,
            version="1.0.0",
            provides_geometry=True,
            supported_mimes=frozenset({"text/plain", "text/markdown"}),
        )

    def transcribe(
        self,
        *,
        content: bytes,
        mime: str,
        page_meta: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = content.decode("utf-8")
        lines = [line for line in text.splitlines()]
        blocks: list[dict[str, Any]] = []
        order = 0
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            blocks.append(
                {
                    "block_id": f"{page_meta.get('page_id', 'page')}-t{index:04d}",
                    "block_kind": "paragraph",
                    "reading_order": order,
                    "text": line,
                    "table_cell": None,
                    "geometry": None,
                    "coordinate_system": None,
                    "source_page": 1,
                    "extractor": {
                        "provider": "text_input",
                        "model": None,
                        "version": "1.0.0",
                        "config": None,
                    },
                    "quality_flags": [],
                }
            )
            order += 1
        return {"raw_output": {"kind": "text_lines", "line_count": len(lines)}, "blocks": blocks, "needs_human": False}


class FixtureRasterTranscriber:
    """Deterministic test provider: returns configured blocks over a PNG.

    This is NOT a real OCR engine. It exists so the pipeline (run records,
    block validation, geometry mapping, risk fragments, competing readings)
    is verified end-to-end without fabricating handwriting capability.
    Blocks come from ``config['fixture_blocks']`` in the PROVIDER's own
    coordinate system; the pipeline stores them verbatim and maps on read.
    """

    def __init__(self, *, name: str = "fixture_ocr", provides_geometry: bool = True) -> None:
        self._name = name
        self._provides_geometry = provides_geometry

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self._name,
            kind="local",
            model="fixture-recognizer",
            version="0.3.0",
            provides_geometry=self._provides_geometry,
            supported_mimes=frozenset({"image/png"}),
        )

    def transcribe(
        self,
        *,
        content: bytes,
        mime: str,
        page_meta: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = config or {}
        if mime not in self.info().supported_mimes:
            raise TranscriberError(
                "unsupported_mime", f"{self._name} cannot process {mime}."
            )
        try:
            width, height = pnglite.get_png_size(content)
        except pnglite.PngError as exc:
            raise TranscriberError("bad_image", str(exc)) from exc
        blocks = config.get("fixture_blocks")
        if blocks is None:
            raise TranscriberError(
                "missing_fixture", "fixture_ocr requires config['fixture_blocks']."
            )
        normalized: list[dict[str, Any]] = []
        for raw in blocks:
            block = dict(raw)
            cs = block.get("coordinate_system")
            if cs is not None and cs.get("frame_width") is None:
                cs = dict(cs)
                cs.setdefault("frame_width", width)
                cs.setdefault("frame_height", height)
                block["coordinate_system"] = cs
            if not self._provides_geometry and block.get("geometry") is not None:
                flags = list(block.get("quality_flags") or [])
                if "geometry_untrusted" not in flags:
                    flags.append("geometry_untrusted")
                block["quality_flags"] = flags
            normalized.append(block)
        return {
            "raw_output": {"kind": "fixture", "note": "deterministic test provider"},
            "blocks": normalized,
            "needs_human": bool(config.get("needs_human", False)),
        }


class CloudStubTranscriber:
    """Cloud-kind provider used to verify the cloud gate — never calls out.

    It raises if actually invoked without an explicit ``config['approved']``
    flag; the pipeline's CloudPolicy check runs first, so reaching the raise
    means the gate failed (test asserts it doesn't).
    """

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="cloud_vision_stub",
            kind="cloud",
            model="stub-vision-1",
            version="0.1.0",
            provides_geometry=True,
            cloud_scope={
                "data_leaving_host": "page_images_and_text",
                "retention": "provider_default",
                "region": "unset",
            },
            supported_mimes=frozenset({"image/png", "text/plain"}),
        )

    def transcribe(
        self,
        *,
        content: bytes,
        mime: str,
        page_meta: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = config or {}
        if not config.get("approved"):
            raise TranscriberError(
                "cloud_not_approved",
                "CloudStubTranscriber requires config['approved']=true — this "
                "stub performs no real cloud call.",
            )
        # Deterministic echo for gate-approved tests only.
        return {
            "raw_output": {"kind": "stub"},
            "blocks": [
                {
                    "block_id": f"{page_meta.get('page_id', 'page')}-c0",
                    "block_kind": "paragraph",
                    "reading_order": 0,
                    "text": content.decode("utf-8", errors="replace")[:200],
                    "table_cell": None,
                    "geometry": None,
                    "coordinate_system": None,
                    "source_page": 1,
                    "extractor": {
                        "provider": "cloud_vision_stub",
                        "model": "stub-vision-1",
                        "version": "0.1.0",
                        "config": None,
                    },
                    "quality_flags": [],
                }
            ],
            "needs_human": False,
        }


class FailingTranscriber:
    """Provider that always fails — used for offline/degradation tests."""

    def __init__(self, *, name: str = "failing_ocr") -> None:
        self._name = name

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self._name,
            kind="local",
            model=None,
            version="0.1.0",
            provides_geometry=False,
            supported_mimes=frozenset({"image/png", "text/plain"}),
        )

    def transcribe(
        self,
        *,
        content: bytes,
        mime: str,
        page_meta: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise TranscriberError("provider_down", f"{self._name} is down (simulated).", http_status=503)


def validate_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate every block against the frozen schema; returns them unchanged."""
    schema = load_named_schema(BLOCK_SCHEMA_NAME)
    for block in blocks:
        validate_payload_against_schema(block, schema)
    return blocks


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(TextInputTranscriber())
    registry.register(CloudStubTranscriber())
    registry.register(FailingTranscriber())
    return registry


def decode_data_url(data_url: str) -> tuple[bytes, str | None]:
    """Decode a base64 data URL used by the JSON upload API."""
    if not data_url.startswith("data:"):
        raise TranscriberError("bad_upload", "Upload must be a base64 data URL.")
    header, _, b64 = data_url.partition(",")
    if not _:
        raise TranscriberError("bad_upload", "Malformed data URL.")
    mime = header[5:].split(";", 1)[0] or None
    try:
        return base64.b64decode(b64, validate=True), mime
    except Exception as exc:  # noqa: BLE001 - surfaced as a client error
        raise TranscriberError("bad_upload", f"Invalid base64 payload: {exc}") from exc
