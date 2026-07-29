@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0fsw_metadata_monitor.py"
set "TORCH311_PY=C:\Users\61452\anaconda3\envs\torch311\python.exe"
set "CONDA_BASE_PY=C:\Users\61452\anaconda3\python.exe"
set "CODEX_PY=C:\Users\61452\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%TORCH311_PY%" (
    "%TORCH311_PY%" "%SCRIPT%"
    if errorlevel 1 pause
    goto :eof
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%SCRIPT%"
    if errorlevel 1 pause
    goto :eof
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py "%SCRIPT%"
    if errorlevel 1 pause
    goto :eof
)

if exist "%CONDA_BASE_PY%" (
    "%CONDA_BASE_PY%" "%SCRIPT%"
    if errorlevel 1 pause
    goto :eof
)

if exist "%CODEX_PY%" (
    "%CODEX_PY%" "%SCRIPT%"
    if errorlevel 1 pause
    goto :eof
)

echo Python was not found on this computer.
echo Install Python 3, or ask Zijue to package this monitor as an exe.
pause
