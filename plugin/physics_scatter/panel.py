"""Physics Scatter dock panel: mesh list, settings, bake button and preview.

The bake itself runs in a separate process (Designer's python.exe with the
vendored packages) so the Designer UI stays responsive.
"""

import glob
import json
import os
import random
import struct
import sys

from PySide6 import QtCore, QtGui, QtWidgets

import sd

from . import sd_bridge

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_ROOT = os.path.join(MODULE_DIR, "_vendor")
DOCK_ID = "physics_scatter_panel"
MENU_ID = "physics_scatter_menu"
NEW_SCATTER = "New scatter"

DEFAULT_MESH = dict(count=40, size=8.0, size_variation=25.0, roughness=0.7, metallic=0.0)
DEFAULT_FLOOR = dict(path="", height=0.1, in_maps=True)


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


class _WheelGuard(QtCore.QObject):
    """Scrolling the panel must not change whatever field passes under the cursor."""

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return False


_WHEEL_GUARD = None


def _guard(widget):
    global _WHEEL_GUARD
    if _WHEEL_GUARD is None:
        _WHEEL_GUARD = _WheelGuard()
    widget.setFocusPolicy(QtCore.Qt.StrongFocus)
    widget.installEventFilter(_WHEEL_GUARD)
    return widget


def _spin(lo, hi, value, step=1.0, decimals=2, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(decimals)
    w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix(suffix)
    return _guard(w)


def image_filter():
    formats = sorted({bytes(f).decode() for f in QtGui.QImageReader.supportedImageFormats()}
                     - {"svg", "svgz", "ico", "cur", "gif", "icns", "xbm", "xpm"})
    return "Images (" + " ".join(f"*.{f}" for f in formats) + ")"


def write_heightmap(src, dst):
    """Copy any image Qt can read as 16-bit grayscale .npy (top row first) for the
    engine, which has no image decoders of its own."""
    img = QtGui.QImage(src)
    if img.isNull():
        raise ValueError(f"Could not read the floor heightmap:\n{src}")
    img = img.convertToFormat(QtGui.QImage.Format_Grayscale16)
    w, h, stride = img.width(), img.height(), img.bytesPerLine()
    bits = bytes(img.constBits())
    rows = b"".join(bits[r * stride:r * stride + 2 * w] for r in range(h))
    header = "{'descr': '<u2', 'fortran_order': False, 'shape': (%d, %d), }" % (h, w)
    header += " " * ((64 - (11 + len(header)) % 64) % 64) + "\n"
    with open(dst, "wb") as f:
        f.write(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header.encode("latin1") + rows)


class MeshTable(QtWidgets.QTableWidget):
    COLUMNS = ["Mesh", "Count", "Size %", "Var %", "Rough", "Metal"]

    def __init__(self):
        super().__init__(0, len(self.COLUMNS))
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for col in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeToContents)
        self.setMinimumHeight(120)

    def add_mesh(self, path, settings=None):
        s = dict(DEFAULT_MESH)
        s.update(settings or {})
        row = self.rowCount()
        self.insertRow(row)
        item = QtWidgets.QTableWidgetItem(os.path.basename(path))
        item.setToolTip(path)
        item.setData(QtCore.Qt.UserRole, path)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.setItem(row, 0, item)
        count = _guard(QtWidgets.QSpinBox())
        count.setRange(0, 5000)
        count.setValue(int(s["count"]))
        self.setCellWidget(row, 1, count)
        self.setCellWidget(row, 2, _spin(0.2, 100.0, s["size"], 0.5, 1))
        self.setCellWidget(row, 3, _spin(0.0, 90.0, s["size_variation"], 5.0, 0))
        self.setCellWidget(row, 4, _spin(0.0, 1.0, s["roughness"], 0.05))
        self.setCellWidget(row, 5, _spin(0.0, 1.0, s["metallic"], 0.05))

    def meshes(self):
        out = []
        for row in range(self.rowCount()):
            out.append(dict(
                path=self.item(row, 0).data(QtCore.Qt.UserRole),
                count=self.cellWidget(row, 1).value(),
                size=self.cellWidget(row, 2).value(),
                size_variation=self.cellWidget(row, 3).value(),
                roughness=self.cellWidget(row, 4).value(),
                metallic=self.cellWidget(row, 5).value(),
            ))
        return out


class PhysicsScatterPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.pending = None
        self._stdout_buffer = b""
        self._stderr_text = ""

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        # Scatter selection.
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Scatter"))
        self.scatter_combo = _guard(QtWidgets.QComboBox())
        self.scatter_combo.currentTextChanged.connect(self._load_selected)
        top.addWidget(self.scatter_combo, 1)
        refresh = QtWidgets.QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("Refresh the list from the current graph's package")
        refresh.clicked.connect(self.refresh_scatters)
        top.addWidget(refresh)
        root.addLayout(top)

        # Meshes.
        mesh_box = QtWidgets.QGroupBox("Meshes (OBJ)")
        mesh_layout = QtWidgets.QVBoxLayout(mesh_box)
        self.table = MeshTable()
        mesh_layout.addWidget(self.table)
        buttons = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add…")
        add.clicked.connect(self._add_meshes)
        remove = QtWidgets.QPushButton("Remove")
        remove.clicked.connect(self._remove_meshes)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        mesh_layout.addLayout(buttons)
        root.addWidget(mesh_box)

        # Settings.
        settings_box = QtWidgets.QGroupBox("Settings")
        form = QtWidgets.QFormLayout(settings_box)
        self.resolution = _guard(QtWidgets.QComboBox())
        self.resolution.addItems(["512", "1024", "2048", "4096"])
        self.resolution.setCurrentText("2048")
        form.addRow("Resolution", self.resolution)
        seed_row = QtWidgets.QHBoxLayout()
        self.seed = _guard(QtWidgets.QSpinBox())
        self.seed.setRange(0, 999999)
        self.seed.setValue(1)
        dice = QtWidgets.QToolButton()
        dice.setText("Random")
        dice.clicked.connect(lambda: self.seed.setValue(random.randint(0, 999999)))
        seed_row.addWidget(self.seed, 1)
        seed_row.addWidget(dice)
        form.addRow("Seed", seed_row)
        self.friction = _spin(0.0, 2.0, 0.7, 0.05)
        form.addRow("Friction", self.friction)
        self.bounciness = _spin(0.0, 1.0, 0.1, 0.05)
        form.addRow("Bounciness", self.bounciness)
        self.quality = _guard(QtWidgets.QComboBox())
        self.quality.addItems(["Low", "Medium", "High"])
        self.quality.setCurrentText("Medium")
        form.addRow("Quality", self.quality)
        self.max_time = _spin(2.0, 60.0, 8.0, 1.0, 1, " s")
        self.max_time.setToolTip("Simulation time limit. It stops earlier once everything is at rest.")
        form.addRow("Max sim time", self.max_time)
        self.normal_format = _guard(QtWidgets.QComboBox())
        self.normal_format.addItems(["DirectX", "OpenGL"])
        form.addRow("Normal format", self.normal_format)
        self.var_rough = _spin(0.0, 0.5, 0.1, 0.05)
        form.addRow("Roughness variation", self.var_rough)
        root.addWidget(settings_box)

        # Floor.
        floor_box = QtWidgets.QGroupBox("Floor")
        floor_form = QtWidgets.QFormLayout(floor_box)
        floor_row = QtWidgets.QHBoxLayout()
        self.floor_path = QtWidgets.QLineEdit()
        self.floor_path.setReadOnly(True)
        self.floor_path.setPlaceholderText("Flat ground")
        browse = QtWidgets.QToolButton()
        browse.setText("…")
        browse.setToolTip("Pick a tileable heightmap for the objects to land on")
        browse.clicked.connect(self._pick_floor)
        clear = QtWidgets.QToolButton()
        clear.setText("✕")
        clear.setToolTip("Back to flat ground")
        clear.clicked.connect(lambda: self._set_floor_path(""))
        floor_row.addWidget(self.floor_path, 1)
        floor_row.addWidget(browse)
        floor_row.addWidget(clear)
        floor_form.addRow("Heightmap", floor_row)
        self.floor_height = _spin(0.5, 100.0, DEFAULT_FLOOR["height"] * 100.0, 1.0, 1, " %")
        self.floor_height.setToolTip("Height of white in the heightmap, in % of the texture width "
                                     "(the same unit as the mesh sizes).")
        floor_form.addRow("Height", self.floor_height)
        self.floor_in_maps = QtWidgets.QCheckBox("Include the floor in Height and Normal")
        self.floor_in_maps.setChecked(DEFAULT_FLOOR["in_maps"])
        self.floor_in_maps.setToolTip("Off: Height and Normal show only the objects (still at the height "
                                      "where they landed).")
        floor_form.addRow(self.floor_in_maps)
        root.addWidget(floor_box)
        self._set_floor_path("")

        # Bake.
        bake_row = QtWidgets.QHBoxLayout()
        self.bake_button = QtWidgets.QPushButton("Simulate && Bake")
        self.bake_button.setMinimumHeight(32)
        self.bake_button.clicked.connect(self.bake)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        bake_row.addWidget(self.bake_button, 1)
        bake_row.addWidget(self.cancel_button)
        root.addLayout(bake_row)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        root.addWidget(self.progress)
        self.status = QtWidgets.QLabel("Add meshes, then Simulate & Bake.")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMinimumHeight(200)
        root.addWidget(self.preview, 1)

        self.refresh_scatters()

    # ----------------------------------------------------------------- state

    def _job_path(self, package, scatter_id):
        root = sd_bridge.scatter_root(package)
        return os.path.join(root, scatter_id, "job.json") if root else None

    def refresh_scatters(self):
        graph = sd_bridge.current_graph()
        package = graph.getPackage() if graph else None
        current = self.scatter_combo.currentText()
        self.scatter_combo.blockSignals(True)
        self.scatter_combo.clear()
        self.scatter_combo.addItem(NEW_SCATTER)
        for ident in sd_bridge.list_scatters(package):
            self.scatter_combo.addItem(ident)
        index = self.scatter_combo.findText(current)
        self.scatter_combo.setCurrentIndex(max(0, index))
        self.scatter_combo.blockSignals(False)
        if index < 0 and current and current != NEW_SCATTER:
            self._load_selected(self.scatter_combo.currentText())

    def _load_selected(self, scatter_id):
        if not scatter_id or scatter_id == NEW_SCATTER:
            return
        graph = sd_bridge.current_graph()
        path = self._job_path(graph.getPackage(), scatter_id) if graph else None
        if not path or not os.path.isfile(path):
            self.status.setText(f"No saved settings found for {scatter_id}.")
            return
        with open(path, "r", encoding="utf-8") as f:
            job = json.load(f)
        self.apply_settings(job)
        preview = os.path.join(os.path.dirname(path), f"preview{job.get('file_suffix', '')}.png")
        self._show_preview(preview)
        self.status.setText(f"Loaded {scatter_id} (bake {job.get('bake_index', '?')}).")

    def settings(self):
        meshes = self.table.meshes()
        for m in meshes:  # the UI shows percentages, the engine wants fractions
            m["size"] = m["size"] / 100.0
            m["size_variation"] = m["size_variation"] / 100.0
        return {
            "resolution": int(self.resolution.currentText()),
            "seed": self.seed.value(),
            "meshes": meshes,
            "physics": {
                "friction": self.friction.value(),
                "bounciness": self.bounciness.value(),
                "quality": self.quality.currentText().lower(),
                "max_time": self.max_time.value(),
            },
            "variation": {
                "roughness": self.var_rough.value(),
            },
            "normal_format": self.normal_format.currentText().lower(),
            "floor": {
                "path": self.floor_path.text(),
                "height": self.floor_height.value() / 100.0,
                "in_maps": self.floor_in_maps.isChecked(),
            },
        }

    def apply_settings(self, job):
        self.table.setRowCount(0)
        for m in job.get("meshes", []):
            s = dict(m)
            s["size"] = s.get("size", 0.08) * 100.0
            s["size_variation"] = s.get("size_variation", 0.25) * 100.0
            self.table.add_mesh(m["path"], s)
        self.resolution.setCurrentText(str(job.get("resolution", 2048)))
        self.seed.setValue(int(job.get("seed", 1)))
        phys = job.get("physics", {})
        self.friction.setValue(phys.get("friction", 0.7))
        self.bounciness.setValue(phys.get("bounciness", 0.1))
        self.quality.setCurrentText(phys.get("quality", "medium").capitalize())
        self.max_time.setValue(phys.get("max_time", 8.0))
        self.normal_format.setCurrentText("OpenGL" if job.get("normal_format") == "opengl" else "DirectX")
        self.var_rough.setValue(job.get("variation", {}).get("roughness", 0.1))
        floor = dict(DEFAULT_FLOOR)
        floor.update(job.get("floor") or {})
        self._set_floor_path(floor["path"])
        self.floor_height.setValue(floor["height"] * 100.0)
        self.floor_in_maps.setChecked(floor["in_maps"])

    def _add_meshes(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add meshes", "", "Wavefront OBJ (*.obj)")
        for p in paths:
            self.table.add_mesh(p)

    def _pick_floor(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Floor heightmap", self.floor_path.text(),
                                                        image_filter())
        if path:
            self._set_floor_path(path)

    def _set_floor_path(self, path):
        self.floor_path.setText(path)
        self.floor_path.setToolTip(path)
        self.floor_height.setEnabled(bool(path))
        self.floor_in_maps.setEnabled(bool(path))

    def _remove_meshes(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _show_preview(self, path):
        if path and os.path.isfile(path):
            pix = QtGui.QPixmap(path)
            size = min(self.preview.width(), 512)
            self.preview.setPixmap(pix.scaled(size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        else:
            self.preview.clear()

    # ------------------------------------------------------------------ bake

    def _fail(self, message):
        self.status.setText(message)
        QtWidgets.QMessageBox.warning(self, "Physics Scatter", message)

    def bake(self):
        if self.process is not None:
            return
        graph = sd_bridge.current_graph()
        if graph is None:
            return self._fail("Open a Substance compositing graph first; the scatter node is added to it.")
        package = graph.getPackage()
        root = sd_bridge.scatter_root(package)
        if root is None:
            return self._fail("Save the package (.sbs) first. Baked maps are stored next to it.")
        job = self.settings()
        if not job["meshes"] or sum(m["count"] for m in job["meshes"]) == 0:
            return self._fail("Add at least one mesh with a count above 0.")
        missing = [m["path"] for m in job["meshes"] if not os.path.isfile(m["path"])]
        floor_path = job["floor"]["path"]
        if floor_path and not os.path.isfile(floor_path):
            missing.append(floor_path)
        if missing:
            return self._fail("File not found:\n" + "\n".join(missing))
        python = find_python()
        if python is None:
            return self._fail("Could not find Designer's python.exe (plugins/pythonsdk).")
        vendor = vendor_dir()
        if vendor is None:
            return self._fail(
                f"This Designer runs Python {sys.version_info.major}.{sys.version_info.minor}, but this build of "
                f"Physics Scatter only includes libraries for Python {supported_pythons()}.")

        scatter_id = self.scatter_combo.currentText()
        if scatter_id == NEW_SCATTER:
            scatter_id = sd_bridge.next_scatter_id(package)
        out_dir = os.path.join(root, scatter_id)
        os.makedirs(out_dir, exist_ok=True)
        if floor_path:
            job["floor"]["data"] = os.path.join(out_dir, "floor_input.npy")
            try:
                write_heightmap(floor_path, job["floor"]["data"])
            except (OSError, ValueError) as exc:
                return self._fail(str(exc))

        previous = {}
        job_path = os.path.join(out_dir, "job.json")
        if os.path.isfile(job_path):
            with open(job_path, "r", encoding="utf-8") as f:
                previous = json.load(f)
        bake_index = int(previous.get("bake_index", 0)) + 1
        job.update(scatter_id=scatter_id, output_dir=out_dir, bake_index=bake_index,
                   file_suffix=f"_b{bake_index:04d}")
        pending_path = os.path.join(out_dir, "job_pending.json")
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2)

        self.pending = dict(job=job, job_path=job_path, pending_path=pending_path, package=package,
                            graph=graph, previous_suffix=previous.get("file_suffix"))
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
        self.bake_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText(f"Baking {scatter_id}…")
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
        self.bake_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

        result = pending.get("result") if pending else None
        if result is None:
            if exit_status == QtCore.QProcess.CrashExit and "error" not in (pending or {}):
                self.progress.setValue(0)
                return  # cancelled
            detail = (pending or {}).get("error") or self._stderr_text.strip()[-1500:] or f"exit code {exit_code}"
            return self._fail(f"Bake failed: {detail}")

        job = pending["job"]
        error = None
        try:
            sd_bridge.apply_bake(pending["package"], pending["graph"], job["scatter_id"],
                                 result["files"], result.get("preview"))
        except (Exception, sd_bridge.APIException) as exc:
            error = exc

        # Even a half-applied bake has created the scatter: keep its settings and
        # select it, so the next bake updates it instead of making another one.
        os.replace(pending["pending_path"], pending["job_path"])
        self.refresh_scatters()
        self.scatter_combo.blockSignals(True)
        self.scatter_combo.setCurrentText(job["scatter_id"])
        self.scatter_combo.blockSignals(False)
        if error is not None:
            return self._fail(f"Baked, but updating the graph failed: {error!r}")

        self._delete_bake_files(job["output_dir"], pending.get("previous_suffix"))
        self._show_preview(result.get("preview"))
        self.progress.setValue(1000)
        sim = result.get("simulation", {})
        settled = "settled" if sim.get("settled") else "time limit reached"
        self.status.setText(
            f"{job['scatter_id']}: {result['object_count']} objects, {sim.get('sim_time', 0):.1f}s simulated "
            f"({settled}), coverage {result.get('coverage', 0) * 100:.0f}%, baked in {result.get('total_time', 0):.0f}s.")

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

    menu = ui.newMenu("Physics Scatter", MENU_ID)
    show = menu.addAction("Show panel")

    def show_panel():
        dock = dock_content.parentWidget()
        while dock is not None and not isinstance(dock, QtWidgets.QDockWidget):
            dock = dock.parentWidget()
        (dock or dock_content).show()
        (dock or dock_content).raise_()
        panel.refresh_scatters()

    show.triggered.connect(show_panel)
    return _Installed(dock_content, panel, menu)


def uninstall(installed):
    if installed is None:
        return
    installed.panel.cancel()
    ui = sd_bridge.ui_manager()
    if ui is not None:
        ui.deleteMenu(MENU_ID)
