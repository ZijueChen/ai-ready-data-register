# AI-Ready Data Register

A lightweight desktop tool for registering experimental data files with structured metadata before they enter a project library.

The current repository includes process-specific prototypes for friction stir welding (FSW) and directed energy deposition (DED), and the code is intended as a template for other manufacturing processes such as coating, laminating, and foil projects.

## Current Features

- Watches an inbox folder for newly added files.
- Registers physical samples first, including process parameters, material/tool information, optional conditions, and notes.
- Opens a raw file registration window for each detected file and links each file to an existing sample.
- Moves registered files into a library folder.
- Lets users add derived result rows that link back to registered raw files.
- Maintains a SQLite metadata index, CSV exports, and a derived results Excel workbook.
- Provides a library browser for viewing the sample/file/result structure and opening registered source files.

## Run From Source

On the current Windows development machine:

```bat
FSW_Metadata_Monitor\run_fsw_metadata_monitor.bat
```

For a packaged Windows build, use the locally generated release folder or create a new executable with PyInstaller.

## Folder Structure

```text
FSW_Metadata_Monitor/
DED_Metadata_Monitor/
```

Treat files in `FSW_Data_Library` as registered archive files. If a file changes, place the changed copy back into the inbox and register it as a new entry/version.

DED currently tracks laser power, scan speed, powder rate, optional argon rate, optional substrate temperature, beam profile CSV files, and optical microscope section images.
