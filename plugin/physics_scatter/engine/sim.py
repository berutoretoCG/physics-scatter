"""Rigid-body pile simulation with MuJoCo on a periodic (tileable) square.

Every body has 8 kinematic "ghost" copies offset by one tile in each direction.
Ghosts collide with real bodies only, so objects near one edge feel the objects
near the opposite edge. Bodies that leave the tile are wrapped back in, which
keeps the pile seamless when the result is baked as a tiling texture.
"""

import math
import time

import mujoco
import numpy as np

TILE = 4.0  # world units per texture tile; objects are sized as a fraction of it

# Speed/accuracy presets. The constraint solver dominates the cost of big piles:
# CG with a capped iteration count is ~2x faster than Newton and stable enough here.
# On a floor heightmap a thin object can touch many prism walls at once; CG then
# fails to converge and the pile explodes, so a floor always uses Newton.
# Floor collisions cost about as much as the solver: the heightfield gets
# floor_cells samples across the smallest objects, at most floor_res per tile.
QUALITY = {
    "low": dict(dt=0.004, timeconst=0.012, solver="cg", iterations=10, hull_vertices=24, condim=3,
                floor_cells=3, floor_res=64),
    "medium": dict(dt=0.002, timeconst=0.006, solver="cg", iterations=15, hull_vertices=32, condim=3,
                   floor_cells=4, floor_res=128),
    "high": dict(dt=0.001, timeconst=0.003, solver="newton", iterations=50, hull_vertices=64, condim=4,
                 floor_cells=6, floor_res=256),
}

OFFSETS = np.array(
    [(dx, dy, 0.0) for dx in (-TILE, 0.0, TILE) for dy in (-TILE, 0.0, TILE) if (dx, dy) != (0.0, 0.0)]
)


def _random_quats(rng, n):
    # Uniformly distributed unit quaternions (w, x, y, z).
    u1, u2, u3 = rng.random((3, n))
    q = np.stack([
        np.sqrt(u1) * np.cos(2 * np.pi * u3),
        np.sqrt(1 - u1) * np.sin(2 * np.pi * u2),
        np.sqrt(1 - u1) * np.cos(2 * np.pi * u2),
        np.sqrt(u1) * np.sin(2 * np.pi * u3),
    ], axis=1)
    return q


