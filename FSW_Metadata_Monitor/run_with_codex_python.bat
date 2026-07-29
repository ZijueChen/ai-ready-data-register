@echo off
setlocal
cd /d "%~dp0"

set "CODEX_PY=C:\Users\61452\anaconda3\envs\torch311\python.exe"

if not exist "%CODEX_PY%" (
    echo Python was not found:
    echo %CODEX_PY%
    pause
    goto :eof
)

"%CODEX_PY%" "%~dp0fsw_metadata_monitor.py"
if errorlevel 1 pause
