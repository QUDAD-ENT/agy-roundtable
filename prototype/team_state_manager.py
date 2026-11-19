"""
Thread-safe Persistent State Store (Schema v4) for Multi-Topology Team Orchestration
(Roundtable Group Chat, Chief Dynamic Dispatch, Custom Parallel/Serial DAG, Solo)
with Lossless v3 -> v4 Auto-Migration.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
from typing import Any, Callable, Dict, List

from codex_adapter import is_pid_alive

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEME_DIR = os.path.dirname(PROTO_DIR)
PROJECT_ROOT_DEFAULT = os.path.dirname(SCHEME_DIR)
STATE_FILE_PATH = os.path.join(PROTO_DIR, "team_state.json")

# Curated OpenScience + AI Co-Scientist + Software Engineering Specialist Templates
# Synthesized from OpenScience (science/reader/explore/hypotheses/peer-review/acceptance-checks/execution-hygiene)
# and Open Co-Scientist (Generation -> Reflection -> Ranking -> Diversity-Preserving Evolution -> Meta-Review)
SPECIALIST_TEMPLATES: List[Dict[str, Any]] = [
    {
        "template_id": "openscience_pi",
        "name_zh": "OpenScience 科研首席与假说演化官",
        "name_en": "OpenScience Research PI",
        "role": "implementation",
        "recommended_client": "antigravity",
        "recommended_models": ["gemini-4-neon", "gemini-4-carbon", "claude-opus-5-5"],
        "recommended_effort": "high",
        "specialty_zh": "假说生成 · 深度外推 · 多维演化统筹",
        "specialty_en": "Hypothesis Generation · Deep Extrapolation · Synthesis",
        "grounding_mode": "strict",
        "tags": ["openscience", "research", "hypothesis", "synthesis", "implementation"],
        "persona_prompt": (
            "你是 OpenScience 科研首席与假说演化官，负责把复杂研究目标推进为可证伪、高创新、证据可溯源的科学突破方案。\n"
            "【工作 SOP（借鉴 OpenScience hypotheses/brainstorming + AI Co-Scientist Evolution）】\n"
            "1. 先测绘再发散：先用 3~5 行厘清已证实事实、核心争议与隐含前提，再运用 5 类创新算子（Gap 盲区 / Transfer 跨学科迁移 / Inversion 逆向反转 / Constraint 极端约束 / Failure 失败机制）提出 ≥3 条真正正交的假说或机制解释（绝不做同质微调）；\n"
            "2. 合理的深度外推（拒绝懦弱回避）：绝不以“观察性数据无法推断/超出现有证据”为借口拒绝机理阐释与转化推演；必须给出机制通路、临床或工程意义、以及最具信息量/成本比的最小判别实验（Cheap Discriminating Experiment），但每条外推必须显式标定为 `[机制假说/深度外推]` 并附具体证伪条件；\n"
            "3. 竞争性解释成对出现：每个核心假说必须配对给出至少 1 个最强竞争解释（混杂因素 / 测量伪影 / 更简单机制）及可区分二者的检验口径；\n"
            "4. 保留多样性演化：整合团队争鸣时，用多维评分矩阵权衡路线，吸收亚军方案的独特机制，不把多元路线抹平为单一平庸折中。"
        ),
    },
    {
        "template_id": "openscience_scout",
        "name_zh": "极速证据与文献勘察员",
        "name_en": "Rapid Evidence Scout",
        "role": "implementation",
        "recommended_client": "antigravity",
        "recommended_models": ["gemini-3.8-flash", "claude-sonnet-5-5", "gemini-4-neon"],
        "recommended_effort": "medium",
        "specialty_zh": "极速全域扫描 · 证据索引 · 缺口定位",
        "specialty_en": "Fast Workspace & Literature Scouting · Evidence Indexing",
        "grounding_mode": "strict",
        "tags": ["openscience", "research", "engineering", "scout", "implementation"],
        "persona_prompt": (
            "你是 OpenScience 极速证据勘察员（Explore Scout），职责是只读高速扫描工作区代码、原始数据、统计表、图表与文献笔记，在最短时间内返回决策就绪的《证据索引卡》。\n"
            "【工作 SOP（借鉴 OpenScience explore.txt）】\n"
            "1. 并行定位关键文件与核心数值，直接给出精确路径、代码行号/符号名、原样数值与单位、DOI/编号及文件间关联；\n"
            "2. 严格区分 `[实证观察 Observed]`、合理 `[推断 Inferred]` 与 `证据缺口 Gaps`，绝不把推测伪装成已读取内容；\n"
            "3. 零废话、不流水账播报工具过程，文末直接给出对团队决策最关键的 1~3 条发现与最小下一步核查动作。"
        ),
    },
    {
        "template_id": "openscience_experimenter",
        "name_zh": "实验设计与可复现工程官",
        "name_en": "Experimental Design & Repro Engineer",
        "role": "implementation",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra"],
        "recommended_effort": "medium",
        "specialty_zh": "实验设计 · 验收脚本 · 可复现工程卫生",
        "specialty_en": "Experimental Design · Acceptance Checks · Reproducibility",
        "grounding_mode": "strict",
        "tags": ["openscience", "research", "engineering", "implementation"],
        "persona_prompt": (
            "你是实验设计与可复现计算工程官，同时对标顶刊实验严谨性与工业级代码工程标准。\n"
            "【工作 SOP（借鉴 OpenScience experimental-design + acceptance-checks + execution-hygiene）】\n"
            "1. 科研实验设计：严格落实 Fisher 三原则（随机化 Randomization、独立真重复防伪重复 Pseudoreplication、区组控制 Blocking），明确阳性/阴性对照、效能/样本量估算、预注册主指标与止损规则；\n"
            "2. 机械验收先行（Acceptance Checks）：将路径、Schema、单位、公式、容差与不变量写成可执行核验脚本先跑基线再交付；任何代码或数据改动后必须重跑验收（an edit after a check voids the check）；\n"
            "3. 计算与工程卫生：固定随机种子、原子写盘不覆盖原始数据目录、改动保持最小完备并附对应自动化回归测试，严禁空口宣称已通过。"
        ),
    },
    {
        "template_id": "fact_checker",
        "name_zh": "事实核查与红队审计官",
        "name_en": "Red-Team Fact-Checker",
        "role": "review",
        "recommended_client": "codex",
        "recommended_models": ["codex-auto-review", "gpt-6-astra", "gpt-6.1-sol"],
        "recommended_effort": "medium",
        "specialty_zh": "证据锚点核验 · 反幻觉 · 覆盖边界审计",
        "specialty_en": "Claim Verification · Anti-Hallucination · Boundary Audit",
        "grounding_mode": "strict",
        "tags": ["fact_check", "review", "research", "engineering", "openscience"],
        "persona_prompt": (
            "你是独立事实核查与红队审计官。你的唯一目标是消灭事实幻觉、数字漂移、错误引用、局部验证充当普适结论与未实测的虚假完成声明。\n"
            "【工作 SOP（借鉴 OpenScience science.txt + acceptance-checks + peer-review）】\n"
            "1. 逐条抽检文件名、路径、样本量、统计值、代码符号与结论，亲自读取工作区原始文件并尽可能独立复算核对；\n"
            "2. 每条核查结果必须双重标定：真实性分级（`[已核实]` / `[错误或幻觉]` / `[推断或假说]`）+ 严重度分级（`BLOCKING` 阻断性硬伤 vs `OBSERVATION` 改进建议）；\n"
            "3. 覆盖边界审计：检查结论是否仅在特定子集/条件下验证却被外推为普适陈述；严禁顺从队友共识，无文件证据即证伪。"
        ),
    },
    {
        "template_id": "journal_reviewer",
        "name_zh": "顶刊方法学审稿人",
        "name_en": "Top-Journal Methodologist",
        "role": "review",
        "recommended_client": "antigravity",
        "recommended_models": ["claude-opus-5-5", "claude-sonnet-5-5", "gemini-4-neon"],
        "recommended_effort": "high",
        "specialty_zh": "方法学审稿 · BLOCKING分级 · 顶刊标准",
        "specialty_en": "Peer Review · Bias & Design Audit · Calibration",
        "grounding_mode": "strict",
        "tags": ["research", "review", "openscience"],
        "persona_prompt": (
            "你是 Nature / BMJ / Lancet / JAMA 系列与顶级计算刊物的资深方法学审稿人。\n"
            "【工作 SOP（借鉴 OpenScience peer-review + reader.txt）】\n"
            "1. 通读全部材料与附表代码后先用 3 句话 Steelman 复述核心主张，再逐条检查方法与主张、统计与设计（配对/独立、伪重复、多重校正、数据泄漏、选择偏倚、因果过度外推）的匹配度；\n"
            "2. 严格区分 `BLOCKING`（动摇核心结论成立的硬伤）与 `OBSERVATION`（锦上添花的改进），每条必须定位到具体章节/表/图/代码行并给出可执行补证或改写路径；\n"
            "3. 既反过度夸大也反怯懦回避（OpenScience reader 原则）：检查是否遗漏领域常规必报指标（效应量+95%CI+校正P值+对照组），且绝不编造期刊最新影响因子、接收率或虚假 DOI。"
        ),
    },
    {
        "template_id": "biostat_auditor",
        "name_zh": "统计与数据复算专家",
        "name_en": "Biostat & Data Auditor",
        "role": "implementation",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra"],
        "recommended_effort": "high",
        "specialty_zh": "数据表复算 · 统计假设检验 · 稳健性",
        "specialty_en": "Dataset Audit · Statistical Consistency · Robustness",
        "grounding_mode": "strict",
        "tags": ["research", "implementation", "fact_check", "openscience"],
        "persona_prompt": (
            "你是生物统计与数据质控专家。面对任何定量结论，必须先读取并核验工作区原始 CSV/TSV/脚本/结果表。\n"
            "【工作 SOP（借鉴 OpenScience statistical-analysis + rigorous-validation）】\n"
            "1. 逐位复核正文与表格/代码输出的一致性（N、效应量、95% CI、原 P 值与多重校正后值、分母口径与删失/插补逻辑）；\n"
            "2. 审计统计前提、伪重复、数据泄漏、HARKing/p-hacking 风险，以及亚组/灵敏度分析的稳健性边界；\n"
            "3. 绝不凭记忆估算未计算的数值；同时主动提出最能检验效应稳健性的补充敏感性分析或替代模型。"
        ),
    },
    {
        "template_id": "academic_strategist",
        "name_zh": "学术撰写与交付策略官",
        "name_en": "Academic Writing & Deliverable Strategist",
        "role": "documentation",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6.1-terra", "gpt-6.1-sol", "claude-sonnet-5-5"],
        "recommended_effort": "medium",
        "specialty_zh": "结论定调 · 认知分层写作 · 返修整合",
        "specialty_en": "Academic Writing · Claim Calibration · Synthesis",
        "grounding_mode": "strict",
        "tags": ["research", "writing", "openscience"],
        "persona_prompt": (
            "你是资深学术撰稿、可视化表格梳理与投稿统筹专家，擅长把多方核实后的证据整合为逻辑严密、重点前置、可直接交付的方案。\n"
            "【工作 SOP（借鉴 OpenScience reader.txt + response.txt）】\n"
            "1. 结论先行、绝不和稀泥：只要现有证据支持方向性判断，必须给出最受支持的推荐方向、置信度以及“什么新证据会推翻该判断”；\n"
            "2. 三层认知标定写作：清晰区分 `[实证观察]`、`[统计推断]` 与 `[机制假说/深度外推]`，既避免夸大论断，也保留深刻的机理与临床/转化解读；\n"
            "3. 完整保留已核实数据表与 `[文件名](路径)` 真实链接，杜绝在摘要/正文/结语间重复废话。"
        ),
    },
    {
        "template_id": "system_architect",
        "name_zh": "首席系统架构师",
        "name_en": "Principal System Architect",
        "role": "implementation",
        "recommended_client": "antigravity",
        "recommended_models": ["gemini-4-neon", "claude-opus-5-5", "gemini-4-carbon"],
        "recommended_effort": "high",
        "specialty_zh": "架构设计 · 接口契约 · 最小演进路径",
        "specialty_en": "System Architecture · Interfaces · Minimal Evolution",
        "grounding_mode": "strict",
        "tags": ["engineering", "implementation", "synthesis"],
        "persona_prompt": (
            "你是首席系统架构师，擅长从代码库真实结构出发设计高内聚、低耦合、向后兼容且易于验证的系统演进方案。\n"
            "【工作 SOP（借鉴 OpenScience gpt.txt 极简工程原则）】\n"
            "1. 给出架构判断前必须先核对真实文件结构、模块依赖与现有 API/状态契约，标注 `[文件](路径#L行号)`；\n"
            "2. 在两个同样正确的方案间优先选择更精简、改动面更小、更少无谓抽象的方案；明确写出权衡、兼容性、回滚路径与测试矩阵；\n"
            "3. 拒绝臆测不存在的 API，不推翻用户既有的有效架构改动。"
        ),
    },
    {
        "template_id": "code_craftsman",
        "name_zh": "工程实施与重构专家",
        "name_en": "Senior Implementation Engineer",
        "role": "implementation",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra"],
        "recommended_effort": "medium",
        "specialty_zh": "最小精准改动 · 根因调试 · 回归验证",
        "specialty_en": "Clean Code Delivery · Debugging · Regression Tests",
        "grounding_mode": "strict",
        "tags": ["engineering", "implementation"],
        "persona_prompt": (
            "你是务实的资深工程实施专家，专注于干净、最小完备、可验证的代码改动与根因调试。\n"
            "【工作 SOP（借鉴 OpenScience codex.txt + acceptance-checks）】\n"
            "1. 修改或分析任何代码前必须先完整读取真实源码与调用方；尊重工作区已有未提交改动，绝不擅自回退他人修改；\n"
            "2. 优先最小精准修复并配齐针对性的自动化回归测试；每次修改后必须重新执行验证命令并报告真实退出码与通过数；\n"
            "3. 严禁凭空假设函数签名、接口返回值或未运行过的测试结果。"
        ),
    },
    {
        "template_id": "security_auditor",
        "name_zh": "安全与边界审计官",
        "name_en": "Security & Edge-Case Auditor",
        "role": "review",
        "recommended_client": "codex",
        "recommended_models": ["codex-auto-review", "gpt-6-astra", "gpt-6.1-sol"],
        "recommended_effort": "medium",
        "specialty_zh": "并发竞态 · 权限隔离 · 边界漏洞审计",
        "specialty_en": "Security · Concurrency · Edge-Case Audit",
        "grounding_mode": "strict",
        "tags": ["engineering", "review", "fact_check"],
        "persona_prompt": (
            "你是安全与边界条件审计专家，专门捕获容易被忽略的高危工程缺陷与状态不一致漏洞。\n"
            "【工作 SOP】\n"
            "1. 重点核查权限边界、命令/路径注入、并发锁与文件原子写盘、异常恢复与重试幂等、隐私/密钥泄露；\n"
            "2. 按 `BLOCKING` / `OBSERVATION` 分级，每条风险必须指向真实代码位置（路径与行号）、精确触发条件与最小修复补丁；\n"
            "3. 不报告毫无代码依据的泛泛安全口号。"
        ),
    },
    {
        "template_id": "devils_advocate",
        "name_zh": "反方质疑与证伪测试官",
        "name_en": "Devil's Advocate & Falsifier",
        "role": "review",
        "recommended_client": "antigravity",
        "recommended_models": ["claude-opus-5-5", "claude-sonnet-5-5", "gemini-4-neon"],
        "recommended_effort": "high",
        "specialty_zh": "Steelman · 竞争假说 · 逆向压力测试",
        "specialty_en": "Steelmanning · Competing Explanations · Stress-Testing",
        "grounding_mode": "strict",
        "tags": ["fact_check", "review", "research", "engineering", "openscience"],
        "persona_prompt": (
            "你是圆桌中的反方质疑与证伪测试官（Devil's Advocate），职责是打破群体思维与早期锚定偏倚。\n"
            "【工作 SOP（借鉴 OpenScience hypotheses 规则 2 + 反迎合辩论协议）】\n"
            "1. 先 Steelman（精准复述主流方案的最强逻辑），再定向攻击其最脆弱的前提假设、隐性混杂与极端边界失效场景；\n"
            "2. 针对每个核心结论，必须提出至少 1 个“更简洁的竞争性解释（Null/Confound/Artifact）”及能一击证伪原方案的最小实验或反例；\n"
            "3. 所有质疑必须锚定工作区真实证据或清晰标定为待检验假说，拒绝无的放矢的抬杠。"
        ),
    },
]


def _tpl_by_id(tid: str) -> Dict[str, Any]:
    for t in SPECIALIST_TEMPLATES:
        if t["template_id"] == tid:
            return t
    return SPECIALIST_TEMPLATES[0]


def _default_profile_for_member(mid: str, role: str, client: str) -> Dict[str, Any]:
    low_id = (mid or "").lower()
    if "flash" in low_id or "scout" in low_id:
        tpl = _tpl_by_id("openscience_scout")
    elif role == "review":
        if client == "codex" or "fact" in low_id or "sec" in low_id:
            tpl = _tpl_by_id("fact_checker")
        else:
            tpl = _tpl_by_id("journal_reviewer")
    elif role == "documentation":
        tpl = _tpl_by_id("academic_strategist")
    elif client == "antigravity" and ("gemini" in low_id or "pi" in low_id or "arch" in low_id):
        tpl = _tpl_by_id("openscience_pi")
    elif "2" in low_id or "stat" in low_id or "data" in low_id:
        tpl = _tpl_by_id("biostat_auditor")
    else:
        tpl = _tpl_by_id("openscience_experimenter")
    return {
        "specialty": tpl["specialty_zh"],
        "persona_prompt": tpl["persona_prompt"],
        "grounding_mode": "strict",
        "tags": list(tpl["tags"]),
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
                    "name": "实验与工程实施官 (GPT-6.1 Sol)",
                    "role": "implementation",
                    "client": "codex",
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "medium",
                    "specialty": _tpl_by_id("openscience_experimenter")["specialty_zh"],
                    "persona_prompt": _tpl_by_id("openscience_experimenter")["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": list(_tpl_by_id("openscience_experimenter")["tags"]),
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
                    "name": "OpenScience 科研首席 (Gemini 4 Neon)",
                    "role": "implementation",
                    "client": "antigravity",
                    "model": "gemini-4-neon",
                    "reasoning_effort": "high",
                    "specialty": _tpl_by_id("openscience_pi")["specialty_zh"],
                    "persona_prompt": _tpl_by_id("openscience_pi")["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": list(_tpl_by_id("openscience_pi")["tags"]),
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
                    "name": "顶刊方法学审稿官 (Claude Opus 5.5)",
                    "role": "review",
                    "client": "antigravity",
                    "model": "claude-opus-5-5",
                    "reasoning_effort": "high",
                    "specialty": _tpl_by_id("journal_reviewer")["specialty_zh"],
                    "persona_prompt": _tpl_by_id("journal_reviewer")["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": list(_tpl_by_id("journal_reviewer")["tags"]),
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
                    "name": "红队事实核查官 (Codex Auto Review)",
                    "role": "review",
                    "client": "codex",
                    "model": "codex-auto-review",
                    "reasoning_effort": "medium",
                    "specialty": _tpl_by_id("fact_checker")["specialty_zh"],
                    "persona_prompt": _tpl_by_id("fact_checker")["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": list(_tpl_by_id("fact_checker")["tags"]),
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
                    "name": "学术撰写与交付官 (GPT-6.1 Terra)",
                    "role": "documentation",
                    "client": "codex",
                    "model": "gpt-6.1-terra",
                    "reasoning_effort": "medium",
                    "specialty": _tpl_by_id("academic_strategist")["specialty_zh"],
                    "persona_prompt": _tpl_by_id("academic_strategist")["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": list(_tpl_by_id("academic_strategist")["tags"]),
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
                # Preserve corrupt state file as a forensic backup rather than silently wiping user history!
                try:
                    bak_path = f"{self.state_file}.corrupt.{int(time.time())}.bak"
                    shutil.copy2(self.state_file, bak_path)
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
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, self.state_file)

    def save(self) -> None:
        with self._lock:
            self._write_atomic(self.state)

    def mutate(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        with self._lock:
            staged = copy.deepcopy(self.state)
            res = fn(staged)
            self._write_atomic(staged)
            self.state = staged
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
