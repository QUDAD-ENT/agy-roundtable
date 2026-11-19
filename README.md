# 🏛️ AGY Roundtable · 圆桌会议

**Anthropomorphic Multi-Agent Collaboration Studio for Google Antigravity CLI & OpenAI Codex CLI**  
**面向 Google Antigravity CLI 与 OpenAI Codex CLI 的双端拟人化多智能体圆桌协作工坊**

[![License: MIT](https://img.shields.io/badge/License-MIT-emerald.svg)](LICENSE)
[![UI: Bilingual EN / 中文](https://img.shields.io/badge/UI-English%20%7C%20%E4%B8%AD%E6%96%87-blue.svg)](#bilingual-interface--%E4%B8%AD%E8%8B%B1%E5%8F%8C%E8%AF%AD%E7%95%8C%E9%9D%A2)
[![Companion: Agy Console](https://img.shields.io/badge/Companion-dneo9527--ctrl%2Fagy--console-purple.svg)](https://github.com/dneo9527-ctrl/agy-console)

[English](#english-overview) · [中文说明](#%E4%B8%AD%E6%96%87%E8%AF%B4%E6%98%8E) · [Associated Project: Agy Console](https://github.com/dneo9527-ctrl/agy-console)

---

## 🔗 Ecosystem & Project Association | 项目关联与生态定位

**AGY Roundtable (`agy-roundtable`)** is an independent companion collaboration studio associated with **[dneo9527-ctrl/agy-console](https://github.com/dneo9527-ctrl/agy-console)**:

- **[Agy Console (`dneo9527-ctrl/agy-console`)](https://github.com/dneo9527-ctrl/agy-console)** focuses on persistent per-window CLI binding, task queues, model fallback/quota failover, and cross-platform update packaging.
- **AGY Roundtable (`agy-roundtable`)** builds a **multi-agent team orchestration layer** on top of official `agy` and `codex` CLIs — enabling **2–6+ heterogeneous models (Gemini 4, Claude 5.5, GPT-6/6.2) to sit at a shared Roundtable, debate across rounds, dynamically `@mention` teammates mid-step, execute parallel/serial DAG pipelines, and collaborate with human-in-the-loop rulings**.

**AGY Roundtable（圆桌会议）** 是与 **[dneo9527-ctrl/agy-console](https://github.com/dneo9527-ctrl/agy-console)** 关联互补的双端多智能体团队协作工坊：
- **[Agy Console](https://github.com/dneo9527-ctrl/agy-console)** 聚焦单窗口固定会话绑定、队列进度、额度候补切换与跨平台安装包分发；
- **AGY Roundtable（本项目）** 聚焦 **多智能体自由编排与实时群聊思辨**：让来自 Antigravity（Gemini 4 / Claude 5.5）与 Codex（GPT 系列）的多位专家围坐圆桌、共享实时群聊、中途互相 `@队友` 提问核查、主帅动态点将以及多阶段串并行搭积木交付。

> Independent community open-source tool. Not an official product of Google, OpenAI, or Anthropic. Requires user-installed and user-authenticated official `agy` and/or `codex` CLIs.  
> 独立社区开源项目，非厂商官方产品；通过本机已安装并登录的官方 `agy` 与 `codex` CLI 合规调用，不读取或提取任何账号密钥。

---

<a id="english-overview"></a>
## 🌍 English Overview

### Key Capabilities

1. **Four Anthropomorphic Execution Topologies**
   - **🪑 Roundtable Chamber (`roundtable`)**: Seat 2–6+ specialists and 1 Moderator/Host. Instant parallel Round 1 independent proposals → multi-round cross-critique over an 8,000-char lossless shared transcript → final Host synthesis & deliverable.
   - **🎖️ Chief Dispatch Bridge (`chief_dispatch`)**: Assign 1 Chief Commander who inspects the active workspace and team roster, issues a tactical dispatch plan (`chief_plan`), dynamically recruits only needed specialists (parallel/serial/review), and signs off at completion.
   - **🧱 Multi-Track Pipeline Studio (`custom_dag`)**: Visually stack Stage `1..N` columns with parallel workstations and independent review gates (bounded 2-round auto-repair before escalating to human ruling).
   - **⚡ 1-on-1 Specialist Desk (`single_member`)**: Direct low-latency specialist execution with session continuation.

2. **Deep OpenScience Research & Scientific Computing / Publication Plotting Fusion**
   - **🔬 Blind Round 1 Isolation + 7 Modular OpenScience Playbooks (`openscience_playbooks.py`)**: Roundtable Round 1 drafts & disk transcripts are strictly sealed from sibling peers until Round 1 finishes; 7 composable OpenScience core protocols (`Statistical Rigor & Power`, `Experimental Design & DOE`, `Execution Hygiene & Submitted Code`, `Publication Figures & Schematics`, `Atomic Sources & Citation Audit`, `Autoresearch & Experiment Loop`, `Peer Review & Manuscript Craft`) can be auto-matched or toggled directly in the Composer.
   - **📊 Pure-Stdlib Publication Figure QA & Plotting Runtime Probe (`figure_qa.py`)**: Live detection of Python (`matplotlib`/`seaborn`/`scipy`/`statsmodels`) and R (`ggplot2`/`patchwork`/`ComplexHeatmap`/`svglite`/`ragg`) plotting runtimes; pure-stdlib binary TIFF IFD dual-axis DPI + physical-mm anti-spoofing verification, PNG/SVG/PDF structural checks, and panel-level source-data CSV/TSV semantic consistency checks (catching swapped group labels & raw-P vs adjusted-P misuse).
   - **🔒 Lock-Free Hash-Locked Mechanical Acceptance & Content-Addressed Provenance DAG (`research_verifier.py`, `provenance_store.py`)**: Executes SHA-256 hash-locked acceptance scripts (`shell=False`) and `readonly_inputs` tamper checks strictly outside state locks; records content-addressed `source → run → artifact → claim` lineage and `experiment_run` baseline-vs-delta tables in sidecar JSONL/JSON ledgers without bloating `team_state.json`.
   - **🧠 Latest-Generation Model-Family Dialects, Live Seat Hot-Swap & 2-Hop Quota Backup**: Calibrated for `Gemini 4 Neon`, `Gemini 4 Carbon`, `Gemini 4 Hydrogen`, `Claude Opus 5.5` / `Sonnet 5.5`, `GPT-6.2-Astra` (`gpt-astra.txt` action-first dialect), and `GPT-6.2-Sol`. Click any seated specialist chip to **hot-swap specialists mid-process** (abort & hand off now, or apply next round) or at any time, **add extra seats** (`+ Add Seat`), or **persistently/auto-bind quota-exhaustion backups** (`bind_quota_backup`, `auto_bind_all_quota_backups`) with **2-hop cascading failover** (strictly respecting `requires_shell` routing).
   - **🎛️ Fully Customizable Specialist Count & High Concurrency (`1–12`)**: Pick any number of experts (`2` to all `N` roster members via quick count chips or custom input), customize Chief Dispatch recruit caps (`1–12`), stack up to `10` DAG stages × `10` parallel workstations, and adjust live parallel concurrency (`1–12`) directly from the Composer or Settings popover.
   - **🛡️ Fail-Closed Review Gate & Hard Sandbox Trust Root**: Malformed/non-JSON review output routes safely to `needs_ruling` (never silently falling back to `ACCEPT`); hard `read-only` / `workspace-write` sandbox ceiling (`danger-full-access` rejected), path-escape guard, loopback CSRF `Origin`/`Content-Type` validation, and `/api/fs/raw_file` root allowlist.

3. **Minimalist Dual-Theme UI (Dark / Paper Light), Artifacts & Figures Gallery, Bilingual (`EN / 中文`)**
   - **✨ Quiet Reading-First Studio**: Single-line sticky top bar, Dark + Paper Light theme toggle, **15** curated OpenScience + Scientific Plotting + Engineering specialist templates, **7** one-click Squad Presets (including **Scientific Code & Publication Plots** and **OpenScience Autoresearch Loop**), `Ctrl+K` Command Palette, and 1-click **Quota Backup**.
   - **🖼️ Artifacts & Figures Tab (`产物与科研图`)**: Pure-white publication proof canvas (immune to dark-mode distortion), `Fit / 100% / 150%` zoom, unified `SVG / PNG / PDF / TIFF` format switcher, Before/After side-by-side comparison, command exit-code ledger, Experiment Baseline-vs-Delta board, and interactive Provenance DAG lineage.
   - **🔔 Auto-Popup Confirmation Modal & Real Local File Previewer**: Clicking any file link (`file:///...`, `.docx`, wide `.csv`/`.tsv` up to 60 cols × 100 rows with explicit truncation indicators, `.md#L29-L35`, `.pdf`, `.svg`, `.png`, `.tiff`) previews real content in-browser (`strict=1` disables silent basename fallback for artifact verification) and supports 1-click OS launch or Win32 Shell Explorer reveal.

### Quick Start (English)

1. **Prerequisites**:
   - Python `3.9+` (zero third-party pip packages required for the web console & HTTP runtime; `mcp` package optional if mounting `codex_team_mcp.py` into an IDE).
   - At least one official CLI installed & signed in: [Google Antigravity CLI (`agy`)](https://antigravity.google/docs/install?tab=cli) and/or OpenAI Codex CLI (`codex`).
2. **Launch Web Studio (`http://127.0.0.1:8765/`)**:
   - **Windows**: Double-click `Start-Roundtable.cmd` (or `打开团队协作面板.cmd`).
   - **macOS / Linux**: Run `chmod +x Start-Roundtable.sh && ./Start-Roundtable.sh`.
   - **Direct Python**: `python prototype/serve_sidebar.py`
3. **Optional MCP Integration (Antigravity / Claude / Codex MCP)**:
   ```json
   {
     "mcpServers": {
       "codex-team": {
         "command": "python",
         "args": ["/absolute/path/to/agy-roundtable/prototype/codex_team_mcp.py"]
       }
     }
   }
   ```
4. **Run Automated Tests**:
   ```bash
   python prototype/test_runtime_mechanisms.py
   ```

---

<a id="中文说明"></a>
## 🇨🇳 中文说明

### 核心特性

1. **四种自由组织阵型（全拟人化现场展示）**
   - **🪑 群聊圆桌议事厅（`roundtable`）**：自由勾选多位 Codex 与 Antigravity 专家围坐圆桌，支持首轮第 1 秒直接并行立论、多轮基于 8,000 字无损群聊记录的交叉 `@点名` 质询互评，最后由主持人收敛拍板。
   - **🎖️ 总管自动点将台（`chief_dispatch`）**：指定 1 位坐镇主帅先探查当前项目目录与团队花名册，按需抽调精兵动态编排并行/串行/审查节点并终审签发，其余成员营帐待命不陪跑。
   - **🧱 多轨搭积木工坊（`custom_dag`）**：自由增删第 `1..N` 阶段与同阶段并行工位，支持插入独立审查关卡（最多 2 轮自动退回修复，超限交人工裁决）。
   - **⚡ 专家一对一专办（`single_member`）**：直连单名专家快速分析或改码。

2. **深度融合 OpenScience 科研方法论、科研编程与出版级绘图闭环**
   - **🔬 第 1 轮真盲评隔离 + 7 大模块化 OpenScience 协议（`openscience_playbooks.py`）**：首轮草稿与磁盘纪要双端严密隔离；内置 7 套可组合核心科研规约（统计规范与效能、实验设计与防伪重复、科研代码洁净与隐性评分器验收、顶刊绘图与示意图、原子声明与文献溯源、自动科研实验循环、顶刊审稿与论文撰写），支持自动匹配或一键点选注入；
   - **📊 纯标准库顶刊科研图 QA 与 Python/R 环境探测（`figure_qa.py`）**：实时检测本机 Python（`matplotlib`/`seaborn`/`scipy`/`statsmodels`）与 R（`ggplot2`/`patchwork`/`ComplexHeatmap`/`svglite`/`ragg`）绘图栈；零第三方依赖解析二进制 TIFF 双轴 DPI 与物理毫米像素联动（识破伪造 600 DPI）、SVG 栅格陷阱与字号下限，以及面板级 Source CSV/TSV 图数一致性核验（拦截组别标签互换与原始 P 值冒充校正 `p_adj`）；
   - **🔒 无锁哈希锁定机械验收与内容寻址溯源谱系（`research_verifier.py`、`provenance_store.py`）**：在状态锁外执行 SHA-256 锁定验收脚本（`shell=False`）与只读原始数据篡改守卫；旁路 JSONL/JSON 账本自动记录 `source → run → artifact → claim` 溯源 DAG 与 `experiment_run` 基线 Delta 对比榜；
   - **🔄 现场热换人、快捷/自动绑定额度替补与 2 跳级联接力**：全面校准最新一代旗舰阵容（`Gemini 4 Neon` / `Gemini 4 Carbon` / `Gemini 4 Hydrogen` / `Claude Opus 5.5` / `GPT-6.2-Astra` / `GPT-6.2-Sol`）；点击任务席位条上任意专家胶囊即可在**进程中**（立即中止接力或下轮生效）或**任意时间**一键换人、增补席位或绑定额度耗尽默认替补，触发 429/限流时优先切至绑定替补并支持最多 **2 跳无重试级联**（严格遵守 `requires_shell` 路由约束）；
   - **🎛️ 自定义专家人数与高并行上限（`1–12`）**：圆桌发起栏内置「自定义人数」快捷栏（`2/3/4/5/6/全选` + 数字框）与直调「并行上限（`1–12`）」；主帅点将支持自定义最多点将人数（`1–12`）；多轨流水线支持最多 `10` 阶段 × `10` 并行工位；花名册总专家数无上限，可随时从 15 位模板招募或克隆。
   - **🛡️ Fail-Closed 审查门禁与安全信任根**：审查输出缺少合法 JSON `verdict` 时自动拦截进入 `needs_ruling`（绝不默认放行）；强制限制 `read-only`/`workspace-write` 沙箱上限、目录越界防逃逸、本机 POST CSRF 校验与 `/api/fs/raw_file` 根目录白名单。

3. **极简双主题（深色 / 纸感浅色）、产物与科研图画廊、中英双语与真实文件预览**
   - **✨ 专注阅读体验**：单行粘滞顶栏、深浅色一键切换、**15 位** OpenScience 科研 + 代码绘图 + 工程专家模板、**7 套**一键阵容预设（含 **科研代码与顶刊绘图闭环** 与 **OpenScience 自动科研实验循环**）、`Ctrl+K` 专家快切与「额度替补」弹窗；
   - **🖼️ 产物与科研图画廊（纯白投稿底板）**：不受深色模式干扰的纯白科研图预览画布，支持 `Fit / 100% / 150%` 缩放、`SVG / PNG / PDF / TIFF` 同源切换、Before/After 并排对比、命令退出码账本、实验对比榜与溯源谱系树；
   - **🔔 AI 提问自动弹窗与宽表/源码严格预览**：宽 CSV/TSV 预览扩容至 60 列 × 100 行并显式提示列截断；官方产物预览支持 `strict=1` 禁用同名静默回退。

### 快速启动（中文）

1. **前置条件**：本机已安装 Python `3.9+`，并已安装登录官方 `agy` CLI 或 `codex` CLI。
2. **一键开启**：
   - **Windows**：双击 `Start-Roundtable.cmd` 或 `打开团队协作面板.cmd`（自动守护拉起后台服务并打开 `http://127.0.0.1:8765/`）。
   - **macOS / Linux**：终端执行 `./Start-Roundtable.sh`。
3. **切换任意工作项目**：在顶部项目栏下拉选择、点击 `📂 浏览任意文件夹...` 或调用系统原生目录选择器即可自由切换到任意本地课题或代码仓库。

---

## 📁 Repository Structure | 目录结构

```text
agy-roundtable/
├── prototype/
│   ├── serve_sidebar.py          # Loopback HTTP server, CSRF/root guard & Hot-Swap/Artifacts REST API (rev 13)
│   ├── sidebar_panel.html        # Bilingual (EN/ZH) Studio UI + Pure-White Figure Gallery + Live Seat Hot-Swap
│   ├── team_runtime.py           # v5 Orchestrator (Roundtable, Chief, DAG, Hot-Swap, 2-Hop Quota Failover)
│   ├── team_state_manager.py     # Atomic state persistence, 15 specialist templates & orphan-PID recovery
│   ├── figure_qa.py              # Pure-stdlib Python/R runtime probe, TIFF/PNG/SVG/PDF & Source-CSV QA
│   ├── research_verifier.py      # Lock-free SHA-256 hash-locked acceptance verifier & readonly_inputs guard
│   ├── openscience_playbooks.py  # 7 modular OpenScience core methodology playbooks & auto-matcher
│   ├── provenance_store.py       # Content-addressed Provenance DAG & Experiment Baseline-vs-Delta ledger
│   ├── agy_adapter.py            # Official Google Antigravity CLI stream-json worker adapter (stdin NDJSON)
│   ├── codex_adapter.py          # Official OpenAI Codex CLI JSONL worker adapter + command/file ledger
│   ├── project_discovery.py      # Cross-platform workspace & folder browser
│   ├── team_cli.py               # CLI client & background daemon launcher
│   ├── codex_team_mcp.py         # MCP server ('codex-team') for IDE native integration
│   └── test_runtime_mechanisms.py# 21 automated lifecycle, P0 security, Figure QA, Hot-Swap & OpenScience tests
├── docs/
│   └── USER_GUIDE.md             # Detailed Bilingual (EN/ZH) User Guide
├── Start-Roundtable.cmd          # Windows 1-click launcher
├── Start-Roundtable.sh           # macOS / Linux 1-click launcher
├── LICENSE                       # MIT License
└── README.md                     # Bilingual project documentation
```

## 🔒 Privacy & Security Notice | 隐私与安全边界

- Binds strictly to `127.0.0.1` loopback interface for trusted single-user local workstations.
- Never reads, copies, or transmits CLI authentication tokens (`auth.json` / keychains); all model turns execute solely through the user's official `agy` and `codex` binaries with explicit `--sandbox` / `--settings` tool boundaries.
- Runtime state (`prototype/team_state.json`) contains local workspace history and is excluded via `.gitignore` — never commit personal task snapshots to public repositories.
