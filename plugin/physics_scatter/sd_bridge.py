"""Everything that touches the Designer API.

A scatter is a compositing graph owned by the plugin (marked in its metadata) and
used by one node in a user graph. Everything about it is on that node: its
settings and Simulate switch are the node's parameters, its floor is the node's
Floor image input, and the last result is written back into its parameters.
Simulating rebuilds the inside of the graph: linked bitmaps of the baked maps,
wired to its Height and Object ID outputs. The graph is named after the user graph
it was made in and can be renamed freely; its maps folder follows on the next
simulation.

Package layout (for scatters made in the graph "my_material"):

    PhysicsScatter/                             folder
        my_material_PhysicsScatter_1            graph
        my_material_PhysicsScatter_1_maps/      folder with the linked bitmaps (files in the
                                                scatter's folder on disk, named by the
                                                graph's metadata)
"""

import ctypes
import json
import os
import re
import struct
import uuid

import sd
from sd.api.apiexception import APIException  # derives from BaseException, not Exception
from sd.api.sbs.sdsbscompgraph import SDSBSCompGraph
from sd.api.sdbasetypes import float2
from sd.api.sdhistoryutils import SDHistoryUtils
from sd.api.sdproperty import SDPropertyCategory, SDPropertyInheritanceMethod
from sd.api.sdresource import EmbedMethod
from sd.api.sdresourcebitmap import SDResourceBitmap
from sd.api.sdresourcefolder import SDResourceFolder
from sd.api.sdtexture import SDTexture
from sd.api.sdtypebool import SDTypeBool
from sd.api.sdtypefloat import SDTypeFloat
from sd.api.sdtypeint import SDTypeInt
from sd.api.sdtypestring import SDTypeString
from sd.api.sdtypeusage import SDTypeUsage
from sd.api.sdusage import SDUsage
from sd.api.sdvaluearray import SDValueArray
from sd.api.sdvaluebool import SDValueBool
from sd.api.sdvalueenum import SDValueEnum
from sd.api.sdvaluefloat import SDValueFloat
from sd.api.sdvalueint import SDValueInt
from sd.api.sdvaluestring import SDValueString
from sd.api.sdvalueusage import SDValueUsage

NAME = "PhysicsScatter"  # scatter graphs are named <user graph>_PhysicsScatter_<n>
ROOT_FOLDER = "PhysicsScatter"
TAGS = "physics scatter"
MARKER = "physics_scatter"  # graph metadata: the scatter's folder name on disk
_USAGES_DONE = "physics_scatter_usages"  # graph metadata: the outputs' usages are current
_HEIGHT_SCALE = "physics_scatter_height"  # graph metadata: how high white is in its Height (see height_scale)
PASSTHROUGH = "sbs::compositing::passthrough"  # the Dot node
FLOOR = "floor"
FLOOR_HEIGHT = 50.0  # % of the texture width: how high white in the Floor input is
MESH_SLOTS = 8
HIDDEN = "false"  # visible_if of the parameters the plugin uses but nobody needs to change

# identifier, label, usage, description
OUTPUTS = [
    ("height", "Height", "height",
     "Height of the pile, floor included. White is the highest point."),
    ("object_id", "Object ID", "objectId",
     "Object k of N is k / N (N is in Last simulation): each object has its own exact gray level, "
     "the ground is black."),
]
OUTPUTS_IDS = tuple(o[0] for o in OUTPUTS)

_GRID = 150.0
_QUALITY = ("low", "medium", "high")


