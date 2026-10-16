# Physics Scatter for Substance 3D Designer

A Substance 3D Designer node that drops OBJ meshes into a pile with real physics and
bakes the settled pile into tileable maps inside your graph.

## Install

1. Download `physics_scatter.sdplugin` from the [latest release](https://github.com/berutoretoCG/physics-scatter/releases/latest).
   In Designer: **Tools → Plugin Manager → Install…**, pick that file.
   Keep that exact file name: Designer looks for a folder with the file's name inside it, so a
   renamed copy (e.g. `physics_scatter (1).sdplugin` from a download) fails with
   "Couldn't read pluginInfo.json". Rename it back if that happens.
2. Make sure the plugin is enabled. It adds a **Physics Scatter** menu to the menu bar.

Requirements: Windows 64-bit, a Designer whose embedded Python is 3.11 or 3.13, and
any GPU with OpenGL 3.3. The package bundles the physics/GPU libraries for both Python
versions and picks the right set automatically (`_vendor/cp311`, `_vendor/cp313`).
Tested in Designer 15.1 (Python 3.11); the 3.13 set was tested with Python 3.13.16.

## Use

Everything about a scatter is on its node, in Designer's **Properties** panel: the
settings, a **Simulate** switch and the result. There is no separate panel.

1. Save your package (.sbs). Maps are written next to it, in `<package>_physics_scatter/`.
2. Open the compositing graph you want the scatter in and pick **Physics Scatter → New scatter
   node**. Pick one or more OBJ files: a `PhysicsScatter_N` node appears in the graph.
3. Select the node. In its Properties:
   - **Run**: the *Simulate* switch, *Last simulation* (what the last run did, or why it
     failed) and *Object count*.
   - **Meshes**: *Mesh slots* (how many of the 8 slots show), then per mesh the *OBJ file*,
     *Count*, *Size %* (of the texture width) and *Size variation %*. Paste a path into
     *OBJ file* (quotes are fine), or use **Physics Scatter → Add OBJ files to selected scatter…**.
   - **Simulation**: *Resolution* of the maps, *Seed*, *Friction*, *Bounciness*,
     *Quality* (1 Low, 2 Medium, 3 High) and *Max sim time*.
   - **Floor**: *Floor height %*, the height of white in the Floor input, in % of the
     texture width.
4. Optional floor: connect a tileable heightmap to the node's **Floor** input. It is read at
   its own bit depth (8-bit, 16-bit or float), and the objects land on it. The floor is
   always part of the Height output.
5. With the node selected, switch **Simulate** on. Progress shows in Designer's status bar
   (bottom of the window). When it's done, the switch goes back to *Idle*, the outputs update
   in place (connections stay) and *Last simulation* says what happened. Switch it to *Idle*
   yourself to cancel.

Outputs:

- **Height**: 16-bit, floor included. White is the highest point.
- **Object ID**: 16-bit gray. Object *k* of *N* is *k / N*, and the ground is black.
  `round(Object ID × Object count)` gives the object's number, 1 to *N*.

All maps tile seamlessly: objects that cross an edge continue on the opposite side.

Rename a scatter in the Explorer (Designer asks which references to update: keep them all
and click Ok). A copied node shares its graph with the original until you simulate it;
then it gets its own graph, keeping its settings and connections.

Everything the plugin adds to the package lives in one `PhysicsScatter` folder: the
scatter graphs and a `<name>_maps` folder of bitmaps for each.

Scatters made with 0.1 or 0.2: select one and pick **Physics Scatter → Convert selected 0.2
scatter**. The settings of its last bake move onto the node. Height and Object ID stay
connected, and the other outputs (Normal, Roughness, …) are removed. A floor picked as a
file in 0.2 must now be connected to the Floor input. Then switch Simulate on.

Simulating takes roughly 5–10 s for ~100 objects and ~40 s for ~500 (Quality 2).
A floor roughly doubles the simulation time. With a Floor connected, Designer computes
your graph once when the simulation starts, which pauses Designer briefly on big graphs.

## Limitations (version 0.3)

- OBJ only (no FBX yet). One OBJ file = one rigid object; materials/UVs in the OBJ are ignored.
- Collision uses each mesh's convex hull: concave shapes (rings, bowls) can't interlock.
- Up to 8 meshes per node.
- Designer's Python API can't make a drop-down list or a file browser on a node, nor run code
  when a parameter changes. So *Quality* is a number and *OBJ file* is a text field. The
  plugin also only notices the Simulate switch on a **selected** node.
- Changing the settings doesn't re-run the simulation by itself: switch Simulate on again.
- The floor is treated as tileable and stretched to the square tile.
- If you move or share the .sbs, keep the `<package>_physics_scatter/` folder next to it.
  OBJ paths are stored on the node; a relative path is relative to the .sbs.

## Third-party libraries

The release package bundles MuJoCo (Apache-2.0), NumPy (BSD-3-Clause), moderngl and
glcontext (MIT), absl-py and etils (Apache-2.0). Their licenses are inside it, in
`_vendor/*/*.dist-info`. This repository holds only the plugin's own code.
