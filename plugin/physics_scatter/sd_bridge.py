"""Everything that touches the Designer API: finding scatters in a package and
writing baked maps into the package as a small graph with one output per map.

Each scatter is a compositing graph named PhysicsScatter_N that holds linked
Bitmap nodes wired to output nodes. Re-baking keeps the output nodes and replaces
everything else in the graph and its maps folder (both belong to the plugin), so
instances of the graph keep their connections in the user's graphs.

Package layout (scatters made by 0.1 sit at the package root and stay there):

    PhysicsScatter/                 folder
        PhysicsScatter_1            graph
        PhysicsScatter_1_maps/      folder with the linked bitmaps
"""

import os

import sd
from sd.api.apiexception import APIException  # derives from BaseException, not Exception
from sd.api.sbs.sdsbscompgraph import SDSBSCompGraph
from sd.api.sdbasetypes import float2
from sd.api.sdhistoryutils import SDHistoryUtils
from sd.api.sdproperty import SDPropertyInheritanceMethod
from sd.api.sdresource import EmbedMethod
from sd.api.sdresourcebitmap import SDResourceBitmap
from sd.api.sdresourcefolder import SDResourceFolder
from sd.api.sdtexture import SDTexture
from sd.api.sdtypeusage import SDTypeUsage
from sd.api.sdusage import SDUsage
from sd.api.sdvaluearray import SDValueArray
from sd.api.sdvalueenum import SDValueEnum
from sd.api.sdvaluestring import SDValueString
from sd.api.sdvalueusage import SDValueUsage

PREFIX = "PhysicsScatter_"
ROOT_FOLDER = "PhysicsScatter"

# identifier, label, usage, components, color space, forced output format
OUTPUTS = [
    ("normal", "Normal", "normal", "RGBA", "Linear", None),
    ("height", "Height", "height", "R", "Linear", "16_bits_per_channel"),
    ("roughness", "Roughness", "roughness", "R", "Linear", None),
    ("metallic", "Metallic", "metallic", "R", "Linear", None),
    ("mask", "Coverage Mask", "mask", "R", "Linear", None),
    ("object_id", "Object ID", "objectId", "RGBA", "sRGB", None),
    ("object_random", "Object Random", "objectRandom", "R", "Linear", None),
]
OUTPUTS_IDS = tuple(o[0] for o in OUTPUTS)
RETIRED_OUTPUTS = ("basecolor",)  # baked by 0.1; removed from old scatters on re-bake

_GRID = 150.0


def ui_manager():
    return sd.getContext().getSDApplication().getQtForPythonUIMgr()


def current_graph():
    """The compositing graph open in the focused graph view, or None."""
    graph = ui_manager().getCurrentGraph()
    return graph if isinstance(graph, SDSBSCompGraph) else None


def package_file(package):
    path = package.getFilePath() if package else ""
    return path if path and os.path.isfile(path) else None


def scatter_root(package):
    path = package_file(package)
    if not path:
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(os.path.dirname(path), f"{stem}_physics_scatter")


def list_scatters(package):
    if package is None:
        return []
    ids = [r.getIdentifier() for r in package.getChildrenResources(True)
           if isinstance(r, SDSBSCompGraph) and r.getIdentifier().startswith(PREFIX)]
    return sorted(ids, key=_scatter_number)


def _scatter_number(identifier):
    try:
        return int(identifier[len(PREFIX):])
    except ValueError:
        return 0


def next_scatter_id(package):
    """Lowest free number in the package. A map folder left on disk by a deleted
    (or never saved) scatter is reused: the bake replaces its files."""
    used = {_scatter_number(i) for i in list_scatters(package)}
    n = 1
    while n in used:
        n += 1
    return f"{PREFIX}{n}"


def _find_resource(package, identifier, cls, recursive=True):
    for r in package.getChildrenResources(recursive):
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


def _output_identifier(node):
    value = node.getAnnotationPropertyValueFromId("identifier")
    return value.get() if value is not None else None


