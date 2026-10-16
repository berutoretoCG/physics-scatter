"""Small PNG writer (8/16-bit gray or RGB) using zlib and the PNG 'Up' filter."""

import struct
import zlib

import numpy as np


def _chunk(tag, data):
    out = struct.pack(">I", len(data)) + tag + data
    return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path, img, level=6):
    img = np.ascontiguousarray(img)
    if img.dtype not in (np.uint8, np.uint16):
        raise TypeError("write_png expects uint8 or uint16 data")
    h, w = img.shape[:2]
    channels = 1 if img.ndim == 2 else img.shape[2]
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    bit_depth = 16 if img.dtype == np.uint16 else 8

    data = img.astype(">u2") if bit_depth == 16 else img
    rows = data.reshape(h, -1).view(np.uint8)
    # 'Up' filter: each byte minus the byte above it (mod 256).
    filtered = rows.copy()
    filtered[1:] = rows[1:] - rows[:-1]
    raw = np.empty((h, rows.shape[1] + 1), dtype=np.uint8)
    raw[:, 0] = 2
    raw[0, 0] = 0
    raw[:, 1:] = filtered
    raw[0, 1:] = rows[0]

    header = struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", header))
        f.write(_chunk(b"IDAT", zlib.compress(raw.tobytes(), level)))
        f.write(_chunk(b"IEND", b""))


def to_u8(x):
    return (np.clip(x, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def to_u16(x):
    return (np.clip(x, 0.0, 1.0) * 65535.0 + 0.5).astype(np.uint16)
