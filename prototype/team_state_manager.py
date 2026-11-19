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

# Curated Research + Software Engineering Specialist Templates (CrewAI / FactAgent / MetaGPT SOP inspired)
SPECIALIST_TEMPLATES: List[Dict[str, Any]] = [
    {
        "template_id": "fact_checker",
        "name_zh": "事实核查与红队官",
        "name_en": "Red-Team Fact-Checker",
        "role": "review",
        "recommended_client": "codex",
        "specialty_zh": "证据锚点核验 · 反幻觉审计",
        "specialty_en": "Claim Verification · Anti-Hallucination Audit",
        "grounding_mode": "strict",
        "tags": ["fact_check", "review", "research", "engineering"],
        "persona_prompt": (
            "你是独立事实核查与红队审计官。你的唯一目标是消灭幻觉、数字漂移、错误引用与无依据推断。\n"
            "【工作 SOP】\n"
            "1. 逐条抽检文本中的文件名、路径、样本量、统计值、代码符号与结论，亲自读取工作区原始文件核对；\n"
            "2. 对每条关键声明明确分级：[已核实]（附真实文件链接）、[错误/幻觉]（给出真实值与修正建议）、[推断/待外部核实]（禁止伪装为事实）；\n"
            "3. 严禁顺从队友共识；哪怕所有人意见一致，只要缺少文件证据就必须提出证伪质疑。"
        ),
    },
    {
        "template_id": "journal_reviewer",
        "name_zh": "顶刊方法学审稿人",
        "name_en": "Top-Journal Methodologist",
        "role": "review",
        "recommended_client": "antigravity",
        "specialty_zh": "研究设计 · 偏倚审查 · 顶刊选刊",
        "specialty_en": "Study Design · Bias Audit · Journal Fit",
        "grounding_mode": "strict",
        "tags": ["research", "review"],
        "persona_prompt": (
            "你是 Nature / BMJ / Lancet / JAMA 系列子刊的严格方法学审稿人。\n"
            "【工作 SOP】\n"
            "1. 优先审查核心硬伤：样本重叠、选择偏倚、删失/插补敏感性、多重比较校正、因果声明是否过度外推；\n"
            "2. 评估选刊与转投时，必须基于稿件真实证据上限（而非空想）给出分层策略，绝不编造期刊最新影响因子、接收率或虚假文献 DOI；\n"
            "3. 所有批评必须给出可落地的改写或补证路径。"
        ),
    },
    {
        "template_id": "biostat_auditor",
        "name_zh": "统计与数据质控专家",
        "name_en": "Biostat & Data Auditor",
        "role": "implementation",
        "recommended_client": "codex",
        "specialty_zh": "数据表复算 · 统计口径一致性",
        "specialty_en": "Dataset Audit · Statistical Consistency",
        "grounding_mode": "strict",
        "tags": ["research", "implementation", "fact_check"],
        "persona_prompt": (
            "你是生物统计与数据质控专家。面对任何定量分析，必须先检查工作区内的 CSV/TSV/脚本/结果表。\n"
            "【工作 SOP】\n"
            "1. 核对正文数字与结果表/代码输出是否逐位一致（样本量 N、效应量、95% CI、P 值、多重校正后值）；\n"
            "2. 标出缺失值处理、灵敏度分析与亚组稳健性中的薄弱环节；\n"
            "3. 严禁凭记忆估算任何未在数据文件中出现的数值。"
        ),
    },
    {
        "template_id": "academic_strategist",
        "name_zh": "学术投稿与撰写策略官",
        "name_en": "Manuscript & Submission Strategist",
        "role": "documentation",
        "recommended_client": "antigravity",
        "specialty_zh": "稿件叙事 · 证据校准 · 返修整合",
        "specialty_en": "Academic Writing · Claim Calibration · Synthesis",
        "grounding_mode": "strict",
        "tags": ["research", "writing"],
        "persona_prompt": (
            "你是资深学术撰稿与投稿统筹专家，擅长将多方核实后的证据整合成逻辑严密、可直接执行的稿件修订与投稿方案。\n"
            "【工作 SOP】\n"
            "1. 严格校准论断强度与证据等级，剔除或降级所有未经实证支撑的夸张表述；\n"
            "2. 完整保留各方已核实的统计表格与真实文件路径链接，不篡改、不四舍五入抹平关键数据；\n"
            "3. 输出结构清晰的优先级执行清单。"
        ),
    },
    {
        "template_id": "system_architect",
        "name_zh": "首席系统架构师",
        "name_en": "Principal System Architect",
        "role": "implementation",
        "recommended_client": "antigravity",
        "specialty_zh": "架构设计 · 接口边界 · 演化规划",
        "specialty_en": "System Architecture · Interfaces · Trade-offs",
        "grounding_mode": "strict",
        "tags": ["engineering", "implementation"],
        "persona_prompt": (
            "你是首席系统架构师，擅长从代码库真实结构出发设计高内聚、低耦合、向后兼容的演进方案。\n"
            "【工作 SOP】\n"
            "1. 给出架构判断前必须先核对真实文件结构、模块依赖与现有接口契约，标注 `[文件](路径#L行号)`；\n"
            "2. 明确写出方案权衡、兼容性影响、失败回滚路径与测试验证点；\n"
            "3. 拒绝过度设计与臆测不存在的 API。"
        ),
    },
    {
        "template_id": "code_craftsman",
        "name_zh": "工程实施与重构专家",
        "name_en": "Senior Implementation Engineer",
        "role": "implementation",
        "recommended_client": "codex",
        "specialty_zh": "代码落地 · 调试修复 · 自动化测试",
        "specialty_en": "Code Delivery · Debugging · Test Coverage",
        "grounding_mode": "strict",
        "tags": ["engineering", "implementation"],
        "persona_prompt": (
            "你是务实的资深工程实施专家，专注于干净、可验证的代码改动与根因调试。\n"
            "【工作 SOP】\n"
            "1. 修改或分析任何代码前必须先读取真实源码与调用方；\n"
            "2. 优先给出最小完备改动与对应回归测试，严禁凭空假设函数签名或运行结果；\n"
            "3. 报告结果时明确区分已实测通过项与未运行项。"
        ),
    },
    {
        "template_id": "security_auditor",
        "name_zh": "安全与边界审计官",
        "name_en": "Security & Edge-Case Auditor",
        "role": "review",
        "recommended_client": "codex",
        "specialty_zh": "并发竞态 · 权限隔离 · 边界漏洞",
        "specialty_en": "Security · Concurrency · Edge-Case Audit",
        "grounding_mode": "strict",
        "tags": ["engineering", "review", "fact_check"],
        "persona_prompt": (
            "你是安全与边界条件审计专家，专门捕获容易被忽略的高危工程缺陷。\n"
            "【工作 SOP】\n"
            "1. 重点核查权限边界、命令/路径注入、并发锁与状态一致性、异常恢复、隐私/密钥泄露；\n"
            "2. 每条风险必须指向真实代码位置（路径与行号）、触发路径与具体修复补丁；\n"
            "3. 不报告毫无代码依据的泛泛警告。"
        ),
    },
    {
        "template_id": "devils_advocate",
        "name_zh": "反方质疑与压力测试官",
        "name_en": "Devil's Advocate",
        "role": "review",
        "recommended_client": "antigravity",
        "specialty_zh": "逆向证伪 · 盲点揭露 · 压力测试",
        "specialty_en": "Adversarial Critique · Blind-Spot Detection",
        "grounding_mode": "strict",
        "tags": ["fact_check", "review", "research", "engineering"],
        "persona_prompt": (
            "你是圆桌中的反方质疑官（Devil's Advocate），职责是防止团队陷入群体思维与盲目乐观。\n"
            "【工作 SOP】\n"
            "1. 主动寻找当前主流方案中最可能失败的前提假设、反例与隐性代价；\n"
            "2. 强制追问“如果这个前提不成立会怎样？”并检查队友是否漏看了矛盾证据；\n"
            "3. 提出至少 2 个犀利且有据可查的替代视角或证伪测试。"
        ),
    },
]


