"""S03: minimal stdlib-only PNG support for real derived artifacts.

Scope is deliberate: the ingestion pipeline generates its own fixture PNGs in
tests and needs genuinely materialized derived versions (rotate / crop /
preview) to verify the transform chain and geometry mapping against REAL
pixels. External camera/scanner images (JPEG, complex PNG variants) are
stored unchanged and get quality flag ``preview_pending`` — no re-encoding
attempt, no fabricated previews.

Supported decode subset: 8-bit, color type 0 (gray) or 2 (RGB), filter 0,
single IDAT, no interlace. That covers every PNG this repo produces.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any


class PngError(ValueError):
    """Unsupported or malformed PNG for this minimal codec."""


def encode_png(width: int, height: int, rgb_rows: list[bytes]) -> bytes:
    """Encode 8-bit RGB pixels (one bytes per row, 3*width bytes)."""
    if len(rgb_rows) != height:
        raise PngError("row count mismatch")
    for row in rgb_rows:
        if len(row) != width * 3:
            raise PngError("row width mismatch")
    raw = b"".join(b"\x00" + row for row in rgb_rows)  # filter 0 per scanline

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def decode_png(data: bytes) -> tuple[int, int, list[bytes]]:
    """Decode the supported subset. Returns (width, height, rgb_rows)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("not a PNG file")
    pos = 8
    width = height = None
    bit_depth = color_type = interlace = None
    idat = b""
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos : pos + 8])
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif tag == b"IDAT":
            idat += chunk_data
        elif tag == b"IEND":
            break
    if width is None or not idat:
        raise PngError("missing IHDR or IDAT")
    if bit_depth != 8 or interlace != 0:
        raise PngError(f"unsupported PNG variant (depth={bit_depth}, interlace={interlace})")
    if color_type == 2:
        bpp = 3
    elif color_type == 0:
        bpp = 1
    else:
        raise PngError(f"unsupported color type {color_type}")

    raw = zlib.decompress(idat)
    stride = width * bpp
    if len(raw) < (stride + 1) * height:
        raise PngError("truncated PNG data")
    prev = bytearray(stride)
    rows: list[bytes] = []
    offset = 0
    for _ in range(height):
        filter_byte = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        offset += 1 + stride
        if filter_byte != 0:
            _unfilter(filter_byte, line, prev, bpp)
        prev = bytearray(line)
        if color_type == 0:
            line = bytes(bytearray(line[i] for i in range(width) for _ in range(3)))
        rows.append(bytes(line))
    return width, height, rows


def _unfilter(ftype: int, line: bytearray, prev: bytearray, bpp: int) -> None:
    if ftype == 1:  # Sub
        for i in range(bpp, len(line)):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:  # Up
        for i in range(len(line)):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:  # Average
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + (left + prev[i]) // 2) & 0xFF
    elif ftype == 4:  # Paeth
        for i in range(len(line)):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pr) & 0xFF
    else:
        raise PngError(f"unsupported filter type {ftype}")


def get_png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("not a PNG file")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def rotate_rows(
    rows: list[bytes], angle: int, width: int, height: int
) -> tuple[int, int, list[bytes]]:
    """Rotate RGB pixel rows clockwise by 90/180/270 degrees."""
    if angle == 90:
        new_rows = []
        for x in range(width):
            new_rows.append(
                b"".join(rows[height - 1 - y][x * 3 : x * 3 + 3] for y in range(height))
            )
        return height, width, new_rows
    if angle == 180:
        new_rows = [
            b"".join(
                rows[height - 1 - y][(width - 1 - x) * 3 : (width - x) * 3]
                for x in range(width)
            )
            for y in range(height)
        ]
        return width, height, new_rows
    if angle == 270:
        new_rows = []
        for x in range(width - 1, -1, -1):
            new_rows.append(
                b"".join(rows[y][x * 3 : x * 3 + 3] for y in range(height))
            )
        return height, width, new_rows
    raise PngError(f"unsupported rotation angle: {angle}")


def crop_rows(
    rows: list[bytes], x: int, y: int, width: int, height: int, src_width: int
) -> list[bytes]:
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise PngError("invalid crop rectangle")
    if x + width > src_width or y + height > len(rows):
        raise PngError("crop rectangle outside image")
    return [rows[row_y][x * 3 : (x + width) * 3] for row_y in range(y, y + height)]


def downscale_rows(
    rows: list[bytes], width: int, height: int, target_width: int
) -> tuple[int, int, list[bytes]]:
    """Nearest-neighbour downscale for preview generation."""
    if target_width <= 0 or target_width >= width:
        raise PngError("preview target width must be in (0, width)")
    target_height = max(1, round(height * target_width / width))
    new_rows = []
    for ty in range(target_height):
        sy = min(height - 1, ty * height // target_height)
        row = bytearray()
        for tx in range(target_width):
            sx = min(width - 1, tx * width // target_width)
            row += rows[sy][sx * 3 : sx * 3 + 3]
        new_rows.append(bytes(row))
    return target_width, target_height, new_rows


def solid_png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Uniform-color PNG — the base for drawn fixtures."""
    row = bytes(bytearray(byte for _ in range(width) for byte in color))
    return encode_png(width, height, [row] * height)


def stamp_region_png(
    width: int,
    height: int,
    color: tuple[int, int, int],
    rect: tuple[int, int, int, int],
    ink: tuple[int, int, int],
) -> bytes:
    """Uniform PNG with one darker rectangle — a synthetic 'handwriting' mark."""
    x0, y0, w0, h0 = rect
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            if x0 <= x < x0 + w0 and y0 <= y < y0 + h0:
                row += bytes(ink)
            else:
                row += bytes(color)
        rows.append(bytes(row))
    return encode_png(width, height, rows)


def png_color_at(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    width, height, rows = decode_png(data)
    if not (0 <= x < width and 0 <= y < height):
        raise PngError("pixel outside image")
    off = x * 3
    return rows[y][off], rows[y][off + 1], rows[y][off + 2]


def describe_png(data: bytes) -> dict[str, Any]:
    width, height = get_png_size(data)
    return {"format": "png", "width": width, "height": height}
