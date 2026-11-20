# 🏛️ AGY Roundtable

**Heterogeneous Multi-Agent Roundtable Studio for Google Antigravity CLI & OpenAI Codex CLI**

**[← 简体中文主页 (README.md)](README.md)** · **English**

[![Release: v5.6.0](https://img.shields.io/badge/Release-v5.6.0-indigo.svg)](https://github.com/QUDAD-ENT/agy-roundtable/releases/tag/v5.6.0)
[![Zero Pip Deps](https://img.shields.io/badge/Core-Pure%20Python%20Stdlib-emerald.svg)](#why-agy-roundtable)
[![Tests: 34/34 Passing](https://img.shields.io/badge/Tests-34%2F34%20Passing-brightgreen.svg)](#quick-start)
[![License: MIT](https://img.shields.io/badge/License-MIT-slate.svg)](LICENSE)
[![Companion: Agy Console](https://img.shields.io/badge/Companion-dneo9527--ctrl%2Fagy--console-purple.svg)](https://github.com/dneo9527-ctrl/agy-console)

[Why AGY Roundtable?](#why-agy-roundtable) · [What's New in v5.6.0](#whats-new-in-v560) · [Core Architecture](#core-architecture) · [Quick Start](#quick-start) · [Ecosystem](#ecosystem--project-association) · [Repository Structure](#repository-structure)

> **Stop letting a single model grade its own homework.**  
> **AGY Roundtable** seats heterogeneous frontier models (**Gemini 4**, **Claude 5.5**, and **GPT-6.2**) at a single shared Roundtable — featuring **sealed Blind Round 1**, **anonymous Borda consensus voting**, **user-controlled 3-tier sandboxes + cross-drive `--add-dir` grants**, **3-zone KV-cache long-context compression**, **zero-dependency bilingual SQLite FTS5 cross-task memory**, **native Git Worktree parallel isolation**, and **publication-grade scientific Figure QA**.  
> Built on **100% Python standard library** with **zero third-party pip dependencies** and **zero API-key proxying**, executing strictly through your locally signed-in official `agy` and `codex` CLIs.

---

<a id="why-agy-roundtable"></a>
## 🏆 Why AGY Roundtable?

How **AGY Roundtable v5.6.0** compares against single-vendor CLI agent teams and code-heavy multi-agent frameworks:

| Dimension | Single-Vendor CLI Teams | Generic Code Frameworks (AutoGen / CrewAI / LangGraph) | **🏛️ AGY Roundtable v5.6.0** |
| :--- | :--- | :--- | :--- |
| **Cross-Vendor Heterogeneous Debate** | Locked to one vendor's model family & blind spots | Requires API keys, separate billing & custom Python glue | **Native Antigravity (`Gemini 4` / `Claude 5.5`) + Codex (`GPT-6.2`)** in one live Roundtable with mid-step `@mention` verification |
| **Anti-Groupthink Governance** | Shared thread prone to early anchoring bias | Unstructured chat loops prone to sycophancy | **Sealed Blind Round 1** (memory + disk isolation) + **Anonymous Seat Masking (`Seat A/B/C`)** + **Deterministic Borda Voting** |
| **User-Controlled Sandbox & Cross-Drive Access** | Coarse global CLI flags; hard to mix per-role access | Often unsandboxed host execution or heavy Docker setups | **3-Tier Permission Center**: `read-only` / `workspace-write` + **`--add-dir` (up to 8 external folders)** / **2-step opt-in `danger-full-access`** (1-click instant relock) + **Per-Seat Roundtable overrides** |
| **Long-Context & Cross-Task Memory** | Context window overflow or lossy truncation | Requires external vector DBs & embedding APIs | **3-Zone KV-Cache Stable Prefix** + observation masking + immutable round snapshots + peak-occupancy auto rollover (`L0→L1→L2`) + **Pure-Stdlib Bilingual SQLite FTS5** & **AST RepoMap** |
| **Parallel File-Write Safety** | Concurrent agents overwrite each other in one tree | Rarely integrated with native Git worktrees | **Opt-in Native Git Worktree Isolation** per writable step, live unified diff viewer, dirty-repo guard & conflict-safe `--no-ff` merge with auto-rollback |
| **Scientific Rigor & Publication Figure QA** | General software engineering only | Not built-in | **7 Modular OpenScience Playbooks**, pure-stdlib **TIFF/PNG/SVG/PDF/CSV anti-fabrication gate**, **automated VLM Visual Critique & Self-Repair loop**, SHA-256 locked verifiers & Provenance DAG |
| **Fluency & Calling Resilience** | Long Markdown freezes UI; 429/stalls abort runs | Requires custom retry state machines | **Linear $O(N)$ zero-ReDoS Markdown**, `slim + 304 + gzip` polling (**-95.9% bandwidth**), `0%` idle CPU/disk writes, dynamic idle watchdog + **2-hop cross-engine failover**, **zero Windows CMD / `Rterm` crash popups** |
| **Setup & Maintenance** | Terminal-only UX | Heavy Python/Node dependency trees | **Zero third-party pip packages** (pure Python stdlib + single-file Web Studio), 1-click launch, **built-in GitHub auto-update notification modal** |

### 📊 Verified Engineering Benchmarks

- **HTTP State Polling**: `5.22 MB` full state → **`215 KB`** cold slim gzip (`-95.9%`) → **`0 B` / `1.09 ms`** when unchanged via `HTTP 304 Not Modified` (`since_rev` + `ETag`).
- **Zero-ReDoS Markdown Parser**: 600-character unclosed-link adversarial input reduced from catastrophic backtracking freeze → **`0.1 ms`** linear $O(N)$ scanner + 180-entry LRU cache.
- **Idle Footprint**: **`0` disk writes/sec** and **`0%` background CPU delta** when idle.
- **Automated Regression Suite**: **`34 / 34`** end-to-end tests passing (`~29s`) covering P0 sandbox trust root, 3-tier opt-in & instant relock, `--add-dir` propagation, per-seat overrides, SQLite FTS5 search, Git Worktree merge, Figure QA, and quota failover.

---

<a id="whats-new-in-v560"></a>
## ✨ What's New in v5.6.0

1. **🛡️ User-Controlled 3-Tier Sandbox & Directory Permission Center**
   - Live topbar status pill (`🔒 RO` / `✏️ RW` / `⚠️ Full`) opens the dedicated **Sandbox & Directory Permission Center**:
     - **🔒 Read-Only (`read-only`)**: Strict read-only inspection & peer review (with optional global ceiling lock).
     - **✏️ Workspace-Write (`workspace-write`)**: Recommended daily mode for coding, plotting, and running R/Python scripts.
     - **⚠️ Full Access (`danger-full-access`)**: **Fail-closed locked by default** (`allow_danger_full_access=false`). Unlocked only via an explicit 2-step risk acknowledgment dialog; displays a prominent red warning banner when selected, and supports **1-click Relock & Clamp** (immediately clamping global ceiling, default task mode, and all members back to `workspace-write`).
2. **📁 Cross-Drive / External Authorized Directories (`--add-dir`)**
   - Grant up to **8** external data or code directories (paste path or use the native OS Folder Picker via `/api/fs/pick_dir`) under `workspace-write` — passed directly to Codex (`--add-dir` / `writable_roots`) and Antigravity (`--add-dir`) without exposing full-machine access.
3. **🪑 Per-Seat Roundtable Permissions & MCP Default Sync**
   - In any writable Roundtable, click the `🔒 / ✏️ / ⚠️` badge on each participant chip to assign per-seat overrides (`seat_sandboxes`) — e.g., keep reviewers `read-only` while allowing statistical/plotting specialists to write files.
   - MCP `team_dispatch_task` now defaults `sandbox=None` to inherit your configured `default_task_sandbox`, and supports `allow_member_writes`, `add_dirs_json`, and `seat_sandboxes_json`.
4. **🪟 Zero Windows `Rterm.exe` Crash Modals & Zero Console Flashes**
   - Inherited Win32 `SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)` across the server process tree eliminates blocking `Rterm.exe / Rscript.exe 0xc0000005` crash modals on Windows; read-only sandbox guardrails steer quick calculations to Python while R runs cleanly in `workspace-write`.
   - Unified `CREATE_NO_WINDOW + SW_HIDE` (without `DETACHED_PROCESS`) + `-c notify=[]` eliminates stray black CMD console popups.
5. **🔔 Automatic Update Popups for Older Versions**
   - Older installations (via GitHub `VERSION.json` / Releases API) and cached browser tabs (via `X-Server-Rev`) automatically display a `New vX.Y.Z` topbar badge and release changelog modal with 1-click `git pull` copy.

---

<a id="core-architecture"></a>
## 🧩 Core Architecture

### 1. Four Anthropomorphic Collaboration Topologies
- **🪑 Roundtable Chamber (`roundtable`)**: Seat 2–N heterogeneous specialists + 1 Host. Sealed Blind Round 1 independent proposals → multi-round adversarial cross-critique with `@mention` peer verification & optional anonymous Borda voting → Host multi-dimensional synthesis.
- **🎖️ Chief Dispatch Bridge (`chief_dispatch`)**: Assign 1 Chief Commander to inspect the workspace & roster, dynamically recruit `1–12` needed specialists into a parallel/serial/review DAG, and sign off.
- **🧱 Multi-Track Pipeline Studio (`custom_dag`)**: Stack up to `10` stages × `10` parallel workstations with independent fail-closed review gates (bounded 2-round auto-repair before human ruling).
- **⚡ 1-on-1 Specialist Desk (`single_member`)**: Direct low-latency specialist execution with native session continuation.

### 2. Long-Context Compression, Bilingual FTS5 Memory & Parallel Isolation
- **🧠 3-Zone KV-Cache Context Engine (`context_engine.py`)**: Prefix-stable compilation, externalized observation masking (`BlobStore`), newest-first immutable round snapshots + Pinned Historical Anchor Ledger, dual billing vs single-call peak occupancy calibration, and lossless session rollover (`L0 → L1 → L2`).
- **🔎 Pure-Stdlib Bilingual SQLite FTS5 Memory & AST RepoMap (`memory_index.py`)**: Zero-dependency English + CJK bi-gram BM25 full-text memory across tasks/snapshots/blobs (strictly skipped during Blind Round 1) + AST Structural RepoMap (Python `ast` + JS/TS/HTML/R + import centrality).
- **🌿 Native Git Worktree Parallel Isolation (`worktree_manager.py`)**: Isolated branch/worktree per writable step, live unified diff viewer, dirty-repo guard, and conflict-safe `--no-ff` merge with auto-rollback.

### 3. OpenScience Research & Publication Figure QA
- **🔬 7 Modular OpenScience Playbooks (`openscience_playbooks.py`)**: Statistical Rigor, Experimental Design, Code Hygiene, Publication Figures, Atomic Citations, Autoresearch Loop, and Peer Review.
- **🎨 Dual-Gate Publication Figure QA + VLM Critique Loop (`figure_qa.py`)**: Live Python/R plotting probe, pure-stdlib binary TIFF IFD dual-axis DPI & physical-mm anti-spoofing, PNG/SVG/PDF structural checks, Source-CSV semantic verification (catching swapped group labels & raw-vs-adjusted P misuse), automated multimodal VLM visual critique (`PASS | REVISE`) & bounded self-repair (max 2 rounds), plus optional `.drawio` export.
- **🔒 Lock-Free Hash-Locked Verification & Provenance DAG (`research_verifier.py`, `provenance_store.py`)**: SHA-256 locked acceptance scripts (`shell=False`), `readonly_inputs` tamper guard, content-addressed `source → run → artifact → claim` DAG, and `experiment_run` baseline-vs-delta board.

### 4. Live Hot-Swap, Quota Failover & Dual-Theme Studio
- **🔄 Live Seat Hot-Swap & 2-Hop Quota/Stall Failover**: Calibrated for `Gemini 4 Neon / Carbon / Hydrogen`, `Claude Opus 5.5 / Sonnet 5.5`, and `GPT-6.2-Astra / Sol`. Swap specialists mid-turn or next round, auto-bind quota backups, and cascade up to 2 hops across engines on 429 / idle timeouts / read-only shell blocks.
- **🖼️ Pure-White Figure Proofing Gallery & Local File Previewer**: Dark + Paper Light themes, CVD color-blindness proofing filters (Protan / Deutan / Tritan / Grayscale), `SVG / PNG / PDF / TIFF` format switcher, side-by-side compare, and in-browser preview for wide CSV/TSV (60 cols × 100 rows), DOCX, PDF, and line-anchored source code.

---

<a id="quick-start"></a>
## 🚀 Quick Start

### 1. Prerequisites
- **Python `3.9+`** (**zero third-party pip packages required** for the web Studio & HTTP runtime; `mcp` package optional only if mounting `codex_team_mcp.py`).
- At least one official CLI installed & signed in: [Google Antigravity CLI (`agy`)](https://antigravity.google/docs/install?tab=cli) and/or OpenAI Codex CLI (`codex`).

### 2. Launch Web Studio (`http://127.0.0.1:8765/`)
- **Windows**: Double-click `Start-Roundtable.cmd` (or `打开团队协作面板.cmd`).
- **macOS / Linux**:
  ```bash
  chmod +x Start-Roundtable.sh && ./Start-Roundtable.sh
  ```
- **Direct Python**:
  ```bash
  python prototype/serve_sidebar.py
  ```

### 3. Optional IDE MCP Integration (Antigravity / Claude / Codex MCP)
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

### 4. Run Automated Regression Suite (`34` tests)
```bash
python prototype/test_runtime_mechanisms.py
```

---

<a id="ecosystem--project-association"></a>
## 🔗 Ecosystem & Project Association

**AGY Roundtable (`agy-roundtable`)** is an independent companion collaboration studio associated with **[dneo9527-ctrl/agy-console](https://github.com/dneo9527-ctrl/agy-console)**:

- **[Agy Console (`dneo9527-ctrl/agy-console`)](https://github.com/dneo9527-ctrl/agy-console)** focuses on persistent per-window CLI binding, task queues, model fallback/quota failover, and cross-platform update packaging.
- **AGY Roundtable (`agy-roundtable`)** builds the **heterogeneous multi-agent orchestration & scientific verification layer** on top of official `agy` and `codex` CLIs — seating multiple frontier models at a shared Roundtable with blind peer review, `@mention` consultations, Chief Dispatch, multi-stage DAGs, Git Worktree isolation, and publication Figure QA.

> **Disclaimer**: Independent community open-source tool; not an official product of Google, OpenAI, or Anthropic. Executes solely through user-installed and user-authenticated official `agy` and/or `codex` CLIs without reading or transmitting account credentials.

---

<a id="repository-structure"></a>
## 📁 Repository Structure

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
│   └── USER_GUIDE.md             # User Guide
├── VERSION.json                  # Canonical version manifest (v5.6.0 / server_rev 21) for auto-update popups
├── Start-Roundtable.cmd          # Windows 1-click launcher
├── Start-Roundtable.sh           # macOS / Linux 1-click launcher
├── LICENSE                       # MIT License
├── README.md                     # Chinese Documentation (Default)
└── README_EN.md                  # English Documentation
```

## 🔒 Privacy & Security Notice

- Binds strictly to `127.0.0.1` loopback interface and rejects non-loopback `Host` or cross-site `Origin` POST requests.
- Never reads, copies, or transmits CLI authentication files (`auth.json` / keychains); all model turns execute solely through the user's official `agy` and `codex` binaries under explicit user-chosen sandbox tiers (`read-only`, `workspace-write` + `--add-dir`, or 2-step opt-in `danger-full-access`).
- Local runtime state (`prototype/team_state.json`) is excluded via `.gitignore` so personal workspace history is never committed.
