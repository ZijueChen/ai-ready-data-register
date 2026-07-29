# AI-Ready Data Register

A lightweight desktop tool for registering experimental data files with structured metadata before they enter a project library.

The current prototype is configured for friction stir welding data, but the code is intended as a template for other manufacturing processes such as coating, laminating, and foil projects.

## Current Features

- Watches an inbox folder for newly added files.
- Opens a registration window for each detected file.
- Records sample ID, file type, process parameters, material/tool information, optional conditions, and notes.
- Moves registered files into a library folder.
- Maintains a SQLite metadata index and CSV exports.
- Provides a library browser for viewing the sample/file structure and opening registered files.

## Run From Source

On the current Windows development machine:

```bat
FSW_Metadata_Monitor\run_fsw_metadata_monitor.bat
```

For a packaged Windows build, use the locally generated release folder or create a new executable with PyInstaller.

## Folder Structure

```text
FSW_Metadata_Monitor/
├── fsw_metadata_monitor.py
├── run_fsw_metadata_monitor.bat
├── README_OPERATOR.txt
├── FSW_Data_Inbox/
└── FSW_Data_Library/
```

Treat files in `FSW_Data_Library` as registered archive files. If a file changes, place the changed copy back into the inbox and register it as a new entry/version.