def _param_specs():
    # Ungrouped first: what you set on every scatter. Then the groups, then the hidden ones.
    specs = [
        dict(id="simulate", type="bool", default=False, label="Simulate", label_false="Idle", label_true="Simulate",
             description="Switch to Simulate to drop the meshes and bake the maps (the node must be selected). "
                         "It switches back to Idle when done; switch it to Idle yourself to cancel. "
                         "Progress shows in Designer's status bar."),
        dict(id="last_run", type="string", default="Not simulated yet.", label="Last simulation",
             description="Written by each simulation: how many objects fell, or why it failed."),
        dict(id="count", type="int", default=80, label="Count", min=1, max=2000, clamp=False,
             description="How many objects fall. With several meshes, their Amount % shares them out."),
        dict(id="size", type="float", default=8.0, label="Size %", min=0.5, max=50.0,
             description="Size of the objects, in % of the texture width."),
        dict(id="size_variation", type="float", default=25.0, label="Size variation %", min=0.0, max=90.0,
             description="Random size change per object, ± this %."),
        dict(id="mesh_slots", type="int", default=1, label="Mesh slots", group="Meshes", min=1, max=MESH_SLOTS,
             description="How many mesh slots to show."),
    ]
    for i in range(1, MESH_SLOTS + 1):
        group = f"Meshes/Mesh {i}"
        shown = None if i == 1 else f'input["mesh_slots"] >= {i}'
        several = 'input["mesh_slots"] > 1' if i == 1 else shown
        specs += [
            dict(id=f"mesh{i}_path", type="string", default="", label="OBJ file", group=group, visible_if=shown,
                 description="Path of a Wavefront .obj file, absolute or relative to the .sbs. Paste it (quotes are "
                             "fine), or use Physics Scatter → Add OBJ files to selected scatter."),
            dict(id=f"mesh{i}_amount", type="float", default=100.0, label="Amount %", group=group,
                 visible_if=several, min=0.0, max=100.0,
                 description="This mesh's share of Count, relative to the other meshes: 100 and 50 give "
                             "2/3 and 1/3 of the objects. Equal values share Count evenly."),
            dict(id=f"mesh{i}_scale", type="float", default=100.0, label="Scale %", group=group,
                 visible_if=several, min=10.0, max=400.0, clamp=False,
                 description="Size of this mesh, in % of Size %."),
        ]
    specs += [
        dict(id="resolution", type="int", default=2048, label="Resolution", group="Simulation", min=256,
             max=4096, step=256, description="Width and height of the baked maps, in pixels."),
        dict(id="seed", type="int", default=1, label="Seed", group="Simulation", min=0, max=1000, clamp=False,
             description="Change it for another random pile."),
        dict(id="friction", type="float", default=0.7, label="Friction", group="Simulation", min=0.0, max=2.0),
        dict(id="bounciness", type="float", default=0.1, label="Bounciness", group="Simulation", min=0.0,
             max=1.0),
        dict(id="quality", type="int", default=2, label="Quality", group="Simulation", min=1, max=3,
             description="1 = Low, 2 = Medium, 3 = High (smaller simulation steps, slower)."),
        dict(id="max_time", type="float", default=8.0, label="Max sim time (s)", group="Simulation", min=2.0,
             max=60.0, description="Simulation time limit. It stops earlier once everything is at rest."),
        dict(id="object_count", type="int", default=0, label="Object count", visible_if=HIDDEN, min=0, max=5000,
             clamp=False, description="Written by each simulation. Object ID = object index / Object count, so "
                                      "round(Object ID × Object count) gives the index, 1 to Object count."),
        dict(id="floor_height", type="float", default=FLOOR_HEIGHT, label="Floor height %", visible_if=HIDDEN,
             min=0.5, max=100.0, description="Height of white in the Floor input, in % of the texture width."),
    ]
    return specs


PARAMS = _param_specs()
_TYPES = {"int": (SDTypeInt, SDValueInt), "float": (SDTypeFloat, SDValueFloat),
          "string": (SDTypeString, SDValueString), "bool": (SDTypeBool, SDValueBool)}
_SPEC = {p["id"]: p for p in PARAMS}


# ------------------------------------------------------------------ lookups

def ui_manager():
    return sd.getContext().getSDApplication().getQtForPythonUIMgr()


def current_graph():
    """The compositing graph open in the focused graph view, or None."""
    graph = ui_manager().getCurrentGraph()
    return graph if isinstance(graph, SDSBSCompGraph) else None


def selected_nodes():
    """Nodes selected in the focused graph view, or None when no graph view has focus."""
    try:
        return list(ui_manager().getCurrentGraphSelectedNodes() or [])
    except APIException:  # InvalidArgument when no graph view has focus
        return None


def selected_in_all_views(limit=50):
    """(graph, node) for the selected nodes of every open compositing graph view.
    Works while another panel (e.g. Properties) has the focus."""
    found = []
    try:
        ui = sd.getContext().getSDApplication().getUIMgr()
        for i in range(ui.getGraphViewIDCount()):
            view = ui.getGraphViewIDAt(i)
            graph = ui.getGraphFromGraphViewID(view)
            if not isinstance(graph, SDSBSCompGraph):
                continue
            nodes = list(ui.getGraphSelectedNodesFromGraphViewID(view) or [])
            if len(nodes) <= limit:
                found += [(graph, n) for n in nodes]
    except APIException:
        pass
    return found


def selected_scatter_nodes():
    found = []
    for graph, node in selected_in_all_views():
        try:
            if scatter_graph(node):
                found.append((graph, node))
        except APIException:  # deleted since the selection was read
            pass
    return found


def package_file(package):
    path = package.getFilePath() if package else ""
    return path if path and os.path.isfile(path) else None


def scatter_root(package):
    path = package_file(package)
    if not path:
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(os.path.dirname(path), f"{stem}_physics_scatter")


def scatter_key(graph):
    """The scatter's folder name on disk, or None if graph isn't a Physics Scatter."""
    if not isinstance(graph, SDSBSCompGraph):
        return None
    try:
        value = graph.getMetadataDict().getPropertyValueFromId(MARKER)
    except APIException:  # ItemNotFound
        return None
    return value.get() if value is not None else None


def scatter_graph(node):
    graph = node.getReferencedResource() if node is not None else None
    return graph if scatter_key(graph) else None


def scatter_dir(graph):
    root = scatter_root(graph.getPackage())
    return os.path.join(root, scatter_key(graph)) if root else None


def _same_graph(a, b):
    return (a.getUrl() == b.getUrl() and a.getPackage().getFilePath() == b.getPackage().getFilePath()
            and a.getUID() == b.getUID())


def _user_packages(fallback):
    try:
        return list(sd.getContext().getSDApplication().getPackageMgr().getUserPackages())
    except (APIException, AttributeError):
        return [fallback]


def instances_of(graph):
    """Every node, in every open package, that uses graph."""
    found = []
    for package in _user_packages(graph.getPackage()):
        for res in package.getChildrenResources(True):
            if not isinstance(res, SDSBSCompGraph):
                continue
            for node in res.getNodes():
                try:
                    ref = node.getReferencedResource()
                except APIException:  # a node whose resource is missing
                    continue
                if isinstance(ref, SDSBSCompGraph) and _same_graph(ref, graph):
                    found.append(node)
    return found


def _find_resource(package, identifier, cls):
    for r in package.getChildrenResources(True):
        if isinstance(r, cls) and r.getIdentifier() == identifier:
            return r
    return None


def _folder(parent, identifier):
    """The folder `identifier` directly under parent (a package or folder), created if missing."""
    if isinstance(parent, SDResourceFolder):
        children = parent.getChildren(False)
    else:
        children = parent.getChildrenResources(False)
    for r in children:
        if isinstance(r, SDResourceFolder) and r.getIdentifier() == identifier:
            return r
    folder = SDResourceFolder.sNew(parent)
    folder.setIdentifier(identifier)
    return folder


def _parent_folder(package, resource):
    url = resource.getUrl()
    for r in package.getChildrenResources(True):
        if isinstance(r, SDResourceFolder) and any(c.getUrl() == url for c in r.getChildren(False)):
            return r
    return None


def _output_identifier(node):
    value = node.getAnnotationPropertyValueFromId("identifier")
    return value.get() if value is not None else None


def _is_floor_input(node):
    return (node.getDefinition().getId() == "sbs::compositing::input_grayscale"
            and _output_identifier(node) == FLOOR)


def _free_name(package, user_graph):
    """<user graph>_PhysicsScatter_<n>, with the first n not used in the package."""
    base = f"{user_graph.getIdentifier()}_{NAME}_"
    used = {r.getIdentifier() for r in package.getChildrenResources(True)}
    n = 1
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def _new_key(graph):
    return f"{graph.getIdentifier()}_{uuid.uuid4().hex[:6]}"


def ensure_unique_key(graph):
    """A graph duplicated in the Explorer carries the original's key: give it its own."""
    key = scatter_key(graph)
    for r in graph.getPackage().getChildrenResources(True):
        if isinstance(r, SDSBSCompGraph) and not _same_graph(r, graph) and scatter_key(r) == key:
            graph.getMetadataDict().setPropertyValueFromId(MARKER, SDValueString.sNew(_new_key(graph)))
            return


# --------------------------------------------------------------- settings

def has_param(graph, ident):
    try:
        return graph.getPropertyFromId(ident, SDPropertyCategory.Input) is not None
    except APIException:
        return False


def _raw_value(node, ident):
    try:
        value = node.getPropertyValueFromId(ident, SDPropertyCategory.Input)
    except APIException:  # ItemNotFound: a graph without this parameter
        return None
    return value.get() if value is not None else None


def get_value(node, ident):
    value = _raw_value(node, ident)
    return _SPEC[ident]["default"] if value is None else value


def set_value(node, ident, value):
    """Set a parameter of the node, if it differs: every change makes Designer refresh
    the node (and the Properties panel showing it)."""
    if get_value(node, ident) != value:
        node.setInputPropertyValueFromId(ident, _TYPES[_SPEC[ident]["type"]][1].sNew(value))


def finish_run(node, status, object_count=None):
    """Write a run's result on the node and switch Simulate back to Idle."""
    with SDHistoryUtils.UndoGroup("Physics Scatter: result"):
        _write_result(node, status, object_count)


def _write_result(node, status, object_count):
    try:
        if object_count is not None:
            set_value(node, "object_count", int(object_count))
        set_value(node, "last_run", status)
        set_value(node, "simulate", False)
    except APIException:  # the node was deleted meanwhile
        pass


def _resolve_path(path, package):
    path = os.path.expandvars(path.strip().strip('"'))
    if path and not os.path.isabs(path):
        base = os.path.dirname(package_file(package) or "")
        path = os.path.normpath(os.path.join(base, path))
    return path


def split_count(total, weights):
    """Share total out in proportion to weights: whole numbers that add up to total
    (largest remainders get the leftovers)."""
    weight_sum = sum(weights)
    if total <= 0 or weight_sum <= 0:
        return [0] * len(weights)
    exact = [total * w / weight_sum for w in weights]
    counts = [int(e) for e in exact]
    for i in sorted(range(len(weights)), key=lambda i: counts[i] - exact[i])[:total - sum(counts)]:
        counts[i] += 1
    return counts


def read_settings(node):
    """The engine job settings held by a scatter node's parameters (sizes as fractions)."""
    package = node.getReferencedResource().getPackage()
    slots = int(get_value(node, "mesh_slots"))
    several = slots > 1  # Amount % and Scale % only show with more than one slot
    found = []
    for i in range(1, slots + 1):
        path = _resolve_path(get_value(node, f"mesh{i}_path"), package)
        if path:
            amount = max(0.0, float(get_value(node, f"mesh{i}_amount"))) if several else 100.0
            scale = float(get_value(node, f"mesh{i}_scale")) if several else 100.0
            found.append((path, amount, scale))
    counts = split_count(int(get_value(node, "count")), [amount for _path, amount, _scale in found])
    size = float(get_value(node, "size")) / 100.0
    variation = float(get_value(node, "size_variation")) / 100.0
    meshes = [dict(path=path, count=count, size=size * scale / 100.0, size_variation=variation)
              for (path, _amount, scale), count in zip(found, counts)]
    quality = min(max(int(get_value(node, "quality")), 1), 3)
    return {
        "resolution": min(max(int(get_value(node, "resolution")), 64), 8192),
        "seed": int(get_value(node, "seed")),
        "meshes": meshes,
        "physics": {
            "friction": float(get_value(node, "friction")),
            "bounciness": float(get_value(node, "bounciness")),
            "quality": _QUALITY[quality - 1],
            "max_time": float(get_value(node, "max_time")),
        },
        "floor": {"height": float(get_value(node, "floor_height")) / 100.0},
    }


def add_meshes(node, paths):
    """Put OBJ paths into the node's first empty mesh slots; returns the paths that didn't fit."""
    paths = list(paths)
    last = int(get_value(node, "mesh_slots"))
    for i in range(1, MESH_SLOTS + 1):
        if not paths:
            break
        if get_value(node, f"mesh{i}_path").strip() and i <= last:
            continue
        set_value(node, f"mesh{i}_path", paths.pop(0))
        last = max(last, i)
    set_value(node, "mesh_slots", last)
    return paths


# ------------------------------------------------------------- the graph

def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6
    return a == b


def _set_annotation(graph, prop, ident, value):
    """Set a parameter annotation if it differs (see set_value: writes aren't free)."""
    try:
        current = graph.getPropertyAnnotationValueFromId(prop, ident)
        current = current.get() if current is not None else None
    except APIException:
        current = None
    if not _same(current, value.get()):
        graph.setPropertyAnnotationValueFromId(prop, ident, value)


def _set_node_annotation(node, ident, text):
    try:
        current = node.getAnnotationPropertyValueFromId(ident)
        current = current.get() if current is not None else None
    except APIException:
        current = None
    if current != text:
        node.setAnnotationPropertyValueFromId(ident, SDValueString.sNew(text))


