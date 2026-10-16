"""Floor heightmap: the optional ground the pile settles on.

Arrays here use world orientation: row 0 is y = 0 (the bottom of the texture)
and sample (r, c) sits at the pixel center ((c + 0.5) * TILE / W, (r + 0.5) * TILE / H).
The floor is treated as tileable, like the pile.
"""

import numpy as np

from .sim import TILE


def load(path, height):
    """World heights from a .npy image (top row first, as the panel copies it out of
    Designer, at the source's bit depth). height: world height of white, as a
    fraction of the tile width. Float images keep values outside 0..1."""
    img = np.load(path)
    if img.ndim == 3:  # gray + alpha: the gray; color: the mean of R, G, B
        img = img[..., 0] if img.shape[2] == 2 else img[..., :3].mean(axis=2, dtype=np.float64)
    if img.dtype == np.uint16:
        values = img.astype(np.float32) / 65535.0
    elif img.dtype == np.uint8:
        values = img.astype(np.float32) / 255.0
    else:
        values = np.nan_to_num(img.astype(np.float32))
    if values.shape[0] < 2 or values.shape[1] < 2:
        raise ValueError("floor heightmap is too small")
    return np.ascontiguousarray(values[::-1]) * float(height) * TILE


def resample(heights, n):
    """Periodic resample to (n, n): bilinear to a multiple of n, then box-filtered,
    so shrinking a big heightmap averages it instead of aliasing."""
    h, w = heights.shape
    factor = max(1, int(round(max(h, w) / n)))
    size = n * factor
    if (h, w) != (size, size):
        heights = _bilinear_periodic(heights, size)
    return heights.reshape(n, factor, n, factor).mean(axis=(1, 3))


def _bilinear_periodic(img, size):
    h, w = img.shape
    centers = (np.arange(size) + 0.5) / size
    fy = centers * h - 0.5
    fx = centers * w - 0.5
    y0 = np.floor(fy).astype(np.int64)
    x0 = np.floor(fx).astype(np.int64)
    ty = (fy - y0)[:, None]
    tx = (fx - x0)[None, :]
    y0, y1 = y0 % h, (y0 + 1) % h
    x0, x1 = x0 % w, (x0 + 1) % w
    top = img[y0][:, x0] * (1 - tx) + img[y0][:, x1] * tx
    bottom = img[y1][:, x0] * (1 - tx) + img[y1][:, x1] * tx
    return (top * (1 - ty) + bottom * ty).astype(np.float32)


def normals(heights):
    """(H, W, 3) unit world normals of the periodic height grid."""
    h, w = heights.shape
    dx = (np.roll(heights, -1, axis=1) - np.roll(heights, 1, axis=1)) / (2.0 * TILE / w)
    dy = (np.roll(heights, -1, axis=0) - np.roll(heights, 1, axis=0)) / (2.0 * TILE / h)
    n = np.stack([-dx, -dy, np.ones_like(heights)], axis=2)
    return (n / np.linalg.norm(n, axis=2, keepdims=True)).astype(np.float32)
