"""
Multi-threaded HTTP Server & REST API v3 for Dual-Client (Codex + Antigravity)
& Multi-Project Collaboration Workspace.
"""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
if PROTO_DIR not in sys.path:
    sys.path.insert(0, PROTO_DIR)

from codex_adapter import CREATE_NO_WINDOW
from team_runtime import TeamOrchestrator

PORT = int(os.environ.get("TEAM_SIDEBAR_PORT", "8765"))
ORCHESTRATOR = TeamOrchestrator()


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
            creationflags=CREATE_NO_WINDOW,
            timeout=4,
        )
        activated = "true" in (p.stdout or "").lower()
        return {
            "window_activated": activated,
            "note": "已前置 Antigravity 桌面窗口" if activated else "请手动切回 Antigravity 窗口",
        }
    except Exception as exc:
        return {"window_activated": False, "note": str(exc)}


def _resolve_link_target(raw_target: str, task_id: Optional[str] = None) -> Dict[str, Any]:
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

    # Fallback filename search within project_root if agent wrote a bare filename or partial relative path
    if not os.path.exists(abs_p) and os.path.isdir(proj_root):
        target_base = os.path.basename(path_str).lower()
        if target_base:
            for cur_r, dirs, files in os.walk(proj_root):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
                for fn in files:
                    if fn.lower() == target_base:
                        abs_p = os.path.normpath(os.path.join(cur_r, fn))
                        break
                if os.path.exists(abs_p):
                    break

    if not os.path.exists(abs_p):
        raise ValueError(f"未找到目标文件或目录: {abs_p}")

    return {
        "kind": "directory" if os.path.isdir(abs_p) else "file",
        "path": abs_p,
        "name": os.path.basename(abs_p) or abs_p,
        "start_line": start_line,
        "end_line": end_line,
    }


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
        return {**base_info, "preview_type": "image", "raw_url": f"/api/fs/raw_file?path={urllib.parse.quote(abs_p)}"}
    if ext == ".pdf":
        return {**base_info, "preview_type": "pdf", "raw_url": f"/api/fs/raw_file?path={urllib.parse.quote(abs_p)}"}
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
                rows = []
                for idx, r in enumerate(reader):
                    rows.append(r[:24])
                    if idx >= 80:
                        break
                return {**base_info, "preview_type": "table", "rows": rows, "truncated": truncated or len(rows) > 80}
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

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif route == "/api/health":
                self._send_json(200, {"ok": True, "pid": os.getpid(), "schema_version": 4, "server_rev": 6})
            elif route == "/api/state":
                self._send_json(200, ORCHESTRATOR.store.snapshot())
            elif route == "/api/models":
                refresh = qs.get("refresh", ["0"])[0] == "1"
                self._send_json(200, ORCHESTRATOR.get_capabilities(refresh_cli=refresh))
            elif route == "/api/projects":
                self._send_json(200, ORCHESTRATOR.list_projects())
            elif route == "/api/fs/browse":
                target = qs.get("path", [None])[0]
                self._send_json(200, ORCHESTRATOR.browse_fs(target))
            elif route == "/api/fs/preview_file":
                target = qs.get("target", [""])[0]
                task_id = qs.get("task_id", [None])[0]
                resolved = _resolve_link_target(target, task_id=task_id)
                self._send_json(200, _build_file_preview(resolved))
            elif route == "/api/fs/raw_file":
                raw_p = qs.get("path", [""])[0]
                resolved = _resolve_link_target(raw_p)
                if resolved["kind"] != "file":
                    raise ValueError("仅支持读取普通文件")
                abs_p = resolved["path"]
                mime, _ = mimetypes.guess_type(abs_p)
                with open(abs_p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
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
            elif route == "/api/fs/open_explorer":
                proj_p = body.get("path") or ORCHESTRATOR.store.snapshot()["binding"]["project_path"]
                if os.path.isdir(proj_p):
                    subprocess.Popen(["explorer.exe", os.path.normpath(proj_p)])
                    self._send_json(200, {"ok": True})
                else:
                    raise ValueError(f"目录不存在: {proj_p}")
            elif route == "/api/fs/open_item":
                resolved = _resolve_link_target(body.get("target", ""), task_id=body.get("task_id"))
                reveal = bool(body.get("reveal", False))
                if resolved["kind"] == "web":
                    webbrowser.open(resolved["url"])
                    self._send_json(200, {"ok": True, "opened": resolved["url"], "kind": "web"})
                else:
                    abs_p = resolved["path"]
                    if os.name == "nt":
                        if reveal and os.path.isfile(abs_p):
                            subprocess.Popen(["explorer.exe", f"/select,{abs_p}"])
                        elif os.path.isdir(abs_p):
                            subprocess.Popen(["explorer.exe", abs_p])
                        else:
                            os.startfile(abs_p)  # type: ignore[attr-defined]
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", abs_p] if (reveal and os.path.isfile(abs_p)) else ["open", abs_p])
                    else:
                        subprocess.Popen(["xdg-open", os.path.dirname(abs_p) if (reveal and os.path.isfile(abs_p)) else abs_p])
                    self._send_json(200, {"ok": True, "opened": abs_p, "kind": resolved["kind"], "name": resolved["name"]})
            elif route == "/api/binding":
                res = ORCHESTRATOR.update_binding(
                    project_path=body.get("project_path"),
                    remote_control_url=body.get("remote_control_url"),
                    max_concurrency=body.get("max_concurrency"),
                    default_max_auto_repairs=body.get("default_max_auto_repairs"),
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
                    mode=body.get("mode", "next_round"),
                    reason=body.get("reason", "界面编辑成员名称/配置"),
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
                    sandbox=body.get("sandbox", "read-only"),
                    max_auto_repairs=body.get("max_auto_repairs"),
                    require_manual_ruling=bool(body.get("require_manual_ruling", False)),
                    reuse_member_session=bool(body.get("reuse_member_session", False)),
                )
                self._send_json(200, {"ok": True, "task": res})
            elif route == "/api/tasks/interject":
                res = ORCHESTRATOR.interject_chat(
                    task_id=body["task_id"],
                    content=body["content"],
                    target_member_id=body.get("target_member_id") or None,
                    trigger_reply=bool(body.get("trigger_reply", True)),
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
