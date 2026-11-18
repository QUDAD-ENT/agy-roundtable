"""
Coordinator CLI & Client SDK for Antigravity-Codex Team Runtime.
Auto-connects to (or starts) the local runtime server on 127.0.0.1:8765 so that
Antigravity Coordinator and the Sidebar Panel share the exact same scheduler & PID registry.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from codex_adapter import get_runtime_dirs

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE_URL = os.environ.get("TEAM_RUNTIME_URL", "http://127.0.0.1:8765")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) if os.name == "nt" else 0


class TeamRuntimeClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, auto_start: bool = True):
        self.base_url = base_url.rstrip("/")
        self.auto_start = auto_start

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Dict[str, Any]:
        if self.auto_start and path != "/api/health":
            self.ensure_server_running()
        url = f"{self.base_url}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(err_body)
                raise RuntimeError(parsed.get("error") or err_body) from he
            except json.JSONDecodeError:
                raise RuntimeError(f"HTTP {he.code}: {err_body}") from he

    def is_healthy(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        try:
            req = urllib.request.Request(f"{self.base_url}/api/health", method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok")) and int(data.get("schema_version", 0)) >= 4 and int(data.get("server_rev", 0)) >= 8, data
        except Exception:
            return False, None

    def ensure_server_running(self) -> Dict[str, Any]:
        ok, info = self.is_healthy()
        if ok and info:
            return info
        server_py = os.path.join(PROTO_DIR, "serve_sidebar.py")
        py_candidates = [
            os.environ.get("AGY_ROUNDTABLE_PYTHON"),
            r"D:\PYTHON\python.exe" if os.name == "nt" else None,
            shutil.which("python3"),
            shutil.which("python"),
            sys.executable,
        ]
        py_exe = next((p for p in py_candidates if p and os.path.exists(p)), sys.executable)
        env = dict(os.environ)
        t_dir, c_dir = get_runtime_dirs()
        pyc_dir = os.path.join(c_dir, "pycache")
        os.makedirs(pyc_dir, exist_ok=True)
        env["TEMP"] = t_dir
        env["TMP"] = t_dir
        env["PYTHONPYCACHEPREFIX"] = pyc_dir
        env["PYTHONUTF8"] = "1"
        log_path = os.path.join(t_dir, "team_sidebar_server.log")
        log_f = open(log_path, "ab")
        popen_kwargs: Dict[str, Any] = {
            "cwd": PROTO_DIR,
            "env": env,
            "stdout": log_f,
            "stderr": log_f,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = CREATE_NO_WINDOW | DETACHED_PROCESS
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen([py_exe, server_py], **popen_kwargs)
        deadline = time.time() + 6.0
        while time.time() < deadline:
            ok, info = self.is_healthy()
            if ok and info:
                return info
            time.sleep(0.2)
        raise RuntimeError("无法自动启动或连接 127.0.0.1:8765 协作运行时服务")

    def get_state(self) -> Dict[str, Any]:
        self._request("POST", "/api/coordinator/heartbeat", {"source": "antigravity_cli"})
        return self._request("GET", "/api/state")

    def get_models(self, refresh: bool = False) -> Dict[str, Any]:
        return self._request("GET", f"/api/models?refresh={'1' if refresh else '0'}", timeout=20.0)

    def create_task(self, **kwargs: Any) -> Dict[str, Any]:
        self._request("POST", "/api/coordinator/heartbeat", {"source": "antigravity_cli"})
        return self._request("POST", "/api/tasks", kwargs)

    def wait_for_task(self, task_id: str, timeout_sec: float = 180.0, poll_sec: float = 0.8) -> Dict[str, Any]:
        terminal = {"succeeded", "failed", "aborted", "blocked", "needs_ruling", "needs_verification", "awaiting_coordinator", "awaiting_user"}
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            st = self._request("GET", "/api/state")
            t = next((x for x in st.get("tasks", []) if x["id"] == task_id), None)
            if not t:
                raise RuntimeError(f"未找到任务: {task_id}")
            if t["status"] in terminal:
                return t
            time.sleep(poll_sec)
        raise TimeoutError(f"等待任务 {task_id} 超时 ({timeout_sec}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity-Codex Team Runtime CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="获取当前项目绑定、成员与任务完整状态")
    p_mod = sub.add_parser("models", help="查询官方可用模型与思考强度")
    p_mod.add_argument("--refresh", action="store_true")

    p_run = sub.add_parser("dispatch", help="发起单成员或团队任务并可选等待完成")
    p_run.add_argument("--title", default="总管委派任务")
    p_run.add_argument("--goal", required=True)
    p_run.add_argument("--mode", choices=["single_member", "coordinator_auto"], default="single_member")
    p_run.add_argument("--member", default="codex_author")
    p_run.add_argument("--reviewer", default="codex_reviewer")
    p_run.add_argument("--wait", action="store_true")

    p_fup = sub.add_parser("followup", help="沿用原工作者会话追加追问")
    p_fup.add_argument("--task-id", required=True)
    p_fup.add_argument("--prompt", required=True)
    p_fup.add_argument("--wait", action="store_true")

    p_rule = sub.add_parser("ruling", help="提交总管裁决 (accept/reject/defer)")
    p_rule.add_argument("--task-id", required=True)
    p_rule.add_argument("--step-index", type=int, required=True)
    p_rule.add_argument("--decision", choices=["accept", "reject", "defer"], required=True)
    p_rule.add_argument("--reason", required=True)
    p_rule.add_argument("--wait", action="store_true")

    args = parser.parse_args()
    client = TeamRuntimeClient()

    if args.cmd == "status":
        print(json.dumps(client.get_state(), ensure_ascii=False, indent=2))
    elif args.cmd == "models":
        print(json.dumps(client.get_models(refresh=args.refresh), ensure_ascii=False, indent=2))
    elif args.cmd == "dispatch":
        created = client.create_task(
            title=args.title,
            goal=args.goal,
            dispatch_mode=args.mode,
            target_member_id=args.member,
            reviewer_member_id=args.reviewer,
        )["task"]
        out = client.wait_for_task(created["id"]) if args.wait else created
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.cmd == "followup":
        updated = client._request("POST", "/api/tasks/followup", {"task_id": args.task_id, "prompt": args.prompt})["task"]
        out = client.wait_for_task(updated["id"]) if args.wait else updated
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.cmd == "ruling":
        updated = client._request(
            "POST",
            "/api/tasks/ruling",
            {
                "task_id": args.task_id,
                "step_index": args.step_index,
                "decision": args.decision,
                "reason": args.reason,
            },
        )["task"]
        out = client.wait_for_task(updated["id"]) if args.wait else updated
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
