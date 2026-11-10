"""S03: server-owned service settings for ingestion & transcription.

These are NOT client parameters — they never travel through capability
params (a client must not choose the storage root or enable cloud
processing). The HTTP server builds one :class:`ServiceSettings` at startup;
the capability layer threads it to S03 handlers.

- ``storage_root``: resolved from ``CHEMCLAW_LAB_DATA_DIR`` when set,
  otherwise ``<repo>/data`` (workspace storage per decision M-11). All
  writes stay inside this root; ``ingest._ensure_within`` enforces it.
- ``cloud_policy``: default OFF. Turning it on is a deliberate operator
  action with an inspectable scope (plan AC: "确认云端处理的配置范围可检查").
- ``registry_factory``: builds the provider registry per invocation; tests
  swap in fixture providers without touching production code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .ocr_port import CloudPolicy, ProviderRegistry, default_registry

DATA_DIR_ENV = "CHEMCLAW_LAB_DATA_DIR"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_storage_root() -> Path:
    configured = os.environ.get(DATA_DIR_ENV)
    if configured:
        return Path(configured)
    return _repo_root() / "data"


def default_registry_factory() -> ProviderRegistry:
    registry = default_registry()
    # Deterministic offline providers for the S03 pipeline/eval; they cannot
    # invent content (fixture_ocr requires explicit fixture blocks per call).
    from .ocr_port import FixtureRasterTranscriber

    registry.register(FixtureRasterTranscriber(name="fixture_ocr"))
    registry.register(FixtureRasterTranscriber(name="fixture_ocr_no_geometry", provides_geometry=False))
    return registry


def default_manifest_dir() -> Path:
    """S06: the admin agent-manifest directory (plan §12.3).

    Registry loading is restricted to this root: a caller may only name
    ``config_dir`` values INSIDE it, so manifest files cannot be pulled from
    arbitrary paths. Placing a file here still does not enable it.
    """
    return _repo_root() / "runtime" / "agents"


@dataclass
class ServiceSettings:
    storage_root: Path = field(default_factory=default_storage_root)
    manifest_dir: Path = field(default_factory=default_manifest_dir)
    cloud_policy: CloudPolicy = field(default_factory=lambda: CloudPolicy(
        allow_cloud=False,
        allowed_data_classes=(),
        require_explicit_confirmation=True,
        providers=(),
        notes=(
            "Cloud recognition is disabled by default; enabling it requires "
            "an explicit allowlist (providers, data classes) that is "
            "inspectable via evidence.provider.list."
        ),
    ))
    registry_factory: Callable[[], ProviderRegistry] = default_registry_factory

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        return cls(storage_root=default_storage_root())

    def build_registry(self) -> ProviderRegistry:
        return self.registry_factory()


def default_service_settings() -> ServiceSettings:
    """Settings used when the caller (e.g. CLI/tests) supplies none."""
    return ServiceSettings.from_env()
