# 🏛️ AGY Roundtable · 圆桌会议

**Heterogeneous Multi-Agent Roundtable Studio for Google Antigravity CLI & OpenAI Codex CLI**  
**面向 Google Antigravity CLI 与 OpenAI Codex CLI 的跨厂商多智能体圆桌协作工坊**

[![Release: v5.6.0](https://img.shields.io/badge/Release-v5.6.0-indigo.svg)](https://github.com/QUDAD-ENT/agy-roundtable/releases/tag/v5.6.0)
[![Zero Pip Deps](https://img.shields.io/badge/Core-Pure%20Python%20Stdlib-emerald.svg)](#why-agy-roundtable--核心差异化优势)
[![Tests: 34/34 Passing](https://img.shields.io/badge/Tests-34%2F34%20Passing-brightgreen.svg)](#quick-start-english)
[![UI: Bilingual EN / 中文](https://img.shields.io/badge/UI-English%20%7C%20%E4%B8%AD%E6%96%87-blue.svg)](#中文说明)
[![License: MIT](https://img.shields.io/badge/License-MIT-slate.svg)](LICENSE)
[![Companion: Agy Console](https://img.shields.io/badge/Companion-dneo9527--ctrl%2Fagy--console-purple.svg)](https://github.com/dneo9527-ctrl/agy-console)

[Why AGY Roundtable?](#why-agy-roundtable--核心差异化优势) · [What's New in v5.6.0](#whats-new-in-v560--v560-最新亮点) · [English Overview](#english-overview) · [中文说明](#中文说明) · [Quick Start](#quick-start-english)

> **Stop letting one model grade its own homework.**  
> Seat **Gemini 4**, **Claude 5.5**, and **GPT-6.2** at the same Roundtable — with **sealed Blind Round 1**, **anonymous Borda consensus**, **user-controlled 3-tier sandboxes + `--add-dir`**, **KV-cache long-context compression**, **bilingual SQLite FTS5 memory**, **Git Worktree parallel isolation**, and **publication-grade scientific Figure QA** — running 100% locally via your official signed-in CLIs with **zero third-party pip dependencies** and **zero API-key proxying**.
>
> **不要让同一个模型自己给自己批改作业。**  
> 让 **Gemini 4**、**Claude 5.5** 与 **GPT-6.2** 同席圆桌——内置**首轮真盲评隔离**、**匿名互评 Borda 共识计票**、**用户自主三档沙箱与跨目录 `--add-dir` 授权**、**三区 KV 缓存长上下文压缩**、**中英双语 SQLite FTS5 跨任务记忆**、**Git Worktree 并行隔离写盘**与**顶刊级科研图双门控质检**；零第三方 pip 依赖、零密钥托管、100% 本地合规调用本机已登录的官方 CLI。

---

<a id="why-agy-roundtable--核心差异化优势"></a>
## 🏆 Why AGY Roundtable? | 核心差异化优势

How **AGY Roundtable v5.6.0** compares against single-vendor CLI agent teams and code-heavy multi-agent frameworks:

| Dimension / 能力维度 | Single-Vendor CLI Teams (单一厂商 CLI) | Generic Frameworks (AutoGen / CrewAI / LangGraph) | **🏛️ AGY Roundtable v5.6.0** |
| :--- | :--- | :--- | :--- |
| **Heterogeneous Multi-Model Debate**<br>跨厂商异构模型同席思辨 | Locked to one vendor's model family | Requires manual API keys, billing & custom glue code | **Native Antigravity (`Gemini 4` / `Claude 5.5`) + Codex (`GPT-6.2`)** in one live Roundtable, with real-time `@mention` peer verification |
| **Anti-Groupthink Governance**<br>反群体迷思与防锚定机制 | Shared prompt thread; early anchoring bias | Free-form chat loops prone to sycophancy | **Sealed Blind Round 1** (in-memory & on-disk isolation) + **Anonymous Seat Masking (`Seat A/B/C`)** + **Deterministic Borda Voting** |
| **Granular Sandbox & Cross-Drive Control**<br>用户自主三档沙箱与跨目录授权 | Global binary flags; hard to mix per-agent permissions | Usually unsandboxed host execution or heavy Docker | **3-Tier Permission Center**: `read-only` / `workspace-write` + **`--add-dir` (up to 8 cross-drive folders)** / **2-step opt-in `danger-full-access`** (1-click instant relock & clamp) + **Per-Seat Roundtable overrides** |
| **Long-Context & Memory Engine**<br>长上下文治理与跨任务记忆 | Naive truncation or full history replay | External vector DBs, embeddings & heavy infra | **3-Zone KV-Cache Stable Prefix** + observation masking + immutable snapshots + peak-occupancy auto rollover (`L0→L1→L2`) + **Pure-Stdlib Bilingual SQLite FTS5** & **AST RepoMap** |
| **Parallel Write Safety**<br>多工位并行改码隔离 | Concurrent edits can collide in one working tree | Rarely integrated with native Git | **Opt-in Native Git Worktree Isolation** per writable step, live unified diff viewer, dirty-repo guard & conflict-safe `--no-ff` merge with auto-rollback |
| **Scientific Rigor & Figure QA**<br>科研复算与顶刊级出图闭环 | General coding only | Not built-in | **7 Modular OpenScience Playbooks**, pure-stdlib **TIFF/PNG/SVG/PDF/CSV anti-fabrication gate**, automated **VLM Visual Critique & Self-Repair loop**, SHA-256 hash-locked verifiers & Provenance DAG |
| **Calling Resilience & Zero-Freeze UX**<br>高流畅度、防卡死与自动容错 | Stalls or 429 errors abort the turn | Requires custom retry graphs | **Linear $O(N)$ zero-ReDoS Markdown**, `slim + 304 + gzip` polling (**-96% payload**), `0%` idle CPU/disk writes, dynamic idle watchdog (`240s–600s`) + **2-hop cross-engine quota/stall failover**, **zero Windows CMD / `Rterm` crash popups** |
| **Setup & Dependencies**<br>安装成本与依赖 | Vendor CLI only | Heavy Python/Node/Docker stack + API keys | **Zero third-party pip packages** (100% Python stdlib + single-file Studio UI), official CLI auth only, **built-in GitHub auto-update notification popup** |

### 📊 Verified Engineering Benchmarks | 实测工程指标

- **HTTP Polling Bandwidth**: `5.22 MB` full state → **`215 KB`** cold slim gzip (`-95.9%`) → **`0 B` / `1.09 ms`** via `HTTP 304 Not Modified` (`since_rev` + `ETag`).
- **Markdown Renderer Anti-Freeze**: 600-char unclosed-link adversarial input reduced from exponential catastrophic backtracking freeze → **`0.1 ms`** linear $O(N)$ scan + 180-entry LRU cache.
- **Idle Runtime Footprint**: **`0` disk writes/sec** and **`0%` background CPU delta** when no task is actively running.
- **Automated Regression Verification**: **`34 / 34`** end-to-end tests passing (`~29s`) covering P0 sandbox trust root, 3-tier opt-in/relock, `--add-dir`, per-seat overrides, SQLite FTS5 bilingual search, Git Worktree merge, VLM Figure QA, quota failover, and Win32 zero-popup/zero-crash-dialog execution.

---

<a id="whats-new-in-v560--v560-最新亮点"></a>
## ✨ What's New in v5.6.0 | v5.6.0 最新亮点

- **🛡️ User-Controlled 3-Tier Sandbox & Directory Permission Center (`v5.6.0`)**:
  - Live topbar status pill (`🔒 只读` / `✏️ 可写` / `⚠️ 完全访问`) opens the dedicated **Sandbox & Directory Permission Center**.
  - **Tier 1 (`read-only`)**: Strictly read-only review & verification (with optional global read-only lock).
  - **Tier 2 (`workspace-write` + `--add-dir`)**: Recommended daily mode for coding, plotting, and R/Python scripts — plus **up to 8 extra cross-drive/external directories** (paste path or native OS Folder Picker) passed directly to Codex (`--add-dir` / `writable_roots`) and Antigravity (`--add-dir`).
  - **Tier 3 (`danger-full-access`)**: **Fail-closed locked by default** (`allow_danger_full_access=false`); requires explicit 2-step risk confirmation in the UI to unlock, displays a red warning banner whenever selected, and supports **1-click instant relock** (immediately clamping global ceiling, default task mode, and all members back to `workspace-write`).
- **🪑 Per-Seat Roundtable Permissions & MCP Auto-Sync (`v5.6.0`)**:
  - In any writable Roundtable, toggle each seated expert individually (`🔒 / ✏️ / ⚠️`) right on the participant chip (e.g., keep peer reviewers `read-only` while granting `workspace-write` to statistical/plotting engineers).
  - MCP `team_dispatch_task` now defaults `sandbox=None` to automatically follow your configured `default_task_sandbox`, and supports `allow_member_writes`, `add_dirs_json`, and `seat_sandboxes_json`.
- **🪟 Zero Windows `Rterm.exe` Crash Modals & Zero Black CMD Popups (`v5.5.1` + `v5.5.0`)**:
  - Process-tree inherited Win32 `SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)` eliminates blocking `Rterm.exe / Rscript.exe 0xc0000005` crash modals on Windows; read-only sandbox guardrails steer statistical spot-checks to Python while R executes cleanly in `workspace-write`.
  - Unified `hidden_subprocess_kwargs()` (`CREATE_NO_WINDOW` + `SW_HIDE`, no `DETACHED_PROCESS`) + `-c notify=[]` eliminates stray CMD console flashes on Windows.
- **🔔 Automatic Update Popups for Older Versions (`v5.5.0+`)**:
  - Both older local installations (checking remote GitHub `VERSION.json` / Releases API) and cached browser tabs (detecting `X-Server-Rev` hot upgrades) automatically display a non-intrusive topbar `新版 vX.Y.Z` badge and bilingual changelog modal with 1-click `git pull` copy & GitHub Release link.

---

## 🔗 Ecosystem & Project Association | 项目关联与生态定位

**AGY Roundtable (`agy-roundtable`)** is an independent companion collaboration studio associated with **[dneo9527-ctrl/agy-console](https://github.com/dneo9527-ctrl/agy-console)**:

- **[Agy Console (`dneo9527-ctrl/agy-console`)](https://github.com/dneo9527-ctrl/agy-console)** focuses on persistent per-window CLI binding, task queues, model fallback/quota failover, and cross-platform update packaging.
- **AGY Roundtable (`agy-roundtable`)** builds the **multi-agent team orchestration & scientific verification layer** on top of official `agy` and `codex` CLIs — seating heterogeneous models at a shared Roundtable with blind peer review, dynamic `@mention` consultations, Chief Dispatch, multi-stage DAGs, Git Worktree isolation, and publication Figure QA.

> Independent community open-source tool. Not an official product of Google, OpenAI, or Anthropic. Executes solely through user-installed and user-authenticated official `agy` and/or `codex` CLIs without reading or transmitting account credentials.  
> 独立社区开源项目，非厂商官方产品；仅通过本机已安装并登录的官方 `agy` 与 `codex` CLI 合规调用，不读取或提取任何账号密钥。

---

<a id="english-overview"></a>
## 🌍 English Overview

### Core Capabilities

1. **Four Anthropomorphic Execution Topologies**
   - **🪑 Roundtable Chamber (`roundtable`)**: Seat 2–N heterogeneous specialists + 1 Host. Sealed Blind Round 1 independent proposals → multi-round adversarial cross-critique with `@mention` verification & optional anonymous Borda voting → Host multi-dimensional synthesis.
   - **🎖️ Chief Dispatch Bridge (`chief_dispatch`)**: Assign 1 Chief Commander to inspect the workspace & roster, dynamically recruit only needed specialists (`1–12`), orchestrate parallel/serial/review nodes, and sign off.
   - **🧱 Multi-Track Pipeline Studio (`custom_dag`)**: Visually stack up to `10` stages × `10` parallel workstations with independent fail-closed review gates (bounded 2-round auto-repair before human ruling).
   - **⚡ 1-on-1 Specialist Desk (`single_member`)**: Direct low-latency specialist turn with native session continuation.

2. **Context Compression, Cross-Task Memory & Parallel Isolation**
   - **🧠 3-Zone KV-Cache Context Engine (`context_engine.py`)**: Stable prefix hashing, externalized observation masking (`BlobStore`), newest-first immutable round snapshots + Pinned Historical Anchor Ledger, dual billing vs peak-context occupancy calibration, and automatic lossless session rollover (`L0 → L1 → L2`).
   - **🔎 Bilingual SQLite FTS5 Memory & AST RepoMap (`memory_index.py`)**: Zero-dependency English + CJK bi-gram BM25 full-text memory across tasks/snapshots/blobs (strictly disabled during Blind Round 1) + AST Structural RepoMap (Python `ast` + JS/TS/HTML/R + import centrality).
   - **🌿 Native Git Worktree Parallel Isolation (`worktree_manager.py`)**: Opt-in isolated branch/worktree per writable step, live unified diff inspection, dirty-repo guard, and conflict-safe `--no-ff` merge with automatic rollback.

3. **OpenScience Research & Publication-Grade Figure QA**
   - **🔬 7 Modular OpenScience Playbooks (`openscience_playbooks.py`)**: Auto-matched or manually toggled protocols covering Statistical Rigor, Experimental Design, Code Hygiene, Publication Figures, Atomic Citations, Autoresearch Loop, and Peer Review.
   - **🎨 Dual-Gate Publication Figure QA + VLM Critique Loop (`figure_qa.py`)**: Pure-stdlib Python/R backend probe, binary TIFF IFD dual-axis DPI & physical-mm anti-spoofing, PNG/SVG/PDF structural checks, Source-CSV semantic verification (catching group swaps & raw-vs-adjusted P misuse), automated multimodal VLM critique (`PASS | REVISE`) & bounded self-repair (max 2 rounds), plus optional editable `.drawio` export.
   - **🔒 Lock-Free Hash-Locked Verification & Provenance DAG (`research_verifier.py`, `provenance_store.py`)**: SHA-256 locked acceptance scripts (`shell=False`), `readonly_inputs` tamper guard, content-addressed `source → run → artifact → claim` DAG, and `experiment_run` baseline-vs-delta board.

4. **Studio UX, Resilience & Security**
   - **🔄 Live Seat Hot-Swap & 2-Hop Quota/Stall Failover**: Swap any seated specialist mid-process or next round, add seats on the fly, auto-bind quota backups, and seamlessly failover across engines on 429 / idle stall / read-only shell blocks.
   - **🛡️ Hard Security Trust Root**: Loopback-only `127.0.0.1` binding, POST CSRF `Origin`/`Content-Type` validation, `/api/fs/raw_file` root allowlist, path-escape guard, fail-closed review JSON gate, and user-controlled 3-tier sandbox with 2-step opt-in for `danger-full-access`.
   - **✨ Quiet Reading-First Bilingual UI (`EN / 中文`)**: Dark + Paper Light themes, 15 curated specialist templates, 7 Squad Presets, pure-white Figure Gallery (with color-blindness proofing filters & side-by-side compare), wide CSV/TSV/DOCX/PDF/SVG/TIFF local file previewer, and `Ctrl+K` Command Palette.

<a id="quick-start-english"></a>
### Quick Start (English)

1. **Prerequisites**:
   - Python `3.9+` (**zero third-party pip packages required** for the web console & HTTP runtime; `mcp` package optional only if mounting `codex_team_mcp.py`).
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
4. **Run Automated Regression Suite (`34` tests)**:
   ```bash
   python prototype/test_runtime_mechanisms.py
   ```

---

<a id="中文说明"></a>
## 🇨🇳 中文说明

### 核心能力一览

1. **四种拟人化团队编排阵型**
   - **🪑 群聊圆桌议事厅（`roundtable`）**：2~N 位跨引擎专家同席圆桌——首轮真盲评并行立论（杜绝早期锚定偏倚）→ 多轮共享群聊对抗质询、`@队友` 实时核验与可选匿名 Borda 共识计票 → 主持人多维仲裁定稿。
   - **🎖️ 总管自动点将台（`chief_dispatch`）**：主帅探查项目与花名册后按需抽调 `1–12` 名精兵动态编排并行/串行/审查节点并终审签发。
   - **🧱 多轨搭积木工坊（`custom_dag`）**：自由堆叠最多 `10` 阶段 × `10` 并行工位与独立 Fail-Closed 审查关卡（自动退回修复最多 2 轮，超限交人工裁决）。
   - **⚡ 专家一对一专办（`single_member`）**：直连单名专家高响应分析或改码，支持原生会话续聊。

2. **用户自主三档沙箱权限 + 跨目录授权 (`--add-dir`) + 单席细粒度控制**
   - **顶栏权限胶囊直达「沙箱与目录权限中心」**：自由在 **🔒 只读 (`read-only`)**、**✏️ 项目目录可写 (`workspace-write`)** 与 **⚠️ 完全访问 (`danger-full-access`)** 间按需配置；高危完全访问默认安全锁定，需二次风险勾选确认方可解锁，并支持一键立即重新锁定回退。
   - **跨盘 / 外部目录授权 (`--add-dir`)**：无需放开整机权限，即可直接粘贴或通过系统原生选目录弹窗放行最多 8 个外部数据/代码目录，Codex 与 Antigravity CLI 同步生效。
   - **圆桌单席权限 & MCP 自动同步**：发起圆桌时可给每个参会席位单独点选 `🔒 / ✏️ / ⚠️`（例如审稿官只读、统计/绘图官可写）；MCP `team_dispatch_task` 自动跟随面板默认权限。

3. **长上下文压缩、双语 FTS5 记忆、Git Worktree 隔离与顶刊科研绘图闭环**
   - **🧠 三区 KV 缓存长上下文引擎 (`context_engine.py`)**：固定前缀缓存命中、观测块自动外置折叠、不可变轮次状态卡（newest-first 保留 + 历史锚点总账）、单次峰值水位校准与超限自动无损换乘 (`L0 → L1 → L2`)。
   - **🔎 纯标准库中英双语 SQLite FTS5 记忆与 AST 代码图谱 (`memory_index.py`)**：跨任务检索历史状态卡、指标数值与报错结论（盲评第 1 轮严格屏蔽），自动提取 Python/JS/TS/HTML/R 核心符号与依赖图谱。
   - **🌿 原生 Git Worktree 并行写盘隔离 (`worktree_manager.py`)**：同仓库并行改码自动分枝隔离，支持实时 Diff 审查、脏工作区保护与冲突安全 `--no-ff` 合并。
   - **🔬 7 大 OpenScience 科研协议 & 顶刊科研图双门控质检 (`openscience_playbooks.py`, `figure_qa.py`)**：内置 Python/R 绘图栈实时探测、纯标准库 TIFF 双轴物理 DPI 防造假校验、SVG/PNG/PDF 与 Source CSV 图数一致性核验、自动 VLM 视觉审图闭环（最多 2 轮自修）、可选可编辑 `.drawio` 导出与内容寻址溯源 DAG。
   - **⚡ 极速零卡死体验与 Windows 零弹窗运行时**：线性 $O(N)$ 防 ReDoS Markdown 渲染、`slim + 304 + gzip` 增量状态轮询、空闲零磁盘写入、动态努力度感知超时与跨引擎自动容错、Win32 全链路静默子进程与 `SetErrorMode` 根治 Windows 命令行黑框及 `Rterm.exe` 崩溃弹窗，并支持新版本自动检测弹窗提醒。

### 快速启动（中文）

1. **前置条件**：本机已安装 Python `3.9+`（Web 控制台与核心运行时零第三方 pip 依赖），并已安装登录官方 `agy` CLI 或 `codex` CLI。
2. **一键开启**：
   - **Windows**：双击 `Start-Roundtable.cmd` 或 `打开团队协作面板.cmd`（后台无弹窗守护启动并打开 `http://127.0.0.1:8765/`）。
   - **macOS / Linux**：终端执行 `./Start-Roundtable.sh`。
3. **自由切换项目与权限**：顶栏下拉或系统弹窗切换任意本地课题/仓库；点击顶栏右侧权限胶囊即可按需切换三档沙箱或添加跨盘 `--add-dir` 数据目录。

---

## 📁 Repository Structure | 目录结构

```text
agy-roundtable/
├── prototype/
│   ├── serve_sidebar.py          # Loopback HTTP server, slim+304+gzip API, CSRF/root guard & native pickers (rev 21)
│   ├── sidebar_panel.html        # Single-file Bilingual (EN/ZH) Studio UI, 3-Tier Sandbox Center & Figure Gallery
│   ├── team_runtime.py           # Orchestrator (Roundtable, Chief, DAG, Per-Seat Sandbox, Hot-Swap, Failover)
│   ├── team_state_manager.py     # Atomic/debounced state store, 15 specialist templates & orphan-PID recovery
│   ├── context_engine.py         # 3-Zone KV-stable compiler, BlobStore masking, immutable snapshots & rollover
│   ├── memory_index.py           # Pure-stdlib bilingual (EN + CJK) SQLite FTS5 memory & AST Structural RepoMap
│   ├── worktree_manager.py       # Native Git Worktree parallel isolation, diff preview & safe --no-ff merge
│   ├── figure_qa.py              # Pure-stdlib Python/R runtime probe, TIFF/PNG/SVG/PDF/CSV QA & VLM critique loop
│   ├── research_verifier.py      # Lock-free SHA-256 hash-locked acceptance verifier & readonly_inputs guard
│   ├── openscience_playbooks.py  # 7 modular OpenScience core methodology playbooks & auto-matcher
│   ├── provenance_store.py       # Content-addressed Provenance DAG & Experiment Baseline-vs-Delta ledger
│   ├── version_checker.py        # GitHub VERSION.json + Releases API update checker for old-version popups
│   ├── agy_adapter.py            # Official Google Antigravity CLI stream-json adapter (3-tier sandbox + --add-dir)
│   ├── codex_adapter.py          # Official OpenAI Codex CLI JSONL adapter (3-tier sandbox + --add-dir + Win32 guards)
│   ├── project_discovery.py      # Cross-platform workspace & native OS folder picker
│   ├── team_cli.py               # CLI client & hidden zero-popup background daemon launcher
│   ├── codex_team_mcp.py         # MCP server ('codex-team') with default-sandbox sync & per-seat overrides
│   └── test_runtime_mechanisms.py# 34 automated security, context, FTS5, worktree, Figure QA & 3-tier sandbox tests
├── docs/
│   └── USER_GUIDE.md             # Detailed Bilingual (EN/ZH) User Guide
├── VERSION.json                  # Canonical version manifest (v5.6.0 / server_rev 21) for auto-update popups
├── Start-Roundtable.cmd          # Windows 1-click launcher
├── Start-Roundtable.sh           # macOS / Linux 1-click launcher
├── LICENSE                       # MIT License
└── README.md                     # Bilingual project overview & feature comparison
```

## 🔒 Privacy & Security Notice | 隐私与安全边界

- Binds strictly to `127.0.0.1` loopback interface for trusted single-user local workstations.
- Never reads, copies, or transmits CLI authentication tokens (`auth.json` / keychains); all model turns execute solely through the user's official `agy` and `codex` binaries under explicit user-chosen sandbox tiers (`read-only`, `workspace-write` + `--add-dir`, or 2-step opt-in `danger-full-access`).
- Runtime state (`prototype/team_state.json`) contains local workspace history and is excluded via `.gitignore` — never commit personal task snapshots to public repositories.
