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
from typing import Any, Callable, Dict, List, Optional, Tuple

from codex_adapter import is_pid_alive, kill_process_tree

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
        "recommended_models": ["gemini-4-neon", "gemini-4-hydrogen", "claude-opus-5-5", "gemini-4-carbon"],
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
        "recommended_models": ["gemini-4-hydrogen", "claude-sonnet-5-5", "gemini-3.8-flash", "gemini-4-neon"],
        "recommended_effort": "high",
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
        "recommended_models": ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-sol"],
        "recommended_effort": "high",
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
        "recommended_models": ["gpt-6.2-astra", "gpt-6.2-sol", "codex-auto-review", "gpt-6-astra"],
        "recommended_effort": "high",
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
        "template_id": "scientific_plotter",
        "name_zh": "顶刊科研代码绘图专家",
        "name_en": "Publication Scientific Plotter",
        "role": "implementation",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra"],
        "recommended_effort": "high",
        "specialty_zh": "Nature/Science 多子图 · Python/R 矢量出图 · 源表绑定",
        "specialty_en": "Publication Multi-Panel Plots · Python/R Vector · Data Provenance",
        "grounding_mode": "strict",
        "tags": ["openscience", "research", "plotting", "implementation", "engineering"],
        "persona_prompt": (
            "你是顶刊（Nature / Science / Cell / NeurIPS）科研代码绘图与数据可视化专家。\n"
            "【工作 SOP（融合 OpenScience figures + scientific-visualization + Nature 五点绘图契约）】\n"
            "1. 后端排他门控：明确采用 Python（matplotlib/seaborn）或 R（ggplot2/patchwork/ComplexHeatmap）单一后端全程出图与导出；动手前先核查环境包版本与原始数据表；\n"
            "2. 出版级五点契约：先定一句核心论断与 Hero 主子图架构，按印刷终尺寸画布直接构建（单栏 89mm / 双栏 183mm，严禁大图硬缩放导致字号低于 5pt），采用 Okabe-Ito / NMI 低饱和色盲安全配色，白底高对比、无重叠标签，所有数值必须含误差棒/不确定性区间与样本量 N；\n"
            "3. 矢量与高分辨率双轨导出 + 机械溯源：同步输出可编辑矢量 `SVG`、`PDF` 与真 `600 DPI TIFF`（像素宽匹配物理尺寸）及预览 `PNG`，并生成 `figure_manifest.json` 绑定脚本 SHA-256、源数据 CSV/TSV 路径、面板组别与多重校正 P 值列，严禁硬编码或凭记忆绘制任何数据点。"
        ),
    },
    {
        "template_id": "visual_figure_qa",
        "name_zh": "科研绘图架构与视觉质检官",
        "name_en": "Figure Architecture & Visual QA Lead",
        "role": "review",
        "recommended_client": "antigravity",
        "recommended_models": ["gemini-4-carbon", "claude-opus-5-5", "gemini-4-neon"],
        "recommended_effort": "high",
        "specialty_zh": "多子图叙事规划 · 视觉QA质检 · 图表数据一致性",
        "specialty_en": "Figure Narrative Design · Visual QA · Plot-Data Consistency",
        "grounding_mode": "strict",
        "tags": ["openscience", "research", "plotting", "review", "fact_check"],
        "persona_prompt": (
            "你是顶刊科研图表架构统筹与视觉质检官（推荐 Gemini 4 Carbon 广域多模态审阅）。\n"
            "【工作 SOP（融合 OpenScience scientific-visualization + peer-review）】\n"
            "1. 绘图前统筹：审查五点绘图契约（核心论断、子图证据链、Hero 主图占比、色盲/灰度可读性与坐标轴单位），删去不承载独立证据的冗余子图；\n"
            "2. 绘图后视觉与数据双轨 QA：在纯白背景下按印刷终尺寸审查输出图件与 `figure_manifest.json`，逐项核对文字重叠/截断、图例遮挡、坐标轴起止点误导、组别标签是否互换以及是否误绘原始未校正 P 值；\n"
            "3. 按 `BLOCKING`（数据错误、组别错位、DPI/尺寸造假、关键标签被遮挡）与 `OBSERVATION`（配色/间距精修）分级输出可执行重绘指令。"
        ),
    },
    {
        "template_id": "biostat_auditor",
        "name_zh": "统计复算与顶刊科研绘图官",
        "name_en": "Biostat Auditor & Scientific Plotter",
        "role": "implementation",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra"],
        "recommended_effort": "high",
        "specialty_zh": "数据表复算 · 统计稳健性 · Python/R 科研代码绘图",
        "specialty_en": "Dataset Audit · Statistical Consistency · Scientific Plotting",
        "grounding_mode": "strict",
        "tags": ["research", "implementation", "fact_check", "openscience", "plotting"],
        "persona_prompt": (
            "你是生物统计复算与顶刊科研代码绘图专家。面对任何定量分析与图表需求，必须先读取并核验工作区原始 CSV/TSV/脚本/结果表。\n"
            "【工作 SOP（融合 OpenScience statistical-analysis + figures + rigorous-validation）】\n"
            "1. 逐位复核正文与表格/代码输出的一致性（N、效应量、95% CI、原 P 值与多重校正后值、分母口径与删失/插补逻辑），审计伪重复、数据泄漏、HARKing/p-hacking 风险；\n"
            "2. 编写 Python (`matplotlib`/`seaborn`) 或 R (`ggplot2`/`patchwork`/`ComplexHeatmap`) 科研绘图脚本时，坚持终尺寸画布、色盲安全配色、矢量 SVG/PDF + 真 600 DPI TIFF 导出，并落盘 `figure_manifest.json` 绑定源表行键与校正 P 值列；\n"
            "3. 绝不凭记忆估算或编造任何未计算的数值，主动提出检验稳健性的敏感性分析脚本。"
        ),
    },
    {
        "template_id": "academic_strategist",
        "name_zh": "学术撰写与交付策略官",
        "name_en": "Academic Writing & Deliverable Strategist",
        "role": "documentation",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6.2-sol", "gpt-6.2-astra", "claude-sonnet-5-5"],
        "recommended_effort": "high",
        "specialty_zh": "结论定调 · 认知分层写作 · 图注与返修整合",
        "specialty_en": "Academic Writing · Claim Calibration · Figure Legends",
        "grounding_mode": "strict",
        "tags": ["research", "writing", "openscience", "plotting"],
        "persona_prompt": (
            "你是资深学术撰稿、图注规范与投稿统筹专家，擅长把多方核实后的代码结果、统计表与科研图整合为逻辑严密、重点前置、可直接交付的稿件方案。\n"
            "【工作 SOP（借鉴 OpenScience reader.txt + analysis-report + response.txt）】\n"
            "1. 结论先行、绝不和稀泥：只要现有证据支持方向性判断，必须给出最受支持的推荐方向、置信度以及“什么新证据会推翻该判断”；\n"
            "2. 三层认知标定写作：清晰区分 `[实证观察]`、`[统计推断]` 与 `[机制假说/深度外推]`；撰写图注（Figure Legend）时必含精确样本量 `n`、误差棒定义、统计检验与多重校正方法；\n"
            "3. 完整保留已核实数据表、重现命令与 `[文件名](路径)` 真实链接，杜绝在摘要/正文/结语间重复废话。"
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
        "recommended_models": ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-sol"],
        "recommended_effort": "high",
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
        "recommended_models": ["gpt-6.2-astra", "gpt-6.2-sol", "codex-auto-review", "gpt-6-astra"],
        "recommended_effort": "high",
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
    {
        "template_id": "experimental_power_designer",
        "name_zh": "OpenScience 实验设计与统计效能官",
        "name_en": "OpenScience Experimental Design & Power Lead",
        "role": "review",
        "recommended_client": "antigravity",
        "recommended_models": ["claude-opus-5-5", "gemini-4-neon", "claude-sonnet-5-5"],
        "recommended_effort": "high",
        "specialty_zh": "随机化区组 · 抗伪重复 · 效应量收缩与效能计算",
        "specialty_en": "Randomization · Blocking · Anti-Pseudoreplication · Power/MDE",
        "grounding_mode": "strict",
        "tags": ["openscience", "research", "design", "stats", "review"],
        "persona_prompt": (
            "你是 OpenScience 实验设计与统计效能专家（深度融合 experimental-design + statistical-power + statistical-conventions）。\n"
            "【工作 SOP】\n"
            "1. 随机化与区组核查：针对小样本（n<100）或顺序入组设计置换区组（permuted blocks），对关键混杂分层，对整群干预按 cluster 随机化，严查将技术重复误作生物学重复的伪重复缺陷；\n"
            "2. 效应量收缩与效能：拒绝盲从文献/预实验膨胀效应（Winner's Curse），基于最小有科学/临床意义效应量 (MDE) 给出闭式或蒙特卡洛模拟效能评估及样本量敏感性表；\n"
            "3. 对照与预注册界线：为每条主假说配备可区分机制与批次伪影的阴性/阳性/消融对照，并明确主终点与探索性亚组边界。"
        ),
    },
    {
        "template_id": "sources_citation_auditor",
        "name_zh": "OpenScience 原子声明与文献溯源官",
        "name_en": "OpenScience Atomic Sources & Citation Auditor",
        "role": "review",
        "recommended_client": "codex",
        "recommended_models": ["gpt-6.2-astra", "gpt-6.2-sol"],
        "recommended_effort": "high",
        "specialty_zh": "原子化声明拆解 · DOI先读后引 · 四级证据审计",
        "specialty_en": "Atomize-Locate-Grade · Resolve-Before-Cite · Provenance Audit",
        "grounding_mode": "strict",
        "tags": ["openscience", "fact_check", "citations", "review", "research"],
        "persona_prompt": (
            "你是 OpenScience 原子声明与文献溯源审计官（深度融合 sources + citations + provenance）。\n"
            "【工作 SOP】\n"
            "1. Atomize → Locate → Grade：将待审稿件/方案拆解为可独立证伪的原子声明，逐条在工作区文件/数据表/真实文献中定位精确段落或行号，给出四级评分（`SUPPORTED` / `PARTIAL` / `UNSUPPORTED` / `CONTRADICTED`）；\n"
            "2. Resolve-Before-Cite：每条文献归因必须核验规范 DOI / arXiv ID 且确认正文论断确实与该文献原句一致，严禁张冠李戴或凭记忆补齐作者年份；\n"
            "3. 诚实汇报矛盾：若实测数据或原始记录反驳正文声称，直接以精确数字列出 `CONTRADICTED` 纠错表，绝不和稀泥。"
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
        self.rev: int = 1
        self._dirty: bool = False
        self._last_flush_ts: float = time.time()
        self._flush_stop = threading.Event()
        self.state: Dict[str, Any] = self._load_or_init()
        self.recover_interrupted_state()
        self._flusher_thread = threading.Thread(target=self._debounced_flush_loop, name="StateDebouncedFlusher", daemon=True)
        self._flusher_thread.start()

    def _debounced_flush_loop(self) -> None:
        while not self._flush_stop.wait(timeout=1.5):
            try:
                self.flush_if_dirty()
            except Exception:
                pass

    def flush_if_dirty(self) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            self._write_atomic(self.state)
            self._dirty = False
            self._last_flush_ts = time.time()
            return True

    def close(self) -> None:
        self._flush_stop.set()
        try:
            self.flush_if_dirty()
        except Exception:
            pass

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
                "max_sandbox": "workspace-write",
                "allow_danger_full_access": False,
                "extra_add_dirs": [],
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
                    "name": "科研代码与可复现工程官 (GPT-6.2-Sol)",
                    "role": "implementation",
                    "client": "codex",
                    "model": "gpt-6.2-sol",
                    "reasoning_effort": "high",
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
                    "name": "顶刊方法学与代码审稿官 (Claude Opus 5.5)",
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
                    "name": "红队事实核查与代码安全官 (GPT-6.2-Astra)",
                    "role": "review",
                    "client": "codex",
                    "model": "gpt-6.2-astra",
                    "reasoning_effort": "high",
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
                "agy_carbon_plotter": {
                    "id": "agy_carbon_plotter",
                    "name": "科研绘图架构与视觉质检官 (Gemini 4 Carbon)",
                    "role": "review",
                    "client": "antigravity",
                    "model": "gemini-4-carbon",
                    "reasoning_effort": "high",
                    "specialty": _tpl_by_id("visual_figure_qa")["specialty_zh"],
                    "persona_prompt": _tpl_by_id("visual_figure_qa")["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": list(_tpl_by_id("visual_figure_qa")["tags"]),
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
                    "name": "学术撰写与交付策略官 (GPT-6.2-Sol)",
                    "role": "documentation",
                    "client": "codex",
                    "model": "gpt-6.2-sol",
                    "reasoning_effort": "high",
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
                "auto_spawn_quota_backup": True,
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
                        if "auto_spawn_quota_backup" not in loaded.setdefault("settings", {}):
                            loaded["settings"]["auto_spawn_quota_backup"] = True
                            enriched = True
                        for mem in (loaded.get("members") or {}).values():
                            if enrich_member_metadata(mem):
                                enriched = True
                        if self._dedup_historical_state(loaded):
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

    def _dedup_historical_state(self, st: Dict[str, Any]) -> bool:
        """v5.7 P1-5: Losslessly strip 3x–4x duplicate attempt/prompt copies from completed tasks in team_state.json.
        100% of original speeches remain verbatim in chat_room/summary and full prompts remain in prompt_ledger_path.
        """
        changed = False
        tasks = st.get("tasks") or []
        if not tasks:
            return False
        active_id = st.get("active_task_id")
        recent_ids = {t.get("id") for t in sorted(tasks, key=lambda x: x.get("updated_at") or 0, reverse=True)[:3]}
        if active_id:
            recent_ids.add(active_id)
        for t in tasks:
            if t.get("status") not in ("succeeded", "aborted", "failed"):
                continue
            is_recent = t.get("id") in recent_ids
            chat_by_step: Dict[Any, List[str]] = {}
            if not is_recent:
                for cm in t.get("chat_room") or []:
                    chat_by_step.setdefault(cm.get("step_index"), []).append(str(cm.get("content") or ""))
            for s in t.get("steps") or []:
                ltxt = str(s.get("latest_result_text") or "")
                for att in s.get("attempts") or []:
                    if att.get("status") == "succeeded":
                        if att.get("partial_text"):
                            att["partial_text"] = ""
                            changed = True
                        rtxt = str(att.get("result_text") or "")
                        if len(rtxt) > 1200 and (ltxt or not is_recent):
                            att["result_len"] = max(int(att.get("result_len") or 0), len(rtxt))
                            att["result_text"] = rtxt[:400] + f"\n…[完整内容 ({att['result_len']} chars) 见 step.latest_result_text / chat_room]"
                            changed = True
                    plp = att.get("prompt_ledger_path") or (att.get("context_telemetry") or {}).get("prompt_ledger_path")
                    psent = str(att.get("prompt_sent") or "")
                    if plp and len(psent) > 750:
                        att["prompt_sent"] = psent[:600] + f"\n…[完整 Prompt 见 {plp}]"
                        changed = True
                if (not is_recent) and len(ltxt) > 1200:
                    step_chats = chat_by_step.get(s.get("step_index")) or []
                    if any(ltxt[:200] in cc for cc in step_chats):
                        s["latest_result_text"] = ltxt[:400] + f"\n…[历史归档步骤，完整正文 ({len(ltxt)} chars) 保留在 chat_room]"
                        changed = True
        return changed

    def _write_atomic(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp_path = f"{self.state_file}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, self.state_file)

    def save(self) -> None:
        with self._lock:
            self._write_atomic(self.state)
            self._dirty = False
            self._last_flush_ts = time.time()

    def get_rev(self) -> int:
        with self._lock:
            return self.rev

    def read(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        """Zero-copy thread-safe read-only inspection (caller MUST NOT mutate state)."""
        with self._lock:
            return fn(self.state)

    def mutate(self, fn: Callable[[Dict[str, Any]], Any], persist: str = "immediate") -> Any:
        """Mutate state atomically.
        - persist='immediate': deep-copy stage + atomic disk fsync (crash-safe for lifecycle transitions).
        - persist='debounced': fast in-place update + rev bump + background flush every 1.5s (for high-freq telemetry).
        """
        with self._lock:
            if persist == "debounced":
                res = fn(self.state)
                self.rev += 1
                self._dirty = True
                return res
            staged = copy.deepcopy(self.state)
            res = fn(staged)
            self._write_atomic(staged)
            self.state = staged
            self.rev += 1
            self._dirty = False
            self._last_flush_ts = time.time()
            return res

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = copy.deepcopy(self.state)
            snap["state_rev"] = self.rev
            return snap

    def slim_snapshot(self, focus_task_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        """Lightweight UI projection: full detail ONLY for the active focused task, compact headers for history.
        Shrinks multi-megabyte /api/state payloads by >90% while preserving 100% of Studio UI functionality.
        """
        with self._lock:
            st = self.state
            cur_rev = self.rev
            binding = st.get("binding") or {}
            cur_proj_norm = binding.get("normalized_project_path") or (
                normalize_win_path(binding["project_path"]) if binding.get("project_path") else ""
            )
            tasks = st.get("tasks") or []

            # Determine which task is currently focused in the Studio workspace
            eff_focus = focus_task_id or st.get("active_task_id")
            if not eff_focus or not any(t.get("id") == eff_focus for t in tasks):
                unarchived = [
                    t for t in tasks
                    if not t.get("archived")
                    and (
                        (t.get("normalized_project_path") or (normalize_win_path(t["project_path"]) if t.get("project_path") else ""))
                        == cur_proj_norm
                    )
                ]
                eff_focus = unarchived[-1]["id"] if unarchived else (tasks[-1]["id"] if tasks else None)

            def _slim_attempt(att: Dict[str, Any], step_status: str) -> Dict[str, Any]:
                is_running = att.get("status") == "running" or step_status == "running"
                is_troubled = step_status in ("failed", "aborted", "blocked", "needs_verification", "needs_ruling")
                raw_res = att.get("result_text") or ""
                raw_part = att.get("partial_text") or ""
                out_a: Dict[str, Any] = {
                    "attempt_id": att.get("attempt_id"),
                    "round_num": att.get("round_num"),
                    "member_id": att.get("member_id"),
                    "session_id": att.get("session_id"),
                    "is_fallback_session": att.get("is_fallback_session", False),
                    "fallback_of_session_id": att.get("fallback_of_session_id"),
                    "requested_config": copy.deepcopy(att.get("requested_config") or {}),
                    "accepted_config": copy.deepcopy(att.get("accepted_config") or {}),
                    "status": att.get("status"),
                    "blocking_reason": att.get("blocking_reason"),
                    "started_at": att.get("started_at"),
                    "ended_at": att.get("ended_at"),
                    "pid": att.get("pid"),
                    "error_kind": att.get("error_kind"),
                    "stderr_excerpt": att.get("stderr_excerpt") or "",
                    "result_len": len(raw_res),
                    "partial_len": len(raw_part),
                }
                if att.get("usage"):
                    out_a["usage"] = copy.deepcopy(att["usage"])
                if att.get("context_telemetry"):
                    out_a["context_telemetry"] = copy.deepcopy(att["context_telemetry"])
                if is_running:
                    out_a["partial_text"] = raw_part[-2400:] if len(raw_part) > 2400 else raw_part
                elif is_troubled:
                    # Keep partial/result text on troubled steps so 1-click "Adopt Partial" rescue works verbatim
                    out_a["partial_text"] = raw_part
                    out_a["result_text"] = raw_res
                return out_a

            def _project_focused_task(t: Dict[str, Any]) -> Dict[str, Any]:
                has_summary = bool((t.get("summary") or "").strip())
                steps_raw = t.get("steps") or []
                cur_idx = t.get("current_step_index")
                if cur_idx is None and steps_raw:
                    cur_idx = len(steps_raw) - 1
                slim_steps = []
                for s in steps_raw:
                    sst = s.get("status") or ""
                    keep_full_latest = (not has_summary) and (
                        sst == "running" or s.get("step_index") == cur_idx or s.get("step_index") == len(steps_raw) - 1
                    )
                    ltxt = s.get("latest_result_text") or ""
                    slim_s = {
                        "step_index": s.get("step_index"),
                        "title": s.get("title"),
                        "step_type": s.get("step_type"),
                        "stage_index": s.get("stage_index"),
                        "round_label": s.get("round_label"),
                        "member_id": s.get("member_id"),
                        "depends_on": list(s.get("depends_on") or []),
                        "review_of": s.get("review_of"),
                        "read_only": s.get("read_only", True),
                        "requires_shell": s.get("requires_shell", False),
                        "sandbox": s.get("sandbox"),
                        "worker_session_id": s.get("worker_session_id"),
                        "status": sst,
                        "repair_count": s.get("repair_count", 0),
                        "consult_count": s.get("consult_count", 0),
                        "pending_questions": copy.deepcopy(s.get("pending_questions")),
                        "blocking_reason": s.get("blocking_reason"),
                        "accepted_attempt_id": s.get("accepted_attempt_id"),
                        "quota_failover_count": s.get("quota_failover_count", 0),
                        "latest_result_text": ltxt if keep_full_latest else (ltxt[:180] if ltxt else ""),
                        "attempts": [_slim_attempt(a, sst) for a in (s.get("attempts") or [])],
                    }
                    if s.get("worktree_info"):
                        slim_s["worktree_info"] = copy.deepcopy(s["worktree_info"])
                    if s.get("visual_qa_report"):
                        slim_s["visual_qa_report"] = copy.deepcopy(s["visual_qa_report"])
                    slim_steps.append(slim_s)

                snaps_lean = []
                for sn in (t.get("round_snapshots") or []):
                    snaps_lean.append({k: v for k, v in sn.items() if k != "rendered_md"})

                proj: Dict[str, Any] = {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "goal": t.get("goal"),
                    "project_path": t.get("project_path"),
                    "normalized_project_path": t.get("normalized_project_path"),
                    "project_name": t.get("project_name"),
                    "archived": t.get("archived", False),
                    "dispatch_mode": t.get("dispatch_mode"),
                    "execution_mode": t.get("execution_mode"),
                    "mode_meta": copy.deepcopy(t.get("mode_meta") or {}),
                    "sandbox": t.get("sandbox"),
                    "add_dirs": list(t.get("add_dirs") or []),
                    "workspace_scope": t.get("workspace_scope"),
                    "worktree_isolation": t.get("worktree_isolation", False),
                    "task_token_budget": t.get("task_token_budget", 0),
                    "status": t.get("status"),
                    "current_step_index": t.get("current_step_index"),
                    "blocking_reason": t.get("blocking_reason"),
                    "steps": slim_steps,
                    "chat_room": copy.deepcopy(t.get("chat_room") or []),
                    "chat_count": len(t.get("chat_room") or []),
                    "recent_events": copy.deepcopy((t.get("recent_events") or [])[-40:]),
                    "pending_questions": copy.deepcopy(t.get("pending_questions")),
                    "reviews": copy.deepcopy(t.get("reviews") or []),
                    "rulings": copy.deepcopy(t.get("rulings") or []),
                    "latest_fact_check": copy.deepcopy(t.get("latest_fact_check")),
                    "borda_scoreboard": copy.deepcopy(t.get("borda_scoreboard")),
                    "openscience_playbooks": list(t.get("openscience_playbooks") or []),
                    "research_signoff": copy.deepcopy(t.get("research_signoff")),
                    "round_snapshots": snaps_lean,
                    "session_metrics": copy.deepcopy(t.get("session_metrics") or {}),
                    "context_stats": copy.deepcopy(t.get("context_stats") or {}),
                    "summary": t.get("summary") or "",
                    "created_at": t.get("created_at"),
                    "updated_at": t.get("updated_at"),
                    "_slim": False,
                }
                return proj

            def _project_summary_task(t: Dict[str, Any]) -> Dict[str, Any]:
                steps_raw = t.get("steps") or []
                return {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "goal": (t.get("goal") or "")[:320],
                    "project_path": t.get("project_path"),
                    "normalized_project_path": t.get("normalized_project_path"),
                    "project_name": t.get("project_name"),
                    "archived": t.get("archived", False),
                    "dispatch_mode": t.get("dispatch_mode"),
                    "execution_mode": t.get("execution_mode"),
                    "sandbox": t.get("sandbox"),
                    "status": t.get("status"),
                    "blocking_reason": t.get("blocking_reason"),
                    "research_signoff": copy.deepcopy(t.get("research_signoff")),
                    "context_stats": copy.deepcopy(t.get("context_stats") or {}),
                    "chat_count": len(t.get("chat_room") or []),
                    "chat_room": [],
                    "steps": [
                        {
                            "step_index": s.get("step_index"),
                            "member_id": s.get("member_id"),
                            "status": s.get("status"),
                            "title": s.get("title"),
                            "round_label": s.get("round_label"),
                            "step_type": s.get("step_type"),
                        }
                        for s in steps_raw
                    ],
                    "created_at": t.get("created_at"),
                    "updated_at": t.get("updated_at"),
                    "_slim": True,
                }

            projected_tasks = [
                _project_focused_task(t) if t.get("id") == eff_focus else _project_summary_task(t)
                for t in tasks
            ]
            return cur_rev, {
                "schema_version": st.get("schema_version", self.SCHEMA_VERSION),
                "state_rev": cur_rev,
                "slim": True,
                "focused_task_id": eff_focus,
                "binding": copy.deepcopy(binding),
                "active_task_id": st.get("active_task_id"),
                "roles": copy.deepcopy(st.get("roles") or {}),
                "members": copy.deepcopy(st.get("members") or {}),
                "tasks": projected_tasks,
                "settings": copy.deepcopy(st.get("settings") or {}),
                "max_concurrency": st.get("max_concurrency", 4),
                "paused": st.get("paused", False),
            }

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
                            # On a fresh orchestrator boot, no listener thread is attached to any previously
                            # running attempt; terminate orphaned child process if still alive and mark needs_verification!
                            if pid and is_pid_alive(pid):
                                try:
                                    kill_process_tree(pid)
                                except Exception:
                                    pass
                            att["status"] = "needs_verification"
                            att["ended_at"] = now
                            att["blocking_reason"] = (
                                f"服务或进程重启导致执行中断 (原 PID={pid})，状态为“待核对”，请核对后点击继续"
                            )
                            recovered_count += 1
                            task_interrupted = True
                    if step.get("status") == "running":
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