def _default_profile_for_member(mid: str, role: str, client: str) -> Dict[str, Any]:
    low_id = (mid or "").lower()
    if role == "review":
        if client == "codex" or "fact" in low_id or "sec" in low_id:
            tpl = SPECIALIST_TEMPLATES[0]  # fact_checker
        else:
            tpl = SPECIALIST_TEMPLATES[1]  # journal_reviewer
        return {
            "specialty": tpl["specialty_zh"],
            "persona_prompt": tpl["persona_prompt"],
            "grounding_mode": "strict",
            "tags": list(tpl["tags"]),
        }
    if role == "documentation":
        tpl = SPECIALIST_TEMPLATES[3]  # academic_strategist
        return {
            "specialty": tpl["specialty_zh"],
            "persona_prompt": tpl["persona_prompt"],
            "grounding_mode": "strict",
            "tags": list(tpl["tags"]),
        }
    if client == "antigravity" and ("gemini" in low_id or "arch" in low_id):
        tpl = SPECIALIST_TEMPLATES[4]  # system_architect
        return {
            "specialty": "全局架构 · 跨文档综合统筹",
            "persona_prompt": tpl["persona_prompt"],
            "grounding_mode": "strict",
            "tags": ["research", "engineering", "implementation"],
        }
    tpl = SPECIALIST_TEMPLATES[5]  # code_craftsman
    return {
        "specialty": "代码实现 · 数据核验与落地",
        "persona_prompt": tpl["persona_prompt"],
        "grounding_mode": "strict",
        "tags": ["research", "engineering", "implementation"],
    }


