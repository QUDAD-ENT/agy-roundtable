#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONUTF8=1

echo "[AGY Roundtable / 圆桌会议] Starting local collaboration studio on http://127.0.0.1:8765 ..."
python3 -c "import os, sys, webbrowser; sys.path.insert(0, os.path.join('${SCRIPT_DIR}', 'prototype')); from team_cli import TeamRuntimeClient; info = TeamRuntimeClient().ensure_server_running(); print('Ready PID=' + str(info.get('pid'))); webbrowser.open('http://127.0.0.1:8765')"
