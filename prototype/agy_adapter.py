"""
Official Antigravity CLI ('agy.exe') Worker Adapter.
Supports user-initiated execution on the local collaboration panel using official agy CLI 1.5.10+:
- Discovers real available Antigravity models & reasoning effort tiers via 'agy models'
- Executes non-interactive turns via 'agy -p --output-format stream-json'
- Streams 'init', 'step_update' (live text_delta), and 'result' events
- Resumes conversations via '--conversation <id>'
- Auto-initializes project association via '--new-project' when running in an unbound folder
- Supports confirmed Windows process-tree cancellation (abort)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from codex_adapter import CREATE_NO_WINDOW, CodexExecutionResult, _safe_env, get_runtime_dirs, is_transient_network_error, kill_process_tree


EFFORT_SUFFIX_RE = re.compile(r"^(.*)-(low|medium|high|xhigh|max)$")
EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "default": 5}


class AgyAdapter:
    def __init__(
        self,
        agy_bin: Optional[str] = None,
        runner_override: Optional[Callable[..., CodexExecutionResult]] = None,
    ):
        candidates = [
            agy_bin,
            os.environ.get("AGY_BIN"),
            r"D:\gemini\tooling\agy\bin\agy.exe" if os.name == "nt" else None,
            os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe") if os.name == "nt" else None,
            os.path.expanduser("~/.local/bin/agy"),
            shutil.which("agy.exe"),
            shutil.which("agy"),
        ]
        self.agy_bin = next((c for c in candidates if c and os.path.exists(c)), "agy")
        _, cache_dir = get_runtime_dirs()
        self.cache_file = os.path.join(cache_dir, "agy_models_cache.json")
        self.runner_override = runner_override
        self._lock = threading.Lock()
        self._active_procs: Dict[str, Tuple[subprocess.Popen, Dict[str, Any]]] = {}
        self._cached_caps: Optional[Dict[str, Any]] = None

    def discover_capabilities(self, refresh_cli: bool = False) -> Dict[str, Any]:
        if not refresh_cli and self._cached_caps is not None:
            return self._cached_caps
        if not refresh_cli and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    disk_caps = json.load(f)
                if disk_caps.get("models") and (time.time() - disk_caps.get("cli", {}).get("probed_at", 0) < 7200):
                    self._cached_caps = disk_caps
                    return self._cached_caps
            except Exception:
                pass

        installed = os.path.exists(self.agy_bin)
        cli_version = None
        raw_variants: List[Tuple[str, str]] = []
        logged_in = False
        err_note = None

        if installed:
            try:
                pv = subprocess.run(
                    [self.agy_bin, "--version"],
                    capture_output=True,
                    text=True,
                    env=_safe_env(),
                    creationflags=CREATE_NO_WINDOW,
                    timeout=8,
                )
                cli_version = f"agy {(pv.stdout or pv.stderr).strip()}"
            except Exception as exc:
                cli_version = f"error: {exc}"

            try:
                pm = subprocess.run(
                    [self.agy_bin, "models"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=_safe_env(),
                    creationflags=CREATE_NO_WINDOW,
                    timeout=18,
                )
                if pm.returncode == 0 and pm.stdout.strip():
                    logged_in = True
                    for line in pm.stdout.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split("\t", 1)
                        slug = parts[0].strip()
                        disp = parts[1].strip() if len(parts) > 1 else slug
                        raw_variants.append((slug, disp))
                else:
                    err_note = (pm.stderr or pm.stdout).strip()
            except Exception as exc:
                err_note = str(exc)

        # Group variants into clean (base_model, supported_efforts, variant_map)
        grouped: Dict[str, Dict[str, Any]] = {}
        for full_slug, disp in raw_variants:
            m = EFFORT_SUFFIX_RE.match(full_slug)
            if m:
                base_slug, eff = m.group(1), m.group(2)
                base_disp = re.sub(r"\s*\((Low|Medium|High|XHigh|Max)\)\s*$", "", disp, flags=re.I).strip()
            else:
                base_slug, eff = full_slug, "default"
                base_disp = disp

            entry = grouped.setdefault(
                base_slug,
                {
                    "slug": base_slug,
                    "display_name": base_disp,
                    "description": "Google Antigravity 官方 CLI 可用模型",
                    "supported_reasoning_efforts": [],
                    "variants": {},
                },
            )
            if eff not in entry["supported_reasoning_efforts"]:
                entry["supported_reasoning_efforts"].append(eff)
            entry["variants"][eff] = full_slug

        models_list: List[Dict[str, Any]] = []
        for entry in grouped.values():
            effs = sorted(entry["supported_reasoning_efforts"], key=lambda x: EFFORT_ORDER.get(x, 99))
            entry["supported_reasoning_efforts"] = effs
            entry["default_reasoning_effort"] = "medium" if "medium" in effs else effs[0]
            models_list.append(entry)

        self._cached_caps = {
            "cli": {
                "installed": installed,
                "binary": self.agy_bin,
                "cli_version": cli_version,
                "logged_in": logged_in,
                "login_status": "Logged in via Antigravity account" if logged_in else (err_note or "Not logged in"),
                "probed_at": time.time(),
            },
            "models": models_list,
        }
        if models_list:
            try:
                os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
                with open(self.cache_file, "w", encoding="utf-8") as f:
                    json.dump(self._cached_caps, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return self._cached_caps

    def resolve_full_model_slug(self, model: Optional[str], reasoning_effort: Optional[str]) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        caps = self.discover_capabilities(refresh_cli=False)
        if not caps["cli"]["installed"]:
            return False, "本机未找到官方 agy.exe", None, {}
        models = caps["models"]
        if not models:
            # Pass through if catalog empty
            return True, None, model, {
                "client": "antigravity",
                "model": model or "client_default",
                "reasoning_effort": reasoning_effort or "default",
                "verified_against": "agy_cli_passthrough",
            }

        by_base = {m["slug"]: m for m in models}
        # Also allow passing full variant slug directly (e.g., 'gemini-4-carbon-high')
        for m in models:
            for eff, full_s in m["variants"].items():
                if model == full_s:
                    return True, None, full_s, {
                        "client": "antigravity",
                        "model": m["slug"],
                        "full_model_slug": full_s,
                        "reasoning_effort": eff,
                        "verified_against": "agy_models",
                    }

        if not model or model in ("client_default", "client_current_setting"):
            chosen_m = by_base.get("gemini-4-carbon") or models[0]
        elif model in by_base:
            chosen_m = by_base[model]
        else:
            return False, f"Antigravity 模型 '{model}' 不在官方可用列表中", None, {}

        eff = reasoning_effort if reasoning_effort and reasoning_effort not in ("client_default", "client_current_setting") else chosen_m["default_reasoning_effort"]
        if eff not in chosen_m["variants"]:
            return (
                False,
                f"模型 '{chosen_m['display_name']}' 不支持强度 '{eff}'（可选：{', '.join(chosen_m['supported_reasoning_efforts'])}）",
                None,
                {},
            )
        full_slug = chosen_m["variants"][eff]
        return True, None, full_slug, {
            "client": "antigravity",
            "model": chosen_m["slug"],
            "full_model_slug": full_slug,
            "reasoning_effort": eff,
            "verified_against": "agy_models",
        }

    def abort_attempt(self, attempt_id: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._active_procs.get(attempt_id)
        if not entry:
            return {"found": False, "terminated": True, "pid": None}
        proc, meta = entry
        meta["abort_requested"] = True
        pid = proc.pid
        terminated = kill_process_tree(pid)
        return {"found": True, "terminated": terminated, "pid": pid}

    def execute_turn(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        cwd: Optional[str] = None,
        sandbox: str = "read-only",
        agy_project_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        idle_timeout_sec: float = 240.0,
        max_transient_retries: int = 2,
        on_start: Optional[Callable[[int], None]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_partial_text: Optional[Callable[[str], None]] = None,
    ) -> CodexExecutionResult:
        requested_cfg = {
            "client": "antigravity",
            "model": model,
            "reasoning_effort": reasoning_effort,
            "sandbox": sandbox,
        }
        ok, err_reason, full_slug, accepted_cfg = self.resolve_full_model_slug(model, reasoning_effort)
        if not ok:
            return CodexExecutionResult(
                session_id=session_id,
                response_text="",
                partial_text="",
                exit_code=2,
                error=err_reason,
                blocking_reason=err_reason,
                requested_config=requested_cfg,
                accepted_config={},
            )

        if self.runner_override is not None:
            return self.runner_override(
                prompt=prompt,
                session_id=session_id,
                model=full_slug,
                reasoning_effort=reasoning_effort,
                cwd=cwd,
                sandbox=sandbox,
                attempt_id=attempt_id,
                on_start=on_start,
                on_event=on_event,
                requested_config=requested_cfg,
                accepted_config=accepted_cfg,
            )

        dir_index = self._build_workdir_index(cwd or os.getcwd())
        agy_safe_prompt = (
            "【Antigravity 无头 CLI 规范：你当前拥有的可用只读工具为 read_file 与 search_web"
            + ("，以及写入工具 write_file / edit_file" if sandbox == "workspace-write" else "")
            + "。严禁调用 run_command、run_workflow、invoke_subagent、ask_question 或 codex-team MCP 工具！"
            "下方已为你自动列出当前项目目录的文件树路径，请直接将所需文件的绝对路径传给 read_file 读取，核查后直接输出完整正文结论。】\n"
            f"{dir_index}\n\n"
            + prompt
        )

        def _invoke_with_project_init(p_txt: str, sess: Optional[str]) -> CodexExecutionResult:
            r = self._run_agy_once(
                prompt=p_txt,
                session_id=sess,
                full_slug=full_slug,
                cwd=cwd,
                sandbox=sandbox,
                agy_project_id=agy_project_id,
                force_new_project=False,
                attempt_id=attempt_id,
                idle_timeout_sec=idle_timeout_sec,
                requested_cfg=requested_cfg,
                accepted_cfg=accepted_cfg,
                on_start=on_start,
                on_event=on_event,
                on_partial_text=on_partial_text,
            )
            if (
                not sess
                and not r.aborted
                and r.exit_code != 0
                and "empty conversation id" in (r.error or "").lower()
            ):
                if on_event:
                    on_event({"type": "agy.init_project", "summary": "首次在该项目目录启动 Antigravity CLI，正在自动关联新工作区项目", "ts": time.time()})
                r = self._run_agy_once(
                    prompt=p_txt,
                    session_id=None,
                    full_slug=full_slug,
                    cwd=cwd,
                    sandbox=sandbox,
                    agy_project_id=None,
                    force_new_project=True,
                    attempt_id=attempt_id,
                    idle_timeout_sec=idle_timeout_sec,
                    requested_cfg=requested_cfg,
                    accepted_cfg=accepted_cfg,
                    on_start=on_start,
                    on_event=on_event,
                    on_partial_text=on_partial_text,
                )
            return r

        cur_sess = session_id
        res: Optional[CodexExecutionResult] = None
        for retry_idx in range(max_transient_retries + 1):
            res = _invoke_with_project_init(agy_safe_prompt, cur_sess)

            # If headless mode denied a tool and produced empty output, retry once instructing allowed built-in file tools
            joined_err = f"{res.error or ''}\n" + "\n".join(res.stderr_lines).lower()
            if (
                not res.aborted
                and not res.response_text.strip()
                and ("no output produced" in joined_err or "permission" in joined_err or "未产生任何文本输出" in joined_err)
            ):
                if on_event:
                    on_event({"type": "agy.retry_file_tools", "activity_label": "🔄 切换内置读取工具重试", "summary": "自动切换已授权内置文件工具重新生成输出", "ts": time.time()})
                retry_prompt = (
                    "上一轮因调用了未授权命令未产生文字输出。严禁调用 run_command 或 run_workflow！"
                    "请直接从下方目录索引中挑选路径并调用 read_file（或 search_web），随后必须直接输出完整的文字分析结论：\n"
                    f"{dir_index}\n\n"
                    + prompt
                )
                res = _invoke_with_project_init(retry_prompt, None)

            if res.aborted or res.final_status == "succeeded" or res.session_not_found:
                return res

            combined_err = f"{res.error or ''}\n" + "\n".join(res.stderr_lines[-8:])
            is_backend_hiccup = (
                is_transient_network_error(combined_err)
                or "empty conversation id" in combined_err.lower()
                or "failed to start conversation" in combined_err.lower()
            )
            if retry_idx < max_transient_retries and is_backend_hiccup:
                backoff = 2.5 * (retry_idx + 1)
                if on_event:
                    on_event({
                        "type": "agy.transient_retry",
                        "activity_label": f"🔄 网络波动第{retry_idx + 1}次重连",
                        "summary": f"检测到网络闪断或空闲超时，{backoff:.1f}秒后开启干净会话自动重试 ({retry_idx + 1}/{max_transient_retries})",
                        "ts": time.time(),
                    })
                time.sleep(backoff)
                # Reset session_id on transient failure so corrupted remote state doesn't trap retries
                cur_sess = None
                continue
            return res
        return res  # type: ignore[return-value]

    def _build_workdir_index(self, raw_dir: str, max_entries: int = 85, max_depth: int = 4) -> str:
        root = os.path.normpath(os.path.abspath(raw_dir))
        if not os.path.isdir(root):
            return ""
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".codex", ".idea", ".vscode"}
        items: List[str] = []
        try:
            for cur_root, dirs, files in os.walk(root):
                rel_dir = os.path.relpath(cur_root, root)
                depth = 0 if rel_dir == "." else (rel_dir.count(os.sep) + 1)
                dirs[:] = [d for d in sorted(dirs) if d not in skip_dirs and not d.startswith(".")]
                if depth >= max_depth:
                    dirs[:] = []
                for fn in sorted(files):
                    if fn.startswith(".") or fn.endswith((".pyc", ".tmp")):
                        continue
                    full_p = os.path.join(cur_root, fn).replace("\\", "/")
                    items.append(f"  - {full_p}")
                    if len(items) >= max_entries:
                        break
                if len(items) >= max_entries:
                    break
        except Exception:
            return ""
        if not items:
            return "【当前目录文件索引】（目录为空）"
        more = "（仅列前 85 项）" if len(items) >= max_entries else ""
        return f"【当前项目文件绝对路径索引{more}】\n" + "\n".join(items)

    def _build_session_settings_json(self, work_dir: str, sandbox: str) -> str:
        allow_rules = [
            "read_file(*)",
            "search_web(*)",
            "list_resources(*)",
            "read_resource(*)",
        ]
        if sandbox == "workspace-write":
            wd_fwd = work_dir.replace("\\", "/").rstrip("/")
            wd_win = work_dir.rstrip("\\")
            for tool_n in ("write_file", "edit_file", "replace_file_content", "multi_replace_file_content"):
                allow_rules.append(f"{tool_n}({wd_fwd}/*)")
                allow_rules.append(f"{tool_n}({wd_win}\\*)")
        return json.dumps({"permissions": {"allow": allow_rules}}, ensure_ascii=False)

    def _run_agy_once(
        self,
        prompt: str,
        session_id: Optional[str],
        full_slug: Optional[str],
        cwd: Optional[str],
        sandbox: str,
        agy_project_id: Optional[str],
        force_new_project: bool,
        attempt_id: Optional[str],
        idle_timeout_sec: float,
        requested_cfg: Dict[str, Any],
        accepted_cfg: Dict[str, Any],
        on_start: Optional[Callable[[int], None]],
        on_event: Optional[Callable[[Dict[str, Any]], None]],
        on_partial_text: Optional[Callable[[str], None]],
    ) -> CodexExecutionResult:
        mode = "plan" if sandbox == "read-only" else "accept-edits"
        work_dir = os.path.normpath(os.path.abspath(cwd or os.getcwd()))
        os.makedirs(work_dir, exist_ok=True)
        settings_json = self._build_session_settings_json(work_dir, sandbox)

        # Stream the prompt over stdin as NDJSON (--input-format stream-json) so Windows never hits
        # CreateProcess's 32KB argv limit ([WinError 206]) on rich multi-round Roundtable transcripts!
        argv = [
            self.agy_bin,
            "--print=",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--mode",
            mode,
            "--add-dir",
            work_dir,
            "--settings",
            settings_json,
        ]
        if full_slug:
            argv.extend(["--model", full_slug])
        if session_id:
            argv.extend(["--conversation", session_id])
        elif force_new_project:
            argv.append("--new-project")
        elif agy_project_id:
            argv.extend(["--project", agy_project_id])

        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir,
            env=_safe_env(),
            creationflags=CREATE_NO_WINDOW,
        )

        # Feed prompt via background stdin writer so large multi-turn prompts never deadlock pipes
        def _feed_stdin() -> None:
            if not proc.stdin:
                return
            try:
                ndjson_line = json.dumps(
                    {"event": "user", "message": {"role": "user", "content": prompt}},
                    ensure_ascii=False,
                ) + "\n"
                proc.stdin.write(ndjson_line)
                proc.stdin.flush()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=_feed_stdin, daemon=True).start()

        meta: Dict[str, Any] = {"abort_requested": False, "watchdog_timed_out": False, "last_io_ts": time.time()}
        key = attempt_id or f"agy-{proc.pid}-{time.time()}"
        with self._lock:
            self._active_procs[key] = (proc, meta)

        def _watchdog_loop() -> None:
            if idle_timeout_sec <= 0:
                return
            while proc.poll() is None and not meta.get("abort_requested"):
                if time.time() - meta["last_io_ts"] > idle_timeout_sec:
                    meta["watchdog_timed_out"] = True
                    kill_process_tree(proc.pid)
                    break
                time.sleep(2.0)

        threading.Thread(target=_watchdog_loop, daemon=True).start()

        if on_start:
            try:
                on_start(proc.pid)
            except Exception:
                pass

        stderr_lines: List[str] = []
        session_missing = {"value": False}

        def _read_stderr():
            if not proc.stderr:
                return
            for raw in proc.stderr:
                meta["last_io_ts"] = time.time()
                ln = raw.strip()
                if not ln:
                    continue
                stderr_lines.append(ln)
                low = ln.lower()
                if "conversation not found" in low or ("not found" in low and "conversation" in low):
                    session_missing["value"] = True

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        res_session_id = session_id
        events: List[Dict[str, Any]] = []
        streamed_text = ""
        final_response = ""
        turn_completed = False
        usage_info: Dict[str, Any] = {}
        stream_error: Optional[str] = None
        accepted_final = dict(accepted_cfg)

        if proc.stdout:
            for raw_line in proc.stdout:
                meta["last_io_ts"] = time.time()
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                events.append(ev)
                etype = ev.get("event")

                if etype == "init":
                    res_session_id = ev.get("conversation_id") or res_session_id
                    init_obj = ev.get("init") or {}
                    if init_obj.get("model"):
                        accepted_final["backend_reported_model"] = init_obj["model"]
                    if on_event:
                        on_event({
                            "type": "agy.init",
                            "session_id": res_session_id,
                            "activity_label": "🚀 已接通模型推理",
                            "summary": f"Antigravity 会话已启动 ({init_obj.get('model', full_slug)})",
                            "ts": time.time(),
                        })
                elif etype == "step_update":
                    su = ev.get("step_update") or {}
                    res_session_id = su.get("conversation_id") or res_session_id
                    stype = su.get("step_type", "step")
                    delta = su.get("text_delta") or ""
                    if stype == "agent_response" and delta:
                        streamed_text += delta
                        if on_partial_text:
                            try:
                                on_partial_text(streamed_text)
                            except Exception:
                                pass
                    elif stype not in ("user_input", "agent_response") and su.get("state") == "ACTIVE":
                        tname = su.get("tool_name") or stype
                        tparams = (su.get("tool_info") or {}).get("parameters") or {}
                        target_hint = ""
                        for pk in ("Path", "SearchPath", "SearchDirectory", "query", "Pattern", "Query"):
                            if tparams.get(pk):
                                raw_v = str(tparams[pk])
                                target_hint = os.path.basename(raw_v.rstrip("/\\")) or raw_v[:32]
                                break
                        tool_cn = {
                            "read_file": "📖 读取文件",
                            "list_dir": "📂 浏览目录",
                            "find_by_name": "🔎 查找文件",
                            "grep_search": "🔎 检索内容",
                            "search_web": "🌐 联网检索",
                            "write_file": "✏️ 写入文件",
                            "edit_file": "✏️ 编辑文件",
                        }.get(tname, f"⚙️ 调用 {tname}")
                        act_lbl = f"{tool_cn}: {target_hint}" if target_hint else tool_cn
                        if on_event:
                            on_event({
                                "type": f"agy.{tname}",
                                "session_id": res_session_id,
                                "activity_label": act_lbl,
                                "summary": act_lbl,
                                "ts": time.time(),
                            })
                elif etype == "result":
                    r_obj = ev.get("result") or {}
                    res_session_id = r_obj.get("conversation_id") or res_session_id
                    usage_info = r_obj.get("usage") or {}
                    status_str = str(r_obj.get("status", "")).upper()
                    if status_str == "SUCCESS":
                        turn_completed = True
                        final_response = (r_obj.get("response") or streamed_text).strip()
                        if on_event:
                            on_event({
                                "type": "agy.completed",
                                "session_id": res_session_id,
                                "summary": f"轮次完成 (耗时 {round(r_obj.get('duration_seconds', 0), 1)}s)",
                                "ts": time.time(),
                            })
                    else:
                        stream_error = r_obj.get("error") or f"agy status={status_str}"
                        if "not found" in str(stream_error).lower() and session_id:
                            session_missing["value"] = True

        exit_code = proc.wait()
        stderr_thread.join(timeout=2.0)

        with self._lock:
            self._active_procs.pop(key, None)

        aborted = bool(meta.get("abort_requested"))
        if not final_response and streamed_text.strip():
            final_response = streamed_text.strip()
        if meta.get("watchdog_timed_out"):
            turn_completed = False
            stream_error = f"watchdog_idle_timeout: 连续 {int(idle_timeout_sec)} 秒无流式响应，已自动中断保护"
        elif not stream_error and exit_code != 0 and stderr_lines:
            stream_error = "\n".join(stderr_lines[-5:])

        # Never allow a 0-char blank response ('jetski: no output produced') to masquerade as succeeded!
        if turn_completed and not final_response.strip():
            turn_completed = False
            stderr_tail = "\n".join(stderr_lines[-3:])
            stream_error = stream_error or (f"模型未产生任何文本输出: {stderr_tail}" if stderr_tail else "模型未产生任何文本输出 (empty response)")

        accepted_final["session_id"] = res_session_id
        return CodexExecutionResult(
            session_id=res_session_id,
            response_text=final_response,
            partial_text=streamed_text.strip(),
            exit_code=exit_code,
            raw_events=events,
            stderr_lines=stderr_lines,
            error=stream_error,
            turn_completed=turn_completed,
            aborted=aborted,
            policy_blocked=False,
            blocking_reason=None,
            session_not_found=session_missing["value"],
            requested_config=requested_cfg,
            accepted_config=accepted_final,
            usage=usage_info,
        )
