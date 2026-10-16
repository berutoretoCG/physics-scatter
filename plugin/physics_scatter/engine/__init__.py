"""Physics Scatter engine: simulate a pile of meshes and bake it to texture maps.

Runs out-of-process (Designer's bundled python.exe with the vendored packages on
PYTHONPATH) so heavy work never blocks the Designer UI:

    python -m physics_scatter.engine job.json

Progress is reported as JSON lines on stdout.
"""

import colorsys
import json
import os
import sys
import time
import traceback

import numpy as np

from . import floor as floor_map
from . import objload, pngio
from .bake import bake
from .sim import TILE, simulate

OUTPUTS = ("height", "normal", "roughness", "metallic", "mask", "object_id", "object_random")


def emit(kind, **payload):
    payload["type"] = kind
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _progress(stage, lo, hi):
    def report(fraction, message=""):
        emit("progress", stage=stage, value=lo + (hi - lo) * fraction, message=message)
    return report


def _object_materials(job, objects, rng):
    rough_v = float(job.get("variation", {}).get("roughness", 0.0))
    n = len(objects)
    rough = np.zeros(n, np.float32)
    metal = np.zeros(n, np.float32)
    for k, obj in enumerate(objects):
        spec = job["meshes"][obj["mesh"]]
        rough[k] = np.clip(spec.get("roughness", 0.5) + rng.uniform(-rough_v, rough_v), 0.0, 1.0)
        metal[k] = np.clip(spec.get("metallic", 0.0), 0.0, 1.0)
    return rough, metal


def _load_floor(job):
    """World heights of the floor heightmap, or None for flat ground."""
    spec = job.get("floor") or {}
    if not spec.get("data"):
        return None
    return floor_map.load(spec["data"], float(spec.get("height", 0.1)))


def _id_colors(n, rng):
    # Distinct, saturated colors so neighbouring objects are easy to tell apart.
    hues = (np.arange(n) * 0.618033988749895 + rng.random()) % 1.0
    rng.shuffle(hues)
    sats = 0.55 + 0.45 * rng.random(n)
    vals = 0.6 + 0.4 * rng.random(n)
    return np.array([colorsys.hsv_to_rgb(h, s, v) for h, s, v in zip(hues, sats, vals)], np.float32)


def run(job_path):
    t0 = time.time()
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)
    out_dir = job["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    suffix = job.get("file_suffix", "")
    seed = int(job.get("seed", 1))
    rng = np.random.default_rng(seed)

    # Load meshes.
    emit("progress", stage="load", value=0.0, message="loading meshes")
    meshes = []
    supports = []
    for spec in job["meshes"]:
        mesh = objload.normalize(objload.load_obj(spec["path"]))
        meshes.append(mesh)
        supports.append(objload.support_points(mesh.positions))

    # One entry per object instance, with its own size.
    objects = []
    for mi, spec in enumerate(job["meshes"]):
        size = float(spec.get("size", 0.1)) * TILE
        var = float(spec.get("size_variation", 0.0))
        for _ in range(int(spec.get("count", 10))):
            s = size * float(np.clip(1.0 + rng.uniform(-var, var), 0.05, 10.0))
            objects.append({"mesh": mi, "scale": s, "points": supports[mi]})
    if not objects:
        raise ValueError("nothing to scatter: all mesh counts are 0")

    floor = _load_floor(job)
    poses, sim_stats = simulate(objects, job.get("physics", {}), seed, _progress("simulate", 0.02, 0.75), floor)

    res = int(job.get("resolution", 2048))
    ss = 2 if res <= 2048 else 1
    in_maps = floor is not None and job["floor"].get("in_maps", True)
    baked = bake(meshes, objects, poses, res, supersample=ss, progress=_progress("bake", 0.75, 0.9),
                 floor=floor if in_maps else None)

    emit("progress", stage="write", value=0.9, message="writing maps")
    cov = baked.coverage
    ids = baked.object_id
    hit = ids >= 0
    safe_ids = np.where(hit, ids, 0)
    rough, metal = _object_materials(job, objects, rng)

    z_peak = float(baked.z.max())
    height_scale = float(job.get("height_scale", 0.0)) or max(z_peak, 1e-6)
    height = baked.z / height_scale

    n = baked.normal
    green = n[..., 1] if job.get("normal_format", "directx") == "opengl" else -n[..., 1]
    normal_rgb = np.stack([n[..., 0], green, n[..., 2]], axis=2) * 0.5 + 0.5

    # Material maps: per-object values, anti-aliased with coverage at the edges.
    roughness = rough[safe_ids] * cov
    metallic = metal[safe_ids] * cov
    id_rgb = np.where(hit[..., None], _id_colors(len(objects), rng)[safe_ids], 0.0)
    rand = rng.random(len(objects)).astype(np.float32) * 0.98 + 0.02
    object_random = np.where(hit, rand[safe_ids], 0.0)

    files = {}

    def save(name, img):
        path = os.path.join(out_dir, f"{name}{suffix}.png")
        pngio.write_png(path, img)
        files[name] = path

    save("height", pngio.to_u16(height))
    save("normal", pngio.to_u8(normal_rgb))
    save("roughness", pngio.to_u8(roughness))
    save("metallic", pngio.to_u8(metallic))
    save("mask", pngio.to_u8(cov))
    save("object_id", pngio.to_u8(id_rgb))
    save("object_random", pngio.to_u8(object_random))

    # Small shaded preview for the panel: clay objects, each a slightly different shade.
    step = max(1, res // 512)
    light = np.array([-0.45, 0.55, 0.7])
    light /= np.linalg.norm(light)
    lambert = np.clip((n * light).sum(axis=2), 0.0, 1.0)
    shade = 0.3 + 0.7 * lambert * (0.55 + 0.45 * np.clip(height, 0, 1))
    clay = np.array([0.62, 0.58, 0.52], np.float32) * (0.8 + 0.4 * rand[safe_ids])[..., None]
    ground = np.array([0.2, 0.2, 0.21], np.float32)
    preview = (clay * cov[..., None] + ground * (1 - cov[..., None])) * shade[..., None]
    preview_path = os.path.join(out_dir, f"preview{suffix}.png")
    pngio.write_png(preview_path, pngio.to_u8(preview[::step, ::step]))

    result = {
        "files": files,
        "preview": preview_path,
        "object_count": len(objects),
        "resolution": res,
        "height_scale": height_scale,
        "height_scale_relative": height_scale / TILE,
        "coverage": float(cov.mean()),
        "simulation": sim_stats,
        "total_time": time.time() - t0,
    }
    with open(os.path.join(out_dir, f"result{suffix}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    emit("done", result=result)
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: python -m physics_scatter.engine job.json\n")
        return 2
    try:
        run(argv[0])
        return 0
    except Exception as exc:  # report to the panel instead of a silent crash
        emit("error", message=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())
        return 1
