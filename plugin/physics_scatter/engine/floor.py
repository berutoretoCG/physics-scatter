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


def collision_grid(heights, n, chunk_rows=16):
    """(n, n) heightfield samples, at the cell centers of an n x n grid, that never
    dip below the floor.

    MuJoCo joins the samples into two triangles per cell, split along the diagonal
    from sample (r, c) to (r + 1, c + 1). Averaged or point samples cut through
    every peak narrower than a cell, and objects then sink into the floor's fine
    detail (a pile used as the floor of another pile). So each sample starts at the
    floor's height there and is raised by the most that any triangle it is a corner
    of falls short of the floor. That raises every triangle to at least the floor,
    everywhere in it, at the floor image's own resolution, and only around the
    details the grid is too coarse for."""
    k = max(1, -(-max(heights.shape) // n))  # fine pixels per cell: at least the floor's own resolution
    m = n * k
    fine = heights if heights.shape == (m, m) else _bilinear_periodic(heights, m)
    fine = _dilate(fine)  # also covers the floor between the fine samples
    # Cell c spans fine pixels c*k + s ... c*k + s + k - 1; u, v: their position in the cell (0..1).
    s = k // 2
    fine = np.roll(fine, (-s, -s), axis=(0, 1)).reshape(n, k, n, k).transpose(0, 2, 1, 3)
    t = (np.arange(k) + s + 0.5) / k - 0.5
    u, v = t[None, :], t[:, None]  # u along x (columns), v along y (rows)
    lower = u >= v  # the triangle (r, c), (r + 1, c + 1), (r, c + 1)

    h = _bilinear_periodic(heights, n).astype(np.float64)
    h00, h10 = h, np.roll(h, -1, axis=1)
    h01, h11 = np.roll(h, -1, axis=0), np.roll(h10, -1, axis=0)
    short_lower = np.empty((n, n))
    short_upper = np.empty((n, n))
    for r0 in range(0, n, chunk_rows):
        rows = slice(r0, r0 + chunk_rows)
        a, b, c, d = (x[rows, :, None, None] for x in (h00, h10, h01, h11))
        surface = np.where(lower, a + u * (b - a) + v * (d - b), a + v * (c - a) + u * (d - c))
        deficit = fine[rows] - surface
        short_lower[rows] = np.where(lower, deficit, -np.inf).max(axis=(2, 3))
        short_upper[rows] = np.where(lower, -np.inf, deficit).max(axis=(2, 3))
    # Sample (r, c) is a corner of both triangles of cell (r, c) and of cell (r - 1, c - 1),
    # of the lower one of cell (r, c - 1) and of the upper one of cell (r - 1, c).
    both = np.maximum(short_lower, short_upper)
    lift = np.maximum.reduce([both, np.roll(both, (1, 1), axis=(0, 1)), np.roll(short_lower, 1, axis=1),
                              np.roll(short_upper, 1, axis=0), np.zeros((n, n))])
    return (h + lift).astype(np.float32)


def _dilate(img):
    """Periodic 3 x 3 maximum."""
    img = np.maximum(img, np.maximum(np.roll(img, 1, axis=0), np.roll(img, -1, axis=0)))
    return np.maximum(img, np.maximum(np.roll(img, 1, axis=1), np.roll(img, -1, axis=1)))


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
