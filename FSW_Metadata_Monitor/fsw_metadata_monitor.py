import csv
import traceback
import hashlib
import os
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_NAME = "FSW Metadata Monitor"
POLL_SECONDS = 3
STABLE_SECONDS = 5
IGNORED_INBOX_FILES = {"PUT_NEW_FSW_FILES_HERE.txt"}

FILE_TYPES = [
    "force_raw",
    "ir_temperature_raw",
    "optical_image",
    "defect_measurement",
    "process_log",
    "other",
    "unrelated",
]

OPTIONAL_STATES = ["unknown", "same_as_usual", "changed"]


@dataclass
class PendingFile:
    path: Path
    size: int
    first_seen: float
    last_seen: float


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def relative_to_root(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return ""


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def local_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def generated_sample_id(index: int) -> str:
    return f"FSW_{datetime.now().strftime('%Y%m%d')}_S{index + 1:03d}"


def safe_text(value: str) -> str:
    value = (value or "").strip()
    keep = []
    for char in value:
        if char.isalnum() or char in ("-", "_", "."):
            keep.append(char)
        elif char.isspace():
            keep.append("_")
    result = "".join(keep).strip("_")
    return result or "unknown"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem}_{index:03d}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique path for {path}")


def infer_file_type(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"]:
        return "optical_image"
    if "trend" in name or "thermal" in name or "temp" in name or "ir" in name:
        return "ir_temperature_raw"
    if "force" in name or suffix in [".xls", ".xlsx", ".tdms"]:
        return "force_raw"
    if "defect" in name or "area" in name:
        return "defect_measurement"
    if suffix in [".pdf", ".doc", ".docx"]:
        return "unrelated"
    return "other"


class MetadataStore:
    def __init__(self, library_dir: Path, root_dir: Path | None = None):
        self.root_dir = root_dir or library_dir.parent
        self.library_dir = library_dir
        self.db_path = library_dir / "metadata.sqlite"
        self.runs_csv = library_dir / "runs.csv"
        self.files_csv = library_dir / "files.csv"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                sample_id TEXT NOT NULL,
                rotation_speed_rpm REAL NOT NULL,
                travel_speed_mm_min REAL NOT NULL,
                tool_id TEXT NOT NULL,
                material TEXT NOT NULL,
                thickness_mm REAL NOT NULL,
                operator TEXT,
                created_time TEXT NOT NULL,
                clamping_condition TEXT,
                backing_condition TEXT,
                cooling_condition TEXT,
                tool_wear_state TEXT,
                surface_condition TEXT,
                optional_notes TEXT
            );

            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                new_filename TEXT NOT NULL,
                original_path TEXT NOT NULL,
                original_relative_path TEXT,
                new_path TEXT NOT NULL,
                new_relative_path TEXT,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                registered_time TEXT NOT NULL,
                status TEXT NOT NULL,
                file_notes TEXT,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            """
        )
        self._ensure_column("files", "original_relative_path", "TEXT")
        self._ensure_column("files", "new_relative_path", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table_name: str, column_name: str, column_type: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table_name})")
        }
        if column_name not in columns:
            self.conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs ORDER BY created_time DESC LIMIT ?", (limit,)
            )
        )

    def insert_run(self, data: dict) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO runs (
                run_id, sample_id, rotation_speed_rpm, travel_speed_mm_min,
                tool_id, material, thickness_mm, operator, created_time,
                clamping_condition, backing_condition, cooling_condition,
                tool_wear_state, surface_condition, optional_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["run_id"],
                data["sample_id"],
                data["rotation_speed_rpm"],
                data["travel_speed_mm_min"],
                data["tool_id"],
                data["material"],
                data["thickness_mm"],
                data.get("operator", ""),
                data["created_time"],
                data.get("clamping_condition", "unknown"),
                data.get("backing_condition", "unknown"),
                data.get("cooling_condition", "unknown"),
                data.get("tool_wear_state", "unknown"),
                data.get("surface_condition", "unknown"),
                data.get("optional_notes", ""),
            ),
        )
        self.conn.commit()
        self.export_csvs()

    def insert_file(self, data: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO files (
                file_id, run_id, original_filename, new_filename,
                original_path, original_relative_path, new_path, new_relative_path,
                file_type, file_size, sha256, registered_time, status, file_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["file_id"],
                data["run_id"],
                data["original_filename"],
                data["new_filename"],
                data["original_path"],
                data.get("original_relative_path", ""),
                data["new_path"],
                data.get("new_relative_path", ""),
                data["file_type"],
                data["file_size"],
                data["sha256"],
                data["registered_time"],
                data["status"],
                data.get("file_notes", ""),
            ),
        )
        self.conn.commit()
        self.export_csvs()

    def library_entries(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT
                    f.file_id,
                    f.run_id,
                    r.sample_id,
                    r.rotation_speed_rpm,
                    r.travel_speed_mm_min,
                    r.tool_id,
                    r.material,
                    r.thickness_mm,
                    r.operator,
                    r.created_time,
                    r.clamping_condition,
                    r.backing_condition,
                    r.cooling_condition,
                    r.tool_wear_state,
                    r.surface_condition,
                    r.optional_notes,
                    f.original_filename,
                    f.new_filename,
                    f.original_path,
                    f.original_relative_path,
                    f.new_path,
                    f.new_relative_path,
                    f.file_type,
                    f.file_size,
                    f.sha256,
                    f.registered_time,
                    f.status,
                    f.file_notes
                FROM files f
                LEFT JOIN runs r ON f.run_id = r.run_id
                ORDER BY r.sample_id, f.file_type, f.registered_time
                """
            )
        )

    def resolve_library_path(self, row: sqlite3.Row) -> Path:
        relative_path = row["new_relative_path"] if "new_relative_path" in row.keys() else ""
        if relative_path:
            return self.root_dir / relative_path
        return Path(row["new_path"])

    def export_csvs(self) -> None:
        self._export_table("runs", self.runs_csv)
        self._export_table("files", self.files_csv)

    def _export_table(self, table_name: str, csv_path: Path) -> None:
        rows = list(self.conn.execute(f"SELECT * FROM {table_name}"))
        if not rows:
            return
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow([row[key] for key in row.keys()])


