import csv
import traceback
import hashlib
import os
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from xml.sax.saxutils import escape


APP_NAME = "DED Metadata Monitor"
POLL_SECONDS = 3
STABLE_SECONDS = 5
IGNORED_INBOX_FILES = {"PUT_NEW_DED_FILES_HERE.txt"}

FILE_TYPES = [
    "beam_profile_csv",
    "melt_pool_section_image",
    "beam_section_image",
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
    return f"DED_{datetime.now().strftime('%Y%m%d')}_S{index + 1:03d}"


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
        if "section" in name or "melt" in name or "pool" in name:
            return "melt_pool_section_image"
        return "beam_section_image"
    if suffix == ".csv":
        return "beam_profile_csv"
    if suffix in [".xls", ".xlsx", ".tdms", ".mp4", ".avi", ".mov"]:
        return "process_log"
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
        self.derived_results_csv = library_dir / "derived_results.csv"
        self.derived_results_xlsx = library_dir / "derived_results.xlsx"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                sample_id TEXT NOT NULL,
                laser_power REAL NOT NULL,
                scan_speed REAL NOT NULL,
                powder_rate REAL NOT NULL,
                argon_rate REAL,
                substrate_temperature REAL,
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

            CREATE TABLE IF NOT EXISTS derived_results (
                result_id TEXT PRIMARY KEY,
                sample_id TEXT NOT NULL,
                source_file_id TEXT,
                test_type TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value TEXT NOT NULL,
                unit TEXT,
                method TEXT,
                operator TEXT,
                created_time TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(source_file_id) REFERENCES files(file_id)
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

    def sample_records(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM runs ORDER BY sample_id"))

    def sample_by_id(self, sample_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE sample_id = ? ORDER BY created_time DESC LIMIT 1",
            (sample_id,),
        ).fetchone()

    def insert_run(self, data: dict) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO runs (
                run_id, sample_id, laser_power, scan_speed,
                powder_rate, argon_rate, substrate_temperature, operator, created_time,
                clamping_condition, backing_condition, cooling_condition,
                tool_wear_state, surface_condition, optional_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["run_id"],
                data["sample_id"],
                data["laser_power"],
                data["scan_speed"],
                data["powder_rate"],
                data["argon_rate"],
                data["substrate_temperature"],
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

    def insert_derived_results(self, rows: list[dict]) -> None:
        self.conn.executemany(
            """
            INSERT INTO derived_results (
                result_id, sample_id, source_file_id, test_type, metric_name,
                metric_value, unit, method, operator, created_time, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["result_id"],
                    row["sample_id"],
                    row.get("source_file_id", ""),
                    row["test_type"],
                    row["metric_name"],
                    row["metric_value"],
                    row.get("unit", ""),
                    row.get("method", ""),
                    row.get("operator", ""),
                    row["created_time"],
                    row.get("notes", ""),
                )
                for row in rows
            ],
        )
        self.conn.commit()
        self.export_csvs()

    def sample_ids(self) -> list[str]:
        return [
            row["sample_id"]
            for row in self.conn.execute("SELECT sample_id FROM runs ORDER BY sample_id")
        ]

    def files_for_sample(self, sample_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT file_id, sample_id, file_type, new_filename, new_path, new_relative_path
                FROM files f
                LEFT JOIN runs r ON f.run_id = r.run_id
                WHERE r.sample_id = ?
                ORDER BY f.file_type, f.registered_time
                """,
                (sample_id,),
            )
        )

    def derived_entries(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT
                    d.result_id,
                    d.sample_id,
                    d.source_file_id,
                    d.test_type,
                    d.metric_name,
                    d.metric_value,
                    d.unit,
                    d.method,
                    d.operator,
                    d.created_time,
                    d.notes,
                    f.new_filename AS source_filename,
                    f.new_path AS source_path,
                    f.new_relative_path AS source_relative_path,
                    f.file_type AS source_file_type
                FROM derived_results d
                LEFT JOIN files f ON d.source_file_id = f.file_id
                ORDER BY d.sample_id, d.test_type, d.created_time, d.metric_name
                """
            )
        )

    def derived_entries_for_sample(self, sample_id: str) -> list[sqlite3.Row]:
        return [
            row for row in self.derived_entries()
            if row["sample_id"] == sample_id
        ]

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
                    r.laser_power,
                    r.scan_speed,
                    r.powder_rate,
                    r.argon_rate,
                    r.substrate_temperature,
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
        self._export_table("derived_results", self.derived_results_csv)
        self._export_derived_results_xlsx()

    def _export_table(self, table_name: str, csv_path: Path) -> None:
        rows = list(self.conn.execute(f"SELECT * FROM {table_name}"))
        if not rows:
            return
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow([row[key] for key in row.keys()])

    def _export_derived_results_xlsx(self) -> None:
        rows = list(self.conn.execute("SELECT * FROM derived_results ORDER BY sample_id, test_type, created_time, metric_name"))
        if not rows:
            return
        headers = list(rows[0].keys())
        values = [headers] + [[row[key] for key in headers] for row in rows]
        self._write_simple_xlsx(self.derived_results_xlsx, "Derived Results", values)

    def _write_simple_xlsx(self, path: Path, sheet_name: str, values: list[list[object]]) -> None:
        def col_name(index: int) -> str:
            name = ""
            index += 1
            while index:
                index, remainder = divmod(index - 1, 26)
                name = chr(65 + remainder) + name
            return name

        rows_xml = []
        for row_idx, row in enumerate(values, start=1):
            cells = []
            for col_idx, value in enumerate(row):
                cell_ref = f"{col_name(col_idx)}{row_idx}"
                text = "" if value is None else str(value)
                cells.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>')
            rows_xml.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(rows_xml)}</sheetData>'
            '</worksheet>'
        )
        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>'
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        )
        workbook_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        )
        content_types_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("_rels/.rels", rels_xml)
            archive.writestr("xl/workbook.xml", workbook_xml)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
            archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


class SampleDialog(tk.Toplevel):
    def __init__(self, master, store: MetadataStore):
        super().__init__(master)
        self.store = store
        self.result = None
        self.title(f"{APP_NAME} - register sample")
        self.geometry("760x620")
        self.minsize(680, 560)
        self.transient(master)
        self.grab_set()

        self.auto_sample_id = tk.BooleanVar(value=True)
        self.sample_id = tk.StringVar(value=generated_sample_id(0))
        self.laser_power = tk.StringVar()
        self.scan_speed = tk.StringVar()
        self.powder_rate = tk.StringVar()
        self.argon_rate = tk.StringVar()
        self.substrate_temperature = tk.StringVar()
        self.operator = tk.StringVar(value=os.environ.get("USERNAME", ""))
        self.clamping = tk.StringVar(value="unknown")
        self.backing = tk.StringVar(value="unknown")
        self.cooling = tk.StringVar(value="unknown")
        self.tool_wear = tk.StringVar(value="unknown")
        self.surface = tk.StringVar(value="unknown")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="Register Sample", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            root,
            text="Create the sample record when the physical DED sample is produced. Raw files can be linked to this sample later.",
            wraplength=700,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 12))

        sample_row = ttk.Frame(root)
        sample_row.columnconfigure(0, weight=1)
        self.sample_entry = ttk.Entry(sample_row, textvariable=self.sample_id)
        self.sample_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(
            sample_row,
            text="Auto",
            variable=self.auto_sample_id,
            command=self._toggle_auto_sample_id,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(sample_row, text="Generate", command=self._generate_sample_id).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        fields = [
            ("Sample ID", sample_row),
            ("Laser power", self.laser_power),
            ("Scan speed", self.scan_speed),
            ("Powder rate", self.powder_rate),
            ("Argon rate (optional)", self.argon_rate),
            ("Substrate temperature (optional)", self.substrate_temperature),
            ("Operator", self.operator),
        ]
        for index, (label, variable) in enumerate(fields, start=2):
            ttk.Label(root, text=label).grid(row=index, column=0, sticky="w", padx=(0, 8), pady=5)
            if isinstance(variable, ttk.Frame):
                variable.grid(row=index, column=1, columnspan=2, sticky="ew", pady=5)
            else:
                ttk.Entry(root, textvariable=variable).grid(row=index, column=1, columnspan=2, sticky="ew", pady=5)

        opt = ttk.LabelFrame(root, text="Production observations")
        opt.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        for col in range(5):
            opt.columnconfigure(col, weight=1)
        option_fields = [
            ("Shielding", self.clamping),
            ("Powder lot", self.backing),
            ("Nozzle", self.cooling),
            ("Substrate prep", self.tool_wear),
            ("Surface", self.surface),
        ]
        for col, (label, variable) in enumerate(option_fields):
            ttk.Label(opt, text=label).grid(row=0, column=col, sticky="w", padx=8, pady=4)
            ttk.Combobox(opt, textvariable=variable, values=OPTIONAL_STATES, state="readonly").grid(
                row=1, column=col, sticky="ew", padx=8, pady=(0, 8)
            )

        ttk.Label(root, text="Anything unusual during sample production").grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(4, 2)
        )
        self.notes = tk.Text(root, height=7, wrap="word")
        self.notes.grid(row=11, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        root.rowconfigure(11, weight=1)

        buttons = ttk.Frame(root)
        buttons.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Register sample", command=self._submit).pack(side="right")
        self._toggle_auto_sample_id()

    def _toggle_auto_sample_id(self) -> None:
        self.sample_entry.configure(state="disabled" if self.auto_sample_id.get() else "normal")

    def _generate_sample_id(self) -> None:
        self.sample_id.set(generated_sample_id(0))

    def _submit(self) -> None:
        data = {
            "sample_id": self.sample_id.get().strip(),
            "run_id": self.sample_id.get().strip(),
            "laser_power": self.laser_power.get().strip(),
            "scan_speed": self.scan_speed.get().strip(),
            "powder_rate": self.powder_rate.get().strip(),
            "argon_rate": self.argon_rate.get().strip(),
            "substrate_temperature": self.substrate_temperature.get().strip(),
            "operator": self.operator.get().strip(),
            "created_time": utc_now(),
            "clamping_condition": self.clamping.get(),
            "backing_condition": self.backing.get(),
            "cooling_condition": self.cooling.get(),
            "tool_wear_state": self.tool_wear.get(),
            "surface_condition": self.surface.get(),
            "optional_notes": self.notes.get("1.0", "end").strip(),
        }
        required = ["sample_id", "laser_power", "scan_speed", "powder_rate"]
        missing = [name for name in required if not data[name]]
        if missing:
            messagebox.showerror(APP_NAME, "Missing required fields: " + ", ".join(missing))
            return
        if self.store.sample_by_id(data["sample_id"]) is not None:
            messagebox.showerror(APP_NAME, "This Sample ID already exists. Use a different Sample ID.")
            return
        try:
            data["laser_power"] = float(data["laser_power"])
            data["scan_speed"] = float(data["scan_speed"])
            data["powder_rate"] = float(data["powder_rate"])
            if data["argon_rate"] != "":
                data["argon_rate"] = float(data["argon_rate"])
            if data["substrate_temperature"] != "":
                data["substrate_temperature"] = float(data["substrate_temperature"])
        except ValueError:
            messagebox.showerror(APP_NAME, "Numeric parameter fields must be numbers when filled.")
            return
        self.result = data
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


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
        self.sample_id = tk.StringVar()
        self.sample_map: dict[str, sqlite3.Row] = {}

        self._build()
        if self.files:
            self._select_file(self.files[0])
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        title = ttk.Label(
            root,
            text=f"{len(self.files)} new raw file(s) detected. Link each file to a registered sample.",
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

        ttk.Label(editor, text="Registered sample").grid(row=2, column=0, sticky="w", padx=8, pady=5)
        self.sample_combo = ttk.Combobox(editor, textvariable=self.sample_id, state="readonly")
        self.sample_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=5)
        self._refresh_samples()

        self.sample_summary = ttk.Label(editor, text="", wraplength=560)
        self.sample_summary.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 10))
        self.sample_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_sample_summary())

        ttk.Label(editor, text="Notes for this raw file").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 2)
        )
        self.notes = tk.Text(editor, height=5, wrap="word")
        self.notes.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=8, pady=(0, 8))
        editor.rowconfigure(5, weight=1)

        editor_buttons = ttk.Frame(editor)
        editor_buttons.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
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
        sample_id = self.sample_id.get().strip()
        return {
            "file_type": infer_file_type(file_path),
            "sample_id": sample_id,
            "file_notes": "",
        }

    def _load_form(self, data: dict) -> None:
        self.file_type.set(data.get("file_type", "other"))
        self.sample_id.set(str(data.get("sample_id", "")))
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", data.get("file_notes", ""))
        self._update_sample_summary()

    def _capture_current(self, draft: bool = False) -> dict | None:
        if self.current_file is None:
            return None
        sample_id = self.sample_id.get().strip()
        sample = self.store.sample_by_id(sample_id) if sample_id else None
        data = {
            "file_type": self.file_type.get().strip(),
            "sample_id": sample_id,
            "file_notes": self.notes.get("1.0", "end").strip(),
        }
        if sample is not None:
            data.update(dict(sample))
            data["file_type"] = self.file_type.get().strip()
            data["file_notes"] = self.notes.get("1.0", "end").strip()
        if draft:
            if not hasattr(self, "_drafts"):
                self._drafts = {}
            self._drafts[self.current_file] = data
        return data

    def _refresh_samples(self) -> None:
        self.sample_map.clear()
        values = []
        for row in self.store.sample_records():
            label = f"{row['sample_id']} | {row['laser_power']} laser x {row['scan_speed']} scan"
            self.sample_map[label] = row
            values.append(label)
        self.sample_combo["values"] = values
        if values and not self.sample_id.get().strip():
            self.sample_combo.current(0)
            self.sample_id.set(self.sample_map[values[0]]["sample_id"])

    def _update_sample_summary(self) -> None:
        label = self.sample_combo.get()
        row = self.sample_map.get(label) or self.store.sample_by_id(self.sample_id.get().strip())
        if row is None:
            self.sample_summary.configure(text="")
            return
        self.sample_id.set(row["sample_id"])
        self.sample_summary.configure(
            text=(
                f"Powder rate: {row['powder_rate']} | Argon: {row['argon_rate'] if row['argon_rate'] not in [None, ''] else 'not recorded'} | "
                f"Substrate temp: {row['substrate_temperature'] if row['substrate_temperature'] not in [None, ''] else 'not recorded'} | Operator: {row['operator'] or ''}"
            )
        )

    def _validate_metadata(self, data: dict) -> dict | None:
        values = {
            "sample_id": data.get("sample_id", ""),
            "file_type": data.get("file_type", ""),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            messagebox.showerror(APP_NAME, "Missing required fields: " + ", ".join(missing))
            return None
        if self.store.sample_by_id(data["sample_id"]) is None:
            messagebox.showerror(APP_NAME, "Select a registered sample before saving this raw file.")
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


class DerivedResultDialog(tk.Toplevel):
    def __init__(
        self,
        master,
        store: MetadataStore,
        initial_sample_id: str = "",
        initial_source_file_id: str = "",
    ):
        super().__init__(master)
        self.store = store
        self.result = None
        self.initial_sample_id = initial_sample_id
        self.initial_source_file_id = initial_source_file_id
        self.title(f"{APP_NAME} - add derived results")
        self.geometry("860x620")
        self.minsize(760, 520)
        self.transient(master)
        self.grab_set()

        self.sample_id = tk.StringVar()
        self.source_file = tk.StringVar()
        self.test_type = tk.StringVar()
        self.method = tk.StringVar()
        self.operator = tk.StringVar(value=os.environ.get("USERNAME", ""))
        self.source_file_map: dict[str, str] = {}
        self.metric_rows: list[tuple[tk.StringVar, tk.StringVar, tk.StringVar]] = []

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.columnconfigure(3, weight=1)
        root.rowconfigure(5, weight=1)

        ttk.Label(root, text="Register Derived Results", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        ttk.Label(
            root,
            text="Use this for values extracted from registered raw files, such as melt pool width, bead height, dilution, UTS, or hardness.",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 12))

        ttk.Label(root, text="Sample ID").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=5)
        sample_values = self.store.sample_ids()
        self.sample_combo = ttk.Combobox(root, textvariable=self.sample_id, values=sample_values)
        self.sample_combo.grid(row=2, column=1, columnspan=3, sticky="ew", pady=5)
        self.sample_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_source_files())
        if self.initial_sample_id:
            self.sample_id.set(self.initial_sample_id)
        elif sample_values:
            self.sample_id.set(sample_values[0])

        ttk.Label(root, text="Source raw file").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=5)
        self.source_combo = ttk.Combobox(root, textvariable=self.source_file, state="readonly")
        self.source_combo.grid(row=3, column=1, columnspan=3, sticky="ew", pady=5)

        ttk.Label(root, text="Test type").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(root, textvariable=self.test_type).grid(row=4, column=1, sticky="ew", pady=5)
        ttk.Label(root, text="Method").grid(row=4, column=2, sticky="w", padx=(16, 8), pady=5)
        ttk.Entry(root, textvariable=self.method, width=24).grid(row=4, column=3, sticky="ew", pady=5)

        metrics = ttk.LabelFrame(root, text="Metric rows")
        metrics.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(12, 8))
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)
        metrics.columnconfigure(2, weight=1)
        self.metrics_frame = metrics
        ttk.Label(metrics, text="Metric name").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(metrics, text="Value").grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(metrics, text="Unit").grid(row=0, column=2, sticky="w", padx=8, pady=4)
        self._add_metric_row()
        self._add_metric_row()

        add_row_bar = ttk.Frame(root)
        add_row_bar.grid(row=6, column=0, columnspan=4, sticky="ew")
        ttk.Button(add_row_bar, text="Add metric row", command=self._add_metric_row).pack(side="left")

        ttk.Label(root, text="Operator").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=(12, 5))
        ttk.Entry(root, textvariable=self.operator).grid(row=7, column=1, sticky="ew", pady=(12, 5))

        ttk.Label(root, text="Notes").grid(row=8, column=0, sticky="nw", padx=(0, 8), pady=5)
        self.notes = tk.Text(root, height=4, wrap="word")
        self.notes.grid(row=8, column=1, columnspan=3, sticky="ew", pady=5)

        buttons = ttk.Frame(root)
        buttons.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Save derived results", command=self._submit).pack(side="right")

        self._refresh_source_files()

    def _add_metric_row(self) -> None:
        row_idx = len(self.metric_rows) + 1
        name_var = tk.StringVar()
        value_var = tk.StringVar()
        unit_var = tk.StringVar()
        self.metric_rows.append((name_var, value_var, unit_var))
        ttk.Entry(self.metrics_frame, textvariable=name_var).grid(
            row=row_idx, column=0, sticky="ew", padx=8, pady=3
        )
        ttk.Entry(self.metrics_frame, textvariable=value_var).grid(
            row=row_idx, column=1, sticky="ew", padx=8, pady=3
        )
        ttk.Entry(self.metrics_frame, textvariable=unit_var).grid(
            row=row_idx, column=2, sticky="ew", padx=8, pady=3
        )

    def _refresh_source_files(self) -> None:
        sample_id = self.sample_id.get().strip()
        self.source_file_map.clear()
        values = ["not linked"]
        selected_label = values[0]
        if sample_id:
            for row in self.store.files_for_sample(sample_id):
                label = f"{row['file_type']} | {row['new_filename']}"
                self.source_file_map[label] = row["file_id"]
                values.append(label)
                if row["file_id"] == self.initial_source_file_id:
                    selected_label = label
        self.source_combo["values"] = values
        self.source_file.set(selected_label if selected_label in values else values[0])

    def _submit(self) -> None:
        sample_id = self.sample_id.get().strip()
        test_type = self.test_type.get().strip()
        if not sample_id or not test_type:
            messagebox.showerror(APP_NAME, "Sample ID and test type are required.")
            return

        rows = []
        created_time = utc_now()
        for name_var, value_var, unit_var in self.metric_rows:
            metric_name = name_var.get().strip()
            metric_value = value_var.get().strip()
            unit = unit_var.get().strip()
            if not metric_name and not metric_value and not unit:
                continue
            if not metric_name or not metric_value:
                messagebox.showerror(APP_NAME, "Each metric row needs at least metric name and value.")
                return
            source_label = self.source_file.get()
            rows.append(
                {
                    "result_id": str(uuid.uuid4()),
                    "sample_id": sample_id,
                    "source_file_id": self.source_file_map.get(source_label, ""),
                    "test_type": test_type,
                    "metric_name": metric_name,
                    "metric_value": metric_value,
                    "unit": unit,
                    "method": self.method.get().strip(),
                    "operator": self.operator.get().strip(),
                    "created_time": created_time,
                    "notes": self.notes.get("1.0", "end").strip(),
                }
            )
        if not rows:
            messagebox.showerror(APP_NAME, "Add at least one metric row.")
            return
        self.result = rows
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
        self.inbox_dir = self.root_dir / "DED_Data_Inbox"
        self.library_dir = self.root_dir / "DED_Data_Library"
        self.inbox_dir.mkdir(exist_ok=True)
        self.library_dir.mkdir(exist_ok=True)
        self.store = MetadataStore(self.library_dir, self.root_dir)
        self.pending: dict[Path, PendingFile] = {}
        self.processing = False
        self._last_no_sample_alert = 0.0
        self._library_split_initialized = False
        self.sample_rows: dict[str, sqlite3.Row] = {}
        self.library_rows: dict[str, sqlite3.Row] = {}
        self.derived_rows: dict[str, sqlite3.Row] = {}
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
        monitor_buttons = ttk.Frame(monitor)
        monitor_buttons.pack(fill="x", padx=10)
        ttk.Button(monitor_buttons, text="Register sample", command=self.register_sample).pack(side="left")
        ttk.Button(monitor_buttons, text="Scan now", command=self.scan).pack(side="right")

        browser = ttk.LabelFrame(body, text="Browse Library")
        body.add(browser, weight=2)
        browser.rowconfigure(1, weight=1)
        browser.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(browser, padding=(8, 8, 8, 4))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(toolbar, text="Refresh", command=self.refresh_library_tree).pack(side="left")
        ttk.Button(toolbar, text="Add derived result", command=self.add_derived_result).pack(
            side="left", padx=(8, 0)
        )
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
        if not self.store.sample_ids():
            now = time.time()
            if now - self._last_no_sample_alert > 30:
                self._last_no_sample_alert = now
                messagebox.showinfo(
                    APP_NAME,
                    "Register the physical sample first. Raw files can then be linked to that sample.",
                )
            self.status.set("Raw files remain in inbox. Register a sample first.")
            return
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

    def register_sample(self) -> None:
        dialog = SampleDialog(self, self.store)
        self.wait_window(dialog)
        if dialog.result:
            self.store.insert_run(dialog.result)
            self.refresh_library_tree()
            self.status.set(f"Registered sample {dialog.result['sample_id']}.")

    def refresh_library_tree(self) -> None:
        self.library_tree.delete(*self.library_tree.get_children())
        self.sample_rows.clear()
        self.library_rows.clear()
        self.derived_rows.clear()
        sample_nodes: dict[str, str] = {}
        type_nodes: dict[tuple[str, str], str] = {}

        for row in self.store.sample_records():
            sample_id = row["sample_id"] or row["run_id"] or "unknown_sample"
            sample_iid = f"sample::{sample_id}"
            label = (
                f"{sample_id}  |  {row['laser_power']} laser power x "
                f"{row['scan_speed']} scan speed"
            )
            self.library_tree.insert("", "end", iid=sample_iid, text=label, open=True)
            sample_nodes[sample_id] = sample_iid
            self.sample_rows[sample_iid] = row

        for row in self.store.library_entries():
            sample_id = row["sample_id"] or row["run_id"] or "unknown_sample"
            file_type = row["file_type"] or "unknown_type"
            sample_iid = f"sample::{sample_id}"
            type_iid = f"type::{sample_id}::{file_type}"
            file_iid = f"file::{row['file_id']}"

            if sample_id not in sample_nodes:
                label = (
                    f"{sample_id}  |  {row['laser_power']} laser power x "
                    f"{row['scan_speed']} scan speed"
                )
                self.library_tree.insert("", "end", iid=sample_iid, text=label, open=True)
                sample_nodes[sample_id] = sample_iid
                self.sample_rows[sample_iid] = row

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

        for row in self.store.derived_entries():
            sample_id = row["sample_id"] or "unknown_sample"
            sample_iid = f"sample::{sample_id}"
            derived_iid = f"derived::{sample_id}"
            result_iid = f"result::{row['result_id']}"

            if sample_id not in sample_nodes:
                self.library_tree.insert("", "end", iid=sample_iid, text=sample_id, open=True)
                sample_nodes[sample_id] = sample_iid

            if not self.library_tree.exists(derived_iid):
                self.library_tree.insert(
                    sample_iid,
                    "end",
                    iid=derived_iid,
                    text="derived_results",
                    open=True,
                )

            unit = f" {row['unit']}" if row["unit"] else ""
            label = f"{row['test_type']}: {row['metric_name']} = {row['metric_value']}{unit}"
            self.library_tree.insert(derived_iid, "end", iid=result_iid, text=label)
            self.derived_rows[result_iid] = row

        if not self.sample_rows and not self.library_rows and not self.derived_rows:
            self._set_library_details("No registered samples or files yet.")
        elif not self.library_tree.selection():
            self._set_library_details("Select a sample, file, or derived result to view its metadata.")

    def on_library_select(self, _event=None) -> None:
        sample_row = self._selected_sample_row()
        if sample_row is not None:
            self._show_sample_details(sample_row)
            return
        derived_row = self._selected_derived_row()
        if derived_row is not None:
            self._show_derived_details(derived_row)
            return
        row = self._selected_library_row()
        if row is None:
            self._set_library_details("Select a file to view its metadata.")
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
            f"Laser power: {row['laser_power']}",
            f"Scan speed: {row['scan_speed']}",
            f"Powder rate: {row['powder_rate']}",
            f"Argon rate: {row['argon_rate'] if row['argon_rate'] not in [None, ''] else 'not recorded'}",
            f"Substrate temperature: {row['substrate_temperature'] if row['substrate_temperature'] not in [None, ''] else 'not recorded'}",
            f"Operator: {row['operator'] or ''}",
            f"Registered time: {row['registered_time']}",
            "",
            f"Shielding: {row['clamping_condition'] or 'unknown'}",
            f"Powder lot: {row['backing_condition'] or 'unknown'}",
            f"Nozzle: {row['cooling_condition'] or 'unknown'}",
            f"Substrate prep: {row['tool_wear_state'] or 'unknown'}",
            f"Surface: {row['surface_condition'] or 'unknown'}",
            "",
            f"Notes: {row['optional_notes'] or row['file_notes'] or ''}",
            "",
            f"SHA256: {row['sha256']}",
        ]
        self._set_library_details("\n".join(details))

    def _show_sample_details(self, row: sqlite3.Row) -> None:
        details = [
            f"Sample ID: {row['sample_id']}",
            "Record type: registered sample",
            "",
            f"Laser power: {row['laser_power']}",
            f"Scan speed: {row['scan_speed']}",
            f"Powder rate: {row['powder_rate']}",
            f"Argon rate: {row['argon_rate'] if row['argon_rate'] not in [None, ''] else 'not recorded'}",
            f"Substrate temperature: {row['substrate_temperature'] if row['substrate_temperature'] not in [None, ''] else 'not recorded'}",
            f"Operator: {row['operator'] or ''}",
            f"Created time: {row['created_time']}",
            "",
            f"Shielding: {row['clamping_condition'] or 'unknown'}",
            f"Powder lot: {row['backing_condition'] or 'unknown'}",
            f"Nozzle: {row['cooling_condition'] or 'unknown'}",
            f"Substrate prep: {row['tool_wear_state'] or 'unknown'}",
            f"Surface: {row['surface_condition'] or 'unknown'}",
            "",
            f"Production notes: {row['optional_notes'] or ''}",
        ]
        self._set_library_details("\n".join(details))

    def _show_derived_details(self, row: sqlite3.Row) -> None:
        source_path = row["source_relative_path"] or row["source_path"] or "not linked"
        details = [
            f"Sample ID: {row['sample_id']}",
            "Record type: derived result",
            "",
            f"Test type: {row['test_type']}",
            f"Metric: {row['metric_name']}",
            f"Value: {row['metric_value']}",
            f"Unit: {row['unit'] or ''}",
            "",
            f"Source file: {row['source_filename'] or 'not linked'}",
            f"Source file type: {row['source_file_type'] or ''}",
            f"Source path: {source_path}",
            "",
            f"Method: {row['method'] or ''}",
            f"Operator: {row['operator'] or ''}",
            f"Created time: {row['created_time']}",
            "",
            f"Notes: {row['notes'] or ''}",
        ]
        self._set_library_details("\n".join(details))

    def _selected_library_row(self) -> sqlite3.Row | None:
        selection = self.library_tree.selection()
        if not selection:
            return None
        return self.library_rows.get(selection[0])

    def _selected_sample_row(self) -> sqlite3.Row | None:
        selection = self.library_tree.selection()
        if not selection:
            return None
        return self.sample_rows.get(selection[0])

    def _selected_derived_row(self) -> sqlite3.Row | None:
        selection = self.library_tree.selection()
        if not selection:
            return None
        return self.derived_rows.get(selection[0])

    def _selected_openable_path(self) -> Path | None:
        row = self._selected_library_row()
        if row is not None:
            return self.store.resolve_library_path(row)
        derived_row = self._selected_derived_row()
        if derived_row is None:
            return None
        if derived_row["source_relative_path"]:
            return self.root_dir / derived_row["source_relative_path"]
        if derived_row["source_path"]:
            return Path(derived_row["source_path"])
        return None

    def add_derived_result(self) -> None:
        if not self.store.sample_ids():
            messagebox.showinfo(APP_NAME, "Register at least one raw file before adding derived results.")
            return
        selected_row = self._selected_library_row()
        initial_sample_id = selected_row["sample_id"] if selected_row is not None else ""
        initial_source_file_id = selected_row["file_id"] if selected_row is not None else ""
        dialog = DerivedResultDialog(
            self,
            self.store,
            initial_sample_id=initial_sample_id,
            initial_source_file_id=initial_source_file_id,
        )
        self.wait_window(dialog)
        if dialog.result:
            self.store.insert_derived_results(dialog.result)
            self.refresh_library_tree()
            self.status.set(f"Added {len(dialog.result)} derived result(s).")

    def _set_library_details(self, text: str) -> None:
        self.library_details.configure(state="normal")
        self.library_details.delete("1.0", "end")
        self.library_details.insert("1.0", text)
        self.library_details.configure(state="disabled")

    def open_selected_library_file(self) -> None:
        path = self._selected_openable_path()
        if path is None:
            messagebox.showinfo(APP_NAME, "Select a file first.")
            return
        if not path.exists():
            messagebox.showerror(APP_NAME, f"File not found:\n{path}")
            return
        os.startfile(path)

    def open_selected_library_folder(self) -> None:
        path = self._selected_openable_path()
        if path is None:
            messagebox.showinfo(APP_NAME, "Select a file first.")
            return
        folder = path.parent if path.parent.exists() else self.library_dir
        os.startfile(folder)

    def copy_selected_library_path(self) -> None:
        path = self._selected_openable_path()
        if path is None:
            messagebox.showinfo(APP_NAME, "Select a file first.")
            return
        self.clipboard_clear()
        self.clipboard_append(str(path))
        self.status.set("Copied selected library path to clipboard.")

    def _build_filename(self, source: Path, run_data: dict, run_id: str, file_type: str) -> str:
        prefix = "_".join(
            [
                safe_text(run_data["sample_id"]),
                f"{safe_text(str(run_data['laser_power']))}laser",
                f"{safe_text(str(run_data['scan_speed']))}scan",
                f"{safe_text(str(run_data['powder_rate']))}powder",
            ]
        )
        return f"{prefix}_{safe_text(file_type)}_{local_stamp()}{source.suffix.lower()}"


def run_self_test() -> None:
    root = app_root()
    library = root / "DED_Data_Library"
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
        log_path = app_root() / "ded_metadata_monitor_crash.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise

