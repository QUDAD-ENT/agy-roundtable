# AGY Roundtable · 圆桌会议 — Bilingual User Guide / 中英双语使用说明

> Associated Companion Project / 关联生态项目: **[dneo9527-ctrl/agy-console](https://github.com/dneo9527-ctrl/agy-console)**

---

## 1. Starting the Studio / 日常开启方式

- **Windows**: Double-click `Start-Roundtable.cmd` (or `打开团队协作面板.cmd`). You can create a Desktop shortcut for 1-click daily access.  
  **Windows 用户**：双击 `Start-Roundtable.cmd` 或 `打开团队协作面板.cmd`（可右键“发送到 -> 桌面快捷方式”），自动检查并启动后台服务后打开 `http://127.0.0.1:8765/`。
- **macOS / Linux**: Run `./Start-Roundtable.sh`.  
  **macOS / Linux 用户**：在终端运行 `./Start-Roundtable.sh`。
- **Inside Antigravity IDE (Auto-Start via MCP)**: Once `codex-team` is added to `mcp_config.json`, launching Antigravity automatically starts `127.0.0.1:8765` in the background.  
  **Antigravity 内置自动随启**：挂载 `codex-team` MCP 后，每次打开 Antigravity 后台服务自动随启，直接访问书签 `http://127.0.0.1:8765/` 即可。

---

## 2. Switching Language (`EN / 中文`) / 一键中英双语切换

- Click **`🌐 English` / `🌐 中文`** in the top-right toolbar to switch the entire web interface between English and Chinese instantaneously without losing drafts or active tasks.  
  点击右上角 **`🌐 English` / `🌐 中文`** 按钮即可实时切换全界面语言，草稿与运行状态零丢失。

---

## 3. Four Collaboration Modes / 四种自由组织方式

1. **🪑 Roundtable Chamber / 群聊圆桌议事厅 (`roundtable`)**
   - Select 2+ specialists around the oval table, pick 1 Host, and choose 1–3 rounds. By default, **`⚡ Instant Parallel Round 1`** starts all participants simultaneously without waiting on an opening turn.  
     点击头像芯片勾选 2 位以上专家入席，默认开启“首轮直接并行开跑”，多轮阅读 8,000 字无损群聊互相 `@点名` 质询，最后由主持人汇总拍板。
2. **🎖️ Chief Dispatch Bridge / 总管自动点将台 (`chief_dispatch`)**
   - Pick 1 Chief Commander to inspect the project and dynamically dispatch only the needed specialists.  
     选定 1 位坐镇主帅勘察项目与花名册，动态点兵编排串行/并行/审查并终审签发。
3. **🧱 Multi-Track Pipeline / 多轨搭积木工坊 (`custom_dag`)**
   - Build Stage `1..N` columns with parallel workstations and independent review gates.  
     自由增删阶段与同阶段并行工位，支持一键载入 3 套预设积木模板。
4. **⚡ 1-on-1 Specialist Desk / 专家一对一专办 (`single_member`)**
   - Direct 1-on-1 execution with any Codex or Antigravity model.  
     面对面直连单名专家极速交付。

---

## 4. Interactive Features / 核心交互亮点

- **Live `@Peer` Consultation & Human Interjection / 实时共享群聊与插话点名**: Agents automatically `@consult` peers when needed; use the bottom bar to interject or `@wake` any specialist.  
  Agent 执行中途可自动发起 `@队友` 提问核查；你也可在底部随时 `@全体` 或点名唤醒任意成员。
- **Auto-Popup Question Confirmation / AI 提问自动弹窗确认**: Toggle **`🔔 Auto-Prompt Modal: ON/OFF`** in the header; click **`👁️ View Context First`** to inspect chat history before confirming.  
  顶部可开关提问自动弹窗，支持先收起翻看群聊上下文再重弹确认。
- **Real Local File Link Opener / 回答内链接真实打开**: Click any `📎` link (`.docx`, `.csv`, `#L29-L35`, `.pdf`, images) to preview real content in-page or open directly in Word / Excel / Explorer.  
  点击回答中的任意文件链接即可在页内直读 Word 正文表格、CSV 数据表或高亮代码行，并支持一键调起本机默认软件打开。
