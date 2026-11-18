"""
Local Project Discovery & Free Folder Browser (Cross-Platform + Windows Native Picker).
Discovers:
- Recent / user-saved projects
- Workspace sibling/category project trees
- ~/.codex/config.toml registered projects
And allows browsing ANY drive / folder or opening the native OS Folder Picker.
"""

from __future__ import annotations

import os
import re
import string
import subprocess
from typing import Any, Dict, List, Optional

from codex_adapter import CREATE_NO_WINDOW

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PROTO_DIR)
WORKSPACE_PARENT = os.path.dirname(os.path.dirname(REPO_ROOT))

SKIP_HIDDEN = {".git", ".stfolder", ".stversions", "__pycache__", "node_modules", ".venv", ".codex", ".agents"}


def list_drives() -> List[str]:
    if os.name != "nt":
        return ["/", os.path.expanduser("~")]
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def discover_projects(recent_paths: Optional[List[str]] = None) -> Dict[str, Any]:
    seen = set()
    groups: List[Dict[str, Any]] = []

    def _add_item(items_list: List[Dict[str, str]], path_str: str, label: Optional[str] = None) -> None:
        if not path_str or not os.path.isdir(path_str):
            return
        norm = os.path.normcase(os.path.normpath(os.path.abspath(path_str)))
        if norm in seen:
            return
        seen.add(norm)
        clean_p = os.path.normpath(os.path.abspath(path_str))
        items_list.append({
            "name": label or os.path.basename(clean_p) or clean_p,
            "path": clean_p,
        })

    # 1. Recent / Pinned
    rec_items: List[Dict[str, str]] = []
    for rp in (recent_paths or []):
        _add_item(rec_items, rp)
    if rec_items:
        groups.append({"group": "⭐ 最近使用项目", "items": rec_items})

    # 2. Workspace structured tree (auto-detected from environment or workspace parent)
    rt_root = os.environ.get("AGY_ROUNDTABLE_PROJECTS_ROOT") or WORKSPACE_PARENT
    if os.path.isdir(rt_root):
        try:
            for cat in sorted(os.listdir(rt_root)):
                if cat in SKIP_HIDDEN or cat.startswith("."):
                    continue
                cat_path = os.path.join(rt_root, cat)
                if not os.path.isdir(cat_path):
                    continue
                cat_items: List[Dict[str, str]] = []
                # Include category root itself + its direct project folders
                _add_item(cat_items, cat_path, f"[{cat} 根目录]")
                for sub in sorted(os.listdir(cat_path)):
                    if sub in SKIP_HIDDEN or sub.startswith("."):
                        continue
                    sub_p = os.path.join(cat_path, sub)
                    if os.path.isdir(sub_p):
                        _add_item(cat_items, sub_p, sub)
                if cat_items:
                    groups.append({"group": f"📂 {cat}", "items": cat_items})
        except Exception:
            pass

    # 3. Codex config.toml projects
    codex_cfg = os.path.expanduser(r"~/.codex/config.toml")
    codex_items: List[Dict[str, str]] = []
    if os.path.exists(codex_cfg):
        try:
            with open(codex_cfg, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^\[projects\.(?:'([^']+)'|\"([^\"]+)\")\]", line.strip())
                    if m:
                        p = m.group(1) or m.group(2)
                        _add_item(codex_items, p)
        except Exception:
            pass
    if codex_items:
        groups.append({"group": "💻 Codex 历史登记项目", "items": codex_items})

    return {"drives": list_drives(), "groups": groups}


def browse_directory(target_path: Optional[str] = None) -> Dict[str, Any]:
    drives = list_drives()
    if not target_path:
        target_path = WORKSPACE_PARENT if os.path.isdir(WORKSPACE_PARENT) else (drives[0] if drives else os.getcwd())
    abs_p = os.path.normpath(os.path.abspath(target_path))
    if not os.path.isdir(abs_p):
        raise ValueError(f"目录不存在或无法访问: {abs_p}")

    parent = os.path.dirname(abs_p)
    has_parent = bool(parent and os.path.normcase(parent) != os.path.normcase(abs_p))

    subdirs: List[Dict[str, str]] = []
    try:
        for entry in sorted(os.scandir(abs_p), key=lambda e: e.name.lower()):
            if not entry.is_dir():
                continue
            if entry.name in ("$RECYCLE.BIN", "System Volume Information"):
                continue
            subdirs.append({"name": entry.name, "path": entry.path})
    except PermissionError as pe:
        raise ValueError(f"无权限读取该目录: {pe}") from pe

    return {
        "current_path": abs_p,
        "parent_path": parent if has_parent else None,
        "drives": drives,
        "subdirs": subdirs,
    }


def pick_folder_native_dialog(initial_dir: Optional[str] = None) -> Dict[str, Any]:
    """Open native Folder Picker Dialog so user can pick any folder visually."""
    if os.name != "nt":
        return {"cancelled": True, "path": None}
    init_escaped = (initial_dir or WORKSPACE_PARENT).replace("'", "''")
    ps_code = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; "
        "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dlg.Description = '选择要切换到的项目工作目录'; "
        f"if (Test-Path -LiteralPath '{init_escaped}') {{ $dlg.SelectedPath = '{init_escaped}' }}; "
        "$res = $dlg.ShowDialog($f); "
        "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dlg.SelectedPath }"
    )
    p = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", ps_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=120,
    )
    chosen = (p.stdout or "").strip()
    if chosen and os.path.isdir(chosen):
        return {"cancelled": False, "path": os.path.normpath(chosen)}
    return {"cancelled": True, "path": None}