def _apply_param(graph, spec):
    prop = graph.getPropertyFromId(spec["id"], SDPropertyCategory.Input)
    sd_type, sd_value = _TYPES[spec["type"]]
    if prop is None:
        prop = graph.newProperty(spec["id"], sd_type.sNew(), SDPropertyCategory.Input)
        graph.setPropertyValue(prop, sd_value.sNew(spec["default"]))
    for ident in ("label", "group", "description", "visible_if"):
        _set_annotation(graph, prop, ident, SDValueString.sNew(spec.get(ident) or ""))
    if spec["type"] == "bool":
        _set_annotation(graph, prop, "editor", SDValueString.sNew("buttons"))
        _set_annotation(graph, prop, "label_false", SDValueString.sNew(spec["label_false"]))
        _set_annotation(graph, prop, "label_true", SDValueString.sNew(spec["label_true"]))
    elif spec["type"] != "string":
        _set_annotation(graph, prop, "editor", SDValueString.sNew("slider"))
        _set_annotation(graph, prop, "min", sd_value.sNew(spec["min"]))
        _set_annotation(graph, prop, "max", sd_value.sNew(spec["max"]))
        _set_annotation(graph, prop, "clamp", SDValueBool.sNew(spec.get("clamp", True)))
        if "step" in spec:
            _set_annotation(graph, prop, "step", sd_value.sNew(spec["step"]))


def _usages(usage, components, colorspace):
    usages = SDValueArray.sNew(SDTypeUsage.sNew(), 0)
    usages.pushBack(SDValueUsage.sNew(SDUsage.sNew(usage, components, colorspace)))
    return usages


def is_current(graph):
    """Whether the graph has this version's parameters (see ensure_structure)."""
    return has_param(graph, "count")


def ensure_structure(graph):
    """Bring a scatter graph made by an earlier 0.3 build up to date."""
    with SDHistoryUtils.UndoGroup("Physics Scatter: update"):
        _ensure_structure(graph)


_PER_MESH_0_3 = ("count", "size", "variation")  # per-mesh parameters of 0.3.0 and 0.3.1


def _take_0_3_settings(graph):
    """0.3.0 and 0.3.1 had Count, Size % and Size variation % per mesh. Deletes the
    graph's parameters, to be made again in this version's order (Designer lists
    parameters in the order they were made and can't reorder them), and returns
    what every node of the graph had in them."""
    if not has_param(graph, "mesh1_count"):
        return []
    old = {f"mesh{i}_{name}" for i in range(1, MESH_SLOTS + 1) for name in _PER_MESH_0_3}
    idents = [p.getId() for p in graph.getProperties(SDPropertyCategory.Input) if p.getId() in _SPEC or p.getId() in old]
    taken = [(node, {ident: _raw_value(node, ident) for ident in idents}) for node in instances_of(graph)]
    for ident in idents:
        graph.deleteProperty(graph.getPropertyFromId(ident, SDPropertyCategory.Input))
    return taken


def _put_0_3_settings(taken):
    """Back on the nodes: the settings kept, and the per-mesh ones of 0.3 as the
    global Count, Size % and Size variation % plus each mesh's Amount % and Scale %
    (same objects, same sizes). Floor height % goes to 50."""
    for node, values in taken:
        try:
            for ident, value in values.items():
                if ident in _SPEC and ident != "floor_height" and value is not None:
                    set_value(node, ident, value)
            meshes = [(i, values.get(f"mesh{i}_count"), values.get(f"mesh{i}_size"), values.get(f"mesh{i}_variation"))
                      for i in range(1, int(values.get("mesh_slots") or 1) + 1)
                      if (values.get(f"mesh{i}_path") or "").strip()]
            if not meshes:
                continue
            counts = [max(0, int(count or 0)) for _i, count, _size, _variation in meshes]
            first_size = float(meshes[0][2] or _SPEC["size"]["default"])
            set_value(node, "count", max(1, sum(counts)))
            set_value(node, "size", first_size)
            set_value(node, "size_variation", float(meshes[0][3] or 0.0))
            for (i, _count, size, _variation), n in zip(meshes, counts):
                set_value(node, f"mesh{i}_amount", 100.0 * n / max(counts) if max(counts) else 100.0)
                set_value(node, f"mesh{i}_scale", 100.0 * float(size or first_size) / first_size)
        except APIException:  # a node that went away
            pass


def _set_graph_annotation(graph, ident, value):
    current = graph.getAnnotationPropertyValueFromId(ident)
    if current is None or current.get() != value.get():
        graph.setAnnotationPropertyValueFromId(ident, value)


