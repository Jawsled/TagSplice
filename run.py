#!/usr/bin/env python3
"""
TagSplice runner.

Subcommands:
  gui                 Launch the GUI (creates a venv and installs PySide6
                      on first run; falls back to tkinter if Qt isn't
                      available). Primary backend is Qt (PySide6).
  cli <args...>       Forward to tagsplice.py (no Qt needed; just mutagen).
  shell               Drop into the project's venv-activated shell.

The venv lives at ./.venv/ inside the project folder. Re-running is a
no-op once it's bootstrapped.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import venv
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
GUI_DEPS = ["mutagen", "PySide6", "tkinterdnd2"]
CLI_DEPS = ["mutagen"]

# Tags always shown in the GUI grid, in this order.
GUI_TRACKED_TAGS = ["TITLE", "ARTIST", "DISPLAY ARTIST", "ALBUMARTIST"]


# ---------------------------------------------------------------------------
# venv / process bootstrap
# ---------------------------------------------------------------------------

def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_exists() -> bool:
    return venv_python().exists()


def create_venv() -> None:
    print(f"[run.py] Creating venv at {VENV_DIR} ...")
    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    builder = venv.EnvBuilder(with_pip=True, clear=False)
    builder.create(str(VENV_DIR))
    print("[run.py] venv created.")


def pip_install(pkgs: list[str]) -> None:
    py = venv_python()
    print(f"[run.py] pip install {' '.join(pkgs)}")
    subprocess.check_call(
        [str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        stdout=sys.stdout,
    )
    subprocess.check_call(
        [str(py), "-m", "pip", "install", *pkgs],
        stdout=sys.stdout,
    )


def ensure_deps(pkgs: list[str]) -> None:
    if not venv_exists():
        create_venv()
    py = venv_python()
    missing = []
    for pkg in pkgs:
        mod = pkg
        try:
            subprocess.check_output(
                [str(py), "-c", f"import {mod}"],
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError:
            missing.append(pkg)
    if missing:
        pip_install(missing)


def run_in_venv(args: list[str]) -> int:
    py = venv_python()
    return subprocess.call([str(py), *args], cwd=str(PROJECT_DIR))


def reexec_in_venv() -> None:
    """Re-launch the current interpreter inside the venv with the same args.

    Used so that PySide6 / tkinter imports happen against the venv's Python
    after `ensure_deps` has installed them.
    """
    py = venv_python()
    os.execv(str(py), [str(py), *sys.argv])


# ---------------------------------------------------------------------------
# Subcommand entry points
# ---------------------------------------------------------------------------

def cmd_gui() -> int:
    ensure_deps(GUI_DEPS)
    # Re-exec into the venv's Python so the GUI imports resolve there.
    reexec_in_venv()


def cmd_cli(args: list[str]) -> int:
    ensure_deps(CLI_DEPS)
    return run_in_venv([str(PROJECT_DIR / "tagsplice.py"), *args])


def cmd_shell() -> int:
    ensure_deps(CLI_DEPS)
    if os.name == "nt":
        activate = VENV_DIR / "Scripts" / "activate.bat"
        return subprocess.call(["cmd", "/k", str(activate)], cwd=str(PROJECT_DIR))
    activate = VENV_DIR / "bin" / "activate"
    return subprocess.call(["bash", "--rcfile", str(activate)], cwd=str(PROJECT_DIR))


def usage() -> int:
    print(__doc__)
    return 2


def main() -> int:
    if len(sys.argv) < 2:
        return usage()
    sub, *rest = sys.argv[1:]
    if sub in ("-h", "--help", "help"):
        return usage()
    if sub == "gui":
        return cmd_gui()
    if sub == "cli":
        return cmd_cli(rest)
    if sub == "shell":
        return cmd_shell()
    print(f"[run.py] unknown subcommand: {sub}\n")
    return usage()


# ===========================================================================
# GUI
# ===========================================================================

sys.path.insert(0, str(PROJECT_DIR))
import tagsplice  # noqa: E402

QT_ERROR: Exception | None = None


def _try_qt() -> bool:
    global QT_ERROR
    try:
        importlib.import_module("PySide6.QtWidgets")
        return True
    except Exception as exc:
        QT_ERROR = exc
        return False


def _try_tk() -> bool:
    for mod in ("tkinter", "tkinter.ttk", "tkinter.filedialog",
                "tkinter.scrolledtext", "tkinter.messagebox"):
        try:
            importlib.import_module(mod)
        except Exception:
            return False
    return True


def _report_no_backend() -> None:
    print(
        "TagSplice GUI could not find a usable GUI toolkit.\n"
        f"  PySide6 error: {QT_ERROR}\n"
        "  tkinter not importable.\n\n"
        "Install PySide6 (e.g. `pip install PySide6`) and re-run, or use the\n"
        "CLI: `python tagsplice.py fix [--apply] <path>`.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Qt implementation (primary)
# ---------------------------------------------------------------------------

def _qt_main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView,
        QLabel, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
        QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    )

    overrides = tagsplice.load_overrides()
    ALBUMARTIST_SEP = " ; "

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("TagSplice")
            self.resize(960, 640)
            self.setAcceptDrops(True)

            self.targets: list[Path] = []
            self.results: list[dict] = []
            self.apply_mode = True
            self.extract_feat = False
            # path -> {tags: snapshot, added: set of tags we wrote}
            self.undo_backups: dict[Path, dict] = {}

            root = QWidget(self)
            self.setCentralWidget(root)
            outer = QVBoxLayout(root)

            toolbar = QHBoxLayout()
            self.add_files_btn = QPushButton("Add files…")
            self.add_dir_btn = QPushButton("Add folder…")
            self.preview_btn = QPushButton("Preview")
            self.apply_chk = QCheckBox("Apply (uncheck for dry-run)")
            self.apply_chk.setChecked(True)
            self.feat_chk = QCheckBox("Extract feat. from TITLE")
            self.feat_chk.setChecked(False)
            self.feat_chk.setToolTip(
                "Optional, non-mainstream: move '(feat. X)'/'(ft. X)' "
                "from TITLE into ARTIST (extra values) and DISPLAY ARTIST "
                "('Primary feat. X'), and strip it from TITLE.")
            self.apply_btn = QPushButton("Apply")
            self.undo_btn = QPushButton("Undo")
            self.undo_btn.setEnabled(False)
            self.clear_btn = QPushButton("Clear")
            toolbar.addWidget(self.add_files_btn)
            toolbar.addWidget(self.add_dir_btn)
            toolbar.addWidget(self.preview_btn)
            toolbar.addSpacing(12)
            toolbar.addWidget(self.apply_chk)
            toolbar.addWidget(self.feat_chk)
            toolbar.addWidget(self.apply_btn)
            toolbar.addWidget(self.undo_btn)
            toolbar.addWidget(self.clear_btn)
            toolbar.addStretch(1)
            outer.addLayout(toolbar)

            splitter = QSplitter(Qt.Orientation.Horizontal)
            outer.addWidget(splitter, 1)

            self.file_list = QListWidget()
            self.file_list.setMinimumWidth(280)
            self.file_list.setSelectionMode(
                QListWidget.SelectionMode.ExtendedSelection)
            splitter.addWidget(self.file_list)

            right = QWidget()
            right_layout = QVBoxLayout(right)
            right_layout.setContentsMargins(0, 0, 0, 0)

            self.preview = QTableWidget(0, 3)
            self.preview.setHorizontalHeaderLabels(["Tag", "Before", "After"])
            self.preview.verticalHeader().setVisible(False)
            self.preview.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.preview.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            header = self.preview.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            right_layout.addWidget(self.preview)

            self.placeholder = QLabel("Select a file to preview its changes.")
            self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.placeholder.setStyleSheet("color: gray;")
            right_layout.addWidget(self.placeholder)
            self.preview.hide()
            splitter.addWidget(right)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 3)

            self.status = QLabel("Ready.")
            outer.addWidget(self.status)

            self.add_files_btn.clicked.connect(self.on_add_files)
            self.add_dir_btn.clicked.connect(self.on_add_dir)
            self.preview_btn.clicked.connect(self.on_preview)
            self.apply_chk.toggled.connect(lambda v: setattr(self, "apply_mode", v))
            self.feat_chk.toggled.connect(self.on_feat_toggled)
            self.apply_btn.clicked.connect(self.on_apply)
            self.undo_btn.clicked.connect(self.on_undo)
            self.clear_btn.clicked.connect(self.on_clear)
            self.file_list.currentItemChanged.connect(self.on_select)

            quit_act = QAction("Quit", self)
            quit_act.triggered.connect(self.close)
            self.addAction(quit_act)

        def on_add_files(self) -> None:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Add audio files", "",
                "Audio (*.flac *.mp3 *.ogg *.oga);;All files (*.*)")
            for p in paths:
                self.targets.append(Path(p))
            self.refresh_list()

        def on_add_dir(self) -> None:
            d = QFileDialog.getExistingDirectory(self, "Add folder")
            if not d:
                return
            self.targets.append(Path(d))
            self.refresh_list()

        def dragEnterEvent(self, event) -> None:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dragMoveEvent(self, event) -> None:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event) -> None:
            if not event.mimeData().hasUrls():
                event.ignore()
                return
            added = 0
            for url in event.mimeData().urls():
                local = url.toLocalFile()
                if not local:
                    continue
                self.targets.append(Path(local))
                added += 1
            if added:
                event.acceptProposedAction()
                self.refresh_list()
                self.status.setText(
                    f"{self.file_list.count()} file(s). "
                    f"Added {added} dropped item(s).")
            else:
                event.ignore()

        def refresh_list(self) -> None:
            files = tagsplice.collect_files(self.targets)
            self.results = [tagsplice.plan_file(
                f, overrides, ALBUMARTIST_SEP, self.extract_feat)
                            for f in files]
            self.file_list.clear()
            for r in self.results:
                name = r["path"].name
                if "error" in r:
                    label = f"{name}  (error)"
                elif not r["changes"]:
                    label = f"{name}  (no changes)"
                else:
                    label = f"{name}  ({sum(len(v) for v in r['changes'].values())} edits)"
                self.file_list.addItem(QListWidgetItem(label))
            # Default: select all rows so Apply affects everything by
            # default. Users can Ctrl/Shift-click to deselect.
            if self.file_list.count():
                self.file_list.selectAll()
                self.file_list.setCurrentRow(0)
            self.status.setText(
                f"{len(files)} file(s). "
                f"{sum(1 for r in self.results if r.get('changes'))} need changes. "
                f"{len(self.file_list.selectedIndexes())} selected."
            )
            if self.file_list.count():
                self.file_list.setCurrentRow(0)
            self._update_undo_button()

        def on_clear(self) -> None:
            self.targets.clear()
            self.results.clear()
            self.undo_backups.clear()
            self.file_list.clear()
            self._clear_preview()
            self.status.setText("Ready.")
            self._update_undo_button()

        def on_preview(self) -> None:
            self.refresh_list()

        def on_feat_toggled(self, checked: bool) -> None:
            self.extract_feat = bool(checked)
            if self.targets:
                self.refresh_list()

        def on_select(self, current: QListWidgetItem | None,
                      _prev: QListWidgetItem | None) -> None:
            if current is None:
                self._clear_preview()
                return
            idx = self.file_list.row(current)
            if 0 <= idx < len(self.results):
                self._render_diff(self.results[idx])

        def _clear_preview(self) -> None:
            self.preview.setRowCount(0)
            self.preview.hide()
            self.placeholder.show()

        def _render_diff(self, r: dict) -> None:
            from PySide6.QtWidgets import QLineEdit
            self.preview.show()
            self.placeholder.hide()
            self.preview.setRowCount(0)
            if "error" in r:
                self.preview.setRowCount(1)
                self.preview.setItem(0, 0, QTableWidgetItem("error"))
                self.preview.setItem(0, 1, QTableWidgetItem(r["error"]))
                self.preview.setItem(0, 2, QTableWidgetItem(""))
                return
            # Always render ARTIST, DISPLAY ARTIST, ALBUMARTIST — even
            # if nothing planned to change — so the user sees the full
            # picture.
            r.setdefault("changes", {})
            original = r.get("original", {})
            changes = r.get("changes", {})
            for row, tag in enumerate(GUI_TRACKED_TAGS):
                old = list(original.get(tag, []))
                new = list(changes.get(tag, old))
                self.preview.insertRow(row)
                self.preview.setItem(row, 0, QTableWidgetItem(tag))
                old_item = QTableWidgetItem(
                    "//".join(str(o) for o in old) or "(empty)")
                old_item.setFlags(old_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if old != new:
                    old_item.setForeground(Qt.GlobalColor.red)
                self.preview.setItem(row, 1, old_item)

                # Editable After cell as a QLineEdit.
                edit = QLineEdit("//".join(str(n) for n in new))
                edit.setToolTip(
                    "Edit the value. Use // to separate multiple values.")
                self.preview.setCellWidget(row, 2, edit)
                # Persist edits back into r["changes"] on every keystroke.
                edit.textChanged.connect(
                    lambda text, _r=r, _tag=tag:
                        _r["changes"].__setitem__(
                            _tag, tagsplice.parse_edit_value(text)))

            self.preview.resizeRowsToContents()

        def _update_undo_button(self) -> None:
            self.undo_btn.setEnabled(bool(self.undo_backups))

        def on_apply(self) -> None:
            if not self.results:
                self.refresh_list()
            # Apply to selected rows only. Empty selection = apply to none.
            selected_idx = {i.row() for i in self.file_list.selectedIndexes()}
            if not selected_idx and self.file_list.count():
                # No selection at all: warn via status rather than silently
                # running on everything.
                self.status.setText("Nothing selected.")
                return
            written = 0
            backups = self.undo_backups
            for idx in sorted(selected_idx):
                if idx >= len(self.results):
                    continue
                r = self.results[idx]
                if "error" in r or not r["changes"]:
                    continue
                if not self.apply_mode:
                    continue
                # Skip tags whose edited After value is empty (user
                # intentionally cleared them).
                changes_to_apply = {
                    tag: vals for tag, vals in r["changes"].items() if vals
                }
                if not changes_to_apply:
                    continue
                path = r["path"]
                if path not in backups:
                    snap = tagsplice.snapshot_tags(path)
                    if snap is None:
                        continue
                    backups[path] = {
                        "tags": snap,
                        "added": set(changes_to_apply.keys()),
                    }
                # Build a temporary result with the user's edits and write.
                write_result = dict(r)
                write_result["changes"] = changes_to_apply
                self._write_changes(path, write_result)
                written += 1
            mode = "Wrote" if self.apply_mode else "Would write"
            self.status.setText(f"{mode} {written} file(s).")
            self._update_undo_button()
            self.refresh_list()

        def _write_changes(self, path: Path, r: dict) -> None:
            audio = tagsplice.open_audio(path)
            if audio is None or audio.tags is None:
                return
            try:
                tagsplice.apply_changes(audio, r["changes"])
            except Exception:
                return

        def on_undo(self) -> None:
            if not self.undo_backups:
                return
            restored = 0
            for path, info in list(self.undo_backups.items()):
                # Snapshot current (post-apply) so we can redo later if needed.
                post = tagsplice.snapshot_tags(path) or {}
                added = info["added"]
                # Delete only the tags WE added (preserve everything else).
                delete_only_added = {
                    k for k in added
                    if not info["tags"].get(k)
                }
                # Restore originals + delete extras.
                ok = tagsplice.write_tags(
                    path, info["tags"], delete=delete_only_added)
                if ok:
                    restored += 1
            self.undo_backups.clear()
            self.status.setText(f"Undid changes to {restored} file(s).")
            self._update_undo_button()
            self.refresh_list()

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


# ---------------------------------------------------------------------------
# Tk fallback
# ---------------------------------------------------------------------------

class TkApp:
    def __init__(self, root: "tk.Tk") -> None:
        from tkinter import ttk

        self.overrides = tagsplice.load_overrides()
        self.albumartist_sep = " ; "

        self.targets: list[Path] = []
        self.results: list[dict] = []
        # path -> {tags: snapshot, added: set of tags we wrote}
        self.undo_backups: dict[Path, dict] = {}

        self.root = root
        root.title("TagSplice")
        root.geometry("960x640")

        # Optional drag-and-drop support via tkinterdnd2.
        self.dnd = None
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
            self.dnd = TkinterDnD
            self.DND_FILES = DND_FILES
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", self._on_drop)
            root.dnd_bind("<<DropEnter>>", lambda e: e.action_unhighlight())
            root.dnd_bind("<<DropLeave>>", lambda e: e.action_unhighlight())
        except Exception:
            self.dnd = None

        self.apply_var = tk.BooleanVar(root, value=True)
        self.extract_feat_var = tk.BooleanVar(root, value=False)
        self.status_var = tk.StringVar(root, value="Ready.")

        toolbar = ttk.Frame(root, padding=6)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="Add files…",
                   command=self.on_add_files).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Add folder…",
                   command=self.on_add_dir).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Preview",
                   command=self.refresh_list).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(toolbar, text="Apply (uncheck for dry-run)",
                        variable=self.apply_var).pack(side=tk.LEFT, padx=12)
        ttk.Checkbutton(toolbar, text="Extract feat. from TITLE",
                        variable=self.extract_feat_var,
                        command=self._on_feat_toggled).pack(side=tk.LEFT)
        self.apply_btn = ttk.Button(toolbar, text="Apply",
                                    command=self.on_apply)
        self.apply_btn.pack(side=tk.LEFT)
        self.undo_btn = ttk.Button(toolbar, text="Undo",
                                   command=self.on_undo, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Clear",
                   command=self.on_clear).pack(side=tk.LEFT, padx=(6, 0))

        paned = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        list_frame = ttk.Frame(paned)
        self.file_list = tk.Listbox(
            list_frame, width=40,
            selectmode=tk.EXTENDED, exportselection=False)
        self.file_list.pack(fill=tk.BOTH, expand=True)
        paned.add(list_frame, weight=1)

        right_frame = ttk.Frame(paned)
        self.placeholder = ttk.Label(
            right_frame,
            text="Select a file to preview its changes.",
            foreground="gray",
            anchor=tk.CENTER,
        )
        self.placeholder.pack(fill=tk.BOTH, expand=True)

        # Treeview as a 3-column grid (Tag / Before / After).
        columns = ("tag", "before", "after")
        self.preview = ttk.Treeview(right_frame, columns=columns,
                                    show="headings", height=10)
        self.preview.heading("tag", text="Tag")
        self.preview.heading("before", text="Before")
        self.preview.heading("after", text="After")
        self.preview.column("tag", width=140, anchor=tk.W)
        self.preview.column("before", width=320, anchor=tk.W)
        self.preview.column("after", width=320, anchor=tk.W)
        self.preview.tag_configure("no_change", foreground="gray")
        self.preview.tag_configure("removed", foreground="#a01010")
        self.preview.tag_configure("added", foreground="#0a6e1d")
        self.preview.pack(fill=tk.BOTH, expand=True)

        # Edit row: when the user clicks a Treeview row, this Entry
        # becomes editable and reflects the After value for that tag.
        edit_frame = ttk.Frame(right_frame)
        edit_frame.pack(fill=tk.X, pady=(4, 0))
        self.edit_label = ttk.Label(edit_frame, text="Edit After:",
                                    width=12, anchor=tk.W)
        self.edit_label.pack(side=tk.LEFT)
        self.edit_var = tk.StringVar()
        self.edit_entry = ttk.Entry(edit_frame, textvariable=self.edit_var,
                                    state=tk.DISABLED)
        self.edit_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.edit_var.trace_add(
            "write", lambda *_a: self._on_edit_changed())
        # (file_idx, tag) of the row currently being edited.
        self._edit_target: tuple[int, str] | None = None

        paned.add(right_frame, weight=3)

        ttk.Label(root, textvariable=self.status_var, anchor=tk.W,
                  padding=(6, 4)).pack(side=tk.BOTTOM, fill=tk.X)

        self.file_list.bind("<<ListboxSelect>>", self.on_select)
        self.preview.bind("<<TreeviewSelect>>", self.on_preview_select)
        self.refresh_list()

    def on_add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add audio files",
            filetypes=[("Audio", "*.flac *.mp3 *.ogg *.oga"),
                       ("All files", "*.*")])
        if not paths:
            return
        for p in paths:
            self.targets.append(Path(p))
        self.refresh_list()

    def on_add_dir(self) -> None:
        d = filedialog.askdirectory(title="Add folder")
        if not d:
            return
        self.targets.append(Path(d))
        self.refresh_list()

    def _on_drop(self, event) -> None:
        """Handle drag-and-drop. `event.data` is a Tk file-list string."""
        if not self.dnd:
            self.status_var.set(
                "Drag-and-drop unavailable; use Add files/folder.")
            return
        paths = self._parse_dnd_data(event.data)
        if not paths:
            return
        for p in paths:
            self.targets.append(Path(p))
        self.refresh_list()
        self.status_var.set(
            f"{self.file_list.size()} file(s). "
            f"Added {len(paths)} dropped item(s).")

    @staticmethod
    def _parse_dnd_data(data: str) -> list[str]:
        """Parse a tkinterdnd2 file-list string into individual paths.

        Path components with spaces are wrapped in {…}; braces and
        trailing backslashes are escaped per the dnd spec.
        """
        if not data:
            return []
        paths: list[str] = []
        i, n = 0, len(data)
        while i < n:
            if data[i] == "{":
                end = data.find("}", i + 1)
                if end == -1:
                    paths.append(data[i + 1:])
                    break
                paths.append(data[i + 1:end])
                i = end + 1
            else:
                # Take until the next space that's not part of an
                # escape sequence.
                j = i
                while j < n:
                    if data[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if data[j] == " ":
                        break
                    j += 1
                paths.append(data[i:j])
                i = j
            # Skip separator spaces between paths.
            while i < n and data[i] == " ":
                i += 1
        return [p for p in paths if p]

    def refresh_list(self) -> None:
        files = tagsplice.collect_files(self.targets)
        self.results = [tagsplice.plan_file(f, self.overrides,
                                            self.albumartist_sep,
                                            self.extract_feat_var.get())
                        for f in files]
        self.file_list.delete(0, tk.END)
        for r in self.results:
            name = r["path"].name
            if "error" in r:
                label = f"{name}  (error)"
            elif not r["changes"]:
                label = f"{name}  (no changes)"
            else:
                label = (f"{name}  "
                         f"({sum(len(v) for v in r['changes'].values())} edits)")
            self.file_list.insert(tk.END, label)
        # Default: select all rows so Apply affects everything. User
        # can Ctrl/Shift-click to deselect.
        if self.file_list.size():
            self.file_list.selection_set(0, tk.END)
        self._refresh_status()
        if self.file_list.size():
            self.show_preview(0)
        self._update_undo_button()
        # Reset the edit entry — its target row may no longer exist.
        self._set_edit_target(None, None, "")
        self._edit_target = None

    def _on_feat_toggled(self) -> None:
        if self.targets:
            self.refresh_list()

    def _refresh_status(self) -> None:
        sel_count = len(self.file_list.curselection())
        if not self.file_list.curselection() and self.file_list.size():
            sel_count = self.file_list.size()
        self.status_var.set(
            f"{self.file_list.size()} file(s). "
            f"{sum(1 for r in self.results if r.get('changes'))} need changes. "
            f"{sel_count} selected."
        )

    def show_preview(self, idx: int) -> None:
        # Wipe existing rows.
        for iid in self.preview.get_children():
            self.preview.delete(iid)
        if not self.results:
            self.placeholder.pack(fill=tk.BOTH, expand=True)
            self.preview.pack_forget()
            return
        r = self.results[idx]
        self.placeholder.pack_forget()
        self.preview.pack(fill=tk.BOTH, expand=True)
        if "error" in r:
            self.preview.insert("", tk.END,
                                values=("error", r["error"], ""))
            self._set_edit_target(None, None, "")
            return
        # Always render ARTIST, DISPLAY ARTIST, ALBUMARTIST — even
        # if nothing planned to change — so the user sees the full
        # picture.
        r.setdefault("changes", {})
        original = r.get("original", {})
        changes = r.get("changes", {})
        for tag in GUI_TRACKED_TAGS:
            old = list(original.get(tag, []))
            new = list(changes.get(tag, old))
            old_text = "//".join(str(o) for o in old) or "(empty)"
            new_text = "//".join(str(n) for n in new) or "(empty)"
            tag_names = ("removed", "added") if old != new else ("no_change",)
            self.preview.insert(
                "", tk.END,
                iid=self._iid(idx, tag),
                values=(tag, old_text, new_text),
                tags=tag_names)
        # Reset edit panel
        self._set_edit_target(None, None, "")

    def _iid(self, file_idx: int, tag: str) -> str:
        return f"f{file_idx}|t{tag}"

    def on_select(self, _evt) -> None:
        sel = self.file_list.curselection()
        if sel:
            # If multiple files are selected, render the first one but
            # don't update the edit target automatically.
            self.show_preview(sel[0])
        else:
            for iid in self.preview.get_children():
                self.preview.delete(iid)
            self.preview.pack_forget()
            self.placeholder.pack(fill=tk.BOTH, expand=True)
        self._refresh_status()

    def on_preview_select(self, _evt) -> None:
        sel = self.preview.selection()
        if not sel:
            self._set_edit_target(None, None, "")
            return
        iid = sel[0]
        # Parse iid -> (file_idx, tag)
        if "|" not in iid:
            return
        f_part, t_part = iid.split("|", 1)
        if not f_part.startswith("f") or not t_part.startswith("t"):
            return
        try:
            file_idx = int(f_part[1:])
            tag = t_part[1:]
        except ValueError:
            return
        r = self.results[file_idx]
        if tag not in r.get("changes", {}):
            return
        current = r["changes"][tag]
        text = "//".join(str(v) for v in current)
        self._set_edit_target(file_idx, tag, text)
        self.edit_entry.configure(state=tk.NORMAL)

    def _set_edit_target(self, file_idx: int | None, tag: str | None,
                         text: str) -> None:
        self._edit_target = (file_idx, tag) if (file_idx is not None
                                                and tag is not None) else None
        # Block trace by saving current and updating directly
        # (StringVar trace fires on every assignment).
        try:
            self.edit_var.trace_remove(
                "write", self._edit_trace_id)
        except (AttributeError, tk.TclError):
            pass
        self.edit_var.set(text)
        try:
            self._edit_trace_id = self.edit_var.trace_add(
                "write", lambda *_a: self._on_edit_changed())
        except tk.TclError:
            pass

    def _on_edit_changed(self) -> None:
        if not self._edit_target:
            return
        file_idx, tag = self._edit_target
        r = self.results[file_idx]
        new_vals = tagsplice.parse_edit_value(self.edit_var.get())
        r["changes"][tag] = new_vals
        # Also update the Treeview row to reflect the edited value.
        iid = self._iid(file_idx, tag)
        if self.preview.exists(iid):
            self.preview.item(iid, values=(
                tag, self.preview.item(iid)["values"][1],
                "//".join(new_vals) or "(empty)",
            ))

    def _update_undo_button(self) -> None:
        if self.undo_backups:
            self.undo_btn.configure(state=tk.NORMAL)
        else:
            self.undo_btn.configure(state=tk.DISABLED)

    def on_apply(self) -> None:
        if not self.results:
            self.refresh_list()
        # Selection-aware: empty selection = apply to none.
        sel = self.file_list.curselection()
        if not sel and self.file_list.size():
            self.status_var.set("Nothing selected.")
            return
        written = 0
        backups = self.undo_backups
        for idx in sel:
            if idx >= len(self.results):
                continue
            r = self.results[idx]
            if "error" in r or not r["changes"]:
                continue
            if not self.apply_var.get():
                continue
            # Skip tags with empty edited values (user cleared them).
            changes_to_apply = {
                tag: vals for tag, vals in r["changes"].items() if vals
            }
            if not changes_to_apply:
                continue
            path = r["path"]
            if path not in backups:
                snap = tagsplice.snapshot_tags(path)
                if snap is None:
                    continue
                backups[path] = {
                    "tags": snap,
                    "added": set(changes_to_apply.keys()),
                }
            audio = tagsplice.open_audio(path)
            if audio is None or audio.tags is None:
                continue
            try:
                tagsplice.apply_changes(audio, changes_to_apply)
            except Exception:
                continue
            written += 1
        mode = "Wrote" if self.apply_var.get() else "Would write"
        self.status_var.set(f"{mode} {written} file(s).")
        self._update_undo_button()
        self.refresh_list()

    def on_undo(self) -> None:
        if not self.undo_backups:
            return
        restored = 0
        for path, info in list(self.undo_backups.items()):
            delete_only_added = {
                k for k in info["added"]
                if not info["tags"].get(k)
            }
            if tagsplice.write_tags(path, info["tags"],
                                    delete=delete_only_added):
                restored += 1
        self.undo_backups.clear()
        self.status_var.set(f"Undid changes to {restored} file(s).")
        self._update_undo_button()
        self.refresh_list()

    def on_clear(self) -> None:
        self.targets.clear()
        self.results.clear()
        self.undo_backups.clear()
        self.file_list.delete(0, tk.END)
        for iid in self.preview.get_children():
            self.preview.delete(iid)
        self.preview.pack_forget()
        self.placeholder.pack(fill=tk.BOTH, expand=True)
        self._set_edit_target(None, None, "")
        self.status_var.set("Ready.")
        self._update_undo_button()


def _tk_main() -> int:
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    TkApp(root)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------

def gui_main() -> int:
    if _try_qt():
        return _qt_main()
    if _try_tk():
        print("[run.py] PySide6 unavailable; using tkinter fallback.")
        return _tk_main()
    _report_no_backend()
    return 1


if __name__ == "__main__":
    # If we're inside the venv (after re-exec), run the GUI.
    # Otherwise, dispatch the subcommand normally.
    if VENV_DIR.exists() and Path(sys.executable).resolve() == venv_python().resolve():
        sys.exit(gui_main())
    sys.exit(main())
