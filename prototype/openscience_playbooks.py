"""
OpenScience Core Research Playbooks & Auto-Matcher (Pure Stdlib).
Distills core scientific methodology SOPs from synthetic-sciences/openscience:
- statistical-conventions & statistical-power
- experimental-design (randomization, blocking, replication, controls)
- execution-hygiene, submitted-code & acceptance-checks
- figures, scientific-visualization & schematics
- sources (atomize-locate-grade) & citations (resolve-before-cite)
- autoresearch & experiments tracking (baseline + delta + steering)
- peer-review, paper-writing & literature-review
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


OPENSCIENCE_PLAYBOOKS: List[Dict[str, Any]] = [
    {
        "id": "statistical_rigor",
        "name_zh": "统计规范与效能",
        "name_en": "Statistical Rigor & Power",
        "source_skills": ["statistical-conventions", "statistical-power"],
        "keywords": [
            "p值", "p-value", "fdr", "anova", "回归", "regression", "cox", "hr", "or",
            "显著", "统计", "样本量", "效能", "power", "ci", "置信区间", "检验", "survival",
            "cohort", "队列", "biomarker", "meta", "corr", "相关", "差异分析", "deseq", "wilcoxon",
        ],
        "prompt_block": (
            "【OpenScience 融合规约 · 统计规范与效能 (statistical-conventions + statistical-power)】\n"
            "1. 设计优先选检验：配对/重复测量严禁用独立样本检验；有序分组（剂量/分期/分级）优先趋势检验而非无序 omnibus 检验；重偏态或小样本离群值须给出非参/稳健敏感性分析；\n"
            "2. 多重检验与效应量强制并报：任何多指标/多组比较必须明确校正方法（BH-FDR / Holm / Bonferroni）并报告校正后 P 值，严禁只报 P 值——必须同时给出效应量（差值/比值/HR/Cohen's d）及 95% CI 与每组真实 n；\n"
            "3. 效能与效应量收缩：评估样本量或先验效应时，必须警示文献/预实验效应量存在发表偏倚与 Winner's Curse 膨胀，优先围绕“最小有科学/临床意义的效应量 (MDE)”做闭式或蒙特卡洛功效评估。"
        ),
    },
    {
        "id": "experimental_design",
        "name_zh": "实验设计与防伪重复",
        "name_en": "Experimental Design & DOE",
        "source_skills": ["experimental-design"],
        "keywords": [
            "实验设计", "随机化", "对照组", "盲法", "区组", "分层", "生物学重复", "伪重复",
            "experimental design", "randomization", "blocking", "factorial", "doe", "ablation",
            "消融", "对照", "control", "cohort design", "trial", "临床试验", "队列设计",
        ],
        "prompt_block": (
            "【OpenScience 融合规约 · 实验设计与防伪重复 (experimental-design)】\n"
            "1. 三要素齐备：明确随机化单位（小样本 n<100 或顺序入组用置换区组 permuted blocks；关键预后因子用分层随机；整群干预按 cluster 单位随机）、独立重复层级（严防将技术重复/同个体多切片误作生物学重复导致伪重复）、区组/批次控制（batch/day/site/litter）；\n"
            "2. 阴性/阳性与竞争性对照：为每条主假说显式配备能区分“真机制 vs 混杂/批次伪影”的最小判别对照或消融组；\n"
            "3. 预设分析界线：区分预注册主终点与探索性亚组分析，避免事后数据窥探（p-hacking / HARKing）。"
        ),
    },
    {
        "id": "execution_hygiene_code",
        "name_zh": "科研代码洁净与验收",
        "name_en": "Execution Hygiene & Submitted Code",
        "source_skills": ["execution-hygiene", "submitted-code", "acceptance-checks"],
        "keywords": [
            "代码", "脚本", "python", "rscript", "重构", "pipeline", "算法", "模型训练", "复现",
            "bug", "测试", "pytest", "benchmark", "优化", "性能", "代码审查", "reproduce",
            "script", "code", "implement", "refactor", "cli", "api", "验收", "训练",
        ],
        "prompt_block": (
            "【OpenScience 融合规约 · 科研代码洁净与隐性评分器验收 (execution-hygiene + submitted-code + acceptance-checks)】\n"
            "1. 非破坏性重跑与早落盘：严禁覆盖或删除原始数据与既有结果目录（重跑写入新子目录或版本化文件名）；尽早在磁盘产出最小可验证产物并按自然边界做 Checkpoint；\n"
            "2. 线程与环境洁净：数值计算脚本开头显式固定随机种子并按需限制线程数（如 `OMP_NUM_THREADS`/`OPENBLAS_NUM_THREADS`），明确入口参数、读写边界与依赖清单，绝不隐式依赖未声明的临时安装包；\n"
            "3. 隐性评分器预演（Hidden-Grader Rehearsal）：交付前逐项核对精确路径、列名/顺序、dtype、数值容差、自洽守恒条件及边界/稀有类别；用扰动输入（重命名 ID/打乱顺序/极值）自测入口不抛未捕获异常，修改后必重跑验收命令并如实报告退出码（严禁 Edit-After-Check）。"
        ),
    },
    {
        "id": "publication_figures_schematics",
        "name_zh": "顶刊绘图与示意图",
        "name_en": "Publication Figures & Schematics",
        "source_skills": ["figures", "scientific-visualization", "schematics"],
        "keywords": [
            "画图", "绘图", "科研图", "出图", "图表", "配图", "可视化", "figure", "plot",
            "matplotlib", "seaborn", "ggplot", "ggplot2", "patchwork", "complexheatmap",
            "svg", "tiff", "dpi", "示意图", "流程图", "schematic", "heatmap", "volcano",
            "forest", "kaplan", "km曲线", "boxplot", "violin", "scatter",
        ],
        "prompt_block": (
            "【OpenScience 融合规约 · 顶刊科研绘图与示意图 (figures + scientific-visualization + schematics)】\n"
            "1. 后端排他与实测数据直绘（零伪造）：明确采用单一后端（Python matplotlib/seaborn 或 R ggplot2/patchwork/ComplexHeatmap）全程出图与导出，严禁跨语言代绘；所有统计图必须由独立固定种子脚本直接读取冻结 Source Data CSV/TSV 绘制，AI 生成位图绝不可充当实验证据；\n"
            "2. 投稿级几何、可编辑字体与三格式导出：按终印版宽直接建画布（Nature 89/183mm · Science 55/120/184mm · Cell 85/114/174mm）、纯白底板 (#FFFFFF)、终稿有效字号 ≥6pt、色盲友好配色（Okabe-Ito / viridis）；Python 强制设 `rcParams['svg.fonttype']='none'` 与 `rcParams['pdf.fonttype']=42`（R 用 `svglite` + `cairo_pdf` + `ragg::agg_tiff`）确保文字可编辑不转曲；同步导出 `SVG` + `PNG` (300 DPI) + `TIFF` (600 DPI 且像素宽匹配物理毫米)，并写出含 `exports`、`script_path`、`source_data_path`、`panel_sources` 的 `figure_manifest.json` 供 `figure_qa.py` 机械核验；\n"
            "3. 示意图与机理图规范（schematics）：组件标签须与正文逐字一致，流向统一（左→右或上→下），仅用单一强调色突出核心贡献，标准组件用中性灰，虚线区分反馈/假说路径。"
        ),
    },
    {
        "id": "sources_citations_audit",
        "name_zh": "原子声明与文献溯源",
        "name_en": "Atomic Sources & Citation Audit",
        "source_skills": ["sources", "citations"],
        "keywords": [
            "核查", "事实", "引用", "文献", "参考文献", "幻觉", "溯源", "citation", "doi",
            "arxiv", "pubmed", "audit", "verify", "fact", "source", "查证", "真实性", "来源",
        ],
        "prompt_block": (
            "【OpenScience 融合规约 · 原子声明审计与先读后引 (sources + citations)】\n"
            "1. 原子化逐条核证（Atomize → Locate → Grade）：将关键段落拆解为可独立判定真伪的原子声明，逐条定位精确原文句子/CSV行号/代码行，并用四级标尺打分：`SUPPORTED`（原文/实测完全支持且口径一致）/ `PARTIAL`（方向对但数值/范围/条件有偏差）/ `UNSUPPORTED`（未见证据支撑）/ `CONTRADICTED`（与实测或原文矛盾）；\n"
            "2. 先解析后引用（Resolve-before-cite）：每条外部文献引用必须先核验真实记录并附唯一规范标识符（优先 DOI，预印本用 arXiv ID），仅在阅读具体论断段落后才归因，严禁凭记忆拼凑作者年份或页码。"
        ),
    },
    {
        "id": "autoresearch_loop",
        "name_zh": "自动科研实验循环",
        "name_en": "Autoresearch & Experiment Loop",
        "source_skills": ["autoresearch", "experiments"],
        "keywords": [
            "实验循环", "基线", "baseline", "超参", "消融实验", "迭代优化", "autoresearch",
            "experiment", "metric", "指标", "benchmark", "sota", "对比实验", "调优", "study",
        ],
        "prompt_block": (
            "【OpenScience 融合规约 · 自动科研实验循环与基线对照 (autoresearch + experiments)】\n"
            "1. 先定单一北极星指标与基线：明确主优化指标、优化方向（maximize/minimize）、计算预算与不可触碰的评估协议；首跑必须先立住可复现 `baseline`（priority 1000）再提出有序假说队列；\n"
            "2. 每次实验记录 Delta 与决策：每轮改动单一主变量以便归因，用对比表并排报告 `run_id | hypothesis | metric | delta_vs_baseline | status (keep/discard/investigate) | evidence_path`；若新实验未击败基线，如实记录负结果并提炼机制教训，严禁事后篡改基线或挑选最佳随机种子；\n"
            "3. 如产出结构化实验运行记录，可在末尾附 ```experiment_run {...}``` 代码块自动入库到工作台实验对比榜与溯源谱系 DAG。"
        ),
    },
    {
        "id": "peer_review_submission",
        "name_zh": "顶刊审稿与论文撰写",
        "name_en": "Peer Review & Manuscript Craft",
        "source_skills": ["peer-review", "paper-writing", "literature-review"],
        "keywords": [
            "审稿", "投稿", "论文", "摘要", "引言", "讨论", "返修", "rebuttal", "manuscript",
            "nature", "science", "lancet", "jama", "bmc", "review", "literature", "综述", "润色",
            "paper", "writing", "journal", "审稿意见",
        ],
        "prompt_block": (
            "【OpenScience 融合规约 · 顶刊审稿、文献综述与稿件撰写 (peer-review + paper-writing + literature-review)】\n"
            "1. 全件核验而非走马观花：审稿须通读正文、附表与代码，有原始输入时必须亲自复算而非轻信文内数字；每条意见严格标注 `BLOCKING`（动摇核心结论，含 `[位置] 问题 · 为何动摇结论 · 最小可解方案`）或 `OBSERVATION`（改进建议）；\n"
            "2. 诚实科学表达与连贯行文：若证据反驳预设假说，须以精确数字坦诚报告而非迎合预期；学术正文使用连贯自然段（每段首句立核心论点），每条断言均能回溯至项目实测产出或已核实文献；\n"
            "3. 文献综述纪律：检索前先锁定时间窗与筛选优先级，按 DOI/arXiv ID/规范标题去重，区分 Discovery / Related Work / PRISMA 系统综述模式。"
        ),
    },
]

PLAYBOOK_BY_ID: Dict[str, Dict[str, Any]] = {p["id"]: p for p in OPENSCIENCE_PLAYBOOKS}


def auto_select_playbooks(goal: str, execution_mode: str = "", squad_preset: str = "", max_count: int = 2) -> List[str]:
    """Automatically select the 1-2 most relevant OpenScience core playbooks from the goal & context (zero bloat)."""
    low = f"{goal or ''} {squad_preset or ''}".lower()
    scored: List[tuple] = []
    for pb in OPENSCIENCE_PLAYBOOKS:
        hits = sum(1 for kw in pb["keywords"] if kw in low)
        if squad_preset == "scientific_coding_plotting" and pb["id"] in ("publication_figures_schematics", "execution_hygiene_code"):
            hits += 4
        elif squad_preset == "openscience_autoresearch" and pb["id"] in ("autoresearch_loop", "execution_hygiene_code"):
            hits += 4
        elif squad_preset == "research_submission" and pb["id"] in ("peer_review_submission", "statistical_rigor"):
            hits += 3
        elif squad_preset == "openscience_discovery" and pb["id"] in ("experimental_design", "statistical_rigor"):
            hits += 2
        if hits > 0:
            scored.append((hits, pb["id"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen = [pid for _, pid in scored[:max_count]]
    if not chosen and execution_mode == "roundtable":
        chosen = ["sources_citations_audit"]
    return chosen


def render_playbooks_prompt(playbook_ids: Optional[List[str]]) -> str:
    """Render selected OpenScience core playbook SOP blocks into a compact prompt section."""
    blocks: List[str] = []
    seen = set()
    for pid in playbook_ids or []:
        if pid in PLAYBOOK_BY_ID and pid not in seen:
            seen.add(pid)
            blocks.append(PLAYBOOK_BY_ID[pid]["prompt_block"])
    return "\n\n".join(blocks)


def get_playbooks_catalog() -> List[Dict[str, Any]]:
    """Return UI-safe metadata for all OpenScience playbooks."""
    return [
        {
            "id": p["id"],
            "name_zh": p["name_zh"],
            "name_en": p["name_en"],
            "source_skills": list(p["source_skills"]),
        }
        for p in OPENSCIENCE_PLAYBOOKS
    ]