def _ensure_structure(graph):
    """Parameters, Floor input and outputs, added if missing; the outputs of older
    versions (Normal, Roughness, …) are removed. Returns {identifier: output node}."""
    taken = _take_0_3_settings(graph)
    for spec in PARAMS:
        _apply_param(graph, spec)
    _put_0_3_settings(taken)
    _set_graph_annotation(graph, "tags", SDValueString.sNew(TAGS))
    _set_graph_annotation(graph, "show_in_library", SDValueBool.sNew(False))
    if not any(_is_floor_input(n) for n in graph.getNodes()):
        node = graph.newNode("sbs::compositing::input_grayscale")
        node.setPosition(float2(-2 * _GRID, 0.0))
        node.setAnnotationPropertyValueFromId("identifier", SDValueString.sNew(FLOOR))
    for node in graph.getNodes():
        if _is_floor_input(node):
            _set_node_annotation(node, "label", "Floor")
            _set_node_annotation(node, "description", "Optional heightmap the objects land on (tileable). "
                                                      "White is half the texture width high; another "
                                                      "scatter's Height keeps its real height.")

    outputs = {}
    for node in graph.getOutputNodes():
        ident = _output_identifier(node)
        if ident in OUTPUTS_IDS and ident not in outputs:
            outputs[ident] = node
        else:
            graph.deleteNode(node)
    metadata = graph.getMetadataDict()
    try:
        usages_done = metadata.getPropertyValueFromId(_USAGES_DONE) is not None
    except APIException:
        usages_done = False
    for row, (ident, label, usage, description) in enumerate(OUTPUTS):
        node = outputs.get(ident)
        if node is None:
            node = graph.newNode("sbs::compositing::output")
            node.setPosition(float2(2 * _GRID, row * _GRID))
            node.setAnnotationPropertyValueFromId("identifier", SDValueString.sNew(ident))
            node.setAnnotationPropertyValueFromId("usages", _usages(usage, "R", "Linear"))
            outputs[ident] = node
        elif not usages_done:  # an output made by 0.1/0.2 (Object ID was color then)
            node.setAnnotationPropertyValueFromId("usages", _usages(usage, "R", "Linear"))
        _set_node_annotation(node, "label", label)
        _set_node_annotation(node, "description", description)
        _set_node_annotation(node, "group", "")
    if not usages_done:
        metadata.setPropertyValueFromId(_USAGES_DONE, SDValueString.sNew("1"))
    return outputs


def _new_scatter_graph(user_graph):
    package = user_graph.getPackage()
    graph = SDSBSCompGraph.sNew(_folder(package, ROOT_FOLDER))
    graph.setIdentifier(_free_name(package, user_graph))
    graph.getMetadataDict().setPropertyValueFromId(MARKER, SDValueString.sNew(_new_key(graph)))
    _ensure_structure(graph)
    return graph


def create_scatter(user_graph):
    """A new scatter graph and its node in user_graph; returns the node."""
    with SDHistoryUtils.UndoGroup("New Physics Scatter"):
        graph = _new_scatter_graph(user_graph)
        node = user_graph.newInstanceNode(graph)
        node.setPosition(_free_position(user_graph))
    return node


def give_own_graph(user_graph, node):
    """Replace node by a node of a new scatter graph with the same parameters and
    connections. Used when its graph is shared (a copied node) or lives in another
    package, so that simulating it changes this node only. Returns the new node."""
    with SDHistoryUtils.UndoGroup("Physics Scatter: separate scatter"):
        graph = _new_scatter_graph(user_graph)
        new = user_graph.newInstanceNode(graph)
        new.setPosition(node.getPosition())
        for prop in node.getProperties(SDPropertyCategory.Input):
            ident = prop.getId()
            if new.getPropertyFromId(ident, SDPropertyCategory.Input) is None:
                continue
            connections = list(node.getPropertyConnections(prop))
            for conn in connections:
                conn.getInputPropertyNode().newPropertyConnectionFromId(conn.getInputProperty().getId(), new, ident)
            if connections or ident == FLOOR:
                continue
            try:
                if ident.startswith("$"):
                    new.setInputPropertyInheritanceMethodFromId(
                        ident, node.getInputPropertyInheritanceMethodFromId(ident))
                value = node.getPropertyValue(prop)
                if value is not None:
                    new.setInputPropertyValueFromId(ident, value)
            except APIException:
                pass
        for prop in node.getProperties(SDPropertyCategory.Output):
            for conn in node.getPropertyConnections(prop):
                new.newPropertyConnectionFromId(prop.getId(), conn.getInputPropertyNode(),
                                                conn.getInputProperty().getId())
        user_graph.deleteNode(node)
    return new


