"""Physics Scatter UI glue. There is no panel: a scatter's settings, its Simulate
switch and its last result are parameters of the node (Designer's Properties
panel). The plugin watches the selected nodes and runs the simulation when a
node's Simulate switch is turned on; progress shows in Designer's status bar.
The menu adds scatter nodes and OBJ files.

The simulation itself runs in a separate process (Designer's python.exe with the
vendored packages) so the Designer UI stays responsive.
"""

import glob
import json
import os
import sys

from PySide6 import QtCore, QtWidgets

import sd

from . import sd_bridge

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_ROOT = os.path.join(MODULE_DIR, "_vendor")
MENU_ID = "physics_scatter_menu"
OBJ_FILTER = "Wavefront OBJ (*.obj)"
POLL_MS = 300


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


class _Abort(Exception):
    pass


class Simulator(QtCore.QObject):
    def __init__(self, window=None):
        super().__init__()
        self.window = window
        self.job = None  # the running simulation
        self.process = None
        self.last_status = ""
        self._progress = None
        self._stdout_buffer = b""
        self._stderr_text = ""
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.cancel()

    # ------------------------------------------------------------ watching

    def poll(self):
        if self.job is not None:
            if not self._switch_on(self.job["node"]):
                self.cancel()
            return
        for user_graph, node in sd_bridge.selected_scatter_nodes():
            graph = sd_bridge.scatter_graph(node)
            if not sd_bridge.has_param(graph, "simulate"):  # made by 0.3.0: add the Run group
                sd_bridge.ensure_structure(graph)
                self._status(f"{graph.getIdentifier()} now has the Simulate switch (Run group).")
            if self._switch_on(node):
                self.start(user_graph, node)
                return

    @staticmethod
    def _switch_on(node):
        try:
            return bool(sd_bridge.get_value(node, "simulate"))
        except sd_bridge.APIException:  # deleted node
            return False

    # ----------------------------------------------------------- feedback

    def _status(self, text, progress=None):
        self.last_status = text
        bar = self.window.statusBar() if self.window is not None else None
        if bar is None:
            return
        bar.showMessage(f"Physics Scatter: {text}")
        bar.repaint()  # shows even when a blocking step (reading the Floor input) follows
        if progress is None:
            if self._progress is not None:
                bar.removeWidget(self._progress)
                self._progress.deleteLater()
                self._progress = None
            return
        if self._progress is None:
            self._progress = QtWidgets.QProgressBar()
            self._progress.setRange(0, 1000)
            self._progress.setMaximumWidth(220)
            bar.addPermanentWidget(self._progress)
        self._progress.setValue(int(progress * 1000))

    def _fail(self, node, message):
        """Report a failure on the node and in a message box; Simulate goes back to Idle."""
        if node is not None:
            sd_bridge.finish_run(node, "Failed: " + message.replace("\n", " "))
        self._status("failed")
        self._timer.stop()  # the message box runs its own event loop
        try:
            QtWidgets.QMessageBox.warning(self.window, "Physics Scatter", message)
        finally:
            self._timer.start()

    # ----------------------------------------------------------- simulate

    def start(self, user_graph, node):
        target = {"node": node}  # _prepare may replace the node (see give_own_graph)
        try:
            job, graph, node, notes = self._prepare(user_graph, target)
        except _Abort as exc:
            return self._fail(target["node"], str(exc))
        except (Exception, sd_bridge.APIException) as exc:
            return self._fail(target["node"], f"Could not read the scatter node: {exc!r}")

        out_dir = job["output_dir"]
        previous = {}
        job_path = os.path.join(out_dir, "job.json")
        if os.path.isfile(job_path):
            with open(job_path, "r", encoding="utf-8") as f:
                previous = json.load(f)
        bake_index = int(previous.get("bake_index", 0)) + 1
        job.update(bake_index=bake_index, file_suffix=f"_b{bake_index:04d}")
        pending_path = os.path.join(out_dir, "job_pending.json")
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2)

        self.job = dict(job=job, job_path=job_path, pending_path=pending_path, graph=graph, node=node,
                        previous_suffix=previous.get("file_suffix"), notes=notes)
        self._stdout_buffer = b""
        self._stderr_text = ""
        proc = QtCore.QProcess(self)
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.remove("PYTHONHOME")
        env.insert("PYTHONPATH", os.pathsep.join([vendor_dir(), os.path.dirname(MODULE_DIR)]))
        env.insert("PYTHONNOUSERSITE", "1")
        env.insert("PYTHONUNBUFFERED", "1")
        proc.setProcessEnvironment(env)
        proc.setProgram(find_python())
        proc.setArguments(["-m", "physics_scatter.engine", pending_path])
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        self.process = proc
        self._status(f"simulating {graph.getIdentifier()}…", 0.0)
        proc.start()

    def _prepare(self, user_graph, target):
        """Check everything and read the node: returns (job, graph, node, notes)."""
        node = target["node"]
        package = user_graph.getPackage()
        root = sd_bridge.scatter_root(package)
        if root is None:
            raise _Abort("Save the package (.sbs) first. The baked maps are stored next to it.")
        if find_python() is None:
            raise _Abort("Could not find Designer's python.exe (plugins/pythonsdk).")
        if vendor_dir() is None:
            raise _Abort(f"This Designer runs Python {sys.version_info.major}.{sys.version_info.minor}, but this "
                         f"build of Physics Scatter only includes libraries for Python {supported_pythons()}.")
        notes = []
        graph = sd_bridge.scatter_graph(node)
        if (graph.getPackage().getFilePath() != package.getFilePath()
                or len(sd_bridge.instances_of(graph)) > 1):
            node = target["node"] = sd_bridge.give_own_graph(user_graph, node)
            graph = sd_bridge.scatter_graph(node)
            notes.append(f"It shared its graph with another node, so it got its own: {graph.getIdentifier()}.")
        sd_bridge.ensure_unique_key(graph)
        sd_bridge.ensure_structure(graph)
        job = sd_bridge.read_settings(node)

        if not job["meshes"] or sum(m["count"] for m in job["meshes"]) == 0:
            raise _Abort("Give the node at least one mesh (OBJ file) with a count above 0: paste a path into "
                         "Meshes → Mesh 1 → OBJ file, or use Physics Scatter → Add OBJ files to selected scatter.")
        missing = [m["path"] for m in job["meshes"] if not os.path.isfile(m["path"])]
        if missing:
            raise _Abort("File not found:\n" + "\n".join(missing))

        out_dir = sd_bridge.scatter_dir(graph)
        os.makedirs(out_dir, exist_ok=True)
        floor_path = os.path.join(out_dir, "floor_input.npy")
        self._status(f"reading the Floor input of {graph.getIdentifier()}…", 0.0)
        try:
            if sd_bridge.capture_floor(user_graph, node, floor_path):
                job["floor"]["data"] = floor_path
        except (Exception, sd_bridge.APIException) as exc:
            raise _Abort(f"Could not read the image connected to Floor: {exc!r}")
        job.update(scatter=graph.getIdentifier(), output_dir=out_dir)
        return job, graph, node, notes

    def cancel(self):
        if self.process is not None:
            self.job["cancelled"] = True
            self.process.kill()

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
                self._status(f"{self.job['job']['scatter']}: {msg.get('stage', '')} {msg.get('message', '')}",
                             msg.get("value", 0))
            elif kind == "done":
                self.job["result"] = msg["result"]
            elif kind == "error":
                self.job["error"] = msg.get("message", "unknown error")
                self._stderr_text += msg.get("trace", "")

    def _on_stderr(self):
        self._stderr_text += bytes(self.process.readAllStandardError()).decode("utf-8", "replace")

    def _on_finished(self, exit_code, exit_status):
        self._on_stdout()
        job, self.job = self.job, None
        self.process.deleteLater()
        self.process = None
        node = job["node"]

        result = job.get("result")
        if result is None:
            if job.get("cancelled"):
                sd_bridge.finish_run(node, "Cancelled.")
                return self._status("cancelled")
            detail = job.get("error") or self._stderr_text.strip()[-1500:] or f"exit code {exit_code}"
            return self._fail(node, f"Simulation failed: {detail}")

        spec = job["job"]
        sim = result.get("simulation", {})
        settled = "settled" if sim.get("settled") else "time limit reached"
        floor = ", on the Floor input" if spec["floor"].get("data") else ""
        summary = (f"{result['object_count']} objects{floor}, {sim.get('sim_time', 0):.1f}s simulated ({settled}), "
                   f"coverage {result.get('coverage', 0) * 100:.0f}%, done in {result.get('total_time', 0):.0f}s.")
        try:
            sd_bridge.apply_bake(job["graph"], node, result["files"], result.get("preview"),
                                 result.get("object_count"), summary)
        except (Exception, sd_bridge.APIException) as exc:
            return self._fail(node, f"Simulated, but updating the graph failed: {exc!r}")
        os.replace(job["pending_path"], job["job_path"])
        self._delete_bake_files(spec["output_dir"], job.get("previous_suffix"))
        self._status(" ".join(job["notes"] + [f"{spec['scatter']}: {summary}"]))

    @staticmethod
    def _delete_bake_files(out_dir, suffix):
        if not suffix:
            return
        for path in glob.glob(os.path.join(out_dir, f"*{suffix}.*")):
            try:
                os.remove(path)
            except OSError:
                pass  # Designer may still hold the old file open; harmless

    # --------------------------------------------------------------- menu

    def new_scatter(self, meshes=None):
        """Add a scatter node to the current graph. meshes: OBJ paths; None asks for them."""
        graph = sd_bridge.current_graph()
        if graph is None:
            return self._fail(None, "Open a Substance compositing graph first; the scatter node is added to it.")
        node = sd_bridge.create_scatter(graph)
        if meshes is None:
            meshes, _ = QtWidgets.QFileDialog.getOpenFileNames(self.window, "Meshes for the new scatter", "",
                                                               OBJ_FILTER)
        if meshes:
            sd_bridge.add_meshes(node, meshes)
        self._status(f"added {node.getReferencedResource().getIdentifier()}. Select it: its settings and the "
                     f"Simulate switch are in the Properties panel.")
        return node

    def add_meshes(self, paths=None):
        selected = sd_bridge.selected_scatter_nodes()
        if len(selected) != 1:
            return self._fail(None, "Select one Physics Scatter node first.")
        node = selected[0][1]
        if paths is None:
            paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self.window, "Add meshes", "", OBJ_FILTER)
        if not paths:
            return
        left = sd_bridge.add_meshes(node, paths)
        if left:
            self._fail(None, f"The node has {sd_bridge.MESH_SLOTS} mesh slots; not added:\n" + "\n".join(left))

    def convert_legacy(self):
        old = [(g, n) for g, n in sd_bridge.selected_in_all_views() if sd_bridge.is_legacy_scatter(n)]
        if len(old) != 1:
            return self._fail(None, "Select one scatter made by Physics Scatter 0.1 or 0.2 first.")
        user_graph, node = old[0]
        root = sd_bridge.scatter_root(user_graph.getPackage())
        name = node.getReferencedResource().getIdentifier()
        job = {}
        job_path = os.path.join(root, name, "job.json") if root else ""
        if os.path.isfile(job_path):
            with open(job_path, "r", encoding="utf-8") as f:
                job = json.load(f)
        sd_bridge.upgrade_legacy(node, job)
        self._status(f"converted {name}: its settings are on the node now. Switch Simulate on to bake it again "
                     f"(a floor file from 0.2 must be connected to the Floor input).")


# --------------------------------------------------------------- plugin glue

class _Installed:
    def __init__(self, simulator, menu):
        self.simulator = simulator
        self.menu = menu


def install():
    ui = sd_bridge.ui_manager()
    if ui is None:
        return None
    simulator = Simulator(ui.getMainWindow())
    menu = ui.newMenu("Physics Scatter", MENU_ID)
    menu.addAction("New scatter node").triggered.connect(lambda: simulator.new_scatter())
    menu.addAction("Add OBJ files to selected scatter…").triggered.connect(lambda: simulator.add_meshes())
    menu.addAction("Convert selected 0.2 scatter").triggered.connect(simulator.convert_legacy)
    return _Installed(simulator, menu)


def uninstall(installed):
    if installed is None:
        return
    installed.simulator.stop()
    ui = sd_bridge.ui_manager()
    if ui is not None:
        ui.deleteMenu(MENU_ID)
