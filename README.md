# Physics Scatter for Substance 3D Designer

A node that drops OBJ meshes into a pile with real physics and bakes the settled
pile into tileable maps inside your graph.

## Install

1. Download `physics_scatter.sdplugin` from the [latest release](https://github.com/berutoretoCG/physics-scatter/releases/latest).
   In Designer: **Tools → Plugin Manager → Install…**, pick that file.
   Keep that exact file name: Designer looks for a folder with the file's name inside it, so a
   renamed copy (e.g. `physics_scatter (1).sdplugin` from a download) fails with
   "Couldn't read pluginInfo.json". Rename it back if that happens.
2. Make sure the plugin is enabled, then open **Physics Scatter → Show panel** in the menu bar.

Requirements: Windows 64-bit, a Designer whose embedded Python is 3.11 or 3.13, and
any GPU with OpenGL 3.3. The package bundles the physics/GPU libraries for both Python
versions and picks the right set automatically (`_vendor/cp311`, `_vendor/cp313`).
Tested in Designer 15.1 (Python 3.11); the 3.13 set was tested with Python 3.13.16.

## Use

1. Save your package (.sbs). Maps are written next to it, in `<package>_physics_scatter/`.
2. Open the compositing graph you want the scatter in and click **New scatter** in the panel
   (or **Physics Scatter → New scatter node**). Pick one or more OBJ files: a
   `PhysicsScatter_N` node appears in the graph.
3. Select the node. Its settings are in Designer's **Properties** panel:
   - **Meshes**: *Mesh slots* (how many of the 8 slots show), then per mesh the *OBJ file*,
     *Count*, *Size %* (of the texture width) and *Size variation %*. **Add OBJ…** in the
     panel fills the selected node's empty slots; you can also type or paste a path.
   - **Simulation**: *Resolution* of the maps, *Seed*, *Friction*, *Bounciness*,
     *Quality* (1 Low, 2 Medium, 3 High) and *Max sim time*.
   - **Floor**: *Floor height %*, the height of white in the Floor input, in % of the
     texture width.
   - **Result**: *Object count*, written by each simulation.
4. Optional floor: connect a tileable heightmap to the node's **Floor** input. It is read at
   its own bit depth (8-bit, 16-bit or float), and the objects land on it. The floor is
   always part of the Height output.
5. With the node selected, click **Simulate** (or **Physics Scatter → Simulate selected node**).
   Only that node is simulated. Its outputs update in place and keep their connections.

Outputs:

- **Height**: 16-bit, floor included. White is the highest point.
- **Object ID**: 16-bit gray. Object *k* of *N* is *k / N*, and the ground is black.
  `round(Object ID × Object count)` gives the object's number, 1 to *N*.

All maps tile seamlessly: objects that cross an edge continue on the opposite side.

Rename a scatter in the Explorer, or with **Rename…** in the panel. A copied node shares
its graph with the original until you simulate it; then it gets its own graph, keeping its
settings and connections.

Everything the plugin adds to the package lives in one `PhysicsScatter` folder: the
scatter graphs and a `<name>_maps` folder of bitmaps for each.

Scatters made with 0.1 or 0.2: select one and click **Simulate** to convert it. The
settings of its last bake move onto the node. Height and Object ID stay connected, and the
other outputs (Normal, Roughness, …) are removed. A floor picked as a file in 0.2 must now
be connected to the Floor input.

Simulating takes roughly 5–10 s for ~100 objects and ~40 s for ~500 (Quality 2).
A floor roughly doubles the simulation time.

## Limitations (version 0.3)

- OBJ only (no FBX yet). One OBJ file = one rigid object; materials/UVs in the OBJ are ignored.
- Collision uses each mesh's convex hull: concave shapes (rings, bowls) can't interlock.
- Up to 8 meshes per node.
- Quality is a number: Designer's Python API can't create drop-down lists.
- The node's settings don't run the simulation by themselves: after changing them, click
  **Simulate** again.
- The floor is treated as tileable and stretched to the square tile.
- If you move or share the .sbs, keep the `<package>_physics_scatter/` folder next to it.
  OBJ paths are stored on the node; a relative path is relative to the .sbs.

## Third-party libraries

The release package bundles MuJoCo (Apache-2.0), NumPy (BSD-3-Clause), moderngl and
glcontext (MIT), absl-py and etils (Apache-2.0). Their licenses are inside it, in
`_vendor/*/*.dist-info`. This repository holds only the plugin's own code.