# ----------------------------------------------------------------- floor

_PIXEL_TYPES = {"8": "|u1", "16": "<u2", "16F": "<f2", "32F": "<f4"}


def _pixel_dtype(pixel_format):
    """numpy dtype string of one channel of an SBSPixelFormat (e.g. LUM16F → <f2)."""
    depth = re.search(r"(\d+F?)$", pixel_format.name)
    if depth is None or depth.group(1) not in _PIXEL_TYPES:
        raise ValueError(f"unsupported pixel format {pixel_format.name}")
    return _PIXEL_TYPES[depth.group(1)]


def write_npy(path, descr, shape, data):
    header = "{'descr': '%s', 'fortran_order': False, 'shape': %r, }" % (descr, tuple(shape))
    header += " " * ((64 - (11 + len(header)) % 64) % 64) + "\n"
    with open(path, "wb") as f:
        f.write(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header.encode("latin1") + data)


def texture_to_npy(texture, path):
    """Save a computed texture as .npy at its own bit depth (8/16-bit, half or float),
    top row first; color textures keep their channels."""
    descr = _pixel_dtype(texture.getPixelFormat())
    size = texture.getSize()
    w, h = size.x, size.y
    bpp = texture.getBytesPerPixel()
    channels = bpp // int(descr[2:])
    pitch = texture.getRowPitch()
    addr = texture.getPixelBufferAddress()
    data = b"".join(ctypes.string_at(addr + r * pitch, w * bpp) for r in range(h))
    write_npy(path, descr, (h, w) if channels == 1 else (h, w, channels), data)


def height_scale(graph):
    """How high white is in a scatter's Height output, as a fraction of the texture
    width: each simulation scales Height so that white is the top of its pile. None
    if it was never simulated."""
    try:
        value = graph.getMetadataDict().getPropertyValueFromId(_HEIGHT_SCALE)
        if value is not None:
            return float(value.get())
    except (APIException, ValueError):
        pass
    # Simulations by 0.4.0 left it only in their result file.
    folder = scatter_dir(graph)
    try:
        with open(os.path.join(folder, "job.json"), "r", encoding="utf-8") as f:
            suffix = json.load(f)["file_suffix"]
        with open(os.path.join(folder, f"result{suffix}.json"), "r", encoding="utf-8") as f:
            return float(json.load(f)["height_scale_relative"])
    except (OSError, TypeError, ValueError, KeyError):
        return None


def floor_scatter(node):
    """The scatter graph whose Height is connected to node's Floor input, directly
    or through Dot nodes; None for any other floor."""
    prop = node.getPropertyFromId(FLOOR, SDPropertyCategory.Input)
    connections = list(node.getPropertyConnections(prop)) if prop is not None else []
    for _hop in range(64):
        if not connections:
            return None
        source, source_prop = connections[0].getInputPropertyNode(), connections[0].getInputProperty()
        try:
            if source.getDefinition().getId() != PASSTHROUGH:
                graph = scatter_graph(source)
                return graph if graph is not None and source_prop.getId() == "height" else None
            connections = [c for p in source.getProperties(SDPropertyCategory.Input)
                           for c in source.getPropertyConnections(p)]
        except APIException:
            return None
    return None


def capture_floor(user_graph, node, path):
    """Compute what is connected to the node's Floor input and save it to path (.npy).
    Returns False when nothing is connected."""
    prop = node.getPropertyFromId(FLOOR, SDPropertyCategory.Input)
    connections = list(node.getPropertyConnections(prop)) if prop is not None else []
    if not connections:
        return False
    # Connections read from a node are seen from its side: the other end is the "input" one.
    source, source_prop = connections[0].getInputPropertyNode(), connections[0].getInputProperty()
    # Only output nodes reliably keep their texture after compute(): read it through a
    # temporary one, else from the node itself; computing again once if both are empty.
    with SDHistoryUtils.UndoGroup("Physics Scatter: read floor"):
        probe = user_graph.newNode("sbs::compositing::output")
        try:
            source.newPropertyConnectionFromId(source_prop.getId(), probe, "inputNodeOutput")
            out_prop = probe.getDefinition().getProperties(SDPropertyCategory.Output)[0]
            texture = None
            for _attempt in range(2):
                user_graph.compute()
                texture = _texture(probe, out_prop) or _texture(source, source_prop)
                if texture is not None:
                    break
            if texture is None:
                raise ValueError(f"Designer computed no image for {_describe(source)}, the node connected to Floor. "
                                 f"Check that it shows an image in the 2D view and switch Simulate on again, "
                                 f"or connect a Bitmap of the floor instead.")
            texture_to_npy(texture, path)
        finally:
            user_graph.deleteNode(probe)
    return True


def _texture(node, prop):
    try:
        value = node.getPropertyValue(prop)
        return value.get() if value is not None else None
    except APIException:
        return None


def _describe(node):
    """A node's name as the user sees it: its label, its graph's name or its kind."""
    try:
        label = node.getAnnotationPropertyValueFromId("label")
        if label is not None and label.get():
            return f'"{label.get()}"'
        resource = node.getReferencedResource()
        if resource is not None:
            return f'"{resource.getIdentifier()}"'
        return f'"{node.getDefinition().getLabel() or node.getDefinition().getId()}"'
    except APIException:
        return "the node"


# ------------------------------------------------------------------ bake

def _owned_bitmaps(graph):
    """The bitmaps of the scatter's maps: those linked to files in its folder on disk.
    (Not "the bitmaps its graph uses": a graph duplicated in the Explorer uses the
    original's bitmaps until it is simulated.)"""
    folder = scatter_dir(graph)
    if not folder:
        return []
    prefix = os.path.normcase(os.path.normpath(folder)) + os.sep
    return [res for res in graph.getPackage().getChildrenResources(True)
            if isinstance(res, SDResourceBitmap)
            and os.path.normcase(os.path.normpath(res.getFilePath())).startswith(prefix)]


def apply_bake(graph, node, files, preview=None, object_count=None, status="", white_height=None):
    """Replace the scatter graph's maps with freshly baked files and write the result
    on its node (Object count, Last simulation, Simulate back to Idle), as one undo
    step. white_height: how high white is in the new Height (see height_scale). Everything in the graph but the outputs and the Floor input belongs to the
    bake and is rebuilt."""
    package = graph.getPackage()
    with SDHistoryUtils.UndoGroup("Physics Scatter: simulate"):
        outputs = _ensure_structure(graph)
        old_bitmaps = _owned_bitmaps(graph)
        folder = _parent_folder(package, old_bitmaps[0]) if old_bitmaps else None
        keep = {n.getIdentifier() for n in outputs.values()}
        for n in list(graph.getNodes()):
            if n.getIdentifier() not in keep and not _is_floor_input(n):
                graph.deleteNode(n)
        for res in old_bitmaps:
            res.delete()

        # The maps folder follows the graph's name. It is renamed only while empty:
        # renaming something in use makes Designer ask which references to update.
        name = f"{graph.getIdentifier()}_maps"
        if folder is None:
            folder = _folder(_folder(package, ROOT_FOLDER), name)
        elif (folder.getIdentifier() != name and folder.getIdentifier().endswith("_maps")
              and not list(folder.getChildren(False))):
            try:
                folder.setIdentifier(name)
            except APIException:
                pass

        for row, (ident, *_rest) in enumerate(OUTPUTS):
            path = files.get(ident)
            if not path:
                continue
            resource = SDResourceBitmap.sNewFromFile(folder, path, EmbedMethod.Linked)
            resource.setIdentifier(ident)
            bitmap = graph.newInstanceNode(resource)
            bitmap.setPosition(float2(0.0, row * _GRID))
            bitmap.setInputPropertyInheritanceMethodFromId("$format", SDPropertyInheritanceMethod.Absolute)
            bitmap.setInputPropertyValueFromId(
                "$format", SDValueEnum.sFromValueId("sbs::compositing::format", "16_bits_per_channel"))
            bitmap.newPropertyConnectionFromId("unique_filter_output", outputs[ident], "inputNodeOutput")

        if white_height:
            graph.getMetadataDict().setPropertyValueFromId(_HEIGHT_SCALE, SDValueString.sNew(f"{white_height:.9g}"))
        if preview and os.path.isfile(preview):
            icon = SDTexture.sFromFile(preview)
            if icon is not None:
                graph.setIcon(icon)
        if node is not None:
            _write_result(node, status, object_count)


def _free_position(graph):
    """Just right of the selection, or to the right of everything else."""
    nodes = selected_nodes() or list(graph.getNodes())
    if not nodes:
        return float2(0.0, 0.0)
    xs = [n.getPosition().x for n in nodes]
    ys = [n.getPosition().y for n in nodes]
    return float2(max(xs) + _GRID, sum(ys) / len(ys))
