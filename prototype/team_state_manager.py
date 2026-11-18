"""
Thread-safe Persistent State Store (Schema v4) for Multi-Topology Team Orchestration
(Roundtable Group Chat, Chief Dynamic Dispatch, Custom Parallel/Serial DAG, Solo)
with Lossless v3 -> v4 Auto-Migration.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List

from codex_adapter import is_pid_alive

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEME_DIR = os.path.dirname(PROTO_DIR)
PROJECT_ROOT_DEFAULT = os.path.dirname(SCHEME_DIR)
STATE_FILE_PATH = os.path.join(PROTO_DIR, "team_state.json")


def normalize_win_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


class TeamStateManager:
    SCHEMA_VERSION = 4

    def __init__(self, state_file: str = STATE_FILE_PATH):
        self.state_file = state_file
        self._lock = threading.RLock()
        self.state: Dict[str, Any] = self._load_or_init()
        self.recover_interrupted_state()

    def _default_state(self) -> Dict[str, Any]:
        proj_abs = os.path.normpath(os.path.abspath(PROJECT_ROOT_DEFAULT))
        qa_abs = os.path.join(SCHEME_DIR, "qa")
        now = time.time()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "binding": {
                "project_name": os.path.basename(proj_abs),
                "project_path": proj_abs,
                "normalized_project_path": normalize_win_path(proj_abs),
                "path_verified": os.path.isdir(proj_abs),
                "qa_path": qa_abs,
                "recent_projects": [proj_abs],
                "project_agy_map": {},
                "antigravity_project_id": os.environ.get("AGY_PROJECT_ID", ""),
                "coordinator_conversation_id": os.environ.get("AGY_CONVERSATION_ID", ""),
                "coordinator_title": "AGY Roundtable Coordinator",
                "remote_control_url": "",
                "coordinator_role_id": "coordinator_default",
                "coordinator_mode": "antigravity_native",
                "last_coordinator_heartbeat": now,
                "verified_at": now,
            },
            "active_task_id": None,
            "roles": {
                "coordinator_default": {
                    "id": "coordinator_default",
                    "name": "总管调度",
                    "description": "负责统筹拆解、点将分派、主持讨论、裁决分歧与最终签发",
                    "is_coordinator": True,
                    "builtin": False,
                },
                "implementation": {
                    "id": "implementation",
                    "name": "实施",
                    "description": "在当前项目目录内执行代码研发、数据分析、实验计算或稿件撰写",
                    "is_coordinator": False,
                    "builtin": True,
                },
                "review": {
                    "id": "review",
                    "name": "审查",
                    "description": "独立复核交付质量，指出具体漏洞、可定位证据、影响与修复建议",
                    "is_coordinator": False,
                    "builtin": True,
                },
                "documentation": {
                    "id": "documentation",
                    "name": "资料整理",
                    "description": "汇总各方结论、规范排版投稿材料、编写说明文档与交接清单",
                    "is_coordinator": False,
                    "builtin": True,
                },
            },
            "members": {
                "codex_author": {
                    "id": "codex_author",
                    "name": "Codex 实施员 1",
                    "role": "implementation",
                    "client": "codex",
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "low",
                    "workspace_scope": ".",
                    "sandbox": "read-only",
                    "session_id": None,
                    "session_history": [],
                    "config_history": [],
                    "pending_config": None,
                    "status": "idle",
                    "live_activity": "",
                    "current_task_id": None,
                    "current_attempt_id": None,
                },
                "agy_gemini": {
                    "id": "agy_gemini",
                    "name": "Antigravity 实施员",
                    "role": "implementation",
                    "client": "antigravity",
                    "model": "gemini-4-carbon",
                    "reasoning_effort": "high",
                    "workspace_scope": ".",
                    "sandbox": "read-only",
                    "session_id": None,
                    "session_history": [],
                    "config_history": [],
                    "pending_config": None,
                    "status": "idle",
                    "live_activity": "",
                    "current_task_id": None,
                    "current_attempt_id": None,
                },
                "agy_claude_reviewer": {
                    "id": "agy_claude_reviewer",
                    "name": "Antigravity 审查员",
                    "role": "review",
                    "client": "antigravity",
                    "model": "claude-sonnet-5-5",
                    "reasoning_effort": "high",
                    "workspace_scope": ".",
                    "sandbox": "read-only",
                    "session_id": None,
                    "session_history": [],
                    "config_history": [],
                    "pending_config": None,
                    "status": "idle",
                    "live_activity": "",
                    "current_task_id": None,
                    "current_attempt_id": None,
                },
                "codex_reviewer": {
                    "id": "codex_reviewer",
                    "name": "Codex 审查员 1",
                    "role": "review",
                    "client": "codex",
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "low",
                    "workspace_scope": ".",
                    "sandbox": "read-only",
                    "session_id": None,
                    "session_history": [],
                    "config_history": [],
                    "pending_config": None,
                    "status": "idle",
                    "live_activity": "",
                    "current_task_id": None,
                    "current_attempt_id": None,
                },
                "codex_documenter": {
                    "id": "codex_documenter",
                    "name": "Codex 资料员 1",
                    "role": "documentation",
                    "client": "codex",
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "low",
                    "workspace_scope": ".",
                    "sandbox": "read-only",
                    "session_id": None,
                    "session_history": [],
                    "config_history": [],
                    "pending_config": None,
                    "status": "idle",
                    "live_activity": "",
                    "current_task_id": None,
                    "current_attempt_id": None,
                },
            },
            "tasks": [],
            "settings": {
                "max_concurrency": 3,
                "default_max_auto_repairs": 2,
            },
            "max_concurrency": 3,
            "paused": False,
        }

    def _migrate_to_v4(self, old: Dict[str, Any]) -> Dict[str, Any]:
        base = self._default_state()
        if isinstance(old.get("binding"), dict):
            base["binding"].update(copy.deepcopy(old["binding"]))
            p_cur = base["binding"].get("project_path", PROJECT_ROOT_DEFAULT)
            if os.path.isdir(p_cur):
                base["binding"]["normalized_project_path"] = normalize_win_path(p_cur)
                base["binding"]["path_verified"] = True

        for rid, r in old.get("roles", {}).items():
            base["roles"][rid] = copy.deepcopy(r)

        # Preserve 100% of user's customized members & names!
        if isinstance(old.get("members"), dict) and old["members"]:
            base["members"] = {}
            for mid, m in old["members"].items():
                if m.get("client") not in ("codex", "antigravity"):
                    continue
                mc = copy.deepcopy(m)
                mc["status"] = "idle"
                mc["live_activity"] = ""
                mc["current_task_id"] = None
                mc["current_attempt_id"] = None
                base["members"][mid] = mc

        migrated_tasks: List[Dict[str, Any]] = []
        for t in old.get("tasks", []):
            tc = copy.deepcopy(t)
            tc.setdefault("execution_mode", tc.get("dispatch_mode", "single_member"))
            tc.setdefault("mode_meta", {})
            tc.setdefault("chat_room", [])
            # Cleanly archive interrupted/old tasks during upgrade per user instruction ("直接终止并升级")
            if tc.get("status") in ("running", "blocked", "aborted"):
                if tc.get("id") == "TASK-75b98e1c":
                    tc["status"] = "aborted"
                    tc["blocking_reason"] = "已按用户指令终止旧版受阻任务并完成 v4 升级"
                    tc["archived"] = True
                    for s in tc.get("steps", []):
                        if s.get("status") in ("running", "pending"):
                            s["status"] = "aborted"
            migrated_tasks.append(tc)

        base["tasks"] = migrated_tasks
        base["active_task_id"] = None
        base["max_concurrency"] = max(3, int(old.get("max_concurrency", 3)))
        base["settings"]["max_concurrency"] = base["max_concurrency"]
        base["paused"] = False
        return base

    def _load_or_init(self) -> Dict[str, Any]:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    ver = int(loaded.get("schema_version", 1))
                    if ver == self.SCHEMA_VERSION:
                        b = loaded.get("binding", {})
                        if b.get("project_path"):
                            b["normalized_project_path"] = normalize_win_path(b["project_path"])
                            b["path_verified"] = os.path.isdir(b["project_path"])
                        return loaded
                    migrated = self._migrate_to_v4(loaded)
                    self._write_atomic(migrated)
                    return migrated
            except Exception:
                pass
        fresh = self._default_state()
        self._write_atomic(fresh)
        return fresh

    def _write_atomic(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp_path = f"{self.state_file}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.state_file)

    def save(self) -> None:
        with self._lock:
            self._write_atomic(self.state)

    def mutate(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        with self._lock:
            res = fn(self.state)
            self._write_atomic(self.state)
            return res

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state)

    def recover_interrupted_state(self) -> int:
        recovered_count = 0
        with self._lock:
            now = time.time()
            for m in self.state.get("members", {}).values():
                if m.get("status") == "working":
                    m["status"] = "idle"
                    m["live_activity"] = ""
                    m["current_task_id"] = None
                    m["current_attempt_id"] = None

            for task in self.state.get("tasks", []):
                task_interrupted = False
                for step in task.get("steps", []):
                    for att in step.get("attempts", []):
                        if att.get("status") == "running":
                            pid = att.get("pid")
                            if not is_pid_alive(pid):
                                att["status"] = "needs_verification"
                                att["ended_at"] = now
                                att["blocking_reason"] = (
                                    f"服务或进程重启导致执行中断 (原 PID={pid})，状态为“待核对”，请核对后点击继续"
                                )
                                recovered_count += 1
                                task_interrupted = True
                    if step.get("status") == "running":
                        alive = any(
                            a.get("status") == "running" and is_pid_alive(a.get("pid"))
                            for a in step.get("attempts", [])
                        )
                        if not alive:
                            step["status"] = "needs_verification"
                            step["blocking_reason"] = "服务重启后未完成步骤处于“待核对”状态，不会自动盲目重跑"
                            task_interrupted = True

                if task_interrupted or task.get("status") == "running":
                    any_step_alive = any(s.get("status") == "running" for s in task.get("steps", []))
                    if not any_step_alive and task.get("status") in ("running", "needs_verification"):
                        task["status"] = "needs_verification"
                        task["blocking_reason"] = "检测到上次运行未正常收尾（待核对），请复核已完成步骤后再继续"
                        task["updated_at"] = now
                        task.setdefault("recent_events", []).append(
                            {
                                "ts": now,
                                "type": "recovery.needs_verification",
                                "summary": "重启恢复检查：未完成尝试已标记为“待核对”",
                            }
                        )

            if recovered_count > 0:
                self._write_atomic(self.state)
        return recovered_count