def _new_output_node(graph, identifier, label, usage, components, colorspace, row):
    node = graph.newNode("sbs::compositing::output")
    node.setPosition(float2(2 * _GRID, row * _GRID))
    node.setAnnotationPropertyValueFromId("identifier", SDValueString.sNew(identifier))
    node.setAnnotationPropertyValueFromId("label", SDValueString.sNew(label))
    node.setAnnotationPropertyValueFromId("group", SDValueString.sNew("Physics Scatter"))
    usages = SDValueArray.sNew(SDTypeUsage.sNew(), 0)
    usages.pushBack(SDValueUsage.sNew(SDUsage.sNew(usage, components, colorspace)))
    node.setAnnotationPropertyValueFromId("usages", usages)
    return node


def apply_bake(package, user_graph, scatter_id, files, preview=None):
    """Create or update the scatter graph from freshly baked files.

    Returns the instance node created in user_graph (first bake only), else None."""
    with SDHistoryUtils.UndoGroup("Physics Scatter bake"):
        graph = _find_resource(package, scatter_id, SDSBSCompGraph)
        created = graph is None
        if created:
            graph = SDSBSCompGraph.sNew(_folder(package, ROOT_FOLDER))
            graph.setIdentifier(scatter_id)

        # The maps folder sits next to the graph; 0.1 scatters keep theirs at the root.
        folder_id = f"{scatter_id}_maps"
        folder = _find_resource(package, folder_id, SDResourceFolder)
        if folder is None:
            folder = _folder(_folder(package, ROOT_FOLDER), folder_id)

        outputs = {}
        for node in graph.getOutputNodes():
            ident = _output_identifier(node)
            if ident:
                outputs[ident] = node
        for ident in RETIRED_OUTPUTS:
            if ident in outputs:
                graph.deleteNode(outputs.pop(ident))

        # Clear the previous bake: every node but the outputs, every bitmap in the
        # maps folder. (Reading the Bitmap node behind an output through
        # getPropertyConnections is easy to get wrong: 0.1 deleted the outputs.)
        keep = {node.getIdentifier() for node in outputs.values()}
        for node in list(graph.getNodes()):
            if node.getIdentifier() not in keep:
                graph.deleteNode(node)
        for resource in list(folder.getChildren(False)):
            if isinstance(resource, SDResourceBitmap):
                resource.delete()

        for row, (ident, label, usage, comps, colorspace, fmt) in enumerate(OUTPUTS):
            path = files.get(ident)
            if not path:
                continue
            out = outputs.get(ident) or _new_output_node(graph, ident, label, usage, comps, colorspace, row)
            resource = SDResourceBitmap.sNewFromFile(folder, path, EmbedMethod.Linked)
            resource.setIdentifier(ident)
            bitmap = graph.newInstanceNode(resource)
            bitmap.setPosition(float2(0.0, row * _GRID))
            if fmt:
                bitmap.setInputPropertyInheritanceMethodFromId("$format", SDPropertyInheritanceMethod.Absolute)
                bitmap.setInputPropertyValueFromId("$format", SDValueEnum.sFromValueId("sbs::compositing::format", fmt))
            bitmap.newPropertyConnectionFromId("unique_filter_output", out, "inputNodeOutput")

        if preview and os.path.isfile(preview):
            icon = SDTexture.sFromFile(preview)
            if icon is not None:
                graph.setIcon(icon)

        instance = None
        if created and user_graph is not None and user_graph.getIdentifier() != scatter_id:
            instance = user_graph.newInstanceNode(graph)
            if instance is not None:
                instance.setPosition(_free_position(user_graph))
        return instance


def _free_position(graph):
    """Just right of the selection, or to the right of everything else."""
    try:
        nodes = list(ui_manager().getCurrentGraphSelectedNodes() or [])
    except APIException:  # InvalidArgument when no graph view has focus
        nodes = []
    if not nodes:
        nodes = list(graph.getNodes())
    if not nodes:
        return float2(0.0, 0.0)
    xs = [n.getPosition().x for n in nodes]
    ys = [n.getPosition().y for n in nodes]
    return float2(max(xs) + _GRID, sum(ys) / len(ys))