class RegisterDialog(tk.Toplevel):
    def __init__(self, master, store: MetadataStore, files: list[Path]):
        super().__init__(master)
        self.store = store
        self.files = files
        self.result = None
        self.title(f"{APP_NAME} - register files")
        self.geometry("1040x700")
        self.minsize(940, 620)
        self.transient(master)
        self.grab_set()

        self.current_file: Path | None = None
        self.saved: dict[Path, dict] = {}
        self._changing_selection = False

        self.file_type = tk.StringVar()
        self.auto_sample_id = tk.BooleanVar(value=True)
        self.sample_id = tk.StringVar()
        self.rotation_speed = tk.StringVar()
        self.travel_speed = tk.StringVar()
        self.tool_id = tk.StringVar()
        self.material = tk.StringVar()
        self.thickness = tk.StringVar()
        self.operator = tk.StringVar(value=os.environ.get("USERNAME", ""))

        self.clamping = tk.StringVar(value="unknown")
        self.backing = tk.StringVar(value="unknown")
        self.cooling = tk.StringVar(value="unknown")
        self.tool_wear = tk.StringVar(value="unknown")
        self.surface = tk.StringVar(value="unknown")

        self._build()
        if self.files:
            self._select_file(self.files[0])
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        title = ttk.Label(
            root,
            text=f"{len(self.files)} new file(s) detected. Register metadata before archiving.",
            font=("Segoe UI", 12, "bold"),
        )
        title.pack(anchor="w")

        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(12, 8))

        files_frame = ttk.LabelFrame(body, text="Detected files")
        body.add(files_frame, weight=1)
        files_frame.rowconfigure(0, weight=1)
        files_frame.columnconfigure(0, weight=1)

        self.file_tree = ttk.Treeview(
            files_frame,
            columns=("status", "type"),
            show="tree headings",
            selectmode="browse",
            height=18,
        )
        self.file_tree.heading("#0", text="Filename")
        self.file_tree.heading("status", text="Status")
        self.file_tree.heading("type", text="Type")
        self.file_tree.column("#0", width=280, minwidth=200)
        self.file_tree.column("status", width=95, minwidth=80, anchor="center")
        self.file_tree.column("type", width=130, minwidth=100)
        self.file_tree.tag_configure("missing", foreground="#a00000")
        self.file_tree.tag_configure("complete", foreground="#008000")
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(files_frame, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.file_tree.bind("<<TreeviewSelect>>", self._on_file_selected)

        for index, file_path in enumerate(self.files):
            self.file_tree.insert(
                "",
                "end",
                iid=str(index),
                text=file_path.name,
                values=("missing", infer_file_type(file_path)),
                tags=("missing",),
            )

        editor = ttk.LabelFrame(body, text="File metadata")
        body.add(editor, weight=2)
        editor.columnconfigure(0, weight=0)
        editor.columnconfigure(1, weight=1)
        editor.columnconfigure(2, weight=0)

        self.selected_label = ttk.Label(editor, text="No file selected", font=("Segoe UI", 10, "bold"))
        self.selected_label.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 10))

        ttk.Label(editor, text="File type").grid(row=1, column=0, sticky="w", padx=8, pady=5)
        ttk.Combobox(editor, textvariable=self.file_type, values=FILE_TYPES, state="readonly").grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=5
        )

        sample_row = ttk.Frame(editor)
        sample_row.columnconfigure(0, weight=1)
        self.sample_entry = ttk.Entry(sample_row, textvariable=self.sample_id)
        self.sample_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(
            sample_row,
            text="Auto",
            variable=self.auto_sample_id,
            command=self._toggle_auto_sample_id,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(sample_row, text="Generate", command=self._generate_sample_for_current).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        fields = [
            ("Sample ID", sample_row),
            ("Rotation speed (rpm)", self.rotation_speed),
            ("Travel speed (mm/min)", self.travel_speed),
            ("Tool ID", self.tool_id),
            ("Material", self.material),
            ("Thickness (mm)", self.thickness),
            ("Operator", self.operator),
        ]
        for index, (label, variable) in enumerate(fields, start=2):
            ttk.Label(editor, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=5)
            if isinstance(variable, ttk.Frame):
                variable.grid(row=index, column=1, columnspan=2, sticky="ew", padx=8, pady=5)
            else:
                ttk.Entry(editor, textvariable=variable).grid(
                    row=index, column=1, columnspan=2, sticky="ew", padx=8, pady=5
                )

        opt = ttk.LabelFrame(editor, text="Recommended notes")
        opt.grid(row=9, column=0, columnspan=3, sticky="ew", padx=8, pady=(12, 8))
        for col in range(5):
            opt.columnconfigure(col, weight=1)
        option_fields = [
            ("Clamping", self.clamping),
            ("Backing/anvil", self.backing),
            ("Cooling", self.cooling),
            ("Tool wear", self.tool_wear),
            ("Surface", self.surface),
        ]
        for col, (label, variable) in enumerate(option_fields):
            ttk.Label(opt, text=label).grid(row=0, column=col, sticky="w", padx=8, pady=4)
            ttk.Combobox(opt, textvariable=variable, values=OPTIONAL_STATES, state="readonly").grid(
                row=1, column=col, sticky="ew", padx=8, pady=(0, 8)
            )

        ttk.Label(editor, text="Anything unusual for this file/sample").grid(
            row=10, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 2)
        )
        self.notes = tk.Text(editor, height=5, wrap="word")
        self.notes.grid(row=11, column=0, columnspan=3, sticky="nsew", padx=8, pady=(0, 8))
        editor.rowconfigure(11, weight=1)

        editor_buttons = ttk.Frame(editor)
        editor_buttons.grid(row=12, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        ttk.Button(editor_buttons, text="Copy from previous completed file", command=self._copy_previous).pack(
            side="left"
        )
        ttk.Button(editor_buttons, text="Save metadata for this file", command=self._save_current).pack(
            side="right"
        )

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Register completed files", command=self._submit).pack(side="right")

    def _on_file_selected(self, _event=None) -> None:
        if self._changing_selection:
            return
        selection = self.file_tree.selection()
        if not selection:
            return
        self._select_file(self.files[int(selection[0])])

    def _select_file(self, file_path: Path) -> None:
        if file_path == self.current_file:
            return
        if self.current_file is not None and self.current_file not in self.saved:
            self._capture_current(draft=True)
        self.current_file = file_path
        index = self.files.index(file_path)
        self._changing_selection = True
        try:
            self.file_tree.selection_set(str(index))
            self.file_tree.focus(str(index))
        finally:
            self._changing_selection = False
        self.selected_label.configure(text=file_path.name)

        data = self.saved.get(file_path) or getattr(self, "_drafts", {}).get(file_path)
        if data is None:
            data = self._default_metadata(file_path, index)
        self._load_form(data)

    def _default_metadata(self, file_path: Path, index: int) -> dict:
        sample_id = generated_sample_id(index)
        return {
            "file_type": infer_file_type(file_path),
            "auto_sample_id": True,
            "sample_id": sample_id,
            "run_id": sample_id,
            "rotation_speed_rpm": "",
            "travel_speed_mm_min": "",
            "tool_id": "",
            "material": "",
            "thickness_mm": "",
            "operator": os.environ.get("USERNAME", ""),
            "clamping_condition": "unknown",
            "backing_condition": "unknown",
            "cooling_condition": "unknown",
            "tool_wear_state": "unknown",
            "surface_condition": "unknown",
            "optional_notes": "",
            "file_notes": "",
            "existing": False,
        }

    def _load_form(self, data: dict) -> None:
        self.file_type.set(data.get("file_type", "other"))
        self.auto_sample_id.set(bool(data.get("auto_sample_id", True)))
        self.sample_id.set(str(data.get("sample_id", "")))
        self.rotation_speed.set(str(data.get("rotation_speed_rpm", "")))
        self.travel_speed.set(str(data.get("travel_speed_mm_min", "")))
        self.tool_id.set(str(data.get("tool_id", "")))
        self.material.set(str(data.get("material", "")))
        self.thickness.set(str(data.get("thickness_mm", "")))
        self.operator.set(str(data.get("operator", "")))
        self.clamping.set(data.get("clamping_condition", "unknown"))
        self.backing.set(data.get("backing_condition", "unknown"))
        self.cooling.set(data.get("cooling_condition", "unknown"))
        self.tool_wear.set(data.get("tool_wear_state", "unknown"))
        self.surface.set(data.get("surface_condition", "unknown"))
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", data.get("optional_notes", ""))
        self._toggle_auto_sample_id()

    def _capture_current(self, draft: bool = False) -> dict | None:
        if self.current_file is None:
            return None
        sample_id = self.sample_id.get().strip()
        data = {
            "file_type": self.file_type.get().strip(),
            "auto_sample_id": self.auto_sample_id.get(),
            "sample_id": sample_id,
            "run_id": sample_id,
            "rotation_speed_rpm": self.rotation_speed.get().strip(),
            "travel_speed_mm_min": self.travel_speed.get().strip(),
            "tool_id": self.tool_id.get().strip(),
            "material": self.material.get().strip(),
            "thickness_mm": self.thickness.get().strip(),
            "operator": self.operator.get().strip(),
            "created_time": utc_now(),
            "clamping_condition": self.clamping.get(),
            "backing_condition": self.backing.get(),
            "cooling_condition": self.cooling.get(),
            "tool_wear_state": self.tool_wear.get(),
            "surface_condition": self.surface.get(),
            "optional_notes": self.notes.get("1.0", "end").strip(),
            "file_notes": self.notes.get("1.0", "end").strip(),
            "existing": False,
        }
        if draft:
            if not hasattr(self, "_drafts"):
                self._drafts = {}
            self._drafts[self.current_file] = data
        return data

    def _toggle_auto_sample_id(self) -> None:
        if self.auto_sample_id.get() and self.current_file is not None:
            index = self.files.index(self.current_file)
            if not self.sample_id.get().strip():
                self.sample_id.set(generated_sample_id(index))
        self.sample_entry.configure(state="disabled" if self.auto_sample_id.get() else "normal")

    def _generate_sample_for_current(self) -> None:
        if self.current_file is None:
            return
        index = self.files.index(self.current_file)
        self.sample_id.set(generated_sample_id(index))

    def _validate_metadata(self, data: dict) -> dict | None:
        values = {
            "sample_id": data.get("sample_id", ""),
            "rotation_speed_rpm": data.get("rotation_speed_rpm", ""),
            "travel_speed_mm_min": data.get("travel_speed_mm_min", ""),
            "tool_id": data.get("tool_id", ""),
            "material": data.get("material", ""),
            "thickness_mm": data.get("thickness_mm", ""),
            "file_type": data.get("file_type", ""),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            messagebox.showerror(APP_NAME, "Missing required fields: " + ", ".join(missing))
            return None
        try:
            data["rotation_speed_rpm"] = float(values["rotation_speed_rpm"])
            data["travel_speed_mm_min"] = float(values["travel_speed_mm_min"])
            data["thickness_mm"] = float(values["thickness_mm"])
        except ValueError:
            messagebox.showerror(APP_NAME, "Speed and thickness fields must be numbers.")
            return None
        return data

    def _save_current(self) -> None:
        data = self._capture_current()
        if data is None:
            return
        valid = self._validate_metadata(data)
        if valid is None:
            return
        self.saved[self.current_file] = valid
        if hasattr(self, "_drafts"):
            self._drafts.pop(self.current_file, None)
        index = self.files.index(self.current_file)
        self.file_tree.item(
            str(index),
            values=("complete", valid["file_type"]),
            tags=("complete",),
        )
        self._select_next_missing()

    def _select_next_missing(self) -> None:
        for file_path in self.files:
            if file_path not in self.saved:
                self._select_file(file_path)
                return

    def _copy_previous(self) -> None:
        if not self.saved:
            messagebox.showinfo(APP_NAME, "No completed file metadata to copy yet.")
            return
        last_file = list(self.saved.keys())[-1]
        data = dict(self.saved[last_file])
        if self.current_file is not None:
            data["file_type"] = infer_file_type(self.current_file)
            data["auto_sample_id"] = False
            data["run_id"] = data.get("sample_id", "")
        self._load_form(data)

    def _submit(self) -> None:
        if self.current_file is not None and self.current_file not in self.saved:
            self._save_current()
        missing = [path.name for path in self.files if path not in self.saved]
        if missing:
            messagebox.showerror(
                APP_NAME,
                "Metadata is still missing for:\n" + "\n".join(missing[:20]),
            )
            return
        file_data = []
        for file_path in self.files:
            metadata = self.saved[file_path]
            file_data.append(
                {
                    "path": file_path,
                    "metadata": metadata,
                    "file_type": metadata["file_type"],
                    "file_notes": metadata.get("file_notes", ""),
                }
            )
        self.result = {"files": file_data}
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x620")
        self.minsize(960, 540)
        self.root_dir = app_root()
        self.inbox_dir = self.root_dir / "FSW_Data_Inbox"
        self.library_dir = self.root_dir / "FSW_Data_Library"
        self.inbox_dir.mkdir(exist_ok=True)
        self.library_dir.mkdir(exist_ok=True)
        self.store = MetadataStore(self.library_dir, self.root_dir)
        self.pending: dict[Path, PendingFile] = {}
        self.processing = False
        self._library_split_initialized = False
        self.library_rows: dict[str, sqlite3.Row] = {}
        self._build()
        self.refresh_library_tree()
        self.after(1000, self.scan)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            root,
            text="Register new files from the inbox, then browse the registered library on the right.",
        ).pack(anchor="w", pady=(8, 12))

        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)

        monitor = ttk.LabelFrame(body, text="Register")
        body.add(monitor, weight=1)

        info = ttk.Frame(monitor, padding=10)
        info.pack(fill="x")
        info.columnconfigure(1, weight=1)
        ttk.Label(info, text="Inbox").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Label(info, text=str(self.inbox_dir), wraplength=360).grid(
            row=0, column=1, sticky="w", pady=3
        )
        ttk.Label(info, text="Library").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Label(info, text=str(self.library_dir), wraplength=360).grid(
            row=1, column=1, sticky="w", pady=3
        )

        ttk.Label(
            monitor,
            text="Soft rule: treat registered Library files as read-only. If a file changes, re-register it as a new version.",
            wraplength=430,
        ).pack(anchor="w", padx=10, pady=(4, 10))

        self.status = tk.StringVar(value="Watching for new files...")
        ttk.Label(monitor, textvariable=self.status).pack(anchor="w", padx=10, pady=(8, 4))
        ttk.Button(monitor, text="Scan now", command=self.scan).pack(anchor="e", padx=10)

        browser = ttk.LabelFrame(body, text="Browse Library")
        body.add(browser, weight=2)
        browser.rowconfigure(1, weight=1)
        browser.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(browser, padding=(8, 8, 8, 4))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(toolbar, text="Refresh", command=self.refresh_library_tree).pack(side="left")
        ttk.Button(toolbar, text="Open file", command=self.open_selected_library_file).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="Open folder", command=self.open_selected_library_folder).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="Copy path", command=self.copy_selected_library_path).pack(
            side="left", padx=(8, 0)
        )

        self.library_pane = tk.PanedWindow(
            browser,
            orient="horizontal",
            sashwidth=14,
            sashpad=4,
            sashrelief="raised",
            showhandle=True,
            handlesize=18,
            handlepad=8,
            bd=0,
        )
        self.library_pane.grid(row=1, column=0, sticky="nsew")

        tree_frame = ttk.Frame(self.library_pane, padding=(8, 0, 4, 8))
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.library_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        self.library_tree.grid(row=0, column=0, sticky="nsew")
        library_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.library_tree.yview)
        self.library_tree.configure(yscrollcommand=library_scroll.set)
        library_scroll.grid(row=0, column=1, sticky="ns")
        self.library_tree.bind("<<TreeviewSelect>>", self.on_library_select)

        details_frame = ttk.Frame(self.library_pane, padding=(4, 0, 8, 8))
        details_frame.rowconfigure(1, weight=1)
        details_frame.columnconfigure(0, weight=1)
        ttk.Label(
            details_frame,
            text="Metadata of the selected file",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.library_details = tk.Text(details_frame, height=18, wrap="word", state="disabled")
        self.library_details.grid(row=1, column=0, sticky="nsew")

        self.library_pane.add(tree_frame, minsize=360)
        self.library_pane.add(details_frame, minsize=300)
        self.after_idle(self._set_default_library_split)

    def _set_default_library_split(self) -> None:
        if self._library_split_initialized:
            return
        width = self.library_pane.winfo_width()
        if width > 100:
            self.library_pane.sash_place(0, int(width * 0.55), 0)
            self._library_split_initialized = True
        else:
            self.after(200, self._set_default_library_split)

    def scan(self) -> None:
        if self.processing:
            self.after(POLL_SECONDS * 1000, self.scan)
            return
        now = time.time()
        current_files = []
        for path in self.inbox_dir.iterdir():
            if (
                path.is_file()
                and not path.name.startswith(".")
                and path.name not in IGNORED_INBOX_FILES
            ):
                current_files.append(path)
                size = path.stat().st_size
                pending = self.pending.get(path)
                if pending is None:
                    self.pending[path] = PendingFile(path, size, now, now)
                elif pending.size != size:
                    pending.size = size
                    pending.last_seen = now

        current_set = set(current_files)
        for path in list(self.pending):
            if path not in current_set:
                self.pending.pop(path, None)

        ready = [
            pending.path
            for pending in self.pending.values()
            if now - pending.last_seen >= STABLE_SECONDS
        ]
        if ready:
            self.processing = True
            self.status.set(f"Registering {len(ready)} file(s)...")
            self._register_files(sorted(ready, key=lambda p: p.name.lower()))
            self.processing = False
        else:
            self.status.set(f"Watching for new files... pending: {len(self.pending)}")
        self.after(POLL_SECONDS * 1000, self.scan)

    def _register_files(self, files: list[Path]) -> None:
        dialog = RegisterDialog(self, self.store, files)
        self.wait_window(dialog)
        if dialog.result is None:
            self.status.set("Registration cancelled. Files remain in inbox.")
            return

        archived_dirs = set()
        for item in dialog.result["files"]:
            source = item["path"]
            if not source.exists():
                continue
            run_data = item["metadata"]
            self.store.insert_run(run_data)
            run_id = run_data["run_id"]
            archive_dir = self.library_dir / "runs" / safe_text(run_id)
            archive_dir.mkdir(parents=True, exist_ok=True)
            archived_dirs.add(str(archive_dir))
            file_type = item["file_type"]
            role_dir = archive_dir / safe_text(file_type)
            role_dir.mkdir(parents=True, exist_ok=True)
            new_name = self._build_filename(source, run_data, run_id, file_type)
            target = ensure_unique_path(role_dir / new_name)
            checksum = sha256_file(source)
            size = source.stat().st_size
            shutil.move(str(source), str(target))
            self.store.insert_file(
                {
                    "file_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "original_filename": source.name,
                    "new_filename": target.name,
                    "original_path": str(source),
                    "original_relative_path": relative_to_root(self.root_dir, source),
                    "new_path": str(target),
                    "new_relative_path": relative_to_root(self.root_dir, target),
                    "file_type": file_type,
                    "file_size": size,
                    "sha256": checksum,
                    "registered_time": utc_now(),
                    "status": "registered",
                    "file_notes": item.get("file_notes", ""),
                }
            )
            self.pending.pop(source, None)

        self.status.set(
            f"Registered {len(dialog.result['files'])} file(s) to {len(archived_dirs)} run folder(s)"
        )
        self.refresh_library_tree()

    def refresh_library_tree(self) -> None:
        self.library_tree.delete(*self.library_tree.get_children())
        self.library_rows.clear()
        sample_nodes: dict[str, str] = {}
        type_nodes: dict[tuple[str, str], str] = {}

        for row in self.store.library_entries():
            sample_id = row["sample_id"] or row["run_id"] or "unknown_sample"
            file_type = row["file_type"] or "unknown_type"
            sample_iid = f"sample::{sample_id}"
            type_iid = f"type::{sample_id}::{file_type}"
            file_iid = f"file::{row['file_id']}"

            if sample_id not in sample_nodes:
                label = (
                    f"{sample_id}  |  {row['rotation_speed_rpm']} rpm x "
                    f"{row['travel_speed_mm_min']} mm/min"
                )
                self.library_tree.insert("", "end", iid=sample_iid, text=label, open=True)
                sample_nodes[sample_id] = sample_iid

            type_key = (sample_id, file_type)
            if type_key not in type_nodes:
                self.library_tree.insert(sample_iid, "end", iid=type_iid, text=file_type, open=True)
                type_nodes[type_key] = type_iid

            path = self.store.resolve_library_path(row)
            marker = "" if path.exists() else " [missing]"
            self.library_tree.insert(
                type_iid,
                "end",
                iid=file_iid,
                text=f"{row['new_filename']}{marker}",
            )
            self.library_rows[file_iid] = row

        if not self.library_rows:
            self._set_library_details("No registered files yet.")
        elif not self.library_tree.selection():
            self._set_library_details("Select a file to view its metadata.")

    def on_library_select(self, _event=None) -> None:
        row = self._selected_library_row()
        if row is None:
            self._set_library_details("Select a registered file to view its metadata.")
            return
        path = self.store.resolve_library_path(row)
        status = "available" if path.exists() else "missing"
        details = [
            f"Sample ID: {row['sample_id']}",
            f"File type: {row['file_type']}",
            f"Status: {status}",
            "",
            f"Current filename: {row['new_filename']}",
            f"Original filename: {row['original_filename']}",
            f"Relative path: {row['new_relative_path'] or relative_to_root(self.root_dir, path)}",
            f"Absolute path: {path}",
            "",
            f"Rotation speed: {row['rotation_speed_rpm']} rpm",
            f"Travel speed: {row['travel_speed_mm_min']} mm/min",
            f"Tool ID: {row['tool_id']}",
            f"Material: {row['material']}",
            f"Thickness: {row['thickness_mm']} mm",
            f"Operator: {row['operator'] or ''}",
            f"Registered time: {row['registered_time']}",
            "",
            f"Clamping: {row['clamping_condition'] or 'unknown'}",
            f"Backing/anvil: {row['backing_condition'] or 'unknown'}",
            f"Cooling: {row['cooling_condition'] or 'unknown'}",
            f"Tool wear: {row['tool_wear_state'] or 'unknown'}",
            f"Surface: {row['surface_condition'] or 'unknown'}",
            "",
            f"Notes: {row['optional_notes'] or row['file_notes'] or ''}",
            "",
            f"SHA256: {row['sha256']}",
        ]
        self._set_library_details("\n".join(details))

    def _selected_library_row(self) -> sqlite3.Row | None:
        selection = self.library_tree.selection()
        if not selection:
            return None
        return self.library_rows.get(selection[0])

    def _set_library_details(self, text: str) -> None:
        self.library_details.configure(state="normal")
        self.library_details.delete("1.0", "end")
        self.library_details.insert("1.0", text)
        self.library_details.configure(state="disabled")

    def open_selected_library_file(self) -> None:
        row = self._selected_library_row()
        if row is None:
            messagebox.showinfo(APP_NAME, "Select a file first.")
            return
        path = self.store.resolve_library_path(row)
        if not path.exists():
            messagebox.showerror(APP_NAME, f"File not found:\n{path}")
            return
        os.startfile(path)

    def open_selected_library_folder(self) -> None:
        row = self._selected_library_row()
        if row is None:
            messagebox.showinfo(APP_NAME, "Select a file first.")
            return
        path = self.store.resolve_library_path(row)
        folder = path.parent if path.parent.exists() else self.library_dir
        os.startfile(folder)

    def copy_selected_library_path(self) -> None:
        row = self._selected_library_row()
        if row is None:
            messagebox.showinfo(APP_NAME, "Select a file first.")
            return
        path = self.store.resolve_library_path(row)
        self.clipboard_clear()
        self.clipboard_append(str(path))
        self.status.set("Copied selected library path to clipboard.")

    def _build_filename(self, source: Path, run_data: dict, run_id: str, file_type: str) -> str:
        prefix = "_".join(
            [
                safe_text(run_data["sample_id"]),
                f"{int(float(run_data['rotation_speed_rpm']))}rpm",
                f"{int(float(run_data['travel_speed_mm_min']))}mmmin",
                safe_text(run_data["tool_id"]),
                safe_text(run_data["material"]),
                f"{safe_text(str(run_data['thickness_mm']))}mm",
            ]
        )
        return f"{prefix}_{safe_text(file_type)}_{local_stamp()}{source.suffix.lower()}"


def run_self_test() -> None:
    root = app_root()
    library = root / "FSW_Data_Library"
    library.mkdir(exist_ok=True)
    store = MetadataStore(library)
    store.export_csvs()
    print("OK: schema initialized")
    print(f"database: {store.db_path}")


if __name__ == "__main__":
    try:
        if "--self-test" in sys.argv:
            run_self_test()
        else:
            app = App()
            app.mainloop()
    except Exception:
        log_path = app_root() / "fsw_metadata_monitor_crash.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise
