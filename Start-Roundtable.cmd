@echo off
chcp 65001 >nul
setlocal
set "REPO_DIR=%~dp0"
set "PROTO_DIR=%REPO_DIR%prototype"
set "PYTHONUTF8=1"

if exist "D:\gemini\tmp" (
    set "TEMP=D:\gemini\tmp"
    set "TMP=D:\gemini\tmp"
    set "PYTHONPYCACHEPREFIX=D:\gemini\cache\pycache"
)

set "PY_EXE=python"
if exist "D:\PYTHON\python.exe" set "PY_EXE=D:\PYTHON\python.exe"

echo [AGY Roundtable / 圆桌会议] Starting local collaboration studio on http://127.0.0.1:8765 ...
"%PY_EXE%" -c "import os, sys, webbrowser; sys.path.insert(0, os.path.join(r'%REPO_DIR%', 'prototype')); from team_cli import TeamRuntimeClient; info = TeamRuntimeClient().ensure_server_running(); print('Ready PID=' + str(info.get('pid'))); webbrowser.open('http://127.0.0.1:8765')"
endlocal