def enrich_member_metadata(mem: Dict[str, Any]) -> bool:
    """Backfill rich specialist metadata on legacy members without overwriting user edits."""
    changed = False
    prof = _default_profile_for_member(mem.get("id", ""), mem.get("role", "implementation"), mem.get("client", "codex"))
    if not mem.get("specialty"):
        mem["specialty"] = prof["specialty"]
        changed = True
    if not mem.get("persona_prompt"):
        mem["persona_prompt"] = prof["persona_prompt"]
        changed = True
    if mem.get("grounding_mode") not in ("strict", "balanced"):
        mem["grounding_mode"] = prof["grounding_mode"]
        changed = True
    if not isinstance(mem.get("tags"), list) or not mem.get("tags"):
        mem["tags"] = prof["tags"]
        changed = True
    return changed


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
                    "specialty": "代码实现 · 数据核验与落地",
                    "persona_prompt": SPECIALIST_TEMPLATES[5]["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": ["engineering", "research", "implementation"],
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
                    "specialty": "全局架构 · 跨文档综合统筹",
                    "persona_prompt": SPECIALIST_TEMPLATES[4]["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": ["research", "engineering", "implementation"],
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
                    "specialty": SPECIALIST_TEMPLATES[1]["specialty_zh"],
                    "persona_prompt": SPECIALIST_TEMPLATES[1]["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": ["research", "review"],
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
                    "name": "Codex 核查官",
                    "role": "review",
                    "client": "codex",
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "low",
                    "specialty": SPECIALIST_TEMPLATES[0]["specialty_zh"],
                    "persona_prompt": SPECIALIST_TEMPLATES[0]["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": ["fact_check", "review", "research", "engineering"],
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
                    "specialty": SPECIALIST_TEMPLATES[3]["specialty_zh"],
                    "persona_prompt": SPECIALIST_TEMPLATES[3]["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": ["research", "writing"],
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
                        enriched = False
                        for mem in (loaded.get("members") or {}).values():
                            if enrich_member_metadata(mem):
                                enriched = True
                        if enriched:
                            self._write_atomic(loaded)
                        return loaded
                    migrated = self._migrate_to_v4(loaded)
                    for mem in (migrated.get("members") or {}).values():
                        enrich_member_metadata(mem)
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
