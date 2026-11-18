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

2. **Genuine Inter-Agent Communication & `@Peer` Consultation**
   - Every task maintains a live **`chat_room`** shared across all participating agents and the human lead.
   - Any agent can emit a `team_consult` block mid-step to **`@mention` a teammate**, waking that teammate to verify facts in read-only mode and injecting the reply straight back into the caller's session.
   - The human lead can **interject live** at the bottom bar (`@All` or `@SpecificMember`) to steer discussion or wake idle specialists.

3. **Human-in-the-Loop UX & Local Workspace Integration**
   - **🌐 One-Click Bilingual UI (`EN / 中文`)**: Switch the entire web console between English and Chinese instantly in the top header.
   - **🔔 Auto-Popup Confirmation Modal**: When an agent poses structured choices (`team_questions`), a frosted-glass modal automatically pops up (with toggleable auto-popup & "View Context First" minimize support).
   - **📎 Real Local File & Line-Anchor Previewer**: Clicking any file link in agent answers (`file:///...`, `.docx`, `.csv`, `.md#L29-L35`, `.pdf`, images, or relative paths) previews the real content in-browser (including zero-dependency Word `.docx` text/table extraction and highlighted line anchors) and offers 1-click launch in native OS apps (Word/Excel/Explorer/Finder).
   - **🛡️ Resilience & Safety**: Session-scoped `--settings` read/write boundaries, write-scope conflict serialization, 240s idle-stream watchdog, transient network auto-retry, and 1-click partial-stream rescue (`accept_step_partial`).

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

2. **Agent 实时共享群聊与 `@队友` 提问互助**
   - 全员共享实时群聊上下文（`chat_room`），任意成员中途可通过 `team_consult` 自动唤醒队友协助核查细节并回传结果继续定稿；
   - 底部常驻 **`🎙️ 现场插话 / 点名`** 栏，负责人可随时向全体或 `@指定成员` 插话指导。

3. **中英双语切换、提问自动弹窗与真实文件链接打开**
   - **🌐 一键中英双语切换（`English / 中文`）**：右上角随时切换全界面中英文显示，不丢失任何输入草稿与任务状态。
   - **🔔 AI 提问自动弹窗确认**：成员提出选项问题时自动弹出居中确认窗（支持右上角开关、暂收起看上下文及随时重弹）。
   - **📎 回答内链接真实打开**：点击回答中的 `.docx` Word 稿件、`.csv` 数据表、`#L29-L35` 行号锚点、PDF、图片或外部网页链接，可直接在页内预览真实正文/表格/高亮代码行，或一键调起本机 Word / Excel / 资源管理器定位。

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
│   ├── serve_sidebar.py          # Loopback HTTP server & local file preview/open REST API (127.0.0.1:8765)
│   ├── sidebar_panel.html        # Bilingual (EN/ZH) anthropomorphic collaboration studio UI
│   ├── team_runtime.py           # v4 Orchestrator (Roundtable, Chief Dispatch, Custom DAG, @Peer Consult)
│   ├── team_state_manager.py     # Atomic state persistence & crash-recovery verification guard
│   ├── agy_adapter.py            # Official Google Antigravity CLI stream-json worker adapter
│   ├── codex_adapter.py          # Official OpenAI Codex CLI JSONL worker adapter + watchdog
│   ├── project_discovery.py      # Cross-platform workspace & folder browser
│   ├── team_cli.py               # CLI client & background daemon launcher
│   ├── codex_team_mcp.py         # MCP server ('codex-team') for IDE native integration
│   └── test_runtime_mechanisms.py# 10 automated lifecycle, concurrency & resilience unit tests
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
