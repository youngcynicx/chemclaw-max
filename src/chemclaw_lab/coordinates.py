"""S03: coordinate systems and derived-image transform chains.

The acceptance test requires exact positioning after rotation, cropping, DPI
changes, and TOPLEFT/BOTTOMLEFT conversions. This module is the single source
of that math:

- Every derived artifact records a transform chain (``transform_v1``).
- :func:`map_geometry_to_original` maps a block's geometry, expressed in the
  coordinate system of the image it was produced on, back to ORIGINAL image
  pixels — inverting the whole chain exactly.
- ``deform_expand`` (e.g. unfolded curved pages) is intentionally
  non-invertible: mapping raises :class:`UnmappableGeometry` and callers must
  present the derived image with a located RANGE instead of fabricating
  original-image coordinates (plan S03 §3, honesty rule).

Conventions (all verified by round-trip tests):
- Forward rotation of the frame content by 90° clockwise on a frame
  (W, H): point (x, y) ↦ (H − y, x); frame becomes (H, W).
- Forward 270° clockwise (= 90° counter-clockwise): (x, y) ↦ (y, W − x).
- Forward 180°: (x, y) ↦ (W − x, H − y).
- Inverses use direct formulas (see ``_invert_rotate_rect``), verified by
  round-trips; they never re-run forward math with a swapped angle.

Frame sizes: the caller always knows the producing frame (stored with the
block's coordinate_system) and the original frame (read from the original
file header). The chain is walked forward from the original frame to record
exact per-step frames, then walked backwards — no size guessing.

All functions are pure and stdlib-only.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .validation import load_named_schema, validate_payload_against_schema


TRANSFORM_SCHEMA_NAME = "transform_v1.json"


class UnmappableGeometry(ValueError):
    """The transform chain contains a non-invertible step."""


class GeometryError(ValueError):
    """Malformed geometry or missing frame information."""


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float


def validate_transform(transform: dict[str, Any]) -> None:
    validate_payload_against_schema(transform, load_named_schema(TRANSFORM_SCHEMA_NAME))


def parse_chain(transform_json: str | dict[str, Any] | None) -> list[dict[str, Any]]:
    if transform_json is None:
        return []
    if isinstance(transform_json, str):
        transform_json = json.loads(transform_json)
    validate_transform(transform_json)
    return list(transform_json.get("steps", []))


def chain_summary(steps: list[dict[str, Any]]) -> dict[str, Any]:
    invertible = all(step["op"] != "deform_expand" for step in steps)
    return {
        "steps": steps,
        "invertible": invertible,
        "step_count": len(steps),
        "ops": [step["op"] for step in steps],
    }


def derived_frame_size(
    producing_size: tuple[float, float], steps: list[dict[str, Any]]
) -> tuple[float, float]:
    """Frame size after applying the chain forward to ``producing_size``."""
    width, height = float(producing_size[0]), float(producing_size[1])
    for step in steps:
        op = step["op"]
        if op == "rotate90":
            width, height = height, width
        elif op == "crop":
            width, height = float(step["width"]), float(step["height"])
        elif op == "rescale_dpi":
            source_dpi = float(step["source_dpi"])
            target_dpi = float(step["target_dpi"])
            if source_dpi <= 0 or target_dpi <= 0:
                raise GeometryError("rescale_dpi requires positive dpi values")
            width, height = width * target_dpi / source_dpi, height * target_dpi / source_dpi
        elif op == "deform_expand":
            raise UnmappableGeometry("deform_expand has no deterministic output size")
    return width, height


def forward_map_rect(
    rect: Rect, steps: list[dict[str, Any]], original_size: tuple[float, float]
) -> Rect:
    """Forward-map a TOPLEFT-pixel rect from the original frame through the chain."""
    recorded = _recorded_frames(original_size, steps)
    width, height = float(original_size[0]), float(original_size[1])
    geom = rect
    for index, step in enumerate(steps):
        op = step["op"]
        if op == "rotate90":
            geom = _forward_rotate_rect(geom, int(step["angle"]), width, height)
        elif op == "rescale_dpi":
            scale = float(step["target_dpi"]) / float(step["source_dpi"])
            geom = Rect(geom.x * scale, geom.y * scale, geom.width * scale, geom.height * scale)
        elif op == "crop":
            # The rect is given in PRE-crop coordinates; later steps operate
            # in the cropped frame, so translate it into that frame.
            geom = Rect(
                geom.x - float(step["x"]),
                geom.y - float(step["y"]),
                geom.width,
                geom.height,
            )
        elif op == "deform_expand":
            raise UnmappableGeometry("forward mapping through deform_expand is undefined")
        width, height = recorded[index]
    return geom


def map_geometry_to_original(
    geometry: dict[str, Any],
    coordinate_system: dict[str, Any],
    steps: list[dict[str, Any]],
    original_size: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Map block geometry from its producing image back to ORIGINAL pixels.

    ``coordinate_system`` describes the image the geometry was produced on:
    ``origin`` (TOPLEFT/BOTTOMLEFT), ``units`` (pixel/normalized), and the
    ``frame_width``/``frame_height`` of THAT image. ``original_size`` is the
    original image's (width, height) — required whenever the chain contains a
    crop, and always supplied by the ingestion service.
    """
    validate_transform({"steps": steps})

    origin = coordinate_system.get("origin", "TOPLEFT")
    units = coordinate_system.get("units", "pixel")
    if units not in ("pixel", "normalized"):
        raise GeometryError(f"unsupported units: {units!r}")
    if origin not in ("TOPLEFT", "BOTTOMLEFT"):
        raise GeometryError(f"unsupported origin: {origin!r}")

    frame_w = coordinate_system.get("frame_width")
    frame_h = coordinate_system.get("frame_height")
    if frame_w is None or frame_h is None:
        raise GeometryError(
            "coordinate_system requires frame_width/frame_height of the image "
            "the geometry was produced on"
        )
    producing_size = (float(frame_w), float(frame_h))

    geom = Rect(
        float(geometry["x"]),
        float(geometry["y"]),
        float(geometry["width"]),
        float(geometry["height"]),
    )

    if units == "normalized":
        geom = Rect(
            geom.x * producing_size[0],
            geom.y * producing_size[1],
            geom.width * producing_size[0],
            geom.height * producing_size[1],
        )

    if origin == "BOTTOMLEFT":
        geom = Rect(geom.x, producing_size[1] - geom.y - geom.height, geom.width, geom.height)

    if not steps:
        mapped = geom
    else:
        if original_size is None:
            if any(step["op"] == "crop" for step in steps):
                raise GeometryError(
                    "mapping through a chain with crop requires the original "
                    "image size"
                )
            original_size = _reconstruct_original_size(producing_size, steps)
        mapped = _invert_chain(geom, steps, original_size)

    result = {
        "x": round(mapped.x, 6),
        "y": round(mapped.y, 6),
        "width": round(mapped.width, 6),
        "height": round(mapped.height, 6),
        "origin": "TOPLEFT",
        "units": "pixel",
        "image_ref": "original",
    }
    if min(result["x"], result["y"], result["width"], result["height"]) < -1e-6:
        raise GeometryError("mapped geometry falls outside the original frame")
    return result


