"""
Multi-threaded HTTP Server & REST API v3 for Dual-Client (Codex + Antigravity)
& Multi-Project Collaboration Workspace.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
if PROTO_DIR not in sys.path:
    sys.path.insert(0, PROTO_DIR)

from codex_adapter import CREATE_NO_WINDOW, get_runtime_dirs, hidden_subprocess_kwargs, suppress_windows_crash_dialogs
from figure_qa import inspect_any_figure
from team_runtime import TeamOrchestrator
from version_checker import check_for_updates, get_local_version_info

suppress_windows_crash_dialogs()

PORT = int(os.environ.get("TEAM_SIDEBAR_PORT", "8765"))
ORCHESTRATOR = TeamOrchestrator()
_LOCAL_VER = get_local_version_info()
APP_VERSION = str(_LOCAL_VER.get("version") or "5.5.0")
SERVER_REV = int(_LOCAL_VER.get("server_rev") or 19)

_STATE_CACHE_LOCK = threading.Lock()
_STATE_HTTP_CACHE: Dict[Tuple[int, bool, Optional[str]], Tuple[bytes, bytes]] = {}


def _focus_antigravity_window() -> Dict[str, Any]:
    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$ok = $ws.AppActivate('Antigravity'); "
        "Write-Output $ok"
    )
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=4,
            **hidden_subprocess_kwargs(),
        )
        activated = "true" in (p.stdout or "").lower()
        return {
            "window_activated": activated,
            "note": "已前置 Antigravity 桌面窗口" if activated else "请手动切回 Antigravity 窗口",
        }
    except Exception as exc:
        return {"window_activated": False, "note": str(exc)}


def _is_allowed_raw_file_path(abs_p: str, task_id: Optional[str] = None) -> bool:
    """P0-0 Root Allowlist for /api/fs/raw_file: restrict strictly to task's project root (when task_id given) or active workspace & runtime dirs."""
    target_nc = os.path.normcase(os.path.realpath(os.path.abspath(abs_p)))
    snap = ORCHESTRATOR.store.snapshot()
    allowed_roots: List[str] = []
    if task_id:
        t_found = next((t for t in snap.get("tasks", []) if t.get("id") == task_id), None)
        if t_found and t_found.get("project_path"):
            allowed_roots.append(t_found["project_path"])
        elif snap.get("binding", {}).get("project_path"):
            allowed_roots.append(snap["binding"]["project_path"])
    else:
        if snap.get("binding", {}).get("project_path"):
            allowed_roots.append(snap["binding"]["project_path"])
        for t in snap.get("tasks", []):
            if t.get("project_path"):
                allowed_roots.append(t["project_path"])
    try:
        tmp_dir, _ = get_runtime_dirs()
        allowed_roots.append(tmp_dir)
    except Exception:
        pass
    for root in allowed_roots:
        if not root:
            continue
        root_nc = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        try:
            if os.path.commonpath([root_nc, target_nc]) == root_nc:
                return True
        except ValueError:
            continue
    return False


def _resolve_link_target(
    raw_target: str,
    task_id: Optional[str] = None,
    allow_basename_fallback: bool = True,
) -> Dict[str, Any]:
    raw = (raw_target or "").strip().strip("'\"")
    if not raw:
        raise ValueError("链接地址为空")

    if raw.lower().startswith(("http://", "https://")):
        return {"kind": "web", "url": raw}

    start_line: Optional[int] = None
    end_line: Optional[int] = None

    # Extract #L10-L20 fragment if present
    if "#" in raw:
        base_part, frag = raw.split("#", 1)
        m_frag = re.match(r"^L?(\d+)(?:-L?(\d+))?$", frag.strip(), re.I)
        if m_frag:
            start_line = int(m_frag.group(1))
            end_line = int(m_frag.group(2)) if m_frag.group(2) else start_line
        raw = base_part

    if raw.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        path_str = urllib.parse.unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path_str = urllib.parse.unquote(parsed.netloc) + path_str
        m_drv = re.match(r"^/+([A-Za-z]:[\\/].*)$", path_str)
        if m_drv:
            path_str = m_drv.group(1)
    else:
        path_str = urllib.parse.unquote(raw)
        m_drv = re.match(r"^/+([A-Za-z]:[\\/].*)$", path_str)
        if m_drv:
            path_str = m_drv.group(1)

    # Extract trailing :line[:end_line] if present (avoid matching drive letter C:\)
    m_col = re.match(r"^(.+\.[A-Za-z0-9_]+):(\d+)(?:-(\d+))?$", path_str)
    if m_col and start_line is None:
        path_str = m_col.group(1)
        start_line = int(m_col.group(2))
        end_line = int(m_col.group(3)) if m_col.group(3) else start_line

    snap = ORCHESTRATOR.store.snapshot()
    proj_root = snap["binding"]["project_path"]
    if task_id:
        t_found = next((t for t in snap.get("tasks", []) if t.get("id") == task_id), None)
        if t_found and t_found.get("project_path"):
            proj_root = t_found["project_path"]

    if os.path.isabs(path_str):
        abs_p = os.path.normpath(path_str)
    else:
        abs_p = os.path.normpath(os.path.join(proj_root, path_str))

    # Fallback filename / partial-subpath search within project_root (disabled when strict=1 for artifact QA!)
    if allow_basename_fallback and not os.path.exists(abs_p) and os.path.isdir(proj_root):
        target_base = os.path.basename(path_str).lower()
        norm_rel = path_str.replace("/", "\\").lstrip(".\\").lower()
        best_match: Optional[str] = None
        if target_base:
            for cur_r, dirs, files in os.walk(proj_root):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
                for fn in files:
                    if fn.lower() == target_base:
                        cand = os.path.normpath(os.path.join(cur_r, fn))
                        if norm_rel and cand.lower().endswith("\\" + norm_rel):
                            best_match = cand
                            break
                        if best_match is None:
                            best_match = cand
                if best_match and norm_rel and best_match.lower().endswith("\\" + norm_rel):
                    break
        if best_match:
            abs_p = best_match

    if not os.path.exists(abs_p):
        raise ValueError(f"未找到目标文件或目录: {abs_p}")

    return {
        "kind": "directory" if os.path.isdir(abs_p) else "file",
        "path": abs_p,
        "name": os.path.basename(abs_p) or abs_p,
        "start_line": start_line,
        "end_line": end_line,
    }


def _reveal_or_open_local_path(abs_p: str, reveal: bool = False) -> str:
    """Open a file/folder or reveal & highlight an item in OS file manager without CLI quoting bugs."""
    norm_p = os.path.normpath(os.path.abspath(abs_p))
    if not os.path.exists(norm_p):
        raise ValueError(f"未找到目标文件或目录: {norm_p}")

    if os.name == "nt":
        parent_p = os.path.dirname(norm_p)
        if reveal and parent_p and parent_p != norm_p:
            revealed_ok = False
            try:
                import ctypes

                ole32 = ctypes.windll.ole32
                shell32 = ctypes.windll.shell32

                ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                ole32.CoInitializeEx.restype = ctypes.c_long
                ole32.CoUninitialize.argtypes = []
                ole32.CoUninitialize.restype = None

                shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
                shell32.ILCreateFromPathW.restype = ctypes.c_void_p
                shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
                shell32.ILFindLastID.restype = ctypes.c_void_p
                shell32.ILFree.argtypes = [ctypes.c_void_p]
                shell32.ILFree.restype = None

                shell32.SHOpenFolderAndSelectItems.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.c_ulong,
                ]
                shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

                try:
                    ctypes.windll.user32.AllowSetForegroundWindow(ctypes.c_ulong(0xFFFFFFFF))
                except Exception:
                    pass

                hr_com = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
                need_uninit = hr_com in (0, 1)
                pidl_parent = None
                pidl_full = None
                try:
                    pidl_parent = shell32.ILCreateFromPathW(parent_p)
                    pidl_full = shell32.ILCreateFromPathW(norm_p)
                    if pidl_parent and pidl_full:
                        pidl_child = shell32.ILFindLastID(pidl_full)
                        if pidl_child:
                            child_arr = (ctypes.c_void_p * 1)(pidl_child)
                            hr_sel = shell32.SHOpenFolderAndSelectItems(pidl_parent, 1, child_arr, 0)
                            if hr_sel == 0:
                                revealed_ok = True
                finally:
                    if pidl_full:
                        shell32.ILFree(pidl_full)
                    if pidl_parent:
                        shell32.ILFree(pidl_parent)
                    if need_uninit:
                        ole32.CoUninitialize()
            except Exception:
                revealed_ok = False

            if not revealed_ok:
                # Pass a raw command string (NOT a list) so "/select," is never wrapped inside quotes!
                subprocess.Popen(f'explorer.exe /select,"{norm_p}"')
        else:
            os.startfile(norm_p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", norm_p] if reveal else ["open", norm_p])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(norm_p) if (reveal and os.path.isfile(norm_p)) else norm_p])

    return norm_p


def _extract_docx_text(docx_path: str, max_chars: int = 45000) -> str:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    blocks: List[str] = []
    total = 0
    with zipfile.ZipFile(docx_path, "r") as zf:
        if "word/document.xml" not in zf.namelist():
            return "（未能读取 Word 正文结构，请点击上方“用系统默认程序打开”查看原件）"
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    body = root.find("w:body", ns)
    if body is None:
        return ""
    for child in body:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            texts = [t.text for t in child.findall(".//w:t", ns) if t.text]
            para = "".join(texts).strip()
            if para:
                blocks.append(para)
                total += len(para)
        elif tag == "tbl":
            t_rows = []
            for row_i, tr in enumerate(child.findall(".//w:tr", ns)):
                cells = []
                for tc in tr.findall("./w:tc", ns):
                    c_txt = " ".join("".join(t.text for t in p.findall(".//w:t", ns) if t.text).strip() for p in tc.findall(".//w:p", ns)).strip()
                    cells.append(c_txt.replace("|", "｜"))
                if cells:
                    t_rows.append("| " + " | ".join(cells) + " |")
                    if row_i == 0:
                        t_rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
            if t_rows:
                tbl_md = "\n".join(t_rows)
                blocks.append(tbl_md)
                total += len(tbl_md)
        if total >= max_chars:
            blocks.append("\n*...（文档较长，预览仅展示前段内容，点击右上角“用本地软件打开”可查阅完整排版的 Word 原件）*")
            break
    return "\n\n".join(blocks)


def _read_text_auto_encoding(file_path: str, max_bytes: int = 180000) -> Tuple[str, bool]:
    size = os.path.getsize(file_path)
    truncated = size > max_bytes
    with open(file_path, "rb") as f:
        raw_bytes = f.read(max_bytes)
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw_bytes.decode(enc), truncated
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace"), truncated


def _build_file_preview(resolved: Dict[str, Any]) -> Dict[str, Any]:
    if resolved["kind"] == "web":
        return {"ok": True, "preview_type": "web", "url": resolved["url"]}

    abs_p = resolved["path"]
    if resolved["kind"] == "directory":
        entries = []
        for name in sorted(os.listdir(abs_p))[:120]:
            fp = os.path.join(abs_p, name)
            entries.append({
                "name": name,
                "path": fp,
                "is_dir": os.path.isdir(fp),
                "size": 0 if os.path.isdir(fp) else os.path.getsize(fp),
            })
        return {
            "ok": True,
            "preview_type": "directory",
            "path": abs_p,
            "name": resolved["name"],
            "entries": entries,
        }

    ext = os.path.splitext(abs_p)[1].lower()
    size = os.path.getsize(abs_p)
    base_info = {
        "ok": True,
        "path": abs_p,
        "name": resolved["name"],
        "ext": ext,
        "size": size,
        "start_line": resolved.get("start_line"),
        "end_line": resolved.get("end_line"),
    }

    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
        return {
            **base_info,
            "preview_type": "image",
            "inspect": inspect_any_figure(abs_p) if ext in (".png", ".svg") else None,
            "raw_url": f"/api/fs/raw_file?path={urllib.parse.quote(abs_p)}",
        }
    if ext in (".tif", ".tiff"):
        return {
            **base_info,
            "preview_type": "tiff",
            "inspect": inspect_any_figure(abs_p),
            "raw_url": f"/api/fs/raw_file?path={urllib.parse.quote(abs_p)}",
        }
    if ext == ".pdf":
        return {
            **base_info,
            "preview_type": "pdf",
            "inspect": inspect_any_figure(abs_p),
            "raw_url": f"/api/fs/raw_file?path={urllib.parse.quote(abs_p)}",
        }
    if ext == ".docx":
        try:
            docx_md = _extract_docx_text(abs_p)
            return {**base_info, "preview_type": "docx", "content": docx_md}
        except Exception as exc:
            return {**base_info, "preview_type": "binary", "note": f"Word 预览解析提示 ({exc})，请点击按钮用本地 Word 打开"}

    text_exts = {
        ".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".py", ".r", ".rmd",
        ".tex", ".bib", ".ris", ".enw", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log",
        ".html", ".htm", ".css", ".js", ".ts", ".sql", ".xml", ".cmd", ".bat", ".ps1", ".sh",
    }
    if ext in text_exts or size < 64000:
        try:
            txt, truncated = _read_text_auto_encoding(abs_p)
            if "\x00" in txt[:512]:
                return {**base_info, "preview_type": "binary", "note": "该文件为二进制/专用格式，请点击上方按钮用系统关联软件打开"}
            if ext in (".csv", ".tsv") and not resolved.get("start_line"):
                delim = "\t" if ext == ".tsv" else ","
                reader = csv.reader(io.StringIO(txt), delimiter=delim)
                max_cols = 60
                max_rows = 100
                rows: List[List[str]] = []
                total_cols = 0
                col_truncated = False
                row_truncated = False
                for r in reader:
                    if len(rows) >= max_rows:
                        row_truncated = True
                        break
                    total_cols = max(total_cols, len(r))
                    if len(r) > max_cols:
                        col_truncated = True
                    rows.append(r[:max_cols])
                return {
                    **base_info,
                    "preview_type": "table",
                    "rows": rows,
                    "total_cols": total_cols,
                    "shown_cols": min(total_cols, max_cols),
                    "col_truncated": col_truncated,
                    "row_truncated": row_truncated or truncated,
                    "truncated": truncated or col_truncated or row_truncated,
                }
            return {
                **base_info,
                "preview_type": "markdown" if (ext in (".md", ".markdown") and not resolved.get("start_line")) else "text",
                "content": txt,
                "truncated": truncated,
            }
        except Exception:
            pass

    return {**base_info, "preview_type": "binary", "note": "该文件为专用格式（如 Excel/PPT/统计数据包），可一键调用本机软件打开"}


class SidebarHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send_raw_bytes(
        self,
        status_code: int,
        raw_body: bytes,
        content_type: str,
        gz_body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        use_gz = self._accepts_gzip() and (gz_body is not None or len(raw_body) > 2048)
        payload = (gz_body if gz_body is not None else gzip.compress(raw_body, compresslevel=4)) if use_gz else raw_body
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if extra_headers:
            for hk, hv in extra_headers.items():
                self.send_header(hk, hv)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status_code: int, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_raw_bytes(status_code, body, "application/json; charset=utf-8", extra_headers=extra_headers)

    def _check_local_post_security(self) -> Optional[str]:
        """P0-0 Local CSRF / Host / Origin / Content-Type guard for loopback write endpoints."""
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        host_hdr = (self.headers.get("Host") or "").strip()
        if host_hdr:
            parsed_h = urllib.parse.urlparse(f"http://{host_hdr}")
            if (parsed_h.hostname or "").lower() not in loopback_hosts:
                return f"安全拦截：拒绝非本机 Host ({host_hdr})"
        origin_hdr = (self.headers.get("Origin") or "").strip()
        if origin_hdr:
            parsed_o = urllib.parse.urlparse(origin_hdr)
            if parsed_o.scheme not in ("http", "https") or (parsed_o.hostname or "").lower() not in loopback_hosts:
                return f"CSRF 安全拦截：拒绝跨站 Origin ({origin_hdr})"
        length = int(self.headers.get("Content-Length", "0") or "0")
        ct = (self.headers.get("Content-Type") or "").strip().lower()
        if length > 0 and (origin_hdr or ct):
            if "application/json" not in ct:
                return f"CSRF 安全拦截：POST 写请求必须使用 application/json (收到 {ct or 'empty'})"
        return None

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if route in ("/", "/index.html"):
                html_path = os.path.join(PROTO_DIR, "sidebar_panel.html")
                with open(html_path, "rb") as f:
                    content = f.read()
                self._send_raw_bytes(200, content, "text/html; charset=utf-8")
            elif route == "/favicon.ico":
                self.send_response(204)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif route == "/api/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "schema_version": 4,
                        "app_version": APP_VERSION,
                        "server_rev": SERVER_REV,
                        "state_rev": ORCHESTRATOR.store.get_rev(),
                        "repo": _LOCAL_VER.get("repo", ""),
                    },
                )
            elif route == "/api/version/check":
                force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
                self._send_json(200, check_for_updates(force=force))
            elif route == "/api/memory/search":
                q = qs.get("q", [""])[0]
                tid = qs.get("task_id", [None])[0]
                lim = int(qs.get("limit", ["8"])[0] or "8")
                self._send_json(200, {"ok": True, **ORCHESTRATOR.search_project_memory(q, task_id=tid, limit=lim)})
            elif route == "/api/memory/stats":
                self._send_json(200, {"ok": True, **ORCHESTRATOR.get_memory_stats()})
            elif route == "/api/tasks/worktrees":
                tid = qs.get("task_id", [""])[0]
                if not tid:
                    raise ValueError("缺少 task_id 参数")
                self._send_json(200, {"ok": True, **ORCHESTRATOR.get_task_worktrees(tid)})
            elif route == "/api/state":
                slim = qs.get("slim", ["0"])[0] in ("1", "true", "yes")
                focus_id = (qs.get("focus_task_id", [""])[0] or "").strip() or None
                since_raw = (qs.get("since_rev", [""])[0] or "").strip()
                cur_rev = ORCHESTRATOR.store.get_rev()
                etag = f'"rev-{cur_rev}-{"s" if slim else "f"}-{focus_id or "auto"}"'
                inm = (self.headers.get("If-None-Match") or "").strip()
                if (since_raw.isdigit() and int(since_raw) == cur_rev) or (inm and inm == etag):
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("X-State-Rev", str(cur_rev))
                    self.send_header("X-App-Version", APP_VERSION)
                    self.send_header("X-Server-Rev", str(SERVER_REV))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                cache_key = (cur_rev, slim, focus_id)
                cached_pair: Optional[Tuple[bytes, bytes]] = None
                with _STATE_CACHE_LOCK:
                    cached_pair = _STATE_HTTP_CACHE.get(cache_key)
                if cached_pair is None:
                    if slim:
                        eff_rev, payload = ORCHESTRATOR.store.slim_snapshot(focus_task_id=focus_id)
                    else:
                        payload = ORCHESTRATOR.store.snapshot()
                        eff_rev = int(payload.get("state_rev") or cur_rev)
                    raw_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    gz_bytes = gzip.compress(raw_bytes, compresslevel=4)
                    cached_pair = (raw_bytes, gz_bytes)
                    cur_rev = eff_rev
                    etag = f'"rev-{cur_rev}-{"s" if slim else "f"}-{focus_id or "auto"}"'
                    with _STATE_CACHE_LOCK:
                        # Evict stale revisions so cache stays tiny (<= 4 entries)
                        for k in list(_STATE_HTTP_CACHE.keys()):
                            if k[0] != cur_rev or len(_STATE_HTTP_CACHE) >= 4:
                                _STATE_HTTP_CACHE.pop(k, None)
                        _STATE_HTTP_CACHE[(cur_rev, slim, focus_id)] = cached_pair
                self._send_raw_bytes(
                    200,
                    cached_pair[0],
                    "application/json; charset=utf-8",
                    gz_body=cached_pair[1],
                    extra_headers={
                        "ETag": etag,
                        "X-State-Rev": str(cur_rev),
                        "X-App-Version": APP_VERSION,
                        "X-Server-Rev": str(SERVER_REV),
                    },
                )
            elif route == "/api/models":
                refresh = qs.get("refresh", ["0"])[0] == "1"
                self._send_json(200, ORCHESTRATOR.get_capabilities(refresh_cli=refresh))
            elif route == "/api/specialists/catalog":
                self._send_json(200, ORCHESTRATOR.get_specialist_catalog())
            elif route == "/api/openscience/playbooks":
                self._send_json(200, {"ok": True, "playbooks": ORCHESTRATOR.get_specialist_catalog().get("openscience_playbooks", [])})
            elif route == "/api/tasks/provenance":
                tid = qs.get("task_id", [""])[0]
                if not tid:
                    raise ValueError("缺少 task_id 参数")
                self._send_json(200, {"ok": True, **ORCHESTRATOR.get_task_provenance(tid)})
            elif route == "/api/members/backup_candidates":
                mid = qs.get("member_id", [""])[0]
                if not mid:
                    raise ValueError("缺少 member_id 参数")
                req_same = qs.get("require_same_client", ["0"])[0] in ("1", "true", "yes")
                pref_cross = qs.get("prefer_cross_engine", ["0"])[0] in ("1", "true", "yes")
                self._send_json(
                    200,
                    {
                        "ok": True,
                        **ORCHESTRATOR.list_backup_candidates(
                            mid, require_same_client=req_same, prefer_cross_engine=pref_cross
                        ),
                    },
                )
            elif route == "/api/env/plotting_backends":
                refresh = qs.get("refresh", ["0"])[0] == "1"
                self._send_json(200, {"ok": True, **ORCHESTRATOR.get_plotting_backends(refresh=refresh)})
            elif route == "/api/tasks/artifacts":
                tid = qs.get("task_id", [""])[0]
                if not tid:
                    raise ValueError("缺少 task_id 参数")
                self._send_json(200, {"ok": True, **ORCHESTRATOR.get_task_artifacts(tid)})
            elif route == "/api/projects":
                self._send_json(200, ORCHESTRATOR.list_projects())
            elif route == "/api/fs/browse":
                target = qs.get("path", [None])[0]
                self._send_json(200, ORCHESTRATOR.browse_fs(target))
            elif route == "/api/fs/preview_file":
                target = qs.get("target", [""])[0]
                task_id = qs.get("task_id", [None])[0]
                strict = qs.get("strict", ["0"])[0] in ("1", "true", "yes")
                resolved = _resolve_link_target(target, task_id=task_id, allow_basename_fallback=not strict)
                self._send_json(200, _build_file_preview(resolved))
            elif route == "/api/fs/raw_file":
                raw_p = qs.get("path", [""])[0]
                task_id = qs.get("task_id", [None])[0]
                resolved = _resolve_link_target(raw_p, task_id=task_id, allow_basename_fallback=False)
                if resolved["kind"] != "file":
                    raise ValueError("仅支持读取普通文件")
                abs_p = resolved["path"]
                if not _is_allowed_raw_file_path(abs_p, task_id=task_id):
                    raise ValueError(f"安全拦截：拒绝读取当前任务绑定项目目录之外的文件: {abs_p}")
                ext_low = os.path.splitext(abs_p)[1].lower()
                mime = "image/svg+xml" if ext_low == ".svg" else (mimetypes.guess_type(abs_p)[0] or "application/octet-stream")
                with open(abs_p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("X-Content-Type-Options", "nosniff")
                if ext_low == ".svg":
                    self.send_header("Content-Security-Policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json(404, {"error": f"Not found: {route}"})
        except ValueError as ve:
            self._send_json(400, {"ok": False, "error": str(ve)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        sec_err = self._check_local_post_security()
        if sec_err:
            self._send_json(403, {"ok": False, "error": sec_err})
            return

        try:
            body = self._read_json_body()
            if route == "/api/toggle_pause":
                cur = ORCHESTRATOR.store.snapshot().get("paused", False)
                self._send_json(200, ORCHESTRATOR.set_pause(not cur))
            elif route == "/api/pause":
                self._send_json(200, ORCHESTRATOR.set_pause(bool(body.get("paused", True))))
            elif route == "/api/coordinator/heartbeat":
                ORCHESTRATOR.touch_coordinator_heartbeat(body.get("source", "antigravity_native"))
                self._send_json(200, {"ok": True})
            elif route == "/api/focus_antigravity":
                self._send_json(200, _focus_antigravity_window())
            elif route == "/api/projects/switch":
                res = ORCHESTRATOR.switch_project(body["project_path"])
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/fs/pick_native":
                res = ORCHESTRATOR.pick_project_native()
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/fs/pick_dir":
                res = ORCHESTRATOR.pick_dir_native(body.get("initial_path") or None)
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/fs/open_explorer":
                proj_p = body.get("path") or ORCHESTRATOR.store.snapshot()["binding"]["project_path"]
                if os.path.isdir(proj_p):
                    opened_p = _reveal_or_open_local_path(proj_p, reveal=False)
                    self._send_json(200, {"ok": True, "opened": opened_p})
                else:
                    raise ValueError(f"目录不存在: {proj_p}")
            elif route == "/api/fs/open_item":
                resolved = _resolve_link_target(body.get("target", ""), task_id=body.get("task_id"))
                reveal = bool(body.get("reveal", False))
                if resolved["kind"] == "web":
                    webbrowser.open(resolved["url"])
                    self._send_json(200, {"ok": True, "opened": resolved["url"], "kind": "web"})
                else:
                    opened_p = _reveal_or_open_local_path(resolved["path"], reveal=reveal)
                    self._send_json(200, {"ok": True, "opened": opened_p, "kind": resolved["kind"], "name": resolved["name"]})
            elif route == "/api/binding":
                res = ORCHESTRATOR.update_binding(
                    project_path=body.get("project_path"),
                    remote_control_url=body.get("remote_control_url"),
                    max_concurrency=body.get("max_concurrency"),
                    default_max_auto_repairs=body.get("default_max_auto_repairs"),
                    auto_spawn_quota_backup=body.get("auto_spawn_quota_backup"),
                    context_budget_chars=body.get("context_budget_chars"),
                    context_session_mode=body.get("context_session_mode"),
                    auto_context_rollover=body.get("auto_context_rollover"),
                    max_turns_per_session=body.get("max_turns_per_session"),
                    enable_cross_task_memory=body.get("enable_cross_task_memory"),
                    enable_ast_repomap=body.get("enable_ast_repomap"),
                    enable_git_worktree_isolation=body.get("enable_git_worktree_isolation"),
                    task_token_budget=body.get("task_token_budget"),
                    max_sandbox=body.get("max_sandbox"),
                    default_task_sandbox=body.get("default_task_sandbox"),
                    roundtable_allow_member_writes=body.get("roundtable_allow_member_writes"),
                    allow_danger_full_access=body.get("allow_danger_full_access"),
                    extra_add_dirs=body.get("extra_add_dirs"),
                )
                self._send_json(200, {"ok": True, "binding": res})
            elif route == "/api/roles":
                res = ORCHESTRATOR.upsert_role(
                    role_id=body["id"],
                    name=body["name"],
                    description=body.get("description", ""),
                    set_as_coordinator=bool(body.get("set_as_coordinator", False)),
                )
                self._send_json(200, {"ok": True, "role": res})
            elif route == "/api/roles/delete":
                res = ORCHESTRATOR.delete_role(body["id"])
                self._send_json(200, {"ok": True, "deleted": res})
            elif route == "/api/members":
                res = ORCHESTRATOR.upsert_member(
                    member_id=body["id"],
                    role_id=body["role"],
                    name=body.get("name"),
                    client=body.get("client", "codex"),
                    model=body.get("model"),
                    reasoning_effort=body.get("reasoning_effort", "medium"),
                    workspace_scope=body.get("workspace_scope", "."),
                    sandbox=body.get("sandbox", "read-only"),
                    specialty=body.get("specialty"),
                    persona_prompt=body.get("persona_prompt"),
                    grounding_mode=body.get("grounding_mode"),
                    tags=body.get("tags"),
                )
                self._send_json(200, {"ok": True, "member": res})
            elif route == "/api/members/delete":
                res = ORCHESTRATOR.delete_member(body["id"])
                self._send_json(200, {"ok": True, "deleted": res})
            elif route == "/api/members/switch":
                res = ORCHESTRATOR.switch_member_config(
                    member_id=body["id"],
                    name=body.get("name"),
                    client=body.get("client"),
                    role_id=body.get("role"),
                    model=body.get("model"),
                    reasoning_effort=body.get("reasoning_effort"),
                    workspace_scope=body.get("workspace_scope"),
                    sandbox=body.get("sandbox"),
                    specialty=body.get("specialty"),
                    persona_prompt=body.get("persona_prompt"),
                    grounding_mode=body.get("grounding_mode"),
                    tags=body.get("tags"),
                    mode=body.get("mode", "next_round"),
                    reason=body.get("reason", "界面编辑成员名称/配置"),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/members/set_sandbox":
                res = ORCHESTRATOR.set_members_sandbox(
                    sandbox=body.get("sandbox", "workspace-write"),
                    member_id=body.get("member_id") or None,
                    client=body.get("client") or None,
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/members/spawn_backup":
                res = ORCHESTRATOR.spawn_quota_backup_member(
                    source_member_id=body["source_member_id"],
                    new_client=body.get("client") or None,
                    new_model=body.get("model") or None,
                    new_effort=body.get("reasoning_effort") or None,
                    prefer_cross_engine=bool(body.get("prefer_cross_engine", False)),
                    task_id=body.get("task_id") or None,
                    step_index=body.get("step_index") if body.get("step_index") is not None else None,
                    auto_reassign=bool(body.get("auto_reassign", False)),
                    bind_as_default=bool(body.get("bind_as_default", True)),
                    reason=body.get("reason", "模型额度预警，补充同角色跨模型替补"),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/members/bind_backup":
                res = ORCHESTRATOR.bind_quota_backup(
                    source_member_id=body["source_member_id"],
                    backup_member_id=body.get("backup_member_id") or None,
                    backup_client=body.get("client") or None,
                    backup_model=body.get("model") or None,
                    backup_effort=body.get("reasoning_effort") or None,
                    prefer_cross_engine=bool(body.get("prefer_cross_engine", False)),
                    clear=bool(body.get("clear", False)),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/members/auto_bind_backups":
                res = ORCHESTRATOR.auto_bind_all_quota_backups(
                    prefer_cross_engine=bool(body.get("prefer_cross_engine", False)),
                    member_ids=body.get("member_ids") or None,
                    overwrite=bool(body.get("overwrite", False)),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/specialists/optimize_roster":
                res = ORCHESTRATOR.optimize_roster(
                    apply_names=bool(body.get("apply_names", True)),
                    refresh_cli=bool(body.get("refresh_cli", False)),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks":
                res = ORCHESTRATOR.create_task(
                    title=body.get("title", ""),
                    goal=body["goal"],
                    dispatch_mode=body.get("dispatch_mode", "single_member"),
                    execution_mode=body.get("execution_mode"),
                    target_member_id=body.get("target_member_id", "codex_author"),
                    reviewer_member_id=body.get("reviewer_member_id", "agy_claude_reviewer"),
                    chief_member_id=body.get("chief_member_id"),
                    host_member_id=body.get("host_member_id"),
                    participants=body.get("participants"),
                    discussion_rounds=int(body.get("discussion_rounds", 2)),
                    enable_host_opening=bool(body.get("enable_host_opening", False)),
                    stages=body.get("stages"),
                    auto_template=body.get("auto_template", "standard_review_loop"),
                    steps=body.get("steps"),
                    project_path=body.get("project_path"),
                    workspace_scope=body.get("workspace_scope"),
                    sandbox=body.get("sandbox"),
                    add_dirs=body.get("add_dirs"),
                    seat_sandboxes=body.get("seat_sandboxes"),
                    max_auto_repairs=body.get("max_auto_repairs"),
                    require_manual_ruling=bool(body.get("require_manual_ruling", False)),
                    reuse_member_session=bool(body.get("reuse_member_session", False)),
                    research_spec=body.get("research_spec"),
                    openscience_playbooks=body.get("openscience_playbooks"),
                    squad_preset=str(body.get("squad_preset") or ""),
                    chief_max_members=body.get("chief_max_members"),
                    anonymous_peer_review=bool(body.get("anonymous_peer_review", False)),
                    enable_worktree_isolation=body.get("enable_worktree_isolation"),
                    task_token_budget=body.get("task_token_budget"),
                    allow_member_writes=body.get("allow_member_writes"),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/worktrees/merge":
                res = ORCHESTRATOR.merge_task_worktree(
                    task_id=body["task_id"],
                    step_index=int(body["step_index"]),
                    cleanup_after=bool(body.get("cleanup_after", True)),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/worktrees/cleanup":
                res = ORCHESTRATOR.cleanup_task_worktree(
                    task_id=body["task_id"],
                    step_index=int(body["step_index"]),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/verify_figure":
                res = ORCHESTRATOR.verify_task_figure(
                    task_id=body["task_id"],
                    manifest_path=body["manifest_path"],
                )
                self._send_json(200, {"ok": True, "report": res})
            elif route == "/api/tasks/compact_context":
                res = ORCHESTRATOR.compact_task_context(
                    task_id=body["task_id"],
                    reset_sessions=bool(body.get("reset_sessions", True)),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/interject":
                res = ORCHESTRATOR.interject_chat(
                    task_id=body["task_id"],
                    content=body["content"],
                    target_member_id=body.get("target_member_id") or None,
                    trigger_reply=bool(body.get("trigger_reply", True)),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/fact_check":
                res = ORCHESTRATOR.fact_check_task(
                    task_id=body["task_id"],
                    checker_member_id=body.get("checker_member_id") or None,
                    target_chat_id=body.get("target_chat_id") or None,
                    focus_note=body.get("focus_note", ""),
                    auto_revise=bool(body.get("auto_revise", False)),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/revise_from_audit":
                res = ORCHESTRATOR.revise_from_fact_check(
                    task_id=body["task_id"],
                    editor_member_id=body.get("editor_member_id") or None,
                    extra_note=body.get("extra_note", ""),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/reanswer":
                res = ORCHESTRATOR.reanswer_with_specialist(
                    task_id=body["task_id"],
                    new_member_id=body["new_member_id"],
                    target_chat_id=body.get("target_chat_id") or None,
                    instruction=body.get("instruction", ""),
                    as_deliverable=bool(body.get("as_deliverable", True)),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/focus":
                res = ORCHESTRATOR.set_focus_task(body.get("task_id"))
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/archive":
                res = ORCHESTRATOR.archive_task(body["task_id"], bool(body.get("archived", True)))
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/archive_completed":
                res = ORCHESTRATOR.archive_completed_tasks(bool(body.get("keep_active", False)))
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/plan":
                res = ORCHESTRATOR.attach_plan(
                    task_id=body["task_id"],
                    steps=body["steps"],
                    coordinator_note=body.get("coordinator_note", ""),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/followup":
                res = ORCHESTRATOR.followup_task(
                    task_id=body["task_id"],
                    prompt=body["prompt"],
                    step_index=body.get("step_index"),
                    target_member_id=body.get("target_member_id") or None,
                    sandbox=body.get("sandbox") or None,
                    title=body.get("title") or None,
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/answer":
                res = ORCHESTRATOR.answer_questions(
                    task_id=body["task_id"],
                    answers=body.get("answers"),
                    custom_note=body.get("custom_note", ""),
                    use_recommended=bool(body.get("use_recommended", False)),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/message":
                res = ORCHESTRATOR.post_message(
                    task_id=body["task_id"],
                    sender_id=body.get("sender_id", "user"),
                    msg_type=body.get("msg_type", "question"),
                    content=body["content"],
                    step_index=body.get("step_index"),
                    require_ruling=bool(body.get("require_ruling", False)),
                )
                self._send_json(200, {"ok": True, "message": res})
            elif route == "/api/tasks/ruling":
                res = ORCHESTRATOR.submit_ruling(
                    task_id=body["task_id"],
                    step_index=int(body["step_index"]),
                    decision=body["decision"],
                    reason=body["reason"],
                    repair_instructions=body.get("repair_instructions"),
                    decided_by=body.get("decided_by", "ag_coordinator"),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/reassign":
                res = ORCHESTRATOR.reassign_step(
                    task_id=body["task_id"],
                    step_index=int(body["step_index"]),
                    new_member_id=body["new_member_id"],
                    reason=body.get("reason", "界面改派成员接手"),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/swap_seat":
                res = ORCHESTRATOR.swap_task_seat(
                    task_id=body["task_id"],
                    old_member_id=body["old_member_id"],
                    new_member_id=body.get("new_member_id") or None,
                    spawn_backup_client=body.get("spawn_backup_client") or None,
                    spawn_backup_model=body.get("spawn_backup_model") or None,
                    spawn_backup_effort=body.get("spawn_backup_effort") or None,
                    abort_running=bool(body.get("abort_running", True)),
                    trigger_fresh_turn=bool(body.get("trigger_fresh_turn", False)),
                    bind_as_quota_backup=bool(body.get("bind_as_quota_backup", False)),
                    reason=body.get("reason", "现场快速换人"),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/add_seat":
                res = ORCHESTRATOR.add_task_seat(
                    task_id=body["task_id"],
                    member_id=body["member_id"],
                    trigger_turn=bool(body.get("trigger_turn", True)),
                    instruction=body.get("instruction", ""),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/abort":
                res = ORCHESTRATOR.abort_attempt(
                    task_id=body.get("task_id"),
                    step_index=body.get("step_index"),
                    attempt_id=body.get("attempt_id"),
                    reason=body.get("reason", "用户点击中止尝试"),
                )
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/continue":
                res = ORCHESTRATOR.continue_task(task_id=body["task_id"])
                self._send_json(200, {"ok": True, **res})
            elif route == "/api/tasks/accept_partial":
                res = ORCHESTRATOR.accept_step_partial(
                    task_id=body["task_id"],
                    step_index=int(body["step_index"]),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/retry_step":
                res = ORCHESTRATOR.retry_single_step(
                    task_id=body["task_id"],
                    step_index=int(body["step_index"]),
                    reset_session=bool(body.get("reset_session", True)),
                    new_member_id=body.get("new_member_id") or None,
                )
                self._send_json(200, {"ok": True, "task": res})
            else:
                self._send_json(404, {"error": f"Unknown endpoint: {route}"})
        except ValueError as ve:
            self._send_json(400, {"ok": False, "error": str(ve)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})


def run_server(port: int = PORT) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), SidebarHandler)
    print(f"Sidebar Panel v4 running at http://127.0.0.1:{port} (PID={os.getpid()})", flush=True)
    try:
        server.serve_forever()
    finally:
        ORCHESTRATOR.stop_scheduler()


if __name__ == "__main__":
    run_server()
