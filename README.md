# Physics Scatter for Substance 3D Designer

Drop OBJ meshes into a panel. The plugin simulates them falling into a pile and
bakes the settled pile into tileable maps inside your graph.

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
2. Open the compositing graph you want the scatter in.
3. In the panel: **Add…** OBJ files and set count, size (% of the texture width), size
   variation, roughness and metallic per mesh.
4. Pick resolution, seed, friction, bounciness, quality and roughness variation.
5. Optional **Floor**: pick a heightmap (PNG, TIF, JPG, … 16-bit PNG/TIF keep full
   precision) and set its **Height** (white, in % of the texture width). The objects
   fall onto it instead of flat ground. With **Include the floor in Height and Normal**
   on, those two maps show the floor between the objects; off, they show only the
   objects (still at the height where they landed).
6. **Simulate & Bake**. The first bake adds a `PhysicsScatter_N` node to your graph.
   Later bakes of the same scatter (pick it in the **Scatter** list) update that node
   in place: its connections stay, and the previous maps are replaced.

Outputs: Normal (DirectX or OpenGL), Height (16-bit), Roughness, Metallic, Coverage
Mask, Object ID, Object Random. All maps tile seamlessly: objects that cross an edge
continue on the opposite side.

Everything the plugin adds to the package lives in one `PhysicsScatter` folder: the
`PhysicsScatter_N` graphs and a `PhysicsScatter_N_maps` folder of bitmaps for each.
(Scatters made with 0.1 stay where they were; re-baking them removes their Base Color
output.) Deleting a scatter frees its number: the next new scatter reuses it and its
folder on disk.

Bake time is roughly 5–10 s for ~100 objects and ~40 s for ~500 (Medium quality).
A floor heightmap roughly doubles the simulation time.

## Limitations (version 0.2)

- OBJ only (no FBX yet). One OBJ file = one rigid object; materials/UVs in the OBJ are ignored.
- Collision uses each mesh's convex hull: concave shapes (rings, bowls) can't interlock.
- The floor heightmap is treated as tileable and stretched to the square tile.
  EXR isn't supported (Designer's Qt can't read it); export the floor as 16-bit PNG.
  No height blend or chaining yet.
- No 3D preview inside the panel; the panel shows a shaded 2D preview.
- If you move or share the .sbs, keep the `<package>_physics_scatter/` folder next to it.

## Third-party libraries

The release package bundles MuJoCo (Apache-2.0), NumPy (BSD-3-Clause), moderngl and
glcontext (MIT), absl-py and etils (Apache-2.0). Their licenses are inside it, in
`_vendor/*/*.dist-info`. This repository holds only the plugin's own code.
