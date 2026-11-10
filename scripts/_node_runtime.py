"""Cross-device Node discovery for smoke/live scripts.

Order: explicit CHEMCLAW_NODE_EXE -> PATH -> repo-local candidate. No
machine-specific absolute path is embedded in the deliverable.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find_node() -> str:
    configured = os.environ.get("CHEMCLAW_NODE_EXE")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    candidates = [
        Path(sys.executable).resolve().parent / "node.exe",
        Path(__file__).resolve().parents[1] / "runtime" / "node" / "node.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise SystemExit(
        "Node.js not found. Set CHEMCLAW_NODE_EXE to node.exe or add node to PATH."
    )
