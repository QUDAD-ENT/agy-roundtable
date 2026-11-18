"""
Team Collaboration Runtime & Orchestrator v4:
Supports 4 Anthropomorphic Execution Topologies + Live Inter-Agent @Mention Consultation:
1. 'roundtable'     — 群聊圆桌自主协作 (Host framing -> Parallel proposals -> Cross-critique -> Synthesis)
2. 'chief_dispatch' — 总管自动点将编排 (Chief inspects project & roster -> Dynamic parallel/serial DAG -> Sign-off)
3. 'custom_dag'     — 自定义多步串并行搭积木 (User-defined multi-stage parallel/serial/review swimlanes)
4. 'single_member'  — 单人极速专办 (1-on-1 specialist desk; plus backward-compatible 'coordinator_auto')
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agy_adapter import AgyAdapter
from codex_adapter import CodexAdapter, CodexExecutionResult, get_runtime_dirs
from project_discovery import browse_directory, discover_projects, pick_folder_native_dialog
from team_state_manager import STATE_FILE_PATH, TeamStateManager, normalize_win_path


SUPPORTED_CLIENTS = ("codex", "antigravity")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _extract_json_obj(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"```(?:json|chief_plan)?\s*(\{.*?\})\s*```", stripped, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(stripped[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def _extract_team_consult(text: str, valid_members: Dict[str, Any], self_id: str) -> Tuple[Optional[Dict[str, str]], str]:
    """Extract ```team_consult {"to": "member_id", "question": "..."} ``` block if present."""
    if not text:
        return None, ""
    m = re.search(r"```team_consult\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            to_id = str(obj.get("to") or "").strip().lstrip("@")
            q_txt = str(obj.get("question") or "").strip()
            # Match by ID or display name
            resolved_id = None
            if to_id in valid_members and to_id != self_id:
                resolved_id = to_id
            else:
                for mid, minfo in valid_members.items():
                    if mid != self_id and minfo.get("name") == to_id:
                        resolved_id = mid
                        break
            if resolved_id and q_txt:
                cleaned = (text[: m.start()] + text[m.end() :]).strip()
                return {"to": resolved_id, "question": q_txt}, cleaned
        except Exception:
            pass
    return None, text


def _extract_ai_questions(text: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    if not text:
        return None, ""
    m = re.search(r"```team_questions\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            qs = _normalize_questions_list(data.get("questions"))
            if qs:
                cleaned = (text[: m.start()] + text[m.end() :]).strip()
                return qs, cleaned or "请根据下方卡片确认你的选项后继续执行。"
        except Exception:
            pass

    parsed = _extract_json_obj(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list) and "verdict" not in parsed and "steps" not in parsed:
        qs = _normalize_questions_list(parsed.get("questions"))
        if qs:
            cleaned = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", text, flags=re.S).strip()
            if cleaned.startswith("{") and cleaned.endswith("}"):
                cleaned = ""
            return qs, cleaned or "AI 需要先确认以下选项再继续执行："

    if "跳过了选择" in text:
        return [
            {
                "id": "q_fallback",
                "question": "AI 刚才尝试发起交互选择但被命令行无头模式跳过，请在下方补充你的偏好或直接让 AI 按推荐项继续：",
                "multi_select": False,
                "options": ["(推荐) 按当前项目最稳妥的标准方案自动决定并继续"],
            }
        ], text

    return None, text


def _normalize_questions_list(raw_qs: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_qs, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, q in enumerate(raw_qs[:6]):
        if not isinstance(q, dict):
            continue
        q_text = str(q.get("question") or q.get("title") or "").strip()
        if not q_text:
            continue
        raw_opts = q.get("options") or []
        opts = [str(o).strip() for o in raw_opts if str(o).strip()][:8]
        if not opts:
            opts = ["(推荐) 按推荐方案继续", "暂不采用，见下方补充说明"]
        out.append(
            {
                "id": str(q.get("id") or f"q{idx + 1}"),
                "question": q_text,
                "multi_select": bool(q.get("multi_select") or q.get("is_multi_select", False)),
                "options": opts,
            }
        )
    return out


def _scopes_overlap(scope_a: List[str], scope_b: List[str], project_root: str) -> bool:
    if not scope_a or not scope_b:
        return True
    for a in scope_a:
        if a in ("*", "."):
            return True
        pa = normalize_win_path(os.path.join(project_root, a))
        for b in scope_b:
            if b in ("*", "."):
                return True
            pb = normalize_win_path(os.path.join(project_root, b))
            if pa == pb or pa.startswith(pb + os.sep) or pb.startswith(pa + os.sep):
                return True
    return False


def _smart_clip(text: str, limit: int = 8000) -> str:
    if not text or len(text) <= limit:
        return text
    head = int(limit * 0.68)
    tail = limit - head
    return (
        text[:head]
        + f"\n\n...[中间省略 {len(text) - limit} 字，前表与末尾结论均已完整保留]...\n\n"
        + text[-tail:]
    )


class TeamOrchestrator:
    def __init__(
        self,
        state_file: str = STATE_FILE_PATH,
        adapter: Optional[CodexAdapter] = None,
        agy_adapter: Optional[AgyAdapter] = None,
        auto_start_scheduler: bool = True,
    ):
        self.store = TeamStateManager(state_file=state_file)
        self.adapter = adapter or CodexAdapter()
        self.agy_adapter = agy_adapter or AgyAdapter()
        self._sched_lock = threading.RLock()
        self._wakeup = threading.Event()
        self._stop_event = threading.Event()
        self._running_attempts: Dict[str, Dict[str, Any]] = {}
        self._scheduler_thread: Optional[threading.Thread] = None
        if auto_start_scheduler:
            self.start_scheduler()

    def start_scheduler(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="TeamScheduler", daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._stop_event.set()
        self._wakeup.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=2.0)

    def touch_coordinator_heartbeat(self, source: str = "antigravity_native") -> None:
        def _upd(st: Dict[str, Any]) -> None:
            st["binding"]["last_coordinator_heartbeat"] = time.time()
            st["binding"]["last_coordinator_source"] = source

        self.store.mutate(_upd)

    def _append_room_chat(
        self,
        st: Dict[str, Any],
        task: Dict[str, Any],
        sender_id: str,
        kind: str,
        content: str,
        to_member_id: Optional[str] = None,
        step_index: Optional[int] = None,
        round_label: str = "",
    ) -> Dict[str, Any]:
        if not content or not content.strip():
            return {}
        mem = st.get("members", {}).get(sender_id, {})
        role_name = st.get("roles", {}).get(mem.get("role", ""), {}).get("name", "总管/用户" if sender_id == "user" else "成员")
        to_name = st.get("members", {}).get(to_member_id, {}).get("name", to_member_id) if to_member_id else None
        entry = {
            "id": _new_id("CHAT"),
            "ts": time.time(),
            "sender_id": sender_id,
            "sender_name": "项目负责人 (你)" if sender_id == "user" else (mem.get("name") or sender_id),
            "sender_role": "负责人" if sender_id == "user" else role_name,
            "sender_client": "human" if sender_id == "user" else mem.get("client", "codex"),
            "sender_model": "" if sender_id == "user" else f"{mem.get('model', '')}/{mem.get('reasoning_effort', '')}",
            "kind": kind,
            "to_member_id": to_member_id,
            "to_member_name": to_name,
            "step_index": step_index,
            "round_label": round_label,
            "content": content.strip(),
        }
        task.setdefault("chat_room", []).append(entry)
        try:
            tmp_dir, _ = get_runtime_dirs()
            rooms_dir = os.path.join(tmp_dir, "team_rooms")
            os.makedirs(rooms_dir, exist_ok=True)
            room_md_path = os.path.join(rooms_dir, f"{task.get('id', 'task')}.md")
            md_lines = [f"# 协作任务实时群聊完整纪要：{task.get('title', '')}\n"]
            for cm in task.get("chat_room", []):
                to_s = f" -> @{cm['to_member_name']}" if cm.get("to_member_name") else ""
                md_lines.append(f"## [{cm.get('round_label') or cm.get('kind')}] {cm.get('sender_name')}{to_s}\n\n{cm.get('content', '')}\n")
            with open(room_md_path, "w", encoding="utf-8") as rf:
                rf.write("\n---\n".join(md_lines))
        except Exception:
            pass
        return entry

    # ------------------------------------------------------------------
    # Capabilities & Free Project Management
    # ------------------------------------------------------------------

    def get_capabilities(self, refresh_cli: bool = False) -> Dict[str, Any]:
        codex_caps = self.adapter.discover_capabilities(refresh_cli=refresh_cli)
        agy_caps = self.agy_adapter.discover_capabilities(refresh_cli=refresh_cli)
        return {
            "cli": codex_caps["cli"],
            "models": codex_caps["models"],
            "clients": {
                "codex": codex_caps,
                "antigravity": agy_caps,
            },
        }

    def _validate_client_model_effort(self, client: str, model: Optional[str], effort: Optional[str]) -> None:
        if client == "codex":
            ok, err, _ = self.adapter.validate_model_effort(model, effort)
            if not ok:
                raise ValueError(err or "无效的 Codex 模型或思考强度组合")
        elif client == "antigravity":
            ok, err, _, _ = self.agy_adapter.resolve_full_model_slug(model, effort)
            if not ok:
                raise ValueError(err or "无效的 Antigravity 模型或思考强度组合")
        else:
            raise ValueError(f"不支持的客户端类型: {client}")

    def list_projects(self) -> Dict[str, Any]:
        snap = self.store.snapshot()
        cat = discover_projects(recent_paths=snap["binding"].get("recent_projects", []))
        cat["current_project_path"] = snap["binding"]["project_path"]
        cat["current_project_name"] = snap["binding"].get("project_name")
        return cat

    def switch_project(self, project_path: str) -> Dict[str, Any]:
        if not project_path or not project_path.strip():
            raise ValueError("项目路径不能为空")
        abs_p = os.path.normpath(os.path.abspath(project_path.strip()))
        if not os.path.isdir(abs_p):
            raise ValueError(f"所选项目目录不存在: {abs_p}")
        norm_p = normalize_win_path(abs_p)

        def _sw(st: Dict[str, Any]) -> Dict[str, Any]:
            b = st["binding"]
            b["project_path"] = abs_p
            b["project_name"] = os.path.basename(abs_p) or abs_p
            b["normalized_project_path"] = norm_p
            b["path_verified"] = True
            b["verified_at"] = time.time()

            rec = [p for p in b.get("recent_projects", []) if normalize_win_path(p) != norm_p]
            rec.insert(0, abs_p)
            b["recent_projects"] = rec[:16]
            b["antigravity_project_id"] = b.setdefault("project_agy_map", {}).get(norm_p, "")

            proj_active = [
                t for t in st.get("tasks", [])
                if not t.get("archived", False) and normalize_win_path(t.get("project_path", "")) == norm_p
            ]
            st["active_task_id"] = proj_active[-1]["id"] if proj_active else None
            return {"binding": b, "active_task_id": st["active_task_id"]}

        return self.store.mutate(_sw)

    def browse_fs(self, path: Optional[str] = None) -> Dict[str, Any]:
        return browse_directory(path)

    def pick_project_native(self) -> Dict[str, Any]:
        cur = self.store.snapshot()["binding"].get("project_path")
        picked = pick_folder_native_dialog(cur)
        if not picked.get("cancelled") and picked.get("path"):
            sw = self.switch_project(picked["path"])
            return {"cancelled": False, "path": picked["path"], **sw}
        return picked

    def update_binding(
        self,
        project_path: Optional[str] = None,
        remote_control_url: Optional[str] = None,
        max_concurrency: Optional[int] = None,
        default_max_auto_repairs: Optional[int] = None,
    ) -> Dict[str, Any]:
        if project_path is not None:
            self.switch_project(project_path)

        def _upd(st: Dict[str, Any]) -> Dict[str, Any]:
            b = st["binding"]
            if remote_control_url is not None:
                b["remote_control_url"] = remote_control_url.strip()
            if max_concurrency is not None:
                mc = max(1, min(8, int(max_concurrency)))
                st["max_concurrency"] = mc
                st.setdefault("settings", {})["max_concurrency"] = mc
            if default_max_auto_repairs is not None:
                mr = max(0, min(5, int(default_max_auto_repairs)))
                st.setdefault("settings", {})["default_max_auto_repairs"] = mr
            return b

        res = self.store.mutate(_upd)
        self._wakeup.set()
        return res

    # ------------------------------------------------------------------
    # Roles & Members Management
    # ------------------------------------------------------------------

    def upsert_role(self, role_id: str, name: str, description: str, set_as_coordinator: bool = False) -> Dict[str, Any]:
        role_id = role_id.strip()
        name = name.strip()
        if not role_id or not name:
            raise ValueError("角色 ID 与名称不能为空")

        def _upd(st: Dict[str, Any]) -> Dict[str, Any]:
            existing = st["roles"].get(role_id, {})
            st["roles"][role_id] = {
                "id": role_id,
                "name": name,
                "description": description.strip(),
                "is_coordinator": bool(existing.get("is_coordinator", False)),
                "builtin": bool(existing.get("builtin", False)),
            }
            if set_as_coordinator:
                for rid, r in st["roles"].items():
                    r["is_coordinator"] = rid == role_id
                st["binding"]["coordinator_role_id"] = role_id
            return st["roles"][role_id]

        return self.store.mutate(_upd)

    def delete_role(self, role_id: str) -> Dict[str, Any]:
        def _del(st: Dict[str, Any]) -> Dict[str, Any]:
            if role_id not in st["roles"]:
                raise ValueError(f"角色不存在: {role_id}")
            in_use = [mid for mid, m in st["members"].items() if m.get("role") == role_id]
            if in_use:
                raise ValueError(f"角色仍被成员使用，无法删除: {', '.join(in_use)}")
            if st["binding"].get("coordinator_role_id") == role_id:
                raise ValueError("当前担任总管职责的角色不能直接删除")
            return st["roles"].pop(role_id)

        return self.store.mutate(_del)

    def upsert_member(
        self,
        member_id: str,
        role_id: str,
        name: Optional[str] = None,
        client: str = "codex",
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = "low",
        workspace_scope: str = ".",
        sandbox: str = "read-only",
    ) -> Dict[str, Any]:
        member_id = member_id.strip()
        if not member_id:
            raise ValueError("成员 ID 不能为空")
        client = client.strip().lower()
        if client not in SUPPORTED_CLIENTS:
            raise ValueError(f"不支持的客户端: {client}")
        use_model = model or ("gpt-6.1-sol" if client == "codex" else "gemini-4-carbon")
        self._validate_client_model_effort(client, use_model, reasoning_effort)

        def _upd(st: Dict[str, Any]) -> Dict[str, Any]:
            if role_id not in st["roles"]:
                raise ValueError(f"未知角色 ID: {role_id}")
            existing = st["members"].get(member_id)
            if existing and existing.get("status") == "working":
                raise ValueError("成员正在运行中，请使用安全配置切换接口")
            st["members"][member_id] = {
                "id": member_id,
                "name": (name or (existing.get("name") if existing else member_id)).strip(),
                "role": role_id,
                "client": client,
                "model": use_model,
                "reasoning_effort": reasoning_effort or "medium",
                "workspace_scope": workspace_scope.strip() or ".",
                "sandbox": sandbox if sandbox in ("read-only", "workspace-write") else "read-only",
                "session_id": existing.get("session_id") if existing and existing.get("client") == client else None,
                "session_history": existing.get("session_history", []) if existing else [],
                "config_history": existing.get("config_history", []) if existing else [],
                "pending_config": None,
                "status": "idle",
                "live_activity": "",
                "current_task_id": None,
                "current_attempt_id": None,
            }
            return st["members"][member_id]

        return self.store.mutate(_upd)

    def delete_member(self, member_id: str) -> Dict[str, Any]:
        def _del(st: Dict[str, Any]) -> Dict[str, Any]:
            m = st["members"].get(member_id)
            if not m:
                raise ValueError(f"成员不存在: {member_id}")
            if m.get("status") == "working":
                raise ValueError("成员正在执行任务，请先中止当前尝试后再移除")
            return st["members"].pop(member_id)

        return self.store.mutate(_del)

    def switch_member_config(
        self,
        member_id: str,
        name: Optional[str] = None,
        client: Optional[str] = None,
        role_id: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        workspace_scope: Optional[str] = None,
        sandbox: Optional[str] = None,
        mode: str = "next_round",
        reason: str = "用户调整成员配置",
    ) -> Dict[str, Any]:
        snap = self.store.snapshot()
        m = snap["members"].get(member_id)
        if not m:
            raise ValueError(f"成员不存在: {member_id}")

        clean_name = name.strip() if (name is not None and name.strip()) else m.get("name", member_id)
        target_client = (client or m.get("client", "codex")).strip().lower()
        target_model = model if model is not None else m.get("model")
        target_effort = reasoning_effort if reasoning_effort is not None else m.get("reasoning_effort")
        m_check = None if target_model in ("client_default", "client_current_setting") else target_model
        e_check = None if target_effort in ("client_default", "client_current_setting") else target_effort
        self._validate_client_model_effort(target_client, m_check, e_check)

        new_cfg = {
            "name": clean_name,
            "client": target_client,
            "role": role_id or m.get("role"),
            "model": target_model,
            "reasoning_effort": target_effort,
            "workspace_scope": workspace_scope if workspace_scope is not None else m.get("workspace_scope", "."),
            "sandbox": sandbox if sandbox is not None else m.get("sandbox", "read-only"),
            "reason": reason,
            "requested_at": time.time(),
        }

        if m.get("status") == "working":
            if mode == "abort_now":
                cur_att = m.get("current_attempt_id")
                cur_task = m.get("current_task_id")
                if cur_att:
                    self.abort_attempt(task_id=cur_task, attempt_id=cur_att, reason=f"中止并切换配置: {reason}")
                deadline = time.time() + 4.0
                while time.time() < deadline:
                    if self.store.snapshot()["members"].get(member_id, {}).get("status") != "working":
                        break
                    time.sleep(0.1)
            else:
                def _set_pending(st: Dict[str, Any]) -> Dict[str, Any]:
                    mem = st["members"][member_id]
                    mem["name"] = clean_name
                    exec_changed = any(
                        new_cfg[k] != mem.get(k)
                        for k in ("client", "role", "model", "reasoning_effort", "workspace_scope", "sandbox")
                    )
                    if exec_changed:
                        mem["pending_config"] = new_cfg
                        return {"applied": "next_round", "member": mem}
                    return {"applied": "immediate", "member": mem}

                return self.store.mutate(_set_pending)

        def _apply_now(st: Dict[str, Any]) -> Dict[str, Any]:
            mem = st["members"][member_id]
            if new_cfg["role"] not in st["roles"]:
                raise ValueError(f"未知角色: {new_cfg['role']}")
            client_changed = mem.get("client") != new_cfg["client"]
            mem.setdefault("config_history", []).append(
                {
                    "changed_at": time.time(),
                    "from": {
                        "name": mem.get("name"),
                        "client": mem.get("client"),
                        "role": mem.get("role"),
                        "model": mem.get("model"),
                        "reasoning_effort": mem.get("reasoning_effort"),
                    },
                    "to": new_cfg,
                    "reason": reason,
                }
            )
            if client_changed and mem.get("session_id"):
                mem.setdefault("session_history", []).append(mem["session_id"])
                mem["session_id"] = None
            mem["name"] = clean_name
            mem["client"] = new_cfg["client"]
            mem["role"] = new_cfg["role"]
            mem["model"] = new_cfg["model"]
            mem["reasoning_effort"] = new_cfg["reasoning_effort"]
            mem["workspace_scope"] = new_cfg["workspace_scope"]
            mem["sandbox"] = new_cfg["sandbox"]
            mem["pending_config"] = None
            return {"applied": "immediate", "member": mem}

        return self.store.mutate(_apply_now)

    # ------------------------------------------------------------------
    # Multi-Topology Task Creation (Roundtable / Chief / Custom DAG / Solo)
    # ------------------------------------------------------------------

    def set_focus_task(self, task_id: Optional[str]) -> Dict[str, Any]:
        def _foc(st: Dict[str, Any]) -> Dict[str, Any]:
            if task_id is None:
                st["active_task_id"] = None
                return {"active_task_id": None}
            t = self._find_task(st, task_id)
            t["archived"] = False
            st["active_task_id"] = task_id
            return {"active_task_id": task_id, "task": t}

        return self.store.mutate(_foc)

    def archive_task(self, task_id: str, archived: bool = True) -> Dict[str, Any]:
        def _arch(st: Dict[str, Any]) -> Dict[str, Any]:
            t = self._find_task(st, task_id)
            t["archived"] = bool(archived)
            if archived and st.get("active_task_id") == task_id:
                cur_norm = st["binding"]["normalized_project_path"]
                others = [
                    x for x in st["tasks"]
                    if not x.get("archived", False)
                    and x["id"] != task_id
                    and normalize_win_path(x.get("project_path", "")) == cur_norm
                ]
                st["active_task_id"] = others[-1]["id"] if others else None
            elif not archived:
                st["active_task_id"] = task_id
            return {"active_task_id": st.get("active_task_id"), "task": t}

        return self.store.mutate(_arch)

    def archive_completed_tasks(self, keep_active: bool = False) -> Dict[str, Any]:
        def _sweep(st: Dict[str, Any]) -> Dict[str, Any]:
            count = 0
            active_id = st.get("active_task_id")
            for t in st["tasks"]:
                if t.get("archived"):
                    continue
                if keep_active and t["id"] == active_id:
                    continue
                if t["status"] in ("succeeded", "failed", "aborted"):
                    t["archived"] = True
                    count += 1
                    if st.get("active_task_id") == t["id"]:
                        st["active_task_id"] = None
            return {"archived_count": count, "active_task_id": st.get("active_task_id")}

        return self.store.mutate(_sweep)

    def create_task(
        self,
        title: str,
        goal: str,
        dispatch_mode: str = "single_member",
        execution_mode: Optional[str] = None,
        target_member_id: Optional[str] = "codex_author",
        reviewer_member_id: Optional[str] = "agy_claude_reviewer",
        chief_member_id: Optional[str] = None,
        host_member_id: Optional[str] = None,
        participants: Optional[List[str]] = None,
        discussion_rounds: int = 2,
        enable_host_opening: bool = False,
        stages: Optional[List[List[Dict[str, Any]]]] = None,
        auto_template: str = "standard_review_loop",
        steps: Optional[List[Dict[str, Any]]] = None,
        project_path: Optional[str] = None,
        workspace_scope: Optional[str] = None,
        sandbox: str = "read-only",
        max_auto_repairs: Optional[int] = None,
        require_manual_ruling: bool = False,
        reuse_member_session: bool = False,
        archive_previous_done: bool = True,
    ) -> Dict[str, Any]:
        if project_path:
            self.switch_project(project_path)

        title = title.strip() or goal.strip().splitlines()[0][:36] or "未命名任务"
        goal = goal.strip()
        if not goal:
            raise ValueError("任务目标不能为空")

        mode = (execution_mode or dispatch_mode or "single_member").strip()
        now = time.time()
        task_id = _new_id("TASK")

        def _create(st: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal mode
            if archive_previous_done and st.get("active_task_id"):
                try:
                    prev_t = self._find_task(st, st["active_task_id"])
                    if prev_t["status"] in ("succeeded", "aborted", "failed"):
                        prev_t["archived"] = True
                except Exception:
                    pass

            proj_abs = st["binding"]["project_path"]
            proj_norm = st["binding"]["normalized_project_path"]
            proj_name = st["binding"].get("project_name") or os.path.basename(proj_abs)
            default_repairs = st.get("settings", {}).get("default_max_auto_repairs", 2)
            repairs_cap = default_repairs if max_auto_repairs is None else max(0, min(5, int(max_auto_repairs)))

            built_steps: List[Dict[str, Any]] = []
            mode_meta: Dict[str, Any] = {}
            scope_default = workspace_scope or "."

            if mode == "roundtable":
                # 1. Roundtable Conference Collaboration
                valid_ids = [m for m in (participants or []) if m in st["members"]]
                if len(valid_ids) < 2:
                    raise ValueError("圆桌协作请至少勾选 2 位参会成员")
                host_id = host_member_id if (host_member_id and host_member_id in st["members"]) else valid_ids[0]
                rounds = max(1, min(3, int(discussion_rounds or 2)))
                mode_meta = {
                    "host_member_id": host_id,
                    "participants": valid_ids,
                    "discussion_rounds": rounds,
                    "enable_host_opening": bool(enable_host_opening),
                }

                roster_desc = "、".join(f"@{mid}({st['members'][mid]['name']})" for mid in valid_ids)
                prev_round_indices: List[int] = []

                if enable_host_opening:
                    built_steps.append(
                        self._make_step(
                            step_index=0,
                            title="主持人开场破题与议题分工",
                            step_type="opening",
                            stage_index=0,
                            round_label="开场定调",
                            member_id=host_id,
                            prompt=(
                                f"你是本次圆桌会议的主持人。参会成员有：{roster_desc}。\n"
                                f"总任务目标：{goal}\n"
                                "请先快速查看项目现状，用简洁清晰的语言拆解核心争点，并点名每位参会成员接下来重点核查/攻关的角度（暂不输出最终定稿）。"
                            ),
                            depends_on=[],
                            review_of=None,
                            read_only=True,
                            write_scope=[],
                            workspace_scope=scope_default,
                            sandbox="read-only",
                        )
                    )
                    prev_round_indices = [0]

                for r_num in range(1, rounds + 1):
                    cur_round_indices: List[int] = []
                    for mid in valid_ids:
                        idx = len(built_steps)
                        cur_round_indices.append(idx)
                        m_name = st["members"][mid]["name"]
                        if r_num == 1:
                            r_title = f"首轮独立研判 ({m_name})"
                            r_label = "第 1 轮 · 独立立论"
                            r_prompt = (
                                f"你是本次圆桌正式参会成员（同席参会者：{roster_desc}）。请结合你的专业职责，独立检查当前项目材料并提出你的具体方案、完整证据表或发现的问题。\n"
                                f"如果有需要其他本场参会队友核实的地方，可在发言中直接点名（或用 team_consult 向本场参会队友提问）。\n总目标：{goal}"
                            )
                        else:
                            r_title = f"第{r_num}轮交叉质询与补充 ({m_name})"
                            r_label = f"第 {r_num} 轮 · 交叉互评"
                            r_prompt = (
                                "请仔细阅读共享群聊中其他参会队友上一轮的全部发言：\n"
                                "1. 明确指出你认同哪位队友的观点、反驳或修正哪位队友的遗漏（请直接用 @名字 点名回应）；\n"
                                "2. 给出收敛后的最终落地建议。"
                            )
                        built_steps.append(
                            self._make_step(
                                step_index=idx,
                                title=r_title,
                                step_type="discussion",
                                stage_index=r_num,
                                round_label=r_label,
                                member_id=mid,
                                prompt=r_prompt,
                                depends_on=list(prev_round_indices),
                                review_of=None,
                                read_only=True,
                                write_scope=[],
                                workspace_scope=scope_default,
                                sandbox="read-only",
                            )
                        )
                    prev_round_indices = cur_round_indices

                # Final Host Synthesis & Execution
                fin_idx = len(built_steps)
                ro_final = sandbox == "read-only"
                built_steps.append(
                    self._make_step(
                        step_index=fin_idx,
                        title="主持人吸纳共识并完成最终交付",
                        step_type="synthesis",
                        stage_index=rounds + 1,
                        round_label="终稿拍板与交付",
                        member_id=host_id,
                        prompt=(
                            f"作为圆桌主持人，请综合群聊中全体成员的多轮讨论与质询意见，明确说明采纳点与分歧裁决，并完成最终任务交付（若开启了写入权限请直接落地修改文件并核验）：\n{goal}"
                        ),
                        depends_on=list(prev_round_indices),
                        review_of=None,
                        read_only=ro_final,
                        write_scope=[] if ro_final else [scope_default],
                        workspace_scope=scope_default,
                        sandbox=sandbox,
                    )
                )
                initial_status = "queued" if st.get("paused") else "running"

            elif mode == "chief_dispatch":
                # 2. Chief Dynamic Roster Selection & DAG Dispatch
                runnable = [k for k, v in st["members"].items() if v.get("client") in SUPPORTED_CLIENTS]
                chief_id = chief_member_id if (chief_member_id and chief_member_id in st["members"]) else (runnable[0] if runnable else "codex_author")
                mode_meta = {"chief_member_id": chief_id, "recruited_members": []}

                roster_lines = [
                    f'- id="{k}" | 姓名="{st["members"][k]["name"]}" | 职责="{st["roles"].get(st["members"][k]["role"], {}).get("name", "")}" | 引擎={st["members"][k]["client"]}/{st["members"][k]["model"]}'
                    for k in runnable
                ]
                built_steps.append(
                    self._make_step(
                        step_index=0,
                        title="主帅察看项目与动态点将排班",
                        step_type="chief_plan",
                        stage_index=0,
                        round_label="主帅点将",
                        member_id=chief_id,
                        prompt=(
                            "你是本次任务的主帅总管。请先快速查看当前项目目录概况，再从以下真实团队花名册中按需点选最合适的 1~5 名成员，编排串行/并行协作与审查步骤（无需所有人都上场）：\n"
                            + "\n".join(roster_lines)
                            + f"\n\n【用户任务目标】{goal}\n"
                            + f"【用户授予的文件权限】sandbox={sandbox}（若为 read-only 则所有步骤 read_only 必须为 true；若为 workspace-write 则实施写文件步骤可设 read_only=false）\n\n"
                            + "请先用一段话说明你的【点将理由与战术分工】，随后在末尾严格附上如下 JSON 代码块：\n"
                            + "```chief_plan\n"
                            + '{"rationale":"一句话战术总结","steps":[\n'
                            + '  {"title":"步骤名","member_id":"花名册中的id","step_type":"execution 或 discussion 或 review","depends_on":[],"review_of":null,"read_only":true,"prompt":"给该成员的具体指令"}\n'
                            + "]}\n```"
                        ),
                        depends_on=[],
                        review_of=None,
                        read_only=True,
                        write_scope=[],
                        workspace_scope=scope_default,
                        sandbox="read-only",
                    )
                )
                initial_status = "queued" if st.get("paused") else "running"

            elif mode == "custom_dag" and stages:
                # 3. User-Built Multi-Stage Parallel/Serial Blocks
                prev_stage_step_indices: List[int] = []
                last_exec_step_idx: Optional[int] = None
                stage_meta_list: List[List[int]] = []

                for stg_i, stage_nodes in enumerate(stages):
                    if not stage_nodes:
                        continue
                    cur_indices: List[int] = []
                    for node in stage_nodes:
                        mid = node.get("member_id") or "codex_author"
                        if mid not in st["members"]:
                            raise ValueError(f"搭积木阶段 {stg_i + 1} 中的成员不存在: {mid}")
                        ntype = node.get("step_type") or "execution"
                        idx = len(built_steps)
                        cur_indices.append(idx)

                        rev_target = None
                        if ntype == "review" and last_exec_step_idx is not None and len(stage_nodes) == 1:
                            rev_target = last_exec_step_idx

                        ro_node = True if ntype in ("review", "discussion") else (
                            bool(node["read_only"]) if "read_only" in node else (sandbox == "read-only")
                        )
                        custom_p = (node.get("prompt") or "").strip()
                        node_prompt = (
                            f"{custom_p}\n\n（所属总任务目标：{goal}）"
                            if custom_p
                            else (
                                f"请对照总目标独立复核前序阶段成果：{goal}"
                                if ntype == "review"
                                else f"请按你的角色职责完成本阶段工作（可参考前序阶段队友成果，必要时用 team_consult @队友协商）：\n{goal}"
                            )
                        )
                        built_steps.append(
                            self._make_step(
                                step_index=idx,
                                title=(node.get("title") or "").strip() or f"第{stg_i + 1}站 · {st['members'][mid]['name']}",
                                step_type=ntype,
                                stage_index=stg_i,
                                round_label=f"第 {stg_i + 1} 阶段",
                                member_id=mid,
                                prompt=node_prompt,
                                depends_on=list(prev_stage_step_indices),
                                review_of=rev_target,
                                read_only=ro_node,
                                write_scope=[] if ro_node else [scope_default],
                                workspace_scope=scope_default,
                                sandbox="read-only" if ro_node else sandbox,
                            )
                        )
                        if ntype in ("execution", "synthesis"):
                            last_exec_step_idx = idx
                    stage_meta_list.append(cur_indices)
                    prev_stage_step_indices = cur_indices

                if not built_steps:
                    raise ValueError("自定义流水线至少需要包含 1 个工位节点")
                mode_meta = {"stage_groups": stage_meta_list}
                initial_status = "queued" if st.get("paused") else "running"

            elif steps:
                mode = "custom_dag" if execution_mode == "custom_dag" else mode
                for idx, raw_s in enumerate(steps):
                    mid = raw_s["member_id"]
                    if mid not in st["members"]:
                        raise ValueError(f"步骤 {idx} 指定的成员不存在: {mid}")
                    ro = bool(raw_s.get("read_only", True))
                    w_scope = raw_s.get("write_scope") or raw_s.get("file_scope") or []
                    built_steps.append(
                        self._make_step(
                            step_index=idx,
                            title=raw_s.get("title") or f"步骤 {idx}",
                            step_type=raw_s.get("step_type") or ("review" if raw_s.get("review_of") is not None else "execution"),
                            stage_index=int(raw_s.get("stage_index", idx)),
                            round_label=raw_s.get("round_label", f"步骤 {idx + 1}"),
                            member_id=mid,
                            prompt=raw_s["prompt"],
                            depends_on=[int(x) for x in raw_s.get("depends_on", [])],
                            review_of=int(raw_s["review_of"]) if raw_s.get("review_of") is not None else None,
                            read_only=ro,
                            write_scope=w_scope,
                            workspace_scope=raw_s.get("workspace_scope") or scope_default,
                            sandbox="read-only" if ro else (raw_s.get("sandbox") or sandbox or "workspace-write"),
                            initial_session_id=raw_s.get("session_id"),
                        )
                    )
                initial_status = "queued" if st.get("paused") else "running"

            elif mode == "single_member":
                mid = target_member_id or "codex_author"
                if mid not in st["members"]:
                    raise ValueError(f"指定成员不存在: {mid}")
                mem_sess = st["members"][mid].get("session_id") if reuse_member_session else None
                built_steps.append(
                    self._make_step(
                        step_index=0,
                        title=title,
                        step_type="execution",
                        stage_index=0,
                        round_label="专员直办",
                        member_id=mid,
                        prompt=goal,
                        depends_on=[],
                        review_of=None,
                        read_only=(sandbox == "read-only"),
                        write_scope=[] if sandbox == "read-only" else [scope_default],
                        workspace_scope=scope_default,
                        sandbox=sandbox,
                        initial_session_id=mem_sess,
                    )
                )
                mode_meta = {"specialist_id": mid}
                initial_status = "queued" if st.get("paused") else "running"

            elif auto_template == "standard_review_loop":
                exec_mid = target_member_id or "codex_author"
                rev_mid = reviewer_member_id or ("agy_claude_reviewer" if "agy_claude_reviewer" in st["members"] else "codex_reviewer")
                if exec_mid not in st["members"] or rev_mid not in st["members"]:
                    raise ValueError("执行成员或审查成员不存在")
                built_steps.append(
                    self._make_step(
                        step_index=0,
                        title="方案实施与结果输出",
                        step_type="execution",
                        stage_index=0,
                        round_label="实施交付",
                        member_id=exec_mid,
                        prompt=goal,
                        depends_on=[],
                        review_of=None,
                        read_only=(sandbox == "read-only"),
                        write_scope=[] if sandbox == "read-only" else [scope_default],
                        workspace_scope=scope_default,
                        sandbox=sandbox,
                    )
                )
                built_steps.append(
                    self._make_step(
                        step_index=1,
                        title="独立质量与证据复核",
                        step_type="review",
                        stage_index=1,
                        round_label="独立复核",
                        member_id=rev_mid,
                        prompt=f"请对照原始目标独立审查步骤 0 的交付物是否完整、准确、无偏离。\n原始目标：{goal}",
                        depends_on=[0],
                        review_of=0,
                        read_only=True,
                        write_scope=[],
                        workspace_scope=scope_default,
                        sandbox="read-only",
                    )
                )
                initial_status = "queued" if st.get("paused") else "running"
            else:
                initial_status = "awaiting_coordinator"

            task_obj: Dict[str, Any] = {
                "id": task_id,
                "title": title,
                "goal": goal,
                "project_path": proj_abs,
                "normalized_project_path": proj_norm,
                "project_name": proj_name,
                "archived": False,
                "dispatch_mode": dispatch_mode,
                "execution_mode": mode,
                "mode_meta": mode_meta,
                "sandbox": sandbox,
                "workspace_scope": scope_default,
                "auto_template": auto_template,
                "require_manual_ruling": require_manual_ruling,
                "max_auto_repairs": repairs_cap,
                "status": initial_status,
                "current_step_index": 0 if built_steps else None,
                "blocking_reason": "当前派发已暂停，排队等待" if st.get("paused") else None,
                "steps": built_steps,
                "chat_room": [],
                "deliverables": [],
                "recent_events": [
                    {
                        "ts": now,
                        "type": "task.created",
                        "summary": f"启动 [{mode}] 协作任务（初始共 {len(built_steps)} 个节点）",
                    }
                ],
                "pending_questions": None,
                "messages": [],
                "reviews": [],
                "rulings": [],
                "summary": "",
                "created_at": now,
                "updated_at": now,
            }
            self._append_room_chat(
                st,
                task_obj,
                sender_id="user",
                kind="user_goal",
                content=goal,
                round_label="任务发起",
            )
            st["tasks"].append(task_obj)
            st["active_task_id"] = task_id
            return task_obj

        created = self.store.mutate(_create)
        self._wakeup.set()
        return created

    def _make_step(
        self,
        step_index: int,
        title: str,
        step_type: str,
        member_id: str,
        prompt: str,
        depends_on: List[int],
        review_of: Optional[int],
        read_only: bool,
        write_scope: List[str],
        workspace_scope: str,
        sandbox: str,
        stage_index: int = 0,
        round_label: str = "",
        initial_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "step_index": step_index,
            "title": title,
            "step_type": step_type,
            "stage_index": stage_index,
            "round_label": round_label,
            "member_id": member_id,
            "prompt": prompt,
            "depends_on": depends_on,
            "review_of": review_of,
            "read_only": read_only,
            "write_scope": write_scope,
            "workspace_scope": workspace_scope,
            "sandbox": sandbox,
            "worker_session_id": initial_session_id,
            "status": "pending",
            "repair_count": 0,
            "consult_count": 0,
            "pending_repair_brief": None,
            "pending_user_answer": None,
            "pending_questions": None,
            "handoff_context": None,
            "blocking_reason": None,
            "accepted_attempt_id": None,
            "latest_result_text": "",
            "attempts": [],
        }

    # ------------------------------------------------------------------
    # Live User Chat Interjection, Answer Questions, Followup & Rulings
    # ------------------------------------------------------------------

    def interject_chat(
        self,
        task_id: str,
        content: str,
        target_member_id: Optional[str] = None,
        trigger_reply: bool = True,
    ) -> Dict[str, Any]:
        """Allow human user to post directly into the live Team Chat Room and optionally @wake a member!"""
        content = content.strip()
        if not content:
            raise ValueError("发言内容不能为空")

        def _interj(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            self._append_room_chat(
                st,
                task,
                sender_id="user",
                kind="user_interjection",
                content=content,
                to_member_id=target_member_id or None,
                round_label="负责人插话",
            )
            task["updated_at"] = time.time()

            # If task is currently not running (or user explicitly @picked an idle member to respond), append a live reply turn!
            if trigger_reply and task["status"] not in ("running",):
                chosen_mid = target_member_id
                if not chosen_mid or chosen_mid not in st["members"]:
                    chosen_mid = (
                        task.get("mode_meta", {}).get("host_member_id")
                        or task.get("mode_meta", {}).get("chief_member_id")
                        or (task["steps"][-1]["member_id"] if task.get("steps") else "codex_author")
                    )
                prev_same_sess = next(
                    (s.get("worker_session_id") for s in reversed(task.get("steps", [])) if s["member_id"] == chosen_mid and s.get("worker_session_id")),
                    None,
                )
                prev_done_indices = [s["step_index"] for s in task.get("steps", []) if s["status"] == "succeeded"]
                new_idx = len(task["steps"])
                ro = task.get("sandbox", "read-only") == "read-only"
                sc = task.get("workspace_scope", ".")
                task["steps"].append(
                    self._make_step(
                        step_index=new_idx,
                        title=f"回应负责人指示 ({st['members'][chosen_mid]['name']})",
                        step_type="execution",
                        stage_index=new_idx,
                        round_label="回应负责人",
                        member_id=chosen_mid,
                        prompt=f"项目负责人在群聊中向你发出新指示：\n{content}\n\n请结合当前项目与群聊上下文直接回应或执行交付。",
                        depends_on=prev_done_indices[-2:] if prev_done_indices else [],
                        review_of=None,
                        read_only=ro,
                        write_scope=[] if ro else [sc],
                        workspace_scope=sc,
                        sandbox=task.get("sandbox", "read-only"),
                        initial_session_id=prev_same_sess,
                    )
                )
                task["current_step_index"] = new_idx
                task["archived"] = False
                st["active_task_id"] = task["id"]
                task["status"] = "queued" if st.get("paused") else "running"
                task["blocking_reason"] = None
            return task

        res = self.store.mutate(_interj)
        self._wakeup.set()
        return res

    def answer_questions(
        self,
        task_id: str,
        answers: Optional[List[Dict[str, Any]]] = None,
        custom_note: str = "",
        use_recommended: bool = False,
    ) -> Dict[str, Any]:
        def _ans(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            pq = task.get("pending_questions")
            step_idx = pq["step_index"] if isinstance(pq, dict) and "step_index" in pq else (task.get("current_step_index") or 0)
            step = task["steps"][int(step_idx)]
            if step["status"] == "running":
                raise ValueError("该步骤已在运行中，请勿重复提交")

            qs_list = (pq.get("questions") if isinstance(pq, dict) else None) or step.get("pending_questions") or []
            lines = ["【用户已完成问题与选项确认，请沿用当前上下文直接完成交付（不要再次重复提问）】"]

            if use_recommended:
                for q in qs_list:
                    rec_opt = (q.get("options") or ["按推荐方案继续"])[0]
                    lines.append(f"- {q.get('question')} → 用户确认采用：{rec_opt}")
                if not qs_list:
                    lines.append("- 用户确认：请按你推荐的最优默认方案直接继续并完成交付。")
            else:
                ans_by_id = {str(a.get("id")): a for a in (answers or []) if isinstance(a, dict)}
                for idx, q in enumerate(qs_list):
                    qid = str(q.get("id") or f"q{idx + 1}")
                    a_obj = ans_by_id.get(qid, {})
                    selected = a_obj.get("selected")
                    sel_str = "；".join(str(x) for x in selected if str(x).strip()) if isinstance(selected, list) else str(selected or "").strip()
                    other_txt = str(a_obj.get("other") or "").strip()
                    combined = " | ".join(x for x in (sel_str, f"补充: {other_txt}" if other_txt else "") if x)
                    if not combined and q.get("options"):
                        combined = q["options"][0]
                    lines.append(f"- {q.get('question')} → 用户选择：{combined or '见整体补充说明'}")

            if custom_note and custom_note.strip():
                lines.append(f"- 用户补充说明：{custom_note.strip()}")

            formatted_reply = "\n".join(lines)
            now = time.time()

            step["pending_user_answer"] = formatted_reply
            step["pending_questions"] = None
            step["status"] = "pending"
            step["blocking_reason"] = None

            task["pending_questions"] = None
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["current_step_index"] = step["step_index"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = now

            self._append_room_chat(
                st,
                task,
                sender_id="user",
                kind="user_choice",
                content=formatted_reply,
                to_member_id=step["member_id"],
                step_index=step["step_index"],
                round_label="选项确认",
            )
            task["messages"].append(
                {
                    "id": _new_id("MSG"),
                    "ts": now,
                    "sender_id": "user",
                    "msg_type": "handoff",
                    "step_index": step["step_index"],
                    "content": formatted_reply,
                }
            )
            task["recent_events"].append(
                {"ts": now, "type": "user.answered_questions", "summary": f"用户已提交选项确认，恢复步骤 {step['step_index']} 原会话继续"}
            )
            return task

        res = self.store.mutate(_ans)
        self._wakeup.set()
        return res

    def attach_plan(self, task_id: str, steps: List[Dict[str, Any]], coordinator_note: str = "") -> Dict[str, Any]:
        if not steps or len(steps) > 8:
            raise ValueError("计划步骤数量需在 1 到 8 步之间")
        self.touch_coordinator_heartbeat()

        def _attach(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            if task["steps"]:
                raise ValueError("该任务已包含执行步骤")
            built = []
            for idx, raw_s in enumerate(steps):
                mid = raw_s["member_id"]
                if mid not in st["members"]:
                    raise ValueError(f"成员不存在: {mid}")
                ro = bool(raw_s.get("read_only", True))
                built.append(
                    self._make_step(
                        step_index=idx,
                        title=raw_s.get("title") or f"步骤 {idx}",
                        step_type=raw_s.get("step_type") or ("review" if raw_s.get("review_of") is not None else "execution"),
                        member_id=mid,
                        prompt=raw_s["prompt"],
                        depends_on=[int(x) for x in raw_s.get("depends_on", [])],
                        review_of=int(raw_s["review_of"]) if raw_s.get("review_of") is not None else None,
                        read_only=ro,
                        write_scope=raw_s.get("write_scope") or raw_s.get("file_scope") or [],
                        workspace_scope=raw_s.get("workspace_scope") or ".",
                        sandbox="read-only" if ro else "workspace-write",
                    )
                )
            task["steps"] = built
            task["current_step_index"] = 0
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return task

        res = self.store.mutate(_attach)
        self._wakeup.set()
        return res

    def post_message(
        self,
        task_id: str,
        sender_id: str,
        msg_type: str,
        content: str,
        step_index: Optional[int] = None,
        require_ruling: bool = False,
    ) -> Dict[str, Any]:
        if msg_type not in ("question", "deviation", "handoff", "coordinator_note"):
            msg_type = "question"
        content = content.strip()
        if not content:
            raise ValueError("消息内容不能为空")

        def _post(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            msg = {
                "id": _new_id("MSG"),
                "ts": time.time(),
                "sender_id": sender_id,
                "msg_type": msg_type,
                "step_index": step_index,
                "content": content,
            }
            task["messages"].append(msg)
            self._append_room_chat(st, task, sender_id=sender_id, kind=msg_type, content=content, step_index=step_index)
            if require_ruling:
                task["status"] = "needs_ruling"
                task["blocking_reason"] = f"成员 {sender_id} 提出问题/偏离待裁决: {content[:100]}"
            task["updated_at"] = msg["ts"]
            return msg

        return self.store.mutate(_post)

    def followup_task(self, task_id: str, prompt: str, step_index: Optional[int] = None) -> Dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("追问内容不能为空")

        def _fup(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            if not task["steps"]:
                raise ValueError("任务尚无执行步骤，无法追问")
            target_idx = (len(task["steps"]) - 1) if step_index is None else int(step_index)
            base_step = task["steps"][target_idx]
            if base_step["status"] == "running":
                raise ValueError("该步骤正在运行中，请勿重复提交")

            mid = base_step["member_id"]
            sess = base_step.get("worker_session_id")

            for other in st["tasks"]:
                if other["id"] == task_id:
                    continue
                if other["created_at"] > task["created_at"] and other["status"] in (
                    "running",
                    "queued",
                    "needs_ruling",
                    "blocked",
                    "needs_verification",
                ):
                    for os_step in other.get("steps", []):
                        if os_step.get("member_id") == mid and (
                            (sess and os_step.get("worker_session_id") == sess)
                            or os_step.get("status") in ("pending", "running", "needs_ruling", "needs_verification", "blocked")
                        ):
                            raise ValueError(
                                f"冲突拦截 (conflict_requires_ruling)：成员 {mid} 在更新的任务 {other['id']} 中存在未结束轮次 ({other['status']})，不得直接恢复旧轮次"
                            )

            new_idx = len(task["steps"])
            new_step = self._make_step(
                step_index=new_idx,
                title=f"续聊追问 #{new_idx}",
                step_type="execution",
                stage_index=new_idx,
                round_label="同会话追问",
                member_id=mid,
                prompt=prompt,
                depends_on=[target_idx],
                review_of=None,
                read_only=base_step.get("read_only", True),
                write_scope=list(base_step.get("write_scope", [])),
                workspace_scope=base_step.get("workspace_scope", "."),
                sandbox=base_step.get("sandbox", "read-only"),
                initial_session_id=sess,
            )
            task["steps"].append(new_step)
            self._append_room_chat(st, task, sender_id="user", kind="user_interjection", content=prompt, to_member_id=mid, step_index=new_idx, round_label="追问")
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["current_step_index"] = new_idx
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return task

        res = self.store.mutate(_fup)
        self._wakeup.set()
        return res

    def submit_ruling(
        self,
        task_id: str,
        step_index: int,
        decision: str,
        reason: str,
        repair_instructions: Optional[str] = None,
        decided_by: str = "ag_coordinator",
    ) -> Dict[str, Any]:
        if decision not in ("accept", "reject", "defer"):
            raise ValueError("裁决动作必须为 accept / reject / defer")
        reason = reason.strip()
        if not reason:
            raise ValueError("总管裁决必须记录明确理由")
        self.touch_coordinator_heartbeat(decided_by)

        def _rule(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            now = time.time()
            task["rulings"].append(
                {"id": _new_id("RULE"), "ts": now, "step_index": int(step_index), "decision": decision, "reason": reason, "decided_by": decided_by}
            )
            self._append_room_chat(st, task, sender_id="user", kind="ruling", content=f"裁决 [{decision}]: {reason}", round_label="总管裁决")

            if step.get("review_of") is not None:
                rev_step = step
                target_step = task["steps"][int(step["review_of"])]
            else:
                target_step = step
                rev_step = next((s for s in task["steps"] if s.get("review_of") == target_step["step_index"]), None)

            if decision == "accept":
                last_rev = next((r for r in reversed(task["reviews"]) if r.get("target_step_index") == target_step["step_index"]), None)
                brief = repair_instructions or (
                    f"审查问题：{last_rev.get('problem')}\n证据：{last_rev.get('evidence')}\n期望修复：{last_rev.get('expected_fix')}"
                    if last_rev
                    else reason
                )
                target_step["repair_count"] = int(target_step.get("repair_count", 0)) + 1
                target_step["pending_repair_brief"] = f"【总管裁决采纳审查意见 - 定向修复】\n裁决理由：{reason}\n{brief}"
                target_step["status"] = "pending"
                target_step["blocking_reason"] = None
                if rev_step is not None:
                    rev_step["status"] = "pending"
                    rev_step["blocking_reason"] = None
                task["status"] = "queued" if st.get("paused") else "running"
                task["blocking_reason"] = None
                task["current_step_index"] = target_step["step_index"]
            elif decision == "reject":
                if target_step["attempts"]:
                    target_step["accepted_attempt_id"] = target_step["attempts"][-1]["attempt_id"]
                target_step["status"] = "succeeded"
                target_step["blocking_reason"] = None
                if rev_step is not None:
                    rev_step["status"] = "succeeded"
                    rev_step["blocking_reason"] = None
                task["blocking_reason"] = None
                self._refresh_task_overall_status_unlocked(st, task)
            else:
                task["status"] = "needs_ruling"
                task["blocking_reason"] = f"总管暂缓裁决：{reason}"

            task["updated_at"] = now
            return task

        res = self.store.mutate(_rule)
        self._wakeup.set()
        return res

    def reassign_step(self, task_id: str, step_index: int, new_member_id: str, reason: str = "总管改派成员接手") -> Dict[str, Any]:
        def _reassign(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            if step["status"] == "running":
                raise ValueError("该步骤正在运行，请先中止当前尝试再改派")
            if new_member_id not in st["members"]:
                raise ValueError(f"新成员不存在: {new_member_id}")

            old_member = step["member_id"]
            step["member_id"] = new_member_id
            step["worker_session_id"] = None
            step["handoff_context"] = {
                "reassigned_from": old_member,
                "reassigned_to": new_member_id,
                "reason": reason,
                "goal": task["goal"],
                "constraints": f"read_only={step['read_only']}, workspace_scope={step['workspace_scope']}, sandbox={step['sandbox']}",
                "completed_steps": [
                    f"- 步骤 {s['step_index']} ({s['title']}): {s['latest_result_text'][:200]}"
                    for s in task["steps"]
                    if s["status"] == "succeeded" and s["step_index"] != step["step_index"]
                ],
                "prior_outputs": [
                    f"- 尝试 {a['attempt_id']} (成员={a['member_id']}, 状态={a['status']}): {(a.get('result_text') or a.get('partial_text') or '')[:200]}"
                    for a in step["attempts"]
                ],
                "open_issues_and_evidence": [
                    f"- 审查问题: {r.get('problem')} | 证据: {r.get('evidence')}"
                    for r in task["reviews"]
                    if r.get("target_step_index") == step["step_index"] and r.get("verdict") == "REJECT"
                ],
                "ts": time.time(),
            }
            step["status"] = "pending"
            step["blocking_reason"] = None
            self._append_room_chat(
                st,
                task,
                sender_id="user",
                kind="handoff",
                content=f"将 [{step['title']}] 改派由 @{st['members'][new_member_id]['name']} 接手（{reason}）",
                to_member_id=new_member_id,
                step_index=int(step_index),
            )
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return task

        res = self.store.mutate(_reassign)
        self._wakeup.set()
        return res

    def set_pause(self, paused: bool) -> Dict[str, Any]:
        def _p(st: Dict[str, Any]) -> Dict[str, Any]:
            st["paused"] = bool(paused)
            now = time.time()
            for t in st["tasks"]:
                if paused and t["status"] == "running":
                    if not any(s["status"] == "running" for s in t["steps"]):
                        t["status"] = "queued"
                        t["blocking_reason"] = "已暂停新任务派发，等待恢复"
                elif not paused and t["status"] == "queued":
                    t["status"] = "running"
                    t["blocking_reason"] = None
                    t["updated_at"] = now
            return {"paused": st["paused"]}

        res = self.store.mutate(_p)
        self._wakeup.set()
        return res

    def abort_attempt(
        self,
        task_id: Optional[str] = None,
        step_index: Optional[int] = None,
        attempt_id: Optional[str] = None,
        reason: str = "用户请求中止当前尝试",
    ) -> Dict[str, Any]:
        snap = self.store.snapshot()
        target_att_ids: List[str] = [attempt_id] if attempt_id else []
        if not target_att_ids and task_id is not None:
            task = self._find_task(snap, task_id)
            for s in task["steps"]:
                if step_index is not None and s["step_index"] != int(step_index):
                    continue
                for a in s["attempts"]:
                    if a["status"] == "running":
                        target_att_ids.append(a["attempt_id"])

        if not target_att_ids:
            return {"aborted": False, "message": "未找到正在运行的尝试"}

        last_kill: Dict[str, Any] = {}
        for aid in target_att_ids:
            k1 = self.adapter.abort_attempt(aid)
            k2 = self.agy_adapter.abort_attempt(aid)
            last_kill = k1 if k1.get("found") else k2

        self._wakeup.set()
        return {"aborted": True, "attempt_ids": target_att_ids, **last_kill}

    def continue_task(self, task_id: str) -> Dict[str, Any]:
        def _cont(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            if any(s["status"] == "running" for s in task["steps"]):
                return {"status": "already_running", "message": "任务已有步骤正在执行", "task": task}

            proj_root = task.get("project_path") or st["binding"]["project_path"]
            if not os.path.isdir(proj_root):
                raise ValueError(f"任务绑定项目目录不可用: {proj_root}")

            for s in task["steps"]:
                if s["status"] == "succeeded":
                    continue
                mid = s["member_id"]
                sess = s.get("worker_session_id")
                for other in st["tasks"]:
                    if other["id"] == task_id:
                        continue
                    if other["created_at"] > task["created_at"] and other["status"] in (
                        "running",
                        "queued",
                        "needs_ruling",
                        "needs_verification",
                        "blocked",
                    ):
                        for os_step in other.get("steps", []):
                            if os_step.get("member_id") == mid and (
                                (sess and os_step.get("worker_session_id") == sess)
                                or os_step.get("status") in ("pending", "running", "needs_ruling", "needs_verification", "blocked")
                            ):
                                raise ValueError(
                                    f"冲突拦截 (conflict_requires_ruling)：成员 {mid} 在更新任务 {other['id']} 中有未结束轮次，请先处理冲突"
                                )

            resumed_steps = []
            for s in task["steps"]:
                if s["status"] in ("aborted", "failed", "needs_verification", "blocked"):
                    if s["status"] == "failed":
                        # Clear broken session ID so network/executor failure doesn't replay corrupted conversation state
                        s["worker_session_id"] = None
                    s["status"] = "pending"
                    s["blocking_reason"] = None
                    resumed_steps.append(s["step_index"])

            if not resumed_steps and all(s["status"] == "succeeded" for s in task["steps"]):
                task["status"] = "succeeded"
                return {"status": "already_completed", "message": "所有步骤均已完成", "task": task}

            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return {"status": "resumed", "resumed_steps": resumed_steps, "task": task}

        res = self.store.mutate(_cont)
        self._wakeup.set()
        return res

    def accept_step_partial(self, task_id: str, step_index: int) -> Dict[str, Any]:
        """Adopt already-streamed partial/result text of an interrupted/failed step as its official deliverable."""
        def _adopt(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            if step["status"] == "running":
                raise ValueError("该步骤正在运行中，请先等待结束或中止")
            text_candidate = ""
            chosen_att = None
            for att in reversed(step.get("attempts", [])):
                cand = (att.get("result_text") or att.get("partial_text") or "").strip()
                if cand:
                    text_candidate = cand
                    chosen_att = att
                    break
            if not text_candidate:
                raise ValueError("该步骤尚无已生成的流式文本可供采纳")

            now = time.time()
            if chosen_att:
                chosen_att["result_text"] = text_candidate
                chosen_att["status"] = "succeeded"
                chosen_att["blocking_reason"] = None
                step["accepted_attempt_id"] = chosen_att["attempt_id"]
            step["latest_result_text"] = text_candidate
            step["status"] = "succeeded"
            step["blocking_reason"] = None

            chat_kind = "deliverable" if step.get("step_type") in ("synthesis", "execution") else "speech"
            self._append_room_chat(
                st,
                task,
                sender_id=step["member_id"],
                kind=chat_kind,
                content=text_candidate,
                step_index=step["step_index"],
                round_label=step.get("round_label") or step["title"],
            )
            task.setdefault("deliverables", []).append(
                {
                    "step_index": step["step_index"],
                    "title": step["title"],
                    "member_id": step["member_id"],
                    "attempt_id": chosen_att["attempt_id"] if chosen_att else "manual_adopt",
                    "session_id": chosen_att.get("session_id") if chosen_att else None,
                    "preview": text_candidate[:240],
                    "ts": now,
                }
            )
            task["recent_events"].append(
                {
                    "ts": now,
                    "type": "step.partial_adopted",
                    "summary": f"已直接采纳步骤 {step['step_index']} ({step['title']}) 已生成的 {len(text_candidate)} 字成果",
                }
            )
            self._refresh_task_overall_status_unlocked(st, task)
            task["updated_at"] = now
            return task

        res = self.store.mutate(_adopt)
        self._wakeup.set()
        return res

    def retry_single_step(
        self,
        task_id: str,
        step_index: int,
        reset_session: bool = True,
        new_member_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retry a single failed/blocked/aborted step cleanly (with fresh session by default)."""
        def _retry(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            if step["status"] == "running":
                raise ValueError("该步骤已在运行中")
            if new_member_id:
                if new_member_id not in st["members"]:
                    raise ValueError(f"成员不存在: {new_member_id}")
                step["member_id"] = new_member_id
                step["worker_session_id"] = None
            elif reset_session:
                step["worker_session_id"] = None

            step["status"] = "pending"
            step["blocking_reason"] = None
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["current_step_index"] = step["step_index"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            task["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "step.retried",
                    "summary": f"已开启干净会话重跑步骤 {step['step_index']} ({step['title']})",
                }
            )
            return task

        res = self.store.mutate(_retry)
        self._wakeup.set()
        return res

    # ------------------------------------------------------------------
    # Internal Scheduler, @Mention Consultation & Execution
    # ------------------------------------------------------------------

    def _find_task(self, st: Dict[str, Any], task_id: str) -> Dict[str, Any]:
        for t in st.get("tasks", []):
            if t.get("id") == task_id:
                return t
        raise ValueError(f"未找到任务: {task_id}")

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wakeup.wait(timeout=0.25)
            self._wakeup.clear()
            if self._stop_event.is_set():
                break
            try:
                self._schedule_tick()
            except Exception:
                pass

    def _schedule_tick(self) -> None:
        with self._sched_lock:
            to_launch: List[Tuple[str, int, str]] = []

            def _pick_candidates(st: Dict[str, Any]) -> None:
                if st.get("paused"):
                    return
                max_conc = int(st.get("max_concurrency", 3))
                active_count = len(self._running_attempts)
                if active_count >= max_conc:
                    return

                busy_members = {mid for mid, m in st["members"].items() if m.get("status") == "working"}
                busy_sessions = {info["session_id"] for info in self._running_attempts.values() if info.get("session_id")}
                active_writes: List[Tuple[str, List[str]]] = [
                    (info["project_root"], info["write_scope"])
                    for info in self._running_attempts.values()
                    if not info.get("read_only", True)
                ]

                for task in st.get("tasks", []):
                    if task.get("status") not in ("running", "queued"):
                        continue
                    proj_root = task.get("project_path") or st["binding"]["project_path"]
                    steps = task.get("steps", [])
                    for step in steps:
                        if active_count >= max_conc:
                            return
                        if step.get("status") != "pending":
                            continue
                        deps_ok = all(
                            0 <= d < len(steps) and steps[d].get("status") == "succeeded"
                            for d in step.get("depends_on", [])
                        )
                        if not deps_ok:
                            continue

                        mid = step["member_id"]
                        mem = st["members"].get(mid)
                        if not mem or mem.get("client") not in SUPPORTED_CLIENTS:
                            continue
                        if mid in busy_members:
                            continue
                        sess = step.get("worker_session_id")
                        if sess and sess in busy_sessions:
                            continue
                        if not step.get("read_only", True):
                            cand_scope = step.get("write_scope") or []
                            conflict = any(
                                normalize_win_path(proj_root) == normalize_win_path(pr)
                                and _scopes_overlap(cand_scope, ws, proj_root)
                                for pr, ws in active_writes
                            )
                            if conflict:
                                continue

                        att_id = _new_id("ATT")
                        now = time.time()
                        req_cfg = {
                            "client": mem["client"],
                            "model": mem["model"],
                            "reasoning_effort": mem["reasoning_effort"],
                            "sandbox": step.get("sandbox", mem.get("sandbox", "read-only")),
                        }
                        step["attempts"].append(
                            {
                                "attempt_id": att_id,
                                "round_num": len(step["attempts"]) + 1,
                                "member_id": mid,
                                "session_id": sess,
                                "is_fallback_session": False,
                                "fallback_of_session_id": None,
                                "requested_config": req_cfg,
                                "accepted_config": {},
                                "prompt_sent": "",
                                "partial_text": "",
                                "result_text": "",
                                "events_summary": [],
                                "stderr_excerpt": "",
                                "status": "running",
                                "blocking_reason": None,
                                "started_at": now,
                                "ended_at": None,
                                "pid": None,
                            }
                        )
                        step["status"] = "running"
                        step["blocking_reason"] = None
                        task["status"] = "running"
                        task["current_step_index"] = step["step_index"]
                        task["blocking_reason"] = None
                        task["updated_at"] = now

                        mem["status"] = "working"
                        mem["live_activity"] = step.get("round_label") or step["title"]
                        mem["current_task_id"] = task["id"]
                        mem["current_attempt_id"] = att_id

                        busy_members.add(mid)
                        if sess:
                            busy_sessions.add(sess)
                        if not step.get("read_only", True):
                            active_writes.append((proj_root, step.get("write_scope") or []))

                        self._running_attempts[att_id] = {
                            "task_id": task["id"],
                            "step_index": step["step_index"],
                            "member_id": mid,
                            "session_id": sess,
                            "project_root": proj_root,
                            "read_only": step.get("read_only", True),
                            "write_scope": step.get("write_scope") or [],
                        }
                        active_count += 1
                        to_launch.append((task["id"], step["step_index"], att_id))

            self.store.mutate(_pick_candidates)

            for task_id, step_index, att_id in to_launch:
                threading.Thread(
                    target=self._execute_attempt_worker,
                    args=(task_id, step_index, att_id),
                    name=f"Worker-{att_id}",
                    daemon=True,
                ).start()

    def _build_step_prompt(self, st: Dict[str, Any], task: Dict[str, Any], step: Dict[str, Any], work_dir: str) -> str:
        if step.get("pending_user_answer"):
            return step["pending_user_answer"]

        mem = st["members"][step["member_id"]]
        role_obj = st["roles"].get(mem["role"], {})
        parts: List[str] = [
            f"【当前项目工作目录】{work_dir}",
            f"【你的身份】{mem.get('name')} (ID: {step['member_id']}) · 职责：{role_obj.get('name', '成员')} — {role_obj.get('description', '')}",
        ]
        if step.get("read_only", True):
            parts.append("【权限边界】当前为只读模式 (read-only)：可读取检查项目内文件，不得修改或删除文件。")

        # Inject Shared Team Chat Room transcript (up to 8,000 chars per message so tables & evidence are never cut!)
        room_msgs = task.get("chat_room") or []
        tmp_dir, _ = get_runtime_dirs()
        room_md_path = os.path.join(tmp_dir, "team_rooms", f"{task.get('id', 'task')}.md")
        if len(room_msgs) > 1:
            transcript_lines = []
            for cm in room_msgs[-16:]:
                to_tag = f" @{cm['to_member_name']}" if cm.get("to_member_name") else ""
                transcript_lines.append(
                    f"[{cm.get('round_label') or cm['kind']}] {cm['sender_name']}{to_tag}：\n{_smart_clip(cm['content'], 8000)}"
                )
            parts.append(
                f"【团队共享群聊记录（完整纪要文件：{room_md_path}）】\n"
                + "\n---\n".join(transcript_lines)
            )
        elif step.get("depends_on"):
            dep_blocks = []
            for d_idx in step["depends_on"]:
                d_step = task["steps"][d_idx]
                dep_blocks.append(
                    f"--- 前序步骤 {d_idx} ({d_step['title']} / {st['members'].get(d_step['member_id'], {}).get('name', d_step['member_id'])}) 成果 ---\n{d_step.get('latest_result_text', '')}"
                )
            parts.append("\n".join(dep_blocks))

        if step.get("handoff_context"):
            hc = step["handoff_context"]
            parts.append(
                "【改派交接摘要】\n"
                f"- 原负责人：{hc.get('reassigned_from')} → 现负责人：{hc.get('reassigned_to')}（原因：{hc.get('reason')}）\n"
                f"- 已完成步骤：{json.dumps(hc.get('completed_steps', []), ensure_ascii=False)}\n"
                f"- 前序产物：{json.dumps(hc.get('prior_outputs', []), ensure_ascii=False)}\n"
                f"- 待解决问题：{json.dumps(hc.get('open_issues_and_evidence', []), ensure_ascii=False)}"
            )

        if step.get("pending_repair_brief"):
            parts.append(step["pending_repair_brief"])
        else:
            parts.append(f"【本步任务】\n{step['prompt']}")

        stype = step.get("step_type", "execution")
        if step.get("review_of") is not None:
            parts.append(
                "【独立审查输出格式契约】\n"
                "请严格核对被审查步骤的实际输出证据，仅输出如下 JSON 对象：\n"
                '{"verdict": "ACCEPT 或 REJECT", "problem": "具体问题（通过填为空字符串）", '
                '"evidence": "可定位的具体证据", "impact": "问题影响", '
                '"expected_fix": "限定原范围的具体修复要求", "scope_expanded": false}'
            )
        elif stype != "chief_plan":
            # Distinguish seated participants vs external standby specialists!
            seated_ids = set((task.get("mode_meta") or {}).get("participants") or [])
            in_room_peers = []
            standby_peers = []
            for pid, pinfo in st["members"].items():
                if pid == step["member_id"] or pinfo.get("client") not in SUPPORTED_CLIENTS:
                    continue
                desc = f"{pid} ({pinfo['name']} · {st['roles'].get(pinfo['role'], {}).get('name', '')})"
                if not seated_ids or pid in seated_ids or pid == (task.get("mode_meta") or {}).get("host_member_id"):
                    in_room_peers.append(desc)
                else:
                    standby_peers.append(desc)

            if (in_room_peers or standby_peers) and stype in ("execution", "discussion", "synthesis"):
                roster_block = f"本场在席队友：{', '.join(in_room_peers) if in_room_peers else '无'}"
                if standby_peers:
                    roster_block += f"\n场外支援专员（未参加本场圆桌发言，仅可请教具体技术/文献核查，切勿向其索要本场发言记录）：{', '.join(standby_peers)}"
                parts.append(
                    "【团队内部 @ 提问协商协议（可选）】\n"
                    f"{roster_block}\n"
                    "注意：上方共享群聊记录已包含队友的完整发言，无需为索要聊天记录而提问。仅当需要某位队友协助核查某个关键事实/细节时，可在末尾附上代码块唤醒TA答复：\n"
                    "```team_consult\n"
                    '{"to":"队友id","question":"具体请教或要求协助核对的问题"}\n'
                    "```"
                )
            parts.append(
                "【向人类负责人提问确认协议（可选）】\n"
                "严禁调用内置 ask_question 弹窗工具。仅当存在必须由人类负责人拍板的方向选择时，在末尾附上如下提问块暂停等待点选：\n"
                "```team_questions\n"
                '{"questions":[{"id":"q1","question":"需要确认的问题","multi_select":false,"options":["(推荐) 方案A","方案B"]}]}\n'
                "```"
            )

        return "\n\n".join(parts)

    def _invoke_member_turn(
        self,
        mem: Dict[str, Any],
        prompt_text: str,
        session_id: Optional[str],
        work_dir: str,
        sandbox_arg: str,
        agy_proj_id: Optional[str],
        attempt_id: str,
        on_start: Any = None,
        on_event: Any = None,
        on_partial_text: Any = None,
        lightweight_effort: bool = False,
    ) -> CodexExecutionResult:
        client_kind = mem.get("client", "codex")
        model_arg = None if mem["model"] in ("client_default", "client_current_setting") else mem["model"]
        effort_arg = None if mem["reasoning_effort"] in ("client_default", "client_current_setting") else mem["reasoning_effort"]
        if lightweight_effort and effort_arg in ("high", "xhigh", "max"):
            try:
                self._validate_client_model_effort(client_kind, model_arg, "medium")
                effort_arg = "medium"
            except Exception:
                pass
        if client_kind == "antigravity":
            return self.agy_adapter.execute_turn(
                prompt=prompt_text,
                session_id=session_id,
                model=model_arg,
                reasoning_effort=effort_arg,
                cwd=work_dir,
                sandbox=sandbox_arg,
                agy_project_id=agy_proj_id,
                attempt_id=attempt_id,
                on_start=on_start,
                on_event=on_event,
                on_partial_text=on_partial_text,
            )
        return self.adapter.execute_turn(
            prompt=prompt_text,
            session_id=session_id,
            model=model_arg,
            reasoning_effort=effort_arg,
            cwd=work_dir,
            sandbox=sandbox_arg,
            attempt_id=attempt_id,
            on_start=on_start,
            on_event=on_event,
            on_partial_text=on_partial_text,
        )

    def _execute_attempt_worker(self, task_id: str, step_index: int, attempt_id: str) -> None:
        try:
            snap = self.store.snapshot()
            task = self._find_task(snap, task_id)
            step = task["steps"][step_index]
            mem = snap["members"][step["member_id"]]
            proj_root = task.get("project_path") or snap["binding"]["project_path"]
            rel_scope = step.get("workspace_scope") or mem.get("workspace_scope") or "."
            work_dir = os.path.normpath(os.path.join(proj_root, rel_scope))

            prompt_text = self._build_step_prompt(snap, task, step, work_dir)
            sess_to_use = step.get("worker_session_id")
            sandbox_arg = step.get("sandbox") or mem.get("sandbox") or "read-only"
            agy_proj_id = snap["binding"].get("project_agy_map", {}).get(normalize_win_path(work_dir)) or (
                snap["binding"].get("antigravity_project_id")
                if normalize_win_path(work_dir) == normalize_win_path(snap["binding"]["project_path"]) and snap["binding"].get("antigravity_project_id")
                else None
            )

            def _save_prompt(st: Dict[str, Any]) -> None:
                t = self._find_task(st, task_id)
                t["steps"][step_index]["attempts"][-1]["prompt_sent"] = prompt_text

            self.store.mutate(_save_prompt)

            def _on_start(pid: int) -> None:
                def _upd_pid(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    att = next(a for a in t["steps"][step_index]["attempts"] if a["attempt_id"] == attempt_id)
                    att["pid"] = pid
                    t["recent_events"].append(
                        {"ts": time.time(), "type": "worker.started", "summary": f"步骤 {step_index} ({mem['name']}) 已启动 PID={pid}"}
                    )
                    t["recent_events"] = t["recent_events"][-40:]

                self.store.mutate(_upd_pid)

            last_partial_flush = {"ts": 0.0}

            def _on_partial_text(txt: str) -> None:
                now = time.time()
                if now - last_partial_flush["ts"] < 0.35:
                    return
                last_partial_flush["ts"] = now

                def _upd_p(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    att = next(a for a in t["steps"][step_index]["attempts"] if a["attempt_id"] == attempt_id)
                    att["partial_text"] = txt
                    t["updated_at"] = now

                self.store.mutate(_upd_p)

            def _on_event(ev: Dict[str, Any]) -> None:
                def _upd_ev(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    s = t["steps"][step_index]
                    att = next(a for a in s["attempts"] if a["attempt_id"] == attempt_id)
                    att["events_summary"].append(ev)
                    att["events_summary"] = att["events_summary"][-30:]
                    if ev.get("session_id"):
                        att["session_id"] = ev["session_id"]
                        s["worker_session_id"] = ev["session_id"]
                    if ev.get("activity_label") and st["members"].get(step["member_id"], {}).get("status") == "working":
                        st["members"][step["member_id"]]["live_activity"] = ev["activity_label"]
                    t["recent_events"].append(
                        {"ts": ev.get("ts", time.time()), "type": ev.get("type", "event"), "summary": f"[{mem['name']}] {ev.get('summary', '')}"}
                    )
                    t["recent_events"] = t["recent_events"][-40:]
                    t["updated_at"] = time.time()

                self.store.mutate(_upd_ev)

            is_light = step.get("step_type") in ("opening", "chief_plan")
            res = self._invoke_member_turn(
                mem, prompt_text, sess_to_use, work_dir, sandbox_arg, agy_proj_id, attempt_id, _on_start, _on_event, _on_partial_text, lightweight_effort=is_light
            )

            used_fallback = False
            fallback_of = None
            if (
                sess_to_use
                and not res.aborted
                and not res.policy_blocked
                and (res.session_not_found or (res.exit_code != 0 and not res.turn_completed))
            ):
                fallback_of = sess_to_use
                used_fallback = True
                prior_texts = [
                    a.get("result_text") or a.get("partial_text") or ""
                    for a in step.get("attempts", [])[:-1]
                    if a.get("result_text") or a.get("partial_text")
                ]
                handoff_prompt = (
                    f"【替代会话交接说明（非原会话恢复，原会话 {fallback_of} 不可用）】\n"
                    f"历史轮次摘要：{json.dumps(prior_texts[-2:], ensure_ascii=False)}\n\n"
                    f"{prompt_text}"
                )
                res = self._invoke_member_turn(
                    mem, handoff_prompt, None, work_dir, sandbox_arg, agy_proj_id, attempt_id, _on_start, _on_event, _on_partial_text
                )

            # Live Peer @Mention Consultation Loop (up to 3 consultations per step!)
            consult_loops = 0
            while res.final_status == "succeeded" and consult_loops < 3:
                cur_snap = self.store.snapshot()
                consult_req, cleaned_before_consult = _extract_team_consult(
                    res.response_text, cur_snap["members"], step["member_id"]
                )
                if not consult_req:
                    break
                consult_loops += 1
                peer_id = consult_req["to"]
                peer_q = consult_req["question"]
                peer_mem = cur_snap["members"][peer_id]

                def _log_ask(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    if cleaned_before_consult:
                        self._append_room_chat(
                            st, t, sender_id=step["member_id"], kind="speech", content=cleaned_before_consult,
                            step_index=step_index, round_label=step.get("round_label") or step["title"]
                        )
                    self._append_room_chat(
                        st, t, sender_id=step["member_id"], kind="mention_ask", content=peer_q,
                        to_member_id=peer_id, step_index=step_index, round_label="@队友协商"
                    )
                    st["members"][step["member_id"]]["live_activity"] = f"正在等待 @{peer_mem['name']} 答复..."
                    if st["members"][peer_id]["status"] == "idle":
                        st["members"][peer_id]["status"] = "working"
                        st["members"][peer_id]["live_activity"] = f"正在回应 @{mem['name']} 的提问"
                    t["recent_events"].append(
                        {"ts": time.time(), "type": "agent.mention_ask", "summary": f"{mem['name']} @{peer_mem['name']} 发起协商提问"}
                    )

                self.store.mutate(_log_ask)

                peer_prompt = (
                    f"【当前项目工作目录】{work_dir}\n"
                    f"【团队实时协助请求】队友 {mem['name']} 在执行“{task['title']}”时向你 (@{peer_mem['name']}) 提问求助：\n"
                    f"“{peer_q}”\n\n"
                    "请只读查看必要材料或依据你的专业职责给出简明、准确、可直接采纳的核查意见（不要再发起新的 team_consult）。"
                )
                peer_res = self._invoke_member_turn(
                    peer_mem, peer_prompt, None, work_dir, "read-only", agy_proj_id, f"{attempt_id}-c{consult_loops}", lightweight_effort=True
                )
                peer_reply = peer_res.response_text.strip() or "（已完成核查，无补充异议，可按原计划推进）"

                def _log_reply(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    t["steps"][step_index]["consult_count"] = int(t["steps"][step_index].get("consult_count", 0)) + 1
                    self._append_room_chat(
                        st, t, sender_id=peer_id, kind="mention_reply", content=peer_reply,
                        to_member_id=step["member_id"], step_index=step_index, round_label="@协商答复"
                    )
                    if st["members"][peer_id].get("live_activity", "").startswith("正在回应"):
                        st["members"][peer_id]["status"] = "idle"
                        st["members"][peer_id]["live_activity"] = ""
                    st["members"][step["member_id"]]["live_activity"] = f"收到 @{peer_mem['name']} 反馈，继续定稿"
                    t["recent_events"].append(
                        {"ts": time.time(), "type": "agent.mention_reply", "summary": f"{peer_mem['name']} 已回复 @{mem['name']}"}
                    )

                self.store.mutate(_log_reply)

                followup_to_caller = (
                    (f"{prompt_text}\n\n" if not res.session_id else "")
                    + f"【收到队友 @{peer_mem['name']} 的协商回复】\n{peer_reply}\n\n"
                    + "请结合该反馈直接完成你的本步最终交付（不要重复发起相同提问）。"
                )
                res = self._invoke_member_turn(
                    mem, followup_to_caller, res.session_id, work_dir, sandbox_arg, agy_proj_id, attempt_id, _on_start, _on_event, _on_partial_text
                )

            self._finalize_attempt(task_id, step_index, attempt_id, res, used_fallback, fallback_of)
        finally:
            with self._sched_lock:
                self._running_attempts.pop(attempt_id, None)
            self._wakeup.set()

    def _expand_chief_plan_unlocked(self, st: Dict[str, Any], task: Dict[str, Any], chief_step: Dict[str, Any], raw_text: str) -> None:
        parsed = _extract_json_obj(raw_text) or {}
        raw_steps = parsed.get("steps") if isinstance(parsed.get("steps"), list) else []
        rationale = str(parsed.get("rationale") or "").strip()

        clean_speech = re.sub(r"```(?:chief_plan|json)?\s*\{.*?\}\s*```", "", raw_text, flags=re.S).strip()
        display_plan = clean_speech or rationale or "已完成项目研判并下发点将分工计划。"
        self._append_room_chat(
            st, task, sender_id=chief_step["member_id"], kind="plan", content=display_plan,
            step_index=0, round_label="主帅点将令"
        )

        valid_members = [k for k, v in st["members"].items() if v.get("client") in SUPPORTED_CLIENTS]
        task_sb = task.get("sandbox", "read-only")
        sc = task.get("workspace_scope", ".")

        if not raw_steps:
            # Fallback sensible 2-step dispatch if Chief wrote prose without JSON block
            exec_m = next((k for k in valid_members if k != chief_step["member_id"]), chief_step["member_id"])
            raw_steps = [
                {
                    "title": "专项执行交付",
                    "member_id": exec_m,
                    "step_type": "execution",
                    "depends_on": [],
                    "read_only": (task_sb == "read-only"),
                    "prompt": f"请根据主帅安排完成任务：{task['goal']}\n主帅指示：{display_plan}",
                }
            ]

        recruited: List[str] = []
        offset = len(task["steps"])  # 1 (since chief_plan is step 0)
        for local_i, rs in enumerate(raw_steps[:6]):
            mid = str(rs.get("member_id") or "").strip()
            if mid not in st["members"]:
                mid = valid_members[local_i % len(valid_members)]
            if mid not in recruited:
                recruited.append(mid)

            global_idx = offset + local_i
            raw_deps = [int(d) + offset for d in (rs.get("depends_on") or []) if isinstance(d, int) and 0 <= int(d) < local_i]
            rev_of_raw = rs.get("review_of")
            rev_of = (int(rev_of_raw) + offset) if (isinstance(rev_of_raw, int) and 0 <= int(rev_of_raw) < local_i) else None
            if rev_of is not None and rev_of not in raw_deps:
                raw_deps.append(rev_of)
            deps = raw_deps if raw_deps else [0]

            stype = str(rs.get("step_type") or ("review" if rev_of is not None else "execution"))
            ro = True if (task_sb == "read-only" or rev_of is not None or stype in ("review", "discussion")) else bool(rs.get("read_only", False))

            task["steps"].append(
                self._make_step(
                    step_index=global_idx,
                    title=str(rs.get("title") or f"点将任务 #{local_i + 1}"),
                    step_type=stype,
                    stage_index=1 if raw_deps == [] else 2,
                    round_label="点将执行" if rev_of is None else "独立审计",
                    member_id=mid,
                    prompt=str(rs.get("prompt") or task["goal"]),
                    depends_on=deps,
                    review_of=rev_of,
                    read_only=ro,
                    write_scope=[] if ro else [sc],
                    workspace_scope=sc,
                    sandbox="read-only" if ro else task_sb,
                )
            )

        # Final Chief Sign-Off Summary Step depending on all worker steps!
        worker_indices = list(range(offset, len(task["steps"])))
        signoff_idx = len(task["steps"])
        task["steps"].append(
            self._make_step(
                step_index=signoff_idx,
                title="主帅终审汇总与签发交付",
                step_type="synthesis",
                stage_index=3,
                round_label="主帅签发",
                member_id=chief_step["member_id"],
                prompt=f"请作为主帅审阅全体被点将成员的执行与复核成果，向项目负责人输出清晰、完整的最终汇总交付：\n原始目标：{task['goal']}",
                depends_on=worker_indices,
                review_of=None,
                read_only=True,
                write_scope=[],
                workspace_scope=sc,
                sandbox="read-only",
                initial_session_id=chief_step.get("worker_session_id"),
            )
        )
        task.setdefault("mode_meta", {})["recruited_members"] = recruited
        task["mode_meta"]["rationale"] = rationale or display_plan[:160]

    def _finalize_attempt(
        self,
        task_id: str,
        step_index: int,
        attempt_id: str,
        res: CodexExecutionResult,
        used_fallback: bool,
        fallback_of: Optional[str],
    ) -> None:
        def _fin(st: Dict[str, Any]) -> None:
            now = time.time()
            task = self._find_task(st, task_id)
            step = task["steps"][step_index]
            mem = st["members"][step["member_id"]]
            att = next(a for a in step["attempts"] if a["attempt_id"] == attempt_id)

            att["session_id"] = res.session_id
            att["is_fallback_session"] = used_fallback
            att["fallback_of_session_id"] = fallback_of
            att["accepted_config"] = res.accepted_config
            att["partial_text"] = res.partial_text
            att["result_text"] = res.response_text
            att["stderr_excerpt"] = "\n".join(res.stderr_lines[-8:])
            att["status"] = res.final_status
            att["blocking_reason"] = res.blocking_reason or res.error
            att["ended_at"] = now

            if res.session_id:
                old_mem_sess = mem.get("session_id")
                if old_mem_sess and old_mem_sess != res.session_id:
                    mem.setdefault("session_history", []).append(old_mem_sess)
                mem["session_id"] = res.session_id
                step["worker_session_id"] = res.session_id

            mem["status"] = "idle"
            mem["live_activity"] = ""
            mem["current_task_id"] = None
            mem["current_attempt_id"] = None
            if mem.get("pending_config"):
                pc = mem["pending_config"]
                if pc.get("name"):
                    mem["name"] = pc["name"]
                if pc.get("client") and pc["client"] != mem.get("client"):
                    if mem.get("session_id"):
                        mem.setdefault("session_history", []).append(mem["session_id"])
                    mem["session_id"] = None
                    mem["client"] = pc["client"]
                mem["role"] = pc["role"]
                mem["model"] = pc["model"]
                mem["reasoning_effort"] = pc["reasoning_effort"]
                mem["workspace_scope"] = pc["workspace_scope"]
                mem["sandbox"] = pc["sandbox"]
                mem["pending_config"] = None

            step["pending_repair_brief"] = None
            step["pending_user_answer"] = None
            step["handoff_context"] = None

            if res.final_status == "aborted":
                step["status"] = "aborted"
                step["blocking_reason"] = "尝试已确认中止，保留已有输出"
                task["status"] = "aborted"
                task["blocking_reason"] = f"步骤 {step_index} 已中止"
                task["updated_at"] = now
                return

            if res.final_status == "blocked":
                step["status"] = "blocked"
                step["blocking_reason"] = res.blocking_reason or "策略拦截 (blocked by policy)"
                task["status"] = "blocked"
                task["blocking_reason"] = f"步骤 {step_index} 受阻: {step['blocking_reason']}"
                task["updated_at"] = now
                return

            if res.final_status != "succeeded":
                step["status"] = "failed"
                step["blocking_reason"] = res.error or f"进程非正常退出 (code={res.exit_code})"
                task["status"] = "failed"
                task["blocking_reason"] = f"步骤 {step_index} 执行失败: {step['blocking_reason']}"
                task["updated_at"] = now
                return

            step["latest_result_text"] = res.response_text
            step["accepted_attempt_id"] = attempt_id

            # Chief Dynamic Plan Expansion!
            if step.get("step_type") == "chief_plan":
                step["status"] = "succeeded"
                step["blocking_reason"] = None
                self._expand_chief_plan_unlocked(st, task, step, res.response_text)
                self._refresh_task_overall_status_unlocked(st, task)
                task["updated_at"] = now
                return

            if step.get("review_of") is not None:
                target_idx = int(step["review_of"])
                target_step = task["steps"][target_idx]
                parsed_rev = _extract_json_obj(res.response_text) or {}
                verdict = str(parsed_rev.get("verdict", "")).upper()
                if verdict not in ("ACCEPT", "REJECT"):
                    verdict = "REJECT" if ("reject" in res.response_text.lower() or "不通过" in res.response_text) else "ACCEPT"

                rev_record = {
                    "id": _new_id("REV"),
                    "ts": now,
                    "review_step_index": step_index,
                    "target_step_index": target_idx,
                    "reviewer_id": step["member_id"],
                    "reviewer_session_id": res.session_id,
                    "round": target_step.get("repair_count", 0) + 1,
                    "verdict": verdict,
                    "problem": str(parsed_rev.get("problem") or ""),
                    "evidence": str(parsed_rev.get("evidence") or res.response_text[:200]),
                    "impact": str(parsed_rev.get("impact") or ""),
                    "expected_fix": str(parsed_rev.get("expected_fix") or ""),
                    "scope_expanded": bool(parsed_rev.get("scope_expanded", False)),
                }
                task["reviews"].append(rev_record)
                rev_chat_txt = (
                    f"✅ 审查通过：{rev_record['evidence']}"
                    if verdict == "ACCEPT"
                    else f"⚠️ 审查打回：{rev_record['problem']}\n证据：{rev_record['evidence']}\n修复要求：{rev_record['expected_fix']}"
                )
                self._append_room_chat(
                    st, task, sender_id=step["member_id"], kind="review", content=rev_chat_txt,
                    to_member_id=target_step["member_id"], step_index=step_index, round_label=f"第{rev_record['round']}轮独立审查"
                )

                if verdict == "ACCEPT":
                    step["status"] = "succeeded"
                    step["blocking_reason"] = None
                else:
                    prev_problems = [
                        r["problem"].strip()
                        for r in task["reviews"][:-1]
                        if r["target_step_index"] == target_idx and r["verdict"] == "REJECT" and r["problem"].strip()
                    ]
                    cur_prob = rev_record["problem"].strip()
                    is_repeat = bool(cur_prob and cur_prob in prev_problems)
                    cap = int(task.get("max_auto_repairs", 2))
                    cur_repairs = int(target_step.get("repair_count", 0))

                    if task.get("require_manual_ruling"):
                        step["status"] = "needs_ruling"
                        task["status"] = "needs_ruling"
                        task["blocking_reason"] = f"审查驳回步骤 {target_idx}，等待总管裁决：{cur_prob or rev_record['evidence'][:80]}"
                        task["updated_at"] = now
                        return

                    if rev_record["scope_expanded"] or is_repeat or cur_repairs >= cap:
                        why = (
                            "审查意见涉及范围扩大"
                            if rev_record["scope_expanded"]
                            else ("同一审查问题重复出现" if is_repeat else f"已达到最大自动修复轮数上限 ({cap} 轮)")
                        )
                        step["status"] = "needs_ruling"
                        step["blocking_reason"] = f"{why}，停止自动循环，等待总管裁决"
                        task["status"] = "needs_ruling"
                        task["blocking_reason"] = f"需要裁决：{why}（问题：{cur_prob[:80]}）"
                        task["updated_at"] = now
                        return

                    target_step["repair_count"] = cur_repairs + 1
                    task["rulings"].append(
                        {
                            "id": _new_id("RULE"),
                            "ts": now,
                            "step_index": step_index,
                            "decision": "accept",
                            "reason": f"第 {target_step['repair_count']}/{cap} 轮自动退回原作者 ({target_step['member_id']}) 原会话修复",
                            "decided_by": "coordinator_policy",
                        }
                    )
                    target_step["pending_repair_brief"] = (
                        f"【第 {target_step['repair_count']}/{cap} 轮审查退回修复】\n"
                        f"- 具体问题：{rev_record['problem']}\n"
                        f"- 审查证据：{rev_record['evidence']}\n"
                        f"- 影响：{rev_record['impact']}\n"
                        f"- 期望修复：{rev_record['expected_fix']}\n"
                        "请在你原有的上下文基础上直接输出修复后的完整交付物。"
                    )
                    target_step["status"] = "pending"
                    step["status"] = "pending"
                    task["current_step_index"] = target_idx
                    task["status"] = "queued" if st.get("paused") else "running"
                    task["updated_at"] = now
                    return
            else:
                ai_qs, cleaned_resp = _extract_ai_questions(res.response_text)
                if ai_qs:
                    step["latest_result_text"] = cleaned_resp
                    step["pending_questions"] = ai_qs
                    step["status"] = "awaiting_user"
                    step["blocking_reason"] = f"成员提出了 {len(ai_qs)} 个问题等待你选择确认后继续"
                    task["pending_questions"] = {
                        "step_index": step_index,
                        "member_id": step["member_id"],
                        "questions": ai_qs,
                        "ts": now,
                    }
                    self._append_room_chat(
                        st, task, sender_id=step["member_id"], kind="question_to_user",
                        content=cleaned_resp, step_index=step_index, round_label="请示负责人"
                    )
                    task["current_step_index"] = step_index
                    task["status"] = "awaiting_user"
                    task["blocking_reason"] = f"🙋 {mem['name']} 提出了 {len(ai_qs)} 个选项问题，请在下方点选确认后继续执行"
                    task["updated_at"] = now
                    return

                step["status"] = "succeeded"
                step["pending_questions"] = None
                step["blocking_reason"] = None
                task["pending_questions"] = None
                chat_kind = "deliverable" if step.get("step_type") in ("synthesis", "execution") else "speech"
                self._append_room_chat(
                    st, task, sender_id=step["member_id"], kind=chat_kind,
                    content=res.response_text, step_index=step_index, round_label=step.get("round_label") or step["title"]
                )
                task["deliverables"].append(
                    {
                        "step_index": step_index,
                        "title": step["title"],
                        "member_id": step["member_id"],
                        "attempt_id": attempt_id,
                        "session_id": res.session_id,
                        "preview": res.response_text[:240],
                        "ts": now,
                    }
                )

            self._refresh_task_overall_status_unlocked(st, task)
            task["updated_at"] = now

        self.store.mutate(_fin)

    def _refresh_task_overall_status_unlocked(self, st: Dict[str, Any], task: Dict[str, Any]) -> None:
        steps = task.get("steps", [])
        if not steps:
            return
        if all(s["status"] == "succeeded" for s in steps):
            task["status"] = "succeeded"
            task["blocking_reason"] = None
            synth = [s for s in steps if s.get("step_type") == "synthesis" and s.get("latest_result_text")]
            exec_steps = [s for s in steps if s.get("review_of") is None and s.get("latest_result_text")]
            chosen = synth[-1] if synth else (exec_steps[-1] if exec_steps else steps[-1])
            task["summary"] = chosen.get("latest_result_text", "")
            task["recent_events"].append(
                {"ts": time.time(), "type": "task.succeeded", "summary": f"协作任务全部 {len(steps)} 个节点已圆满完成"}
            )
        elif any(s["status"] == "awaiting_user" for s in steps):
            task["status"] = "awaiting_user"
        elif any(s["status"] == "needs_ruling" for s in steps):
            task["status"] = "needs_ruling"
        elif any(s["status"] == "blocked" for s in steps):
            task["status"] = "blocked"
        elif any(s["status"] == "failed" for s in steps):
            task["status"] = "failed"
        elif any(s["status"] == "aborted" for s in steps):
            task["status"] = "aborted"
        elif any(s["status"] in ("running", "pending") for s in steps):
            next_p = next((s["step_index"] for s in steps if s["status"] in ("running", "pending")), 0)
            task["current_step_index"] = next_p
            task["status"] = "queued" if st.get("paused") else "running"