def _recorded_frames(
    original_size: tuple[float, float], steps: list[dict[str, Any]]
) -> list[tuple[float, float]]:
    """Frame size AFTER each step, walking forward from the original frame."""
    frames: list[tuple[float, float]] = []
    width, height = float(original_size[0]), float(original_size[1])
    for step in steps:
        op = step["op"]
        if op == "rotate90":
            width, height = height, width
        elif op == "crop":
            width, height = float(step["width"]), float(step["height"])
        elif op == "rescale_dpi":
            scale = float(step["target_dpi"]) / float(step["source_dpi"])
            width, height = width * scale, height * scale
        elif op == "deform_expand":
            raise UnmappableGeometry("deform_expand has no deterministic output size")
        frames.append((width, height))
    return frames


def _invert_chain(
    geom: Rect, steps: list[dict[str, Any]], original_size: tuple[float, float]
) -> Rect:
    """Invert the chain step by step using exact recorded frame sizes."""
    recorded = _recorded_frames(original_size, steps)
    result = geom
    for index in range(len(steps) - 1, -1, -1):
        step = steps[index]
        # The geometry currently lives in the POST-step frame.
        post_w, post_h = recorded[index]
        # The PRE-step frame: the frame this step operated on.
        pre_w, pre_h = recorded[index - 1] if index > 0 else original_size
        op = step["op"]
        if op == "rotate90":
            result = _invert_rotate_rect(result, int(step["angle"]), post_w, post_h)
        elif op == "crop":
            result = Rect(
                result.x + float(step["x"]),
                result.y + float(step["y"]),
                result.width,
                result.height,
            )
            _ = (pre_w, pre_h)
        elif op == "rescale_dpi":
            scale_back = float(step["source_dpi"]) / float(step["target_dpi"])
            result = Rect(
                result.x * scale_back,
                result.y * scale_back,
                result.width * scale_back,
                result.height * scale_back,
            )
        elif op == "deform_expand":
            raise UnmappableGeometry(
                "geometry was produced on a deformed (unfolded) derived image; "
                "original-image coordinates cannot be recovered — present the "
                "derived image and the located range instead"
            )
    return result


def _forward_rotate_rect(rect: Rect, angle: int, width: float, height: float) -> Rect:
    """Forward-rotate a TOPLEFT rect with the frame content (clockwise)."""
    if angle == 90:
        # (x, y) -> (H - y, x); frame (W, H) -> (H, W)
        return Rect(height - rect.y - rect.height, rect.x, rect.height, rect.width)
    if angle == 180:
        return Rect(
            width - rect.x - rect.width,
            height - rect.y - rect.height,
            rect.width,
            rect.height,
        )
    if angle == 270:
        # (x, y) -> (y, W - x); frame (W, H) -> (H, W)
        return Rect(rect.y, width - rect.x - rect.width, rect.height, rect.width)
    raise GeometryError(f"unsupported rotation angle: {angle}")


def _invert_rotate_rect(rect: Rect, angle: int, width: float, height: float) -> Rect:
    """Inverse of forward rotation, with the rect living in the POST-rotation
    frame (width, height). Direct formulas, verified by round-trip tests."""
    if angle == 90:
        # forward: (x, y) -> (H - y, x). Inverse: (x, y) -> (y, W - x - w).
        return Rect(rect.y, width - rect.x - rect.width, rect.height, rect.width)
    if angle == 180:
        return Rect(
            width - rect.x - rect.width,
            height - rect.y - rect.height,
            rect.width,
            rect.height,
        )
    if angle == 270:
        # forward: (x, y) -> (y, W - x). Inverse: (x, y) -> (H - y - h, x).
        return Rect(height - rect.y - rect.height, rect.x, rect.height, rect.width)
    raise GeometryError(f"unsupported rotation angle: {angle}")


def _reconstruct_original_size(
    producing_size: tuple[float, float], steps: list[dict[str, Any]]
) -> tuple[float, float]:
    """Original frame size for crop-free chains, by inverting size effects."""
    width, height = producing_size
    for step in reversed(steps):
        op = step["op"]
        if op == "rotate90":
            width, height = height, width
        elif op == "rescale_dpi":
            scale = float(step["source_dpi"]) / float(step["target_dpi"])
            width, height = width * scale, height * scale
    return width, height


def almost_equal(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=tol)
