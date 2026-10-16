"""Top-down orthographic bake of the settled pile on the GPU (moderngl).

Every object is drawn at its pose plus the 8 neighbouring tile offsets, so
pieces crossing an edge wrap around and the maps tile seamlessly. An optional
floor heightmap is drawn first as a full-tile pass with its own depth, so it
shows in the height/normal maps and hides the parts of objects sunk into it.
The image is rendered supersampled in tiles (to bound memory) and box-filtered down.
"""

import moderngl
import numpy as np

from .sim import TILE, quat_to_mat

_VS = """
#version 330
uniform mat4 u_model;
uniform mat3 u_rot;
uniform vec3 u_view;   // x0, y0, width of the rendered world rectangle
uniform vec2 u_zrange; // zmin, zmax
in vec3 in_pos;
in vec3 in_nrm;
out vec3 v_nrm;
out float v_z;
void main() {
    vec4 w = u_model * vec4(in_pos, 1.0);
    v_nrm = u_rot * in_nrm;
    v_z = w.z;
    float x = (w.x - u_view.x) / u_view.z * 2.0 - 1.0;
    float y = (w.y - u_view.y) / u_view.z * 2.0 - 1.0;
    float d = 1.0 - 2.0 * (w.z - u_zrange.x) / (u_zrange.y - u_zrange.x);
    gl_Position = vec4(x, y, d, 1.0);
}
"""

_FS = """
#version 330
uniform float u_id;
in vec3 v_nrm;
in float v_z;
layout(location = 0) out vec4 o_geo;
layout(location = 1) out float o_id;
void main() {
    vec3 n = normalize(v_nrm);
    if (n.z < 0.0) n = -n;   // tolerate flipped normals: visible surfaces face up
    o_geo = vec4(n, v_z);
    o_id = u_id;
}
"""

# Floor pass: one quad over the rendered rectangle. The heightmap and its normals
# are sampled with GL_REPEAT, so the floor tiles like everything else.
_FLOOR_VS = """
#version 330
uniform vec3 u_view;
out vec2 v_world;
void main() {
    vec2 c = vec2(gl_VertexID & 1, gl_VertexID >> 1);
    v_world = u_view.xy + c * u_view.z;
    gl_Position = vec4(c * 2.0 - 1.0, 0.0, 1.0);
}
"""

_FLOOR_FS = """
#version 330
uniform sampler2D u_height;
uniform sampler2D u_normal;
uniform float u_tile;
uniform vec2 u_zrange;
in vec2 v_world;
layout(location = 0) out vec4 o_geo;
layout(location = 1) out float o_id;
void main() {
    vec2 uv = v_world / u_tile;
    float z = texture(u_height, uv).r;
    o_geo = vec4(normalize(texture(u_normal, uv).xyz), z);
    o_id = 0.0;
    gl_FragDepth = 1.0 - (z - u_zrange.x) / (u_zrange.y - u_zrange.x);
}
"""

_NEIGHBOURS = [(dx, dy) for dx in (-TILE, 0.0, TILE) for dy in (-TILE, 0.0, TILE)]


class BakeResult:
    """Per-pixel fields at the final resolution (row 0 = top of the image, +y up)."""

    def __init__(self, normal, z, coverage, object_id):
        self.normal = normal        # (R, R, 3) world/tangent-space normal, z up
        self.z = z                  # (R, R) world height (anti-aliased)
        self.coverage = coverage    # (R, R) 0..1
        self.object_id = object_id  # (R, R) int, -1 = ground/floor (center sample, not filtered)


