# AGY Roundtable — English User Guide

**[← 中文使用指南 (USER_GUIDE.md)](USER_GUIDE.md)** · **English**

> Associated Companion Project: **[dneo9527-ctrl/agy-console](https://github.com/dneo9527-ctrl/agy-console)**

---

## 1. Starting the Studio

- **Windows**: Double-click `Start-Roundtable.cmd` (or `打开团队协作面板.cmd`). You can create a Desktop shortcut for 1-click daily access; it launches the background daemon silently without console popups and opens `http://127.0.0.1:8765/`.
- **macOS / Linux**: Run `./Start-Roundtable.sh`.
- **Auto-Start via IDE MCP**: Once `codex-team` is added to `mcp_config.json`, launching Antigravity automatically starts `127.0.0.1:8765` in the background.

---

## 2. Switching Language & Theme

- **Language (`EN / 中文`)**: Click **`EN` / `中文`** in the top-right bar to switch the Studio UI language instantaneously without losing drafts or active tasks.
- **Theme (`Dark / Paper Light`)**: Click the sun/moon icon to switch between Dark and Paper Light reading modes.

---

## 3. Four Collaboration Modes

1. **🪑 Roundtable Chamber (`roundtable`)**
   - Select 2+ heterogeneous specialists, pick 1 Host, and choose 1–3 rounds. Sealed Blind Round 1 runs all seats simultaneously without anchoring bias, followed by multi-round `@mention` cross-critique (with optional anonymous Borda voting) and final Host synthesis.
2. **🎖️ Chief Dispatch Bridge (`chief_dispatch`)**
   - Pick 1 Chief Commander to inspect the workspace and dynamically recruit `1–12` needed specialists into a parallel/serial/review DAG.
3. **🧱 Multi-Track Pipeline (`custom_dag`)**
   - Build Stage `1..N` columns with parallel workstations and independent fail-closed review gates.
4. **⚡ 1-on-1 Specialist Desk (`single_member`)**
   - Direct 1-on-1 execution with any Codex or Antigravity model, supporting native session continuation.

---

## 4. 3-Tier Sandbox & Extra Directory Control (v5.6.0)

Click the live permission pill (`🔒 RO` / `✏️ RW` / `⚠️ Full`) in the top-right bar to open the **Sandbox & Directory Permission Center**:

- **🔒 Read-Only (`read-only`)**: Strictly read-only inspection and peer review; zero file edits allowed.
- **✏️ Workspace-Write (`workspace-write`)**: Recommended daily mode for coding, plotting, and running R/Python scripts. Under **Extra Authorized Directories (`--add-dir`)**, paste or pick up to 8 external cross-drive folders without opening full-machine access.
- **⚠️ Full Access (`danger-full-access`)**: Fail-closed locked by default; unlock via the 2-step risk confirmation dialog when needed, and click **Relock & Clamp Now** anytime to instantly revert all settings back to `workspace-write`.
- **Per-Seat Roundtable Overrides**: In writable Roundtables, click the `🔒 / ✏️ / ⚠️` badge on each selected participant chip to customize individual seat permissions.