def _spawn_layout(rng, radii, z0=0.0):
    """Jittered grid in stacked layers above height z0 so nothing overlaps at t=0."""
    n = len(radii)
    cell = 2.0 * radii.max() * 1.05
    per_side = max(1, int(TILE // cell))
    cell_xy = TILE / per_side
    per_layer = per_side * per_side
    layer_step = cell * 1.5

    pos = np.zeros((n, 3))
    order = rng.permutation(n)
    for k, i in enumerate(order):
        layer, slot = divmod(k, per_layer)
        if slot == 0:
            slots = rng.permutation(per_layer)
        cx, cy = divmod(int(slots[slot]), per_side)
        slack = max(0.0, (cell_xy - 2.0 * radii[i]) * 0.5)
        pos[i, 0] = (cx + 0.5) * cell_xy + rng.uniform(-slack, slack)
        pos[i, 1] = (cy + 0.5) * cell_xy + rng.uniform(-slack, slack)
        pos[i, 2] = z0 + 0.01 + radii[i] + layer * layer_step + rng.uniform(0.0, cell * 0.5)
    pos[:, :2] %= TILE
    return pos


class _Sim:
    """Compiled model plus the index arrays the step loop needs."""


def _add_ground(spec, floor, samples, margin):
    """Flat plane at z=0, or a heightfield of the floor. The heightfield extends
    past the tile by `margin` with wrapped copies, so objects hanging over an
    edge rest on the floor from the opposite side. Returns the geom and the
    floor's highest point."""
    ground = spec.worldbody.add_geom()
    if floor is None:
        ground.type = mujoco.mjtGeom.mjGEOM_PLANE
        ground.size = [0.0, 0.0, 1.0]
        return ground, 0.0

    from .floor import resample  # floor imports TILE from here
    grid = resample(floor, min(samples, max(floor.shape)))
    cell = TILE / grid.shape[0]
    pad = int(math.ceil(margin / cell)) + 1
    grid = np.pad(grid, pad, mode="wrap")
    lo, hi = float(grid.min()), float(grid.max())
    if hi - lo < 1e-6 * TILE:
        ground.type = mujoco.mjtGeom.mjGEOM_PLANE
        ground.size = [0.0, 0.0, 1.0]
        ground.pos = [0.0, 0.0, lo]
        return ground, hi

    # Row r of the grid is at y = (r - pad + 0.5) * cell: the samples are centered
    # on the tile. MuJoCo rescales the data to 0..1 and stretches it to lo..hi.
    hfield = spec.add_hfield()
    hfield.name = "floor"
    hfield.nrow, hfield.ncol = grid.shape
    half = (grid.shape[0] - 1) * cell * 0.5
    hfield.size = [half, half, hi - lo, 0.1 * TILE]
    hfield.userdata = grid.ravel().tolist()
    ground.type = mujoco.mjtGeom.mjGEOM_HFIELD
    ground.hfieldname = hfield.name
    ground.pos = [TILE * 0.5, TILE * 0.5, lo]
    return ground, hi


def build(objects, physics, seed, floor=None):
    """objects: list of dicts with 'points' ((K,3) hull support points in unit
    mesh space) and 'scale' (world size). floor: optional (H,W) world heights
    (see floor.py), else the ground is flat at z=0."""
    rng = np.random.default_rng(seed)
    n = len(objects)
    preset = dict(QUALITY.get(physics.get("quality", "medium"), QUALITY["medium"]))
    if floor is not None:
        preset["solver"] = "newton"
    preset.update({k: physics[k] for k in ("hull_vertices", "condim", "solver", "iterations", "floor_res")
                   if k in physics})
    dt, timeconst = preset["dt"], preset["timeconst"]
    friction = float(physics.get("friction", 0.7))
    bounce = float(np.clip(physics.get("bounciness", 0.1), 0.0, 1.0))
    dampratio = 1.0 - 0.9 * bounce
    drag = 3.0 * (1.0 - 0.7 * bounce)  # air drag (1/s): caps fall speed, speeds up settling
    hull_vertices = int(preset["hull_vertices"])
    condim = int(preset["condim"])

    radii = np.array([np.linalg.norm(o["points"], axis=1).max() * o["scale"] for o in objects])
    small = float(np.percentile([o["scale"] for o in objects], 10))
    floor_samples = int(np.clip(math.ceil(preset["floor_cells"] * TILE / small), 32, preset["floor_res"]))

    spec = mujoco.MjSpec()
    spec.option.timestep = dt
    spec.option.gravity = [0.0, 0.0, -9.81]
    spec.option.cone = mujoco.mjtCone.mjCONE_PYRAMIDAL
    spec.option.solver = {"newton": mujoco.mjtSolver.mjSOL_NEWTON, "cg": mujoco.mjtSolver.mjSOL_CG,
                          "pgs": mujoco.mjtSolver.mjSOL_PGS}[preset["solver"]]
    spec.option.iterations = int(preset["iterations"])

    ground, floor_top = _add_ground(spec, floor, floor_samples, radii.max())
    ground.contype = 1
    ground.conaffinity = 1
    ground.friction = [friction, 0.005, 0.0001]
    ground.solref = [timeconst, dampratio]

    start_pos = _spawn_layout(rng, radii, floor_top)
    start_quat = _random_quats(rng, n)

    body_names = []
    ghost_names = []
    for i, obj in enumerate(objects):
        size = obj["scale"]
        mesh = spec.add_mesh()
        mesh.name = f"m{i}"
        mesh.uservert = (obj["points"] * size).ravel().tolist()
        if hull_vertices > 0:
            mesh.maxhullvert = max(4, hull_vertices)

        geom_kw = dict(
            type=mujoco.mjtGeom.mjGEOM_MESH,
            meshname=mesh.name,
            condim=condim,
            friction=[friction, 0.01 * size, 0.001 * size],
            solref=[timeconst, dampratio],
        )

        body = spec.worldbody.add_body()
        body.name = f"b{i}"
        body.pos = start_pos[i].tolist()
        body.quat = start_quat[i].tolist()
        body.add_freejoint()
        g = body.add_geom()
        for k, v in geom_kw.items():
            setattr(g, k, v)
        g.contype = 1
        g.conaffinity = 3
        g.density = 1000.0
        body_names.append(body.name)

        names = []
        for j, off in enumerate(OFFSETS):
            ghost = spec.worldbody.add_body()
            ghost.name = f"g{i}_{j}"
            ghost.mocap = True
            ghost.pos = (start_pos[i] + off).tolist()
            ghost.quat = start_quat[i].tolist()
            gg = ghost.add_geom()
            for k, v in geom_kw.items():
                setattr(gg, k, v)
            gg.contype = 2
            gg.conaffinity = 0
            names.append(ghost.name)
        ghost_names.append(names)

    model = spec.compile()
    data = mujoco.MjData(model)

    body_ids = np.array([model.body(nm).id for nm in body_names])
    jnt_ids = model.body_jntadr[body_ids]
    qadr = model.jnt_qposadr[jnt_ids]
    vadr = model.jnt_dofadr[jnt_ids]
    ghost_mocap = np.array([[model.body_mocapid[model.body(nm).id] for nm in row] for row in ghost_names])

    # Air drag as joint damping proportional to mass / inertia.
    for k in range(n):
        m = model.body_mass[body_ids[k]]
        inertia = float(model.body_inertia[body_ids[k]].mean())
        model.dof_damping[vadr[k]:vadr[k] + 3] = drag * m
        model.dof_damping[vadr[k] + 3:vadr[k] + 6] = drag * inertia

    st = _Sim()
    st.model, st.data = model, data
    st.dt = dt
    st.pos_idx = qadr[:, None] + np.arange(3)
    st.quat_idx = qadr[:, None] + np.arange(3, 7)
    st.lin_idx = vadr[:, None] + np.arange(3)
    st.ang_idx = vadr[:, None] + np.arange(3, 6)
    st.ghost_flat = ghost_mocap.ravel()
    # Safety net: nothing in a settling pile moves (or spins its surface) faster
    # than a few times its terminal fall speed, so clamping there stops a bad
    # contact from blowing up the sim without touching normal motion.
    st.v_max = 4.0 * 9.81 / drag
    st.w_max = (4.0 * st.v_max / np.maximum(radii, 1e-6))[:, None]  # coins can spin fast
    # Time budget: long enough for the top layer to fall at terminal speed, then settle.
    fall = start_pos[:, 2].max() / (9.81 / drag) + 1.0
    st.t_max = max(float(physics.get("max_time", 8.0)), fall + 2.0)
    st.t_min = min(fall, st.t_max)
    return st


def step(st):
    data = st.data
    q = data.qpos
    p = q[st.pos_idx]
    data.mocap_pos[st.ghost_flat] = (p[:, None, :] + OFFSETS[None, :, :]).reshape(-1, 3)
    data.mocap_quat[st.ghost_flat] = np.repeat(q[st.quat_idx], len(OFFSETS), axis=0)
    mujoco.mj_step(st.model, data)
    _clamp(data.qvel, st.lin_idx, st.v_max)
    _clamp(data.qvel, st.ang_idx, st.w_max)
    xy_idx = st.pos_idx[:, :2]
    xy = data.qpos[xy_idx]
    if (xy < 0).any() or (xy >= TILE).any():
        data.qpos[xy_idx] = np.mod(xy, TILE)


def _clamp(qvel, idx, limit):
    v = qvel[idx]
    speed = np.linalg.norm(v, axis=1, keepdims=True)
    if (speed > limit).any():
        qvel[idx] = v * np.minimum(1.0, limit / np.maximum(speed, 1e-12))


def simulate(objects, physics, seed, progress=None, floor=None):
    """Returns (N,7) poses: x, y, z, qw, qx, qy, qz, and run statistics."""
    st = build(objects, physics, seed, floor)
    data, dt = st.data, st.dt
    v_eps = 0.02 * TILE / 4.0
    w_eps = 1.0  # contacts keep tiny objects micro-jittering; ignore that
    quiet_needed = 0.3
    quiet = 0.0
    check_every = max(1, int(round(0.02 / dt)))

    wall = time.time()
    steps = 0
    settling = False
    while data.time < st.t_max:
        if not settling and data.time > st.t_min:
            # Everything has landed: finish with the accurate solver and extra
            # damping so residual creep dies out instead of jittering forever.
            settling = True
            st.model.opt.solver = mujoco.mjtSolver.mjSOL_NEWTON
            st.model.opt.iterations = 50
            st.model.dof_damping[:] *= 3.0
        step(st)
        steps += 1
        if data.warning[mujoco.mjtWarning.mjWARN_BADQACC].number:
            # MuJoCo has already reset the simulation to t=0; going on would loop forever.
            raise RuntimeError("the physics simulation became unstable. Try Quality: High, "
                               "a lower floor height or larger objects.")
        if steps % check_every == 0:
            speeds = np.linalg.norm(data.qvel[st.lin_idx], axis=1)
            v = speeds.max()
            w = np.linalg.norm(data.qvel[st.ang_idx], axis=1).max()
            if settling and v < v_eps and w < w_eps:
                quiet += check_every * dt
                if quiet >= quiet_needed:
                    break
            else:
                quiet = 0.0
            if progress:
                progress(min(1.0, data.time / st.t_max), f"t={data.time:.2f}s  max speed {v:.3f}")

    poses = np.concatenate([data.qpos[st.pos_idx], data.qpos[st.quat_idx]], axis=1)
    poses[:, :2] = np.mod(poses[:, :2], TILE)
    stats = {
        "sim_time": float(data.time),
        "steps": steps,
        "wall_time": time.time() - wall,
        "settled": quiet >= quiet_needed,
        "max_speed": float(np.linalg.norm(data.qvel[st.lin_idx], axis=1).max()),
    }
    return poses, stats


def quat_to_mat(q):
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
