"""Physics Scatter dock panel: simulates the selected scatter node.

The settings live on the node (its parameters in Designer's Properties panel) and
the floor is the node's Floor input. The panel creates scatter nodes, runs the
simulation for the selected one and shows its preview. The simulation itself runs
in a separate process (Designer's python.exe with the vendored packages) so the
Designer UI stays responsive.
"""

import glob
import json
import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

import sd

from . import sd_bridge

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_ROOT = os.path.join(MODULE_DIR, "_vendor")
DOCK_ID = "physics_scatter_panel"
MENU_ID = "physics_scatter_menu"
OBJ_FILTER = "Wavefront OBJ (*.obj)"


def vendor_dir():
    """Bundled libraries (MuJoCo, NumPy, moderngl) built for the Python version
    this Designer embeds; its plugins/pythonsdk/python.exe is the same version."""
    path = os.path.join(VENDOR_ROOT, f"cp{sys.version_info.major}{sys.version_info.minor}")
    return path if os.path.isdir(path) else None


def supported_pythons():
    tags = sorted(d for d in os.listdir(VENDOR_ROOT) if d.startswith("cp")) if os.path.isdir(VENDOR_ROOT) else []
    return ", ".join(f"{t[2]}.{t[3:]}" for t in tags)


def find_python():
    """Designer ships a standalone CPython next to its plugins."""
    roots = [os.path.dirname(sys.executable)]
    roots.append(os.path.abspath(os.path.join(os.path.dirname(sd.__file__), "..", "..", "..")))
    for root in roots:
        candidate = os.path.join(root, "plugins", "pythonsdk", "python.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


class PhysicsScatterPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.pending = None
        self.target = None  # (user graph, scatter node) the panel acts on
        self._target_key = None
        self._stdout_buffer = b""
        self._stderr_text = ""

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        # The selected scatter node.
        box = QtWidgets.QGroupBox("Selected scatter")
        box_layout = QtWidgets.QVBoxLayout(box)
        name_row = QtWidgets.QHBoxLayout()
        self.name_label = QtWidgets.QLabel()
        font = self.name_label.font()
        font.setBold(True)
        self.name_label.setFont(font)
        name_row.addWidget(self.name_label, 1)
        self.rename_button = QtWidgets.QToolButton()
        self.rename_button.setText("Rename…")
        self.rename_button.setToolTip("Rename the scatter graph. Designer then asks which references to "
                                      "update: leave them all selected and click Ok.")
        self.rename_button.clicked.connect(self.rename)
        name_row.addWidget(self.rename_button)
        box_layout.addLayout(name_row)
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        box_layout.addWidget(self.hint)
        buttons = QtWidgets.QHBoxLayout()
        new = QtWidgets.QPushButton("New scatter")
        new.setToolTip("Add a Physics Scatter node to the current graph")
        new.clicked.connect(lambda: self.new_scatter())
        self.add_button = QtWidgets.QPushButton("Add OBJ…")
        self.add_button.setToolTip("Put OBJ files into the selected node's empty mesh slots")
        self.add_button.clicked.connect(lambda: self.add_meshes())
        buttons.addWidget(new)
        buttons.addWidget(self.add_button)
        buttons.addStretch(1)
        box_layout.addLayout(buttons)
        root.addWidget(box)

        # Simulate.
        sim_row = QtWidgets.QHBoxLayout()
        self.simulate_button = QtWidgets.QPushButton("Simulate")
        self.simulate_button.setMinimumHeight(32)
        self.simulate_button.clicked.connect(self.simulate)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        sim_row.addWidget(self.simulate_button, 1)
        sim_row.addWidget(self.cancel_button)
        root.addLayout(sim_row)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        root.addWidget(self.progress)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMinimumHeight(200)
        root.addWidget(self.preview, 1)

        self._set_target(None, 0)
        # Designer has no graph selection callback: follow the selection by polling.
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self.update_selection)
        self._poll.start()

    # ------------------------------------------------------------- selection

    def update_selection(self):
        nodes = sd_bridge.selected_nodes()
        if nodes is None:  # no graph view has focus: keep the last selection
            return
        graph = sd_bridge.current_graph()
        scatters = []
        if graph is not None and len(nodes) <= 50:
            try:
                scatters = [n for n in nodes if sd_bridge.scatter_graph(n) or sd_bridge.is_legacy_scatter(n)]
            except sd_bridge.APIException:  # a node deleted since the selection was read
                pass
        self._set_target((graph, scatters[0]) if len(scatters) == 1 else None, len(scatters))

    def _set_target(self, target, count):
        node = target[1] if target else None
        try:
            graph = node.getReferencedResource() if node is not None else None
        except sd_bridge.APIException:  # the node was deleted
            graph = None
        self.target = target if graph is not None else None
        busy = self.process is not None
        self.rename_button.setEnabled(graph is not None and not busy)
        self.add_button.setEnabled(graph is not None and not busy and sd_bridge.scatter_key(graph) is not None)
        self.simulate_button.setEnabled(graph is not None and not busy)
        if graph is None:
            self.name_label.setText("None")
            self.hint.setText("Several scatter nodes are selected: select only the one to simulate."
                              if count > 1 else
                              "Select a Physics Scatter node in a graph, or add one with New scatter.")
            key = None
        else:
            self.name_label.setText(graph.getIdentifier())
            if sd_bridge.scatter_key(graph):
                self.hint.setText("Its settings are in the Properties panel; connect a heightmap to its "
                                  "Floor input to give the objects a floor.")
            else:
                self.hint.setText("Made by an older Physics Scatter (settings in the panel, more outputs). "
                                  "Simulate converts it: its settings move onto the node, and only Height "
                                  "and Object ID stay.")
            key = (target[0].getIdentifier(), node.getIdentifier(), graph.getIdentifier())
        if key != self._target_key:
            self._target_key = key
            self._show_preview(self._preview_path(graph))

    def _preview_path(self, graph):
        if graph is None or sd_bridge.scatter_root(graph.getPackage()) is None:
            return None
        folder = sd_bridge.scatter_dir(graph) if sd_bridge.scatter_key(graph) else os.path.join(
            sd_bridge.scatter_root(graph.getPackage()), graph.getIdentifier())
        job_path = os.path.join(folder, "job.json")
        if not os.path.isfile(job_path):
            return None
        with open(job_path, "r", encoding="utf-8") as f:
            suffix = json.load(f).get("file_suffix", "")
        return os.path.join(folder, f"preview{suffix}.png")

    def _show_preview(self, path):
        if path and os.path.isfile(path):
            pix = QtGui.QPixmap(path)
            size = max(64, min(self.preview.width(), 512))
            self.preview.setPixmap(pix.scaled(size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        else:
            self.preview.clear()

    # --------------------------------------------------------------- helpers

    def _fail(self, message):
        self.status.setText(message)
        QtWidgets.QMessageBox.warning(self, "Physics Scatter", message)

    def new_scatter(self, meshes=None):
        """Add a scatter node to the current graph. meshes: OBJ paths; None asks for them."""
        graph = sd_bridge.current_graph()
        if graph is None:
            return self._fail("Open a Substance compositing graph first; the scatter node is added to it.")
        try:
            node = sd_bridge.create_scatter(graph)
        except sd_bridge.APIException as exc:
            return self._fail(f"Could not add the scatter node: {exc!r}")
        self._set_target((graph, node), 1)
        name = node.getReferencedResource().getIdentifier()
        self.status.setText(f"Added {name}. Select it to see its settings in the Properties panel.")
        self.add_meshes(meshes)
        return node

    def add_meshes(self, paths=None):
        if self.target is None or sd_bridge.scatter_graph(self.target[1]) is None:
            return self._fail("Select a Physics Scatter node first.")
        if paths is None:
            paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add meshes", "", OBJ_FILTER)
        if not paths:
            return
        left = sd_bridge.add_meshes(self.target[1], paths)
        if left:
            self._fail(f"The node has {sd_bridge.MESH_SLOTS} mesh slots; not added:\n" + "\n".join(left))

    def rename(self, name=None):
        graph = self.target[1].getReferencedResource() if self.target else None
        if graph is None:
            return
        if name is None:
            name, ok = QtWidgets.QInputDialog.getText(self, "Rename scatter", "Name:", text=graph.getIdentifier())
            if not ok:
                return
        try:
            new_name = sd_bridge.rename_scatter(graph, name)
        except sd_bridge.APIException as exc:
            return self._fail(f"Could not rename: {exc!r}")
        self.name_label.setText(new_name)

    # -------------------------------------------------------------- simulate

    def simulate(self):
        if self.process is not None:
            return
        self.update_selection()
        if self.target is None:
            return self._fail("Select one Physics Scatter node in a graph (or add one with New scatter).")
        user_graph, node = self.target
        package = user_graph.getPackage()
        root = sd_bridge.scatter_root(package)
        if root is None:
            return self._fail("Save the package (.sbs) first. Baked maps are stored next to it.")
        python = find_python()
        if python is None:
            return self._fail("Could not find Designer's python.exe (plugins/pythonsdk).")
        vendor = vendor_dir()
        if vendor is None:
            return self._fail(
                f"This Designer runs Python {sys.version_info.major}.{sys.version_info.minor}, but this build of "
                f"Physics Scatter only includes libraries for Python {supported_pythons()}.")

        notes = []
        try:
            if sd_bridge.is_legacy_scatter(node):
                legacy = node.getReferencedResource().getIdentifier()
                job_path = os.path.join(root, legacy, "job.json")
                old_job = {}
                if os.path.isfile(job_path):
                    with open(job_path, "r", encoding="utf-8") as f:
                        old_job = json.load(f)
                sd_bridge.upgrade_legacy(node, old_job)
                notes.append(f"{legacy} was converted: its settings are now on the node.")
            graph = sd_bridge.scatter_graph(node)
            if (graph.getPackage().getFilePath() != package.getFilePath()
                    or len(sd_bridge.instances_of(graph)) > 1):
                node = sd_bridge.give_own_graph(user_graph, node)
                graph = sd_bridge.scatter_graph(node)
                notes.append(f"The node shared its graph with another node, so it got its own: {graph.getIdentifier()}.")
                self._set_target((user_graph, node), 1)
            sd_bridge.ensure_unique_key(graph)
            sd_bridge.ensure_structure(graph)
            job = sd_bridge.read_settings(node)
        except (Exception, sd_bridge.APIException) as exc:
            return self._fail(f"Could not read the scatter node: {exc!r}")

        if not job["meshes"] or sum(m["count"] for m in job["meshes"]) == 0:
            return self._fail("Give the node at least one mesh (OBJ file) with a count above 0: "
                              "Add OBJ…, or type the path in its Properties.")
        missing = [m["path"] for m in job["meshes"] if not os.path.isfile(m["path"])]
        if missing:
            return self._fail("File not found:\n" + "\n".join(missing))

        out_dir = sd_bridge.scatter_dir(graph)
        os.makedirs(out_dir, exist_ok=True)
        floor_path = os.path.join(out_dir, "floor_input.npy")
        try:
            if sd_bridge.capture_floor(user_graph, node, floor_path):
                job["floor"]["data"] = floor_path
        except (Exception, sd_bridge.APIException) as exc:
            return self._fail(f"Could not read the image connected to Floor: {exc!r}")

        previous = {}
        job_path = os.path.join(out_dir, "job.json")
        if os.path.isfile(job_path):
            with open(job_path, "r", encoding="utf-8") as f:
                previous = json.load(f)
        bake_index = int(previous.get("bake_index", 0)) + 1
        job.update(scatter=graph.getIdentifier(), output_dir=out_dir, bake_index=bake_index,
                   file_suffix=f"_b{bake_index:04d}")
        pending_path = os.path.join(out_dir, "job_pending.json")
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2)

        self.pending = dict(job=job, job_path=job_path, pending_path=pending_path, graph=graph, node=node,
                            previous_suffix=previous.get("file_suffix"), notes=notes)
        self._stdout_buffer = b""
        self._stderr_text = ""

        proc = QtCore.QProcess(self)
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.remove("PYTHONHOME")
        env.insert("PYTHONPATH", os.pathsep.join([vendor, os.path.dirname(MODULE_DIR)]))
        env.insert("PYTHONNOUSERSITE", "1")
        env.insert("PYTHONUNBUFFERED", "1")
        proc.setProcessEnvironment(env)
        proc.setProgram(python)
        proc.setArguments(["-m", "physics_scatter.engine", pending_path])
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        self.process = proc
        self.simulate_button.setEnabled(False)
        self.rename_button.setEnabled(False)
        self.add_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText(f"Simulating {graph.getIdentifier()}…")
        proc.start()

    def cancel(self):
        if self.process is not None:
            self.process.kill()
            self.status.setText("Cancelled.")

    def _on_stdout(self):
        self._stdout_buffer += bytes(self.process.readAllStandardOutput())
        *lines, self._stdout_buffer = self._stdout_buffer.split(b"\n")
        for line in lines:
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "progress":
                self.progress.setValue(int(msg.get("value", 0) * 1000))
                self.status.setText(f"{msg.get('stage', '')}: {msg.get('message', '')}")
            elif kind == "done":
                self.pending["result"] = msg["result"]
            elif kind == "error":
                self.pending["error"] = msg.get("message", "unknown error")
                self._stderr_text += msg.get("trace", "")

    def _on_stderr(self):
        self._stderr_text += bytes(self.process.readAllStandardError()).decode("utf-8", "replace")

    def _on_finished(self, exit_code, exit_status):
        self._on_stdout()
        pending, self.pending = self.pending, None
        self.process.deleteLater()
        self.process = None
        self.cancel_button.setEnabled(False)
        self._set_target(self.target, 1 if self.target else 0)

        result = pending.get("result") if pending else None
        if result is None:
            if exit_status == QtCore.QProcess.CrashExit and "error" not in (pending or {}):
                self.progress.setValue(0)
                return  # cancelled
            detail = (pending or {}).get("error") or self._stderr_text.strip()[-1500:] or f"exit code {exit_code}"
            return self._fail(f"Simulation failed: {detail}")

        job = pending["job"]
        try:
            sd_bridge.apply_bake(pending["graph"], pending["node"], result["files"], result.get("preview"),
                                 result.get("object_count"))
        except (Exception, sd_bridge.APIException) as exc:
            return self._fail(f"Simulated, but updating the graph failed: {exc!r}")
        os.replace(pending["pending_path"], pending["job_path"])
        self._delete_bake_files(job["output_dir"], pending.get("previous_suffix"))
        self._show_preview(result.get("preview"))
        self.progress.setValue(1000)
        sim = result.get("simulation", {})
        settled = "settled" if sim.get("settled") else "time limit reached"
        floor = ", on the Floor input" if job["floor"].get("data") else ""
        self.status.setText(" ".join(pending.get("notes", []) + [
            f"{job['scatter']}: {result['object_count']} objects{floor}, {sim.get('sim_time', 0):.1f}s "
            f"simulated ({settled}), coverage {result.get('coverage', 0) * 100:.0f}%, "
            f"done in {result.get('total_time', 0):.0f}s."]))

    @staticmethod
    def _delete_bake_files(out_dir, suffix):
        if not suffix:
            return
        for path in glob.glob(os.path.join(out_dir, f"*{suffix}.*")):
            try:
                os.remove(path)
            except OSError:
                pass  # Designer may still hold the old file open; harmless


# --------------------------------------------------------------- plugin glue

class _Installed:
    def __init__(self, dock_content, panel, menu):
        self.dock_content = dock_content
        self.panel = panel
        self.menu = menu


def install():
    ui = sd_bridge.ui_manager()
    if ui is None:
        return None
    dock_content = ui.newDockWidget(DOCK_ID, "Physics Scatter")
    layout = QtWidgets.QVBoxLayout(dock_content)
    layout.setContentsMargins(0, 0, 0, 0)
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    panel = PhysicsScatterPanel()
    scroll.setWidget(panel)
    layout.addWidget(scroll)

    def show_panel():
        dock = dock_content.parentWidget()
        while dock is not None and not isinstance(dock, QtWidgets.QDockWidget):
            dock = dock.parentWidget()
        (dock or dock_content).show()
        (dock or dock_content).raise_()

    menu = ui.newMenu("Physics Scatter", MENU_ID)
    menu.addAction("Show panel").triggered.connect(show_panel)
    menu.addAction("New scatter node").triggered.connect(lambda: (show_panel(), panel.new_scatter()))
    menu.addAction("Simulate selected node").triggered.connect(lambda: (show_panel(), panel.simulate()))
    return _Installed(dock_content, panel, menu)


def uninstall(installed):
    if installed is None:
        return
    installed.panel._poll.stop()
    installed.panel.cancel()
    ui = sd_bridge.ui_manager()
    if ui is not None:
        ui.deleteMenu(MENU_ID)
