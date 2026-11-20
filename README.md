# 🏛️ AGY Roundtable · 圆桌会议

**面向 Google Antigravity CLI 与 OpenAI Codex CLI 的跨厂商异构多智能体圆桌协作工坊**

**简体中文** · **[English Documentation (README_EN.md) →](README_EN.md)**

[![最新版本: v5.6.0](https://img.shields.io/badge/版本-v5.6.0-indigo.svg)](https://github.com/QUDAD-ENT/agy-roundtable/releases/tag/v5.6.0)
[![零第三方依赖](https://img.shields.io/badge/核心运行时-Python纯标准库-emerald.svg)](#核心差异化优势)
[![自动化测试: 34/34 通过](https://img.shields.io/badge/回归测试-34%2F34%20全通过-brightgreen.svg)](#快速启动)
[![开源协议: MIT](https://img.shields.io/badge/开源协议-MIT-slate.svg)](LICENSE)
[![关联项目: Agy Console](https://img.shields.io/badge/生态关联-dneo9527--ctrl%2Fagy--console-purple.svg)](https://github.com/dneo9527-ctrl/agy-console)

[核心差异化优势](#核心差异化优势) · [v5.6.0 最新升级](#v560-最新升级) · [核心能力架构](#核心能力架构) · [快速启动](#快速启动) · [项目关联](#项目关联与生态定位) · [目录结构](#目录结构)

> **拒绝让同一个模型“自己写代码、自己审自己”。**  
> **AGY Roundtable** 让来自不同厂商的顶尖模型（**Gemini 4**、**Claude 5.5**、**GPT-6.2**）围坐同一张圆桌——内置**首轮真盲评隔离**、**匿名互评 Borda 共识计票**、**用户自主三档沙箱与跨盘 `--add-dir` 目录授权**、**三区 KV 缓存长上下文压缩**、**纯标准库中英双语 SQLite FTS5 跨任务记忆**、**Git Worktree 并行写盘隔离**以及**顶刊级科研图双门控质检闭环**。  
> 核心运行时 **零第三方 pip 依赖**、**零 API Key 中转托管**，100% 本地直连你已安装并登录的官方 `agy` 与 `codex` CLI。

---

## 🏆 核心差异化优势

与「单一厂商内置 CLI 团队」以及「AutoGen / CrewAI / LangGraph 等代码编排框架」相比，**AGY Roundtable v5.6.0** 的核心优势对照：

| 对比维度 | 单一厂商 CLI 团队 | 传统多智能体代码框架 (AutoGen / CrewAI 等) | **🏛️ AGY Roundtable v5.6.0** |
| :--- | :--- | :--- | :--- |
| **跨厂商异构同席思辨** | 锁定单一厂商同源模型，易产生同质盲区 | 需自行配置多平台 API Key、计费与胶水代码 | **原生打通 Antigravity（Gemini 4 / Claude 5.5）+ Codex（GPT-6.2）** 同席圆桌，支持中途实时 `@队友` 核验 |
| **反群体迷思与防锚定** | 共享上下文串行跟帖，极易受首位发言锚定 | 自由聊天循环容易相互附和、“和稀泥” | **首轮真盲评物理隔离**（内存与磁盘双密封）+ **匿名席位掩码（席位 A/B/C）** + **Borda 共识计分牌** |
| **沙箱与跨盘权限自主控制** | 粗粒度全局参数，难以按角色混配读写权限 | 多为无沙箱裸跑或依赖笨重的 Docker 容器 | **三档可视化权限中心**：`只读` / `项目可写` + **`--add-dir` 跨盘目录授权（最多 8 个）** / **二次确认解锁 `完全访问`**（支持一键立即回收降级）+ **圆桌单席独立权限** |
| **长上下文治理与跨任务记忆** | 超长对话易爆窗口或粗暴截断丢细节 | 依赖外部向量数据库、Embedding 服务与繁琐配置 | **三区 KV 缓存稳定前缀** + 观测块外置折叠 + 不可变轮次状态卡 + 单次峰值水位校准无损换乘（`L0→L1→L2`）+ **纯标准库中英双语 SQLite FTS5 检索**与 **AST 代码结构图谱** |
| **多工位并行改码安全** | 多人同时写同一工作区易互相覆盖冲突 | 极少原生集成 Git 工作树隔离 | **可选原生 Git Worktree 分支隔离**、实时 Unified Diff 预览、脏工作区守卫与冲突安全 `--no-ff` 自动合并回滚 |
| **科研复算与顶刊出图质检** | 仅面向常规软件开发 | 无内置科研与图件核验能力 | **7 大模块化 OpenScience 科研协议**、纯标准库 **TIFF/PNG/SVG/PDF/CSV 防造假双门控**、**VLM 自动视觉审图自修闭环**、SHA-256 锁定验收脚本与溯源谱系树 |
| **高流畅度与调用高可用** | 长文渲染易卡顿，遇限流或卡死直接中断 | 需手工编写复杂重试与降级逻辑 | **线性 $O(N)$ 零 ReDoS Markdown 渲染**、`slim + 304 + gzip` 增量轮询（**-95.9% 流量**）、空闲零写盘、动态超时心跳 + **2 跳跨引擎自动容错接力**、**Windows 零命令行黑框与零 `Rterm` 崩溃弹窗** |
| **部署成本与更新体验** | 仅限终端命令行交互 | 依赖庞大的 Python/Node 环境栈 | **核心零第三方 pip 依赖**（纯 Python 标准库 + 单文件 Web 工坊）、双击即用、**内置 GitHub 新版本自动弹窗通知** |

### 📊 实测工程指标

- **轮询带宽压缩**：`5.22 MB` 全量状态 → **`215 KB`** 首次增量压缩包（`-95.9%`）→ 未变化时 **`0 B` / `1.09 ms`**（`HTTP 304 Not Modified`）。
- **Markdown 防卡死引擎**：针对 600 字符未闭合链接的极端输入，耗时从正则回溯卡死降至 **`0.1 ms`**（线性 $O(N)$ 括号扫描 + 180 条 LRU 缓存）。
- **空闲资源占用**：无活跃任务时后台调度器 **`0` 次/秒磁盘写入**、**`0%` 额外 CPU 波动**。
- **自动化回归测试**：内置 **`34 / 34` 项全链路自动化测试**（约 `29s` 跑完），覆盖沙箱信任根、三档解锁与一键回收、`--add-dir` 透传、单席权限、SQLite FTS5 双语检索、Git Worktree 合并、科研图 QA 及额度容错切换。

---

## ✨ v5.6.0 最新升级

1. **🛡️ 用户自主三档沙箱与目录权限中心**
   - 顶栏右侧新增实时权限胶囊（`🔒 只读` / `✏️ 可写` / 红色 `⚠️ 完全访问`），一键打开「沙箱与目录权限中心」弹窗：
     - **🔒 只读模式 (`read-only`)**：仅允许读取核查，严禁修改任何文件（支持一键全局锁死只读上限）。
     - **✏️ 项目目录可写 (`workspace-write`)**：日常写代码、科研画图与运行 R/Python 脚本推荐模式。
     - **⚠️ 完全访问 (`danger-full-access`)**：默认安全锁定（`allow_danger_full_access=false`），需在权限中心勾选二次风险确认后解锁；选中时任务栏自动展示醒目红色风险横幅，且随时可点击「立即重新锁定并回收」，瞬间将全局上限、默认权限与所有成员强制回退至 `workspace-write`。
2. **📁 跨盘 / 外部目录授权 (`--add-dir`)**
   - 无需放开整机无沙箱权限，即可直接粘贴或调用系统原生选目录弹窗（`/api/fs/pick_dir`）授权最多 **8 个**跨盘数据或代码文件夹（如 `H:\project 1\BioData`），自动透传给 Codex CLI（新会话 `--add-dir` / 续聊 `writable_roots`）与 Antigravity CLI。
3. **🪑 圆桌单席细粒度权限 + MCP 默认同步**
   - 在可写或完全访问圆桌中，可直接在每个参会席位胶囊上点选切换 `🔒 / ✏️ / ⚠️`（例如让审稿专家保持严格只读，仅授权统计/绘图专家写文件）。
   - MCP `team_dispatch_task` 默认自动跟随用户在面板中配置的默认沙箱权限（`default_task_sandbox`），并支持透传 `allow_member_writes`、`add_dirs_json` 与 `seat_sandboxes_json`。
4. **🪟 根治 Windows 下 `Rterm.exe` 崩溃弹窗与命令行黑框闪烁**
   - 服务进程树继承 Win32 `SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)`，彻底拦截 Windows 下 R 只读受限令牌触发的 `0xc0000005` 阻塞式崩溃弹窗，并在只读沙箱下自动引导使用 Python 复算（确需运行 R 脚本可切至 `workspace-write`）。
   - 全模块统一 `CREATE_NO_WINDOW + SW_HIDE`（禁用冲突的 `DETACHED_PROCESS`）与 `-c notify=[]`，彻底消除后台命令行黑框闪现。
5. **🔔 老版本自动检测与更新弹窗通知**
   - 老版本实例（对比 GitHub `VERSION.json` / Releases API）与未刷新的旧网页标签页（对比 `X-Server-Rev`）均会自动在顶栏点亮 **`新版 vX.Y.Z`** 提示并弹出更新说明窗，支持一键复制 `git pull` 更新命令。

---

## 🧩 核心能力架构

### 1. 四大拟人化协作阵型
- **🪑 群聊圆桌议事厅（`roundtable`）**：2~N 位跨引擎专家围坐圆桌——首轮真盲评并行立论 → 多轮共享群聊对抗质询、`@队友` 实时交叉核验与可选匿名 Borda 计票 → 主持人多维仲裁拍板交付。
- **🎖️ 总管自动点将台（`chief_dispatch`）**：由 1 位主帅先勘察当前工作区结构与花名册，按需抽调 `1–12` 名精兵动态生成并行/串行/审查 DAG 并终审签发，其余成员待命不陪跑。
- **🧱 多轨搭积木工坊（`custom_dag`）**：可视化自由编排最多 `10` 阶段 × `10` 并行工位，支持插入独立 Fail-Closed 审查门禁（最多 2 轮自动退回返修，超限转交人工裁决）。
- **⚡ 专家一对一专办（`single_member`）**：直连单名专家极速分析或改码，支持沿用原生 Session ID 多轮追问。

### 2. 长上下文压缩、双语 FTS5 记忆与并行隔离
- **🧠 三区 KV 缓存长上下文引擎（`context_engine.py`）**：稳定系统前缀提升缓存命中率；超长代码块/宽表自动外置落盘为 `BlobStore` 摘要指针；每轮结束自动封存不可变状态卡（优先保留最新轮次并维护历史锚点总账）；区分累计计费 Token 与单次真实上下文峰值，超水位自动无损换乘新会话（`L0 → L1 → L2`）。
- **🔎 纯标准库中英双语 SQLite FTS5 记忆与 AST 代码图谱（`memory_index.py`）**：内置英文词元 + 中文双字切分（CJK Bi-gram）BM25 全文检索引擎，自动跨任务召回历史结论与指标（盲评第 1 轮严格屏蔽以防锚定）；纯标准库解析 Python `ast` 及 JS/TS/HTML/R 核心符号与引用中心度生成结构图谱。
- **🌿 原生 Git Worktree 并行写盘隔离（`worktree_manager.py`）**：开启后每个可写工位在独立临时分支与工作树中运行，面板可直接查看文件变更 Diff，支持脏工作区保护与无冲突 `--no-ff` 安全合并。

### 3. OpenScience 科研方法论与顶刊科研图质检闭环
- **🔬 7 大可组合 OpenScience 科研协议（`openscience_playbooks.py`）**：涵盖统计规范与效能、实验设计与防伪重复、科研代码洁净与隐性验收、顶刊绘图与示意图、原子声明与文献溯源、自动科研实验循环、顶刊审稿与论文撰写，支持任务目标自动匹配或手动点选。
- **🎨 出版级科研图双门控 QA 与 VLM 自动审图闭环（`figure_qa.py`）**：实时探测本机 Python（`matplotlib`/`seaborn`/`scipy`/`statsmodels`）与 R（`ggplot2`/`patchwork`/`ComplexHeatmap`）环境；零第三方依赖解析二进制 TIFF 双轴真实物理 DPI（识破低分辨率伪造 600 DPI）、SVG/PNG/PDF 结构合规性与面板级 Source CSV/TSV 图数一致性（自动拦截组别标签互换及未校正 P 值混用）；支持多模态 VLM 自动视觉审图与最多 2 轮自动返修重绘，并可同步导出可编辑 `.drawio` 矢量流程图。
- **🔒 无锁哈希机械验收与内容寻址溯源谱系（`research_verifier.py`、`provenance_store.py`）**：在状态锁外安全执行 SHA-256 哈希锁定的验收脚本与只读原始数据防篡改校验，自动记录 `数据源 → 运行 → 产物 → 结论声明` 溯源 DAG 与实验基线对比榜。

### 4. 现场热换人、额度替补与双主题工作台
- **🔄 现场热换人与 2 跳额度容错接力**：全面适配 `Gemini 4 Neon / Carbon / Hydrogen`、`Claude Opus 5.5 / Sonnet 5.5`、`GPT-6.2-Astra / GPT-6.2-Sol`；运行途中点击席位胶囊即可立即换人接力或下轮换人，支持一键全员绑定同角色跨模型额度替补，遇 429 限流或长时间无响应自动触发最多 2 跳跨引擎级联切换。
- **🖼️ 纯白底板科研图画廊与本地真实文件预览**：深色 / 纸感浅色双主题一键切换，内置色觉障碍（Protan / Deutan / Tritan / 灰度）投稿预检滤镜、`SVG / PNG / PDF / TIFF` 同源切换、Before/After 并排对比，以及支持 60 列宽表 CSV/TSV、DOCX、PDF、源码行号锚点的浏览器内真实文件预览器。

---

## 🚀 快速启动

### 1. 环境准备
- 本机已安装 **Python `3.9+`**（Web 工作台与核心调度服务**无需安装任何第三方 pip 包**；仅在需要将 `codex_team_mcp.py` 挂载为 IDE MCP 工具时可选安装 `mcp` 包）。
- 本机已安装并登录至少一个官方 CLI：[Google Antigravity CLI (`agy`)](https://antigravity.google/docs/install?tab=cli) 或 OpenAI Codex CLI (`codex`)。

### 2. 一键启动 Web 协作工坊（`http://127.0.0.1:8765/`）
- **Windows**：直接双击仓库根目录的 `打开团队协作面板.cmd`（或 `Start-Roundtable.cmd`），后台无弹窗静默拉起服务并自动打开浏览器页面。
- **macOS / Linux**：
  ```bash
  chmod +x Start-Roundtable.sh && ./Start-Roundtable.sh
  ```
- **命令行直启**：
  ```bash
  python prototype/serve_sidebar.py
  ```

### 3. 挂载至 IDE 原生 MCP（可选，支持 Antigravity / Claude / Codex MCP）
```json
{
  "mcpServers": {
    "codex-team": {
      "command": "python",
      "args": ["/你的绝对路径/agy-roundtable/prototype/codex_team_mcp.py"]
    }
  }
}
```

### 4. 运行全量自动化回归测试（共 34 项）
```bash
python prototype/test_runtime_mechanisms.py
```

---

## 🔗 项目关联与生态定位

**AGY Roundtable（圆桌会议）** 是与 **[dneo9527-ctrl/agy-console](https://github.com/dneo9527-ctrl/agy-console)** 关联互补的双端多智能体团队协作工坊：

- **[Agy Console (`dneo9527-ctrl/agy-console`)](https://github.com/dneo9527-ctrl/agy-console)** 聚焦单窗口固定会话绑定、队列任务进度、模型额度候补切换与跨平台安装包分发；
- **AGY Roundtable（本项目）** 聚焦 **跨厂商多智能体编排、盲评对抗思辨与科研级质量核验**：让多位来自 Antigravity 与 Codex 的异构模型专家同席圆桌、中途互相 `@队友` 核查、主帅动态点将、多轨流水线并行改码并完成机械化验收。

> **合规声明**：本项目为独立社区开源工具，非 Google、OpenAI 或 Anthropic 官方产品。所有模型调用均通过用户本机已安装并自行登录的官方 `agy` 与 `codex` CLI 执行，绝不读取、复制或上传任何账号凭证。

---

## 📁 目录结构

```text
agy-roundtable/
├── prototype/
│   ├── serve_sidebar.py          # 本地回环 HTTP 服务、slim+304+gzip 增量接口、CSRF/路径守卫与系统目录选择 (rev 21)
│   ├── sidebar_panel.html        # 单文件中英双语协作工坊前端、三档沙箱权限中心与纯白底板科研图画廊
│   ├── team_runtime.py           # 核心编排调度器（圆桌、点将、流水线、单席沙箱、热换人、2 跳容错切换）
│   ├── team_state_manager.py     # 原子/防抖状态存储、15 位专家人设模板与孤儿进程安全恢复
│   ├── context_engine.py         # 三区 KV 缓存编译、BlobStore 观测折叠、不可变状态卡与无损换乘
│   ├── memory_index.py           # 纯标准库中英双语 SQLite FTS5 全文记忆库与 AST 代码结构图谱
│   ├── worktree_manager.py       # 原生 Git Worktree 并行写盘隔离、Diff 预览与安全合并回滚
│   ├── figure_qa.py              # 纯标准库 Python/R 环境探测、TIFF/PNG/SVG/PDF/CSV 质检与 VLM 审图闭环
│   ├── research_verifier.py      # 无锁 SHA-256 锁定验收脚本执行器与只读原始数据防篡改守卫
│   ├── openscience_playbooks.py  # 7 大模块化 OpenScience 科研方法论协议与自动匹配器
│   ├── provenance_store.py       # 内容寻址溯源谱系 DAG 与实验基线 Delta 对比账本
│   ├── version_checker.py        # GitHub VERSION.json 与 Releases API 版本检测器（驱动老版本更新弹窗）
│   ├── agy_adapter.py            # 官方 Google Antigravity CLI 流式适配器（支持三档沙箱与 --add-dir）
│   ├── codex_adapter.py          # 官方 OpenAI Codex CLI 适配器（支持三档沙箱、--add-dir 与 Win32 防弹窗）
│   ├── project_discovery.py      # 跨平台工作区自动发现与原生文件夹选择器
│   ├── team_cli.py               # 命令行客户端与后台零弹窗守护启动器
│   ├── codex_team_mcp.py         # MCP 服务端 ('codex-team')，支持默认沙箱自动同步与单席权限覆盖
│   └── test_runtime_mechanisms.py# 34 项全链路安全、长上下文、FTS5、Worktree、科研图 QA 与三档沙箱测试
├── docs/
│   └── USER_GUIDE.md             # 详细使用说明文档
├── VERSION.json                  # 版本清单文件（v5.6.0 / server_rev 21，供旧版本在线检测更新）
├── 打开团队协作面板.cmd          # Windows 中文一键启动脚本
├── Start-Roundtable.cmd          # Windows 一键启动脚本
├── Start-Roundtable.sh           # macOS / Linux 一键启动脚本
├── LICENSE                       # MIT 开源许可证
├── README.md                     # 中文主页介绍（默认展示）
└── README_EN.md                  # 独立英文项目介绍 (English Documentation)
```

## 🔒 隐私与安全边界

- 服务严格仅绑定本机回环地址 `127.0.0.1`，拒绝非本机 `Host` 及跨站 `Origin` 写请求。
- 绝不读取、拷贝或外传任何 CLI 登录态文件（如 `auth.json` 或系统密钥链）；所有模型调用均在用户显式选定的沙箱边界（`read-only`、`workspace-write` + `--add-dir` 或二次确认解锁的 `danger-full-access`）内运行。
- 本地运行状态文件 `prototype/team_state.json` 已加入 `.gitignore`，不会随代码提交泄露个人课题记录。