def bake(meshes, objects, poses, resolution, supersample=2, tile_px=2048, progress=None, floor=None):
    """meshes: list of objload.Mesh (normalized). objects: list of dicts with
    'mesh' (index) and 'scale'. poses: (N,7) from sim.simulate. floor: optional
    (H,W) world heights to draw under the objects (see floor.py)."""
    ctx = moderngl.create_standalone_context(require=330)
    try:
        prog = ctx.program(vertex_shader=_VS, fragment_shader=_FS)

        vaos = []
        for mesh in meshes:
            tri_pos = mesh.positions[mesh.faces].astype(np.float32).reshape(-1, 3)
            tri_nrm = mesh.corner_normals.reshape(-1, 3)
            buf = ctx.buffer(np.hstack([tri_pos, tri_nrm]).astype(np.float32).tobytes())
            vaos.append(ctx.vertex_array(prog, [(buf, "3f 3f", "in_pos", "in_nrm")]))

        # World transforms and bounds per object.
        mats = []
        zmax = 0.0
        bounds = []
        for obj, pose in zip(objects, poses):
            rot = quat_to_mat(pose[3:7])
            s = obj["scale"]
            m = np.eye(4)
            m[:3, :3] = rot * s
            m[:3, 3] = pose[:3]
            mats.append((m, rot))
            verts = meshes[obj["mesh"]].positions
            world_z = (verts @ (rot * s).T)[:, 2] + pose[2]
            zmax = max(zmax, float(world_z.max()))
            radius = float(np.linalg.norm(verts, axis=1).max() * s)
            bounds.append(radius)
        zmin = 0.0
        floor_pass = None
        if floor is not None:
            zmin = min(zmin, float(floor.min()))
            zmax = max(zmax, float(floor.max()))
            floor_pass = _floor_pass(ctx, floor)
        zrange = (zmin - 0.01 * TILE, zmax + 0.01 * TILE)

        full = resolution * supersample
        tile_px = min(tile_px, full)
        tiles_per_side = (full + tile_px - 1) // tile_px
        out_tile = tile_px // supersample

        normal = np.zeros((resolution, resolution, 3), np.float32)
        z = np.zeros((resolution, resolution), np.float32)
        coverage = np.zeros((resolution, resolution), np.float32)
        object_id = np.full((resolution, resolution), -1, np.int32)

        geo_tex = ctx.texture((tile_px, tile_px), 4, dtype="f4")
        id_tex = ctx.texture((tile_px, tile_px), 1, dtype="f4")
        depth = ctx.depth_renderbuffer((tile_px, tile_px))
        fbo = ctx.framebuffer(color_attachments=[geo_tex, id_tex], depth_attachment=depth)
        ctx.enable(moderngl.DEPTH_TEST)
        ctx.disable(moderngl.CULL_FACE)

        px_world = TILE / full
        total = tiles_per_side * tiles_per_side
        done = 0
        for ty in range(tiles_per_side):
            for tx in range(tiles_per_side):
                x0 = tx * tile_px * px_world
                y0 = ty * tile_px * px_world
                w = tile_px * px_world
                fbo.use()
                # One clear value for both attachments: geo = (up normal, z=0),
                # id = 0 which means ground (objects are stored as index + 1).
                fbo.clear(0.0, 0.0, 1.0, 0.0, depth=1.0)
                if floor_pass is not None:
                    floor_pass(x0, y0, w, zrange)
                prog["u_view"].value = (x0, y0, w)
                prog["u_zrange"].value = zrange
                for k, (obj, (m, rot)) in enumerate(zip(objects, mats)):
                    r = bounds[k]
                    prog["u_id"].value = float(k + 1)
                    prog["u_rot"].write(rot.T.astype("f4").tobytes())
                    for dx, dy in _NEIGHBOURS:
                        cx = poses[k, 0] + dx
                        cy = poses[k, 1] + dy
                        if cx + r < x0 or cx - r > x0 + w or cy + r < y0 or cy - r > y0 + w:
                            continue
                        mm = m.copy()
                        mm[0, 3] = cx
                        mm[1, 3] = cy
                        prog["u_model"].write(mm.T.astype("f4").tobytes())
                        vaos[obj["mesh"]].render(moderngl.TRIANGLES)

                geo = np.frombuffer(geo_tex.read(), np.float32).reshape(tile_px, tile_px, 4)
                ids = np.frombuffer(id_tex.read(), np.float32).reshape(tile_px, tile_px)
                _downsample_into(geo, ids, supersample, tx, ty, out_tile, resolution,
                                 normal, z, coverage, object_id)
                done += 1
                if progress:
                    progress(done / total, f"bake tile {done}/{total}")

        # Rendered rows go bottom-up (y=0 first); images are stored top-down.
        return BakeResult(normal[::-1].copy(), z[::-1].copy(), coverage[::-1].copy(), object_id[::-1].copy())
    finally:
        ctx.release()


def _floor_pass(ctx, floor):
    from .floor import normals  # floor imports TILE from sim
    h, w = floor.shape
    height_tex = ctx.texture((w, h), 1, np.ascontiguousarray(floor, np.float32).tobytes(), dtype="f4")
    normal_tex = ctx.texture((w, h), 3, normals(floor).tobytes(), dtype="f4")
    for tex in (height_tex, normal_tex):
        tex.repeat_x = tex.repeat_y = True
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    prog = ctx.program(vertex_shader=_FLOOR_VS, fragment_shader=_FLOOR_FS)
    prog["u_height"].value = 0
    prog["u_normal"].value = 1
    prog["u_tile"].value = TILE
    vao = ctx.vertex_array(prog, [])

    def draw(x0, y0, w, zrange):
        height_tex.use(0)
        normal_tex.use(1)
        prog["u_view"].value = (x0, y0, w)
        prog["u_zrange"].value = zrange
        vao.render(moderngl.TRIANGLE_STRIP, vertices=4)
    return draw


def _downsample_into(geo, ids, ss, tx, ty, out_tile, resolution, normal, z, coverage, object_id):
    h = geo.shape[0] // ss
    oy = ty * out_tile
    ox = tx * out_tile
    h = min(h, resolution - oy)
    w = min(geo.shape[1] // ss, resolution - ox)
    if h <= 0 or w <= 0:
        return
    g = geo[:h * ss, :w * ss].reshape(h, ss, w, ss, 4)
    i = ids[:h * ss, :w * ss].reshape(h, ss, w, ss)
    hit = (i > 0.5).astype(np.float32)
    n = g[..., :3].mean(axis=(1, 3))
    n /= np.maximum(np.linalg.norm(n, axis=2, keepdims=True), 1e-6)
    normal[oy:oy + h, ox:ox + w] = n
    z[oy:oy + h, ox:ox + w] = g[..., 3].mean(axis=(1, 3))  # background: 0 or the floor
    coverage[oy:oy + h, ox:ox + w] = hit.mean(axis=(1, 3))
    c = ss // 2
    object_id[oy:oy + h, ox:ox + w] = np.round(i[:, c, :, c]).astype(np.int32) - 1
