"""Minimal Wavefront OBJ loader.

Returns triangle soup data ready for baking (per-corner normals) plus the
unique vertex positions used to build collision hulls. Material, texture
coordinates and groups are ignored: every OBJ file becomes one rigid body.
"""

import numpy as np


class Mesh:
    def __init__(self, positions, faces, corner_normals):
        self.positions = positions          # (V, 3) float64
        self.faces = faces                  # (T, 3) int64, indices into positions
        self.corner_normals = corner_normals  # (T, 3, 3) float32

    @property
    def triangle_count(self):
        return len(self.faces)


def _resolve(index, count):
    # OBJ indices are 1-based; negative indices count back from the end.
    return index - 1 if index > 0 else count + index


def load_obj(path):
    positions = []
    normals = []
    face_v = []
    face_n = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                positions.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("vn "):
                p = line.split()
                normals.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                vi = []
                ni = []
                for token in line.split()[1:]:
                    parts = token.split("/")
                    vi.append(_resolve(int(parts[0]), len(positions)))
                    if len(parts) >= 3 and parts[2]:
                        ni.append(_resolve(int(parts[2]), len(normals)))
                    else:
                        ni.append(-1)
                # Fan-triangulate polygons.
                for k in range(1, len(vi) - 1):
                    face_v.append((vi[0], vi[k], vi[k + 1]))
                    face_n.append((ni[0], ni[k], ni[k + 1]))

    if not positions or not face_v:
        raise ValueError(f"{path}: no geometry found (needs 'v' and 'f' lines)")

    pos = np.asarray(positions, dtype=np.float64)
    faces = np.asarray(face_v, dtype=np.int64)
    fn = np.asarray(face_n, dtype=np.int64)

    smooth = _smooth_vertex_normals(pos, faces)
    corner = smooth[faces]  # (T, 3, 3)
    if normals:
        nrm = np.asarray(normals, dtype=np.float64)
        has = fn >= 0
        corner[has] = nrm[fn[has]]
    lengths = np.linalg.norm(corner, axis=2, keepdims=True)
    corner = corner / np.maximum(lengths, 1e-12)

    return Mesh(pos, faces, corner.astype(np.float32))


def _smooth_vertex_normals(pos, faces):
    a, b, c = pos[faces[:, 0]], pos[faces[:, 1]], pos[faces[:, 2]]
    face_n = np.cross(b - a, c - a)  # area weighted
    vn = np.zeros_like(pos)
    for k in range(3):
        np.add.at(vn, faces[:, k], face_n)
    lengths = np.linalg.norm(vn, axis=1, keepdims=True)
    return vn / np.maximum(lengths, 1e-12)


def normalize(mesh):
    """Center on the bounding box and scale so the largest extent is 1."""
    lo = mesh.positions.min(axis=0)
    hi = mesh.positions.max(axis=0)
    extent = float((hi - lo).max())
    if extent <= 0:
        raise ValueError("mesh has zero size")
    mesh.positions = (mesh.positions - (lo + hi) * 0.5) / extent
    return mesh


def fibonacci_directions(n):
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / n)
    theta = np.pi * (1 + 5 ** 0.5) * i
    return np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)


def support_points(positions, directions=800, chunk=200_000):
    """Vertices that are extreme along many directions: a compact point set whose
    convex hull closely matches the full mesh hull, so MuJoCo hulls stay cheap."""
    if len(positions) <= directions:
        return positions.copy()
    dirs = fibonacci_directions(directions)
    best_val = np.full(directions, -np.inf)
    best_idx = np.zeros(directions, dtype=np.int64)
    for start in range(0, len(positions), chunk):
        block = positions[start:start + chunk]
        proj = block @ dirs.T
        idx = proj.argmax(axis=0)
        val = proj[idx, np.arange(directions)]
        better = val > best_val
        best_val[better] = val[better]
        best_idx[better] = idx[better] + start
    return positions[np.unique(best_idx)]
