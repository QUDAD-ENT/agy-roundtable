"""
Official Codex Worker Adapter for Antigravity Coordinator.
Provides:
- Official CLI & login status probing (without reading or extracting credentials)
- Real model & reasoning effort discovery from ~/.codex/models_cache.json
- Live JSONL event streaming via subprocess.Popen (no cmd.exe quote mangling)
- Confirmed process-tree cancellation (abort) retaining partial outputs & events
- Policy rejection (blocked by policy) and incomplete-turn detection
- Session availability / fallback detection
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from context_engine import classify_turn_error


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) if os.name == "nt" else 0


def hidden_subprocess_kwargs(new_process_group: bool = False) -> Dict[str, Any]:
    """Win32 zero-popup subprocess flags: CREATE_NO_WINDOW (NEVER mixed with DETACHED_PROCESS!)
    + STARTF_USESHOWWINDOW / SW_HIDE so neither direct children nor grandchildren flash console windows."""
    if os.name != "nt":
        return {}
    flags = CREATE_NO_WINDOW | (CREATE_NEW_PROCESS_GROUP if new_process_group else 0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"creationflags": flags, "startupinfo": si}


def get_runtime_dirs() -> Tuple[str, str]:
    """Return (tmp_dir, cache_dir). Uses D:\\gemini on local D-drive setups or portable temp dir otherwise."""
    custom_root = os.environ.get("AGY_ROUNDTABLE_STORAGE_DIR")
    if custom_root:
        t_dir = os.path.join(custom_root, "tmp")
        c_dir = os.path.join(custom_root, "cache")
    elif os.name == "nt" and os.path.isdir(r"D:\gemini"):
        t_dir = r"D:\gemini\tmp"
        c_dir = r"D:\gemini\cache"
    else:
        base = os.path.join(tempfile.gettempdir(), "agy-roundtable")
        t_dir = os.path.join(base, "tmp")
        c_dir = os.path.join(base, "cache")
    os.makedirs(t_dir, exist_ok=True)
    os.makedirs(c_dir, exist_ok=True)
    return t_dir, c_dir


def _safe_env() -> Dict[str, str]:
    """Enforce isolated cache/tmp storage directory without altering CLI authentication."""
    env = dict(os.environ)
    t_dir, c_dir = get_runtime_dirs()
    pyc_dir = os.path.join(c_dir, "pycache")
    os.makedirs(pyc_dir, exist_ok=True)
    env["TEMP"] = t_dir
    env["TMP"] = t_dir
    env["TMPDIR"] = t_dir
    env["PYTHONPYCACHEPREFIX"] = pyc_dir
    env["PYTHONUTF8"] = "1"
    return env


def is_pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        p = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=4,
            **hidden_subprocess_kwargs(),
        )
        return f'"{int(pid)}"' in p.stdout
    except Exception:
        return False


def kill_process_tree(pid: int, timeout_sec: float = 5.0) -> bool:
    """Request termination of a running worker process tree and confirm exit."""
    if not pid or pid <= 0:
        return True
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            pass
    else:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.15)
    if os.name != "nt" and is_pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass
    return not is_pid_alive(pid)


@dataclass
class CodexExecutionResult:
    session_id: Optional[str]
    response_text: str
    partial_text: str
    exit_code: int
    raw_events: List[Dict[str, Any]] = field(default_factory=list)
    stderr_lines: List[str] = field(default_factory=list)
    error: Optional[str] = None
    turn_completed: bool = False
    aborted: bool = False
    policy_blocked: bool = False
    blocking_reason: Optional[str] = None
    session_not_found: bool = False
    requested_config: Dict[str, Any] = field(default_factory=dict)
    accepted_config: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)
    command_logs: List[Dict[str, Any]] = field(default_factory=list)
    changed_files: List[Dict[str, Any]] = field(default_factory=list)
    edit_after_check: bool = False
    context_overflow: bool = False
    error_kind: Optional[str] = None
    side_effect_state: str = "none"

    @property
    def final_status(self) -> str:
        if self.aborted:
            return "aborted"
        if self.policy_blocked:
            return "blocked"
        if self.exit_code == 0 and self.turn_completed:
            return "succeeded"
        return "failed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "response_text": self.response_text,
            "partial_text": self.partial_text,
            "exit_code": self.exit_code,
            "final_status": self.final_status,
            "turn_completed": self.turn_completed,
            "aborted": self.aborted,
            "policy_blocked": self.policy_blocked,
            "blocking_reason": self.blocking_reason,
            "session_not_found": self.session_not_found,
            "context_overflow": self.context_overflow,
            "error_kind": self.error_kind,
            "side_effect_state": self.side_effect_state,
            "requested_config": self.requested_config,
            "accepted_config": self.accepted_config,
            "usage": self.usage,
            "command_logs": self.command_logs,
            "changed_files": self.changed_files,
            "edit_after_check": self.edit_after_check,
            "error": self.error,
            "event_count": len(self.raw_events),
        }


TRANSIENT_ERR_PATTERNS = (
    "network issue",
    "connectex",
    "connection attempt failed",
    "timed out",
    "timeout",
    "econnreset",
    "connection reset",
    "temporarily unavailable",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway",
    "retryable\":true",
    "retryable\": true",
    "watchdog_idle_timeout",
)


def is_transient_network_error(err_text: str) -> bool:
    low = (err_text or "").lower()
    return any(pat in low for pat in TRANSIENT_ERR_PATTERNS)


def resolve_idle_timeout(reasoning_effort: Optional[str], idle_timeout_sec: Optional[float] = None) -> float:
    """Effort-aware idle watchdog ceiling so deep reasoning bursts ('high'/'xhigh') aren't falsely killed at 240s."""
    if idle_timeout_sec is not None:
        return float(idle_timeout_sec)
    eff = (reasoning_effort or "").strip().lower()
    if eff in ("xhigh", "max"):
        return 600.0
    if eff == "high":
        return 480.0
    if eff == "medium":
        return 300.0
    return 240.0


class CodexAdapter:
    def __init__(
        self,
        codex_bin: Optional[str] = None,
        models_cache_path: Optional[str] = None,
        runner_override: Optional[Callable[..., CodexExecutionResult]] = None,
    ):
        self.node_bin = shutil.which("node.exe") or shutil.which("node")
        appdata_npm = os.path.expandvars(r"%APPDATA%\npm") if os.name == "nt" else ""
        default_js = os.path.join(appdata_npm, "node_modules", "@openai", "codex", "bin", "codex.js") if appdata_npm else ""
        self.codex_js = default_js if (default_js and os.path.exists(default_js)) else None
        self.codex_bin = (
            codex_bin
            or os.environ.get("CODEX_BIN")
            or shutil.which("codex.cmd")
            or shutil.which("codex")
            or (os.path.join(appdata_npm, "codex.cmd") if appdata_npm else "codex")
        )
        self.models_cache_path = models_cache_path or os.path.expanduser(r"~/.codex/models_cache.json")
        self.runner_override = runner_override
        self._lock = threading.Lock()
        self._active_procs: Dict[str, Tuple[subprocess.Popen, Dict[str, Any]]] = {}
        self._cached_probe: Optional[Dict[str, Any]] = None

    def _base_argv(self) -> List[str]:
        if self.node_bin and self.codex_js:
            return [self.node_bin, self.codex_js]
        return ["cmd.exe", "/d", "/s", "/c", self.codex_bin]

    def _highwater_cache_path(self) -> Optional[str]:
        default_cache = os.path.normcase(os.path.abspath(os.path.expanduser(r"~/.codex/models_cache.json")))
        cur_cache = os.path.normcase(os.path.abspath(self.models_cache_path))
        if cur_cache != default_cache:
            return None
        tmp_dir, _ = get_runtime_dirs()
        return os.path.join(tmp_dir, "codex_models_highwater.json")

    def discover_capabilities(self, refresh_cli: bool = False) -> Dict[str, Any]:
        """Return official Codex models, supported reasoning efforts, and CLI login status.
        Preserves a high-water mirror so an older CLI binary overwriting models_cache.json
        never hides newer flagship slugs (e.g., gpt-6.2-sol / gpt-6.2-astra) already reported by
        a newer local Codex client and accepted by the backend.
        """
        merged: Dict[str, Dict[str, Any]] = {}
        hw_path = self._highwater_cache_path()
        if hw_path and os.path.exists(hw_path):
            try:
                with open(hw_path, "r", encoding="utf-8") as hf:
                    hw_raw = json.load(hf)
                for m in hw_raw.get("models", []):
                    slug = m.get("slug")
                    if slug:
                        merged[slug] = m
            except Exception:
                pass

        cache_meta: Dict[str, Any] = {"source": self.models_cache_path, "available": False}
        live_count = 0
        if os.path.exists(self.models_cache_path):
            try:
                with open(self.models_cache_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                cache_meta = {
                    "source": self.models_cache_path,
                    "available": True,
                    "fetched_at": raw.get("fetched_at"),
                    "client_version": raw.get("client_version"),
                }
                for idx, m in enumerate(raw.get("models", [])):
                    slug = m.get("slug")
                    if not slug:
                        continue
                    live_count += 1
                    efforts = [
                        lvl.get("effort")
                        for lvl in m.get("supported_reasoning_levels", [])
                        if lvl.get("effort")
                    ]
                    merged[slug] = {
                        "slug": slug,
                        "priority": int(m.get("priority", idx + 10)),
                        "display_name": m.get("display_name") or slug,
                        "description": m.get("description") or "",
                        "default_reasoning_effort": m.get("default_reasoning_level") or (efforts[0] if efforts else "low"),
                        "supported_reasoning_efforts": efforts,
                    }
            except Exception as exc:
                cache_meta["error"] = str(exc)

        def _model_sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
            slug = str(item.get("slug") or "")
            # Prefer newer generations (e.g. gpt-6.2 > gpt-6.1 > gpt-6 > gpt-5.6) even when an older CLI overwrote priority=1
            m_ver = re.search(r"gpt-(\d+)(?:\.(\d+))?", slug.lower())
            if m_ver:
                maj = int(m_ver.group(1))
                mino = int(m_ver.group(2) or 0)
                gen_rank = -(maj * 100 + mino)
            else:
                gen_rank = 0
            return (gen_rank, int(item.get("priority", 99)), slug)

        models: List[Dict[str, Any]] = sorted(merged.values(), key=_model_sort_key)
        if hw_path and live_count > 0:
            try:
                os.makedirs(os.path.dirname(hw_path), exist_ok=True)
                with open(hw_path, "w", encoding="utf-8") as hf:
                    json.dump({"updated_at": time.time(), "models": models}, hf, ensure_ascii=False, indent=2)
            except Exception:
                pass

        if refresh_cli or self._cached_probe is None:
            cli_version = None
            login_status = None
            logged_in = False
            try:
                pv = subprocess.run(
                    self._base_argv() + ["--version"],
                    capture_output=True,
                    text=True,
                    env=_safe_env(),
                    timeout=8,
                    **hidden_subprocess_kwargs(),
                )
                cli_version = (pv.stdout or pv.stderr).strip()
            except Exception as exc:
                cli_version = f"error: {exc}"

            try:
                pl = subprocess.run(
                    self._base_argv() + ["login", "status"],
                    capture_output=True,
                    text=True,
                    env=_safe_env(),
                    timeout=8,
                    **hidden_subprocess_kwargs(),
                )
                login_status = ((pl.stdout or "") + " " + (pl.stderr or "")).strip()
                logged_in = pl.returncode == 0 and ("Logged in" in login_status)
            except Exception as exc:
                login_status = f"error: {exc}"

            self._cached_probe = {
                "cli_version": cli_version,
                "login_status": login_status,
                "logged_in": logged_in,
                "probed_at": time.time(),
            }

        return {
            "cli": self._cached_probe,
            "models_cache": cache_meta,
            "models": models,
        }

    def validate_model_effort(
        self, model: Optional[str], reasoning_effort: Optional[str]
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Validate requested model and effort against reported official capabilities."""
        caps = self.discover_capabilities(refresh_cli=False)
        models_by_slug = {m["slug"]: m for m in caps["models"]}
        if not models_by_slug:
            # No cache file; pass through transparently and mark client_default
            return True, None, {
                "client": "codex",
                "model": model or "client_default",
                "reasoning_effort": reasoning_effort or "client_default",
                "source": "client_default_no_cache",
            }

        if model and model not in models_by_slug:
            return (
                False,
                f"模型 '{model}' 不在当前官方报告的可用模型列表中",
                {},
            )

        selected = models_by_slug[model] if model else next(iter(models_by_slug.values()))
        supported = selected.get("supported_reasoning_efforts") or []
        if reasoning_effort and supported and reasoning_effort not in supported:
            return (
                False,
                f"模型 '{selected['slug']}' 不支持思考强度 '{reasoning_effort}'（支持：{', '.join(supported)}）",
                {},
            )

        accepted = {
            "client": "codex",
            "model": model or "client_default",
            "reasoning_effort": reasoning_effort or (selected["default_reasoning_effort"] if model else "client_default"),
            "verified_against": "models_cache.json",
        }
        return True, None, accepted

    def abort_attempt(self, attempt_id: str) -> Dict[str, Any]:
        """Abort a live attempt by killing its OS process tree and confirming exit."""
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
        skip_git_check: bool = True,
        attempt_id: Optional[str] = None,
        idle_timeout_sec: Optional[float] = None,
        max_transient_retries: int = 2,
        on_start: Optional[Callable[[int], None]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_partial_text: Optional[Callable[[str], None]] = None,
    ) -> CodexExecutionResult:
        eff_idle_timeout = resolve_idle_timeout(reasoning_effort, idle_timeout_sec)
        requested_cfg = {
            "client": "codex",
            "model": model,
            "reasoning_effort": reasoning_effort,
            "sandbox": sandbox,
        }
        ok, err_reason, accepted_cfg = self.validate_model_effort(model, reasoning_effort)
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
                model=model,
                reasoning_effort=reasoning_effort,
                cwd=cwd,
                sandbox=sandbox,
                attempt_id=attempt_id,
                on_start=on_start,
                on_event=on_event,
                requested_config=requested_cfg,
                accepted_config=accepted_cfg,
            )

        cur_sess = session_id
        res: Optional[CodexExecutionResult] = None
        for retry_idx in range(max_transient_retries + 1):
            res = self._run_codex_once(
                prompt=prompt,
                session_id=cur_sess,
                model=model,
                reasoning_effort=reasoning_effort,
                cwd=cwd,
                sandbox=sandbox,
                skip_git_check=skip_git_check,
                attempt_id=attempt_id,
                idle_timeout_sec=eff_idle_timeout,
                requested_cfg=requested_cfg,
                accepted_cfg=accepted_cfg,
                on_start=on_start,
                on_event=on_event,
                on_partial_text=on_partial_text,
            )
            if res.aborted or res.final_status == "succeeded" or res.policy_blocked or res.session_not_found or res.context_overflow:
                return res
            combined_err = f"{res.error or ''}\n" + "\n".join(res.stderr_lines[-8:])
            # Cap watchdog_idle_timeout retries to 1 so stalled calls fail over fast instead of burning 3x timeout!
            is_watchdog = "watchdog_idle_timeout" in combined_err.lower()
            allowed_retries = min(max_transient_retries, 1) if is_watchdog else max_transient_retries
            if retry_idx < allowed_retries and is_transient_network_error(combined_err):
                backoff = 2.0 * (retry_idx + 1)
                if on_event:
                    on_event({
                        "type": "codex.transient_retry",
                        "summary": f"检测到网络波动或响应超时，{int(backoff)}秒后自动第 {retry_idx + 1} 次重试",
                        "ts": time.time(),
                    })
                time.sleep(backoff)
                # Use fresh session if prior attempt never completed properly
                cur_sess = None if not res.response_text.strip() else cur_sess
                continue
            return res
        return res  # type: ignore[return-value]

    def _run_codex_once(
        self,
        prompt: str,
        session_id: Optional[str],
        model: Optional[str],
        reasoning_effort: Optional[str],
        cwd: Optional[str],
        sandbox: str,
        skip_git_check: bool,
        attempt_id: Optional[str],
        idle_timeout_sec: float,
        requested_cfg: Dict[str, Any],
        accepted_cfg: Dict[str, Any],
        on_start: Optional[Callable[[int], None]],
        on_event: Optional[Callable[[Dict[str, Any]], None]],
        on_partial_text: Optional[Callable[[str], None]],
    ) -> CodexExecutionResult:

        # P0-0 Hard Sandbox Trust Root: reject danger-full-access or any non-allowlisted sandbox mode!
        if sandbox not in ("read-only", "workspace-write"):
            raise ValueError(f"非法 sandbox 策略（仅允许 read-only / workspace-write）: {sandbox}")

        argv = list(self._base_argv())
        if session_id:
            argv.extend(["exec", "resume", session_id, "--json", "-c", 'approval_policy="never"', "-c", f'sandbox_mode="{sandbox}"', "-c", "notify=[]"])
            if skip_git_check:
                argv.append("--skip-git-repo-check")
            if model:
                argv.extend(["-m", model])
            if reasoning_effort:
                argv.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
            argv.append("-")
        else:
            argv.extend(["exec", "--json", "--sandbox", sandbox, "-c", 'approval_policy="never"', "-c", "notify=[]"])
            if skip_git_check:
                argv.append("--skip-git-repo-check")
            if model:
                argv.extend(["-m", model])
            if reasoning_effort:
                argv.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
            argv.append("-")

        work_dir = cwd or os.getcwd()
        if sandbox == "read-only":
            if not os.path.isdir(work_dir):
                raise ValueError(f"只读工作目录不存在: {work_dir}")
        else:
            os.makedirs(work_dir, exist_ok=True)

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
            **hidden_subprocess_kwargs(),
        )

        meta: Dict[str, Any] = {"abort_requested": False, "watchdog_timed_out": False, "last_io_ts": time.time()}
        key = attempt_id or f"anon-{proc.pid}-{time.time()}"
        with self._lock:
            self._active_procs[key] = (proc, meta)

        def _watchdog_loop() -> None:
            if idle_timeout_sec <= 0:
                return
            last_hb_ts = time.time()
            while proc.poll() is None and not meta.get("abort_requested"):
                now_w = time.time()
                silent = now_w - meta["last_io_ts"]
                if silent > idle_timeout_sec:
                    meta["watchdog_timed_out"] = True
                    kill_process_tree(proc.pid)
                    break
                # Soft live heartbeat during long deep-reasoning silence so UI clearly shows process is alive
                if silent >= 45.0 and (now_w - last_hb_ts) >= 30.0:
                    last_hb_ts = now_w
                    if on_event:
                        try:
                            on_event({
                                "type": "thinking.heartbeat",
                                "activity_label": f"🧠 深度思考推理中（已静默 {int(silent)}s · 进程活跃）",
                                "summary": f"深度思考推理中（已静默 {int(silent)}s / 上限 {int(idle_timeout_sec)}s）",
                                "ts": now_w,
                            })
                        except Exception:
                            pass
                time.sleep(2.0)

        threading.Thread(target=_watchdog_loop, daemon=True).start()

        if on_start:
            try:
                on_start(proc.pid)
            except Exception:
                pass

        # Prevent Windows Codex read-only sandbox policy blocks caused by multi-line PowerShell / $env: assignments
        stdin_prompt = prompt
        if os.name == "nt" and sandbox == "read-only" and not session_id:
            ro_hygiene = (
                "【Codex Windows 只读沙箱执行规范：严禁执行多行 PowerShell 脚本、$env: 环境变量赋值、"
                "heredoc (@'...'@) 或文件重定向（会触发 policy 拦截导致本轮受阻）；"
                "请仅使用简洁单行只读命令（如 Get-Content -TotalCount / Select-String / Get-ChildItem / 单行只读 python -c）。】\n\n"
            )
            stdin_prompt = ro_hygiene + prompt

        # Feed prompt via stdin and close stdin cleanly
        def _feed_stdin():
            try:
                if proc.stdin:
                    proc.stdin.write(stdin_prompt)
                    proc.stdin.close()
            except Exception:
                pass

        stdin_thread = threading.Thread(target=_feed_stdin, daemon=True)
        stdin_thread.start()

        stderr_lines: List[str] = []
        policy_reasons: List[str] = []
        session_missing_flag = {"value": False}

        def _read_stderr():
            if not proc.stderr:
                return
            for raw_line in proc.stderr:
                meta["last_io_ts"] = time.time()
                line = raw_line.strip()
                if not line:
                    continue
                stderr_lines.append(line)
                low = line.lower()
                if "blocked by policy" in low or ("rejected(" in low and "policy" in low):
                    reason = "官方沙箱/审批策略拦截了工具执行 (blocked by policy)"
                    m = re.search(r"Rejected\((.+)\)", line)
                    if m:
                        reason = f"策略拦截 (blocked by policy): {m.group(1)[:220]}"
                    policy_reasons.append(reason)
                    if on_event:
                        try:
                            on_event({"type": "tool.blocked", "summary": reason, "ts": time.time()})
                        except Exception:
                            pass
                if (
                    "no rollout found" in low
                    or "session not found" in low
                    or "conversation not found" in low
                    or "could not find session" in low
                    or "no session" in low
                ):
                    session_missing_flag["value"] = True

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        res_session_id = session_id
        response_text = ""
        partial_chunks: List[str] = []
        events: List[Dict[str, Any]] = []
        command_logs: List[Dict[str, Any]] = []
        changed_files: List[Dict[str, Any]] = []
        last_ok_cmd_seq = -1
        last_file_edit_seq = -1
        event_seq = 0
        turn_completed = False
        usage_info: Dict[str, Any] = {}
        stream_error: Optional[str] = None

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
                ev_type = ev.get("type", "unknown")

                if ev_type == "thread.started":
                    res_session_id = ev.get("thread_id") or res_session_id
                    if on_event:
                        on_event(
                            {
                                "type": "thread.started",
                                "session_id": res_session_id,
                                "summary": f"会话已接通: {res_session_id}",
                                "ts": time.time(),
                            }
                        )
                elif ev_type == "turn.started":
                    if on_event:
                        on_event({"type": "turn.started", "summary": "模型推理轮次开始", "ts": time.time()})
                elif ev_type in ("item.started", "item.updated", "item.completed"):
                    item = ev.get("item") or {}
                    itype = item.get("type", "item")
                    if itype == "agent_message":
                        txt = item.get("text") or ""
                        if txt:
                            partial_chunks.append(txt)
                            if on_partial_text:
                                try:
                                    on_partial_text(txt)
                                except Exception:
                                    pass
                            if ev_type == "item.completed":
                                response_text = txt
                    elif itype == "command_execution":
                        cmd_str = item.get("command") or ""
                        istatus = item.get("status") or ""
                        if istatus in ("declined", "failed") and "policy" in json.dumps(item).lower():
                            policy_reasons.append(f"命令被策略拒绝: {cmd_str}")
                        if ev_type == "item.completed":
                            event_seq += 1
                            raw_rc = item.get("exit_code")
                            agg_out = str(item.get("aggregated_output") or "")
                            command_logs.append(
                                {
                                    "id": item.get("id") or f"cmd_{event_seq}",
                                    "command": cmd_str,
                                    "exit_code": raw_rc,
                                    "status": istatus,
                                    "output_excerpt": agg_out[-800:] if agg_out else "",
                                    "ts": time.time(),
                                }
                            )
                            if raw_rc == 0:
                                last_ok_cmd_seq = event_seq
                    elif itype == "file_change" and ev_type == "item.completed":
                        event_seq += 1
                        raw_changes = item.get("changes") or []
                        if isinstance(raw_changes, list) and raw_changes:
                            for ch in raw_changes:
                                if isinstance(ch, dict) and ch.get("path"):
                                    changed_files.append(
                                        {
                                            "path": str(ch.get("path")),
                                            "kind": str(ch.get("kind") or "update"),
                                            "ts": time.time(),
                                        }
                                    )
                            last_file_edit_seq = event_seq
                        elif item.get("path"):
                            changed_files.append(
                                {
                                    "path": str(item.get("path")),
                                    "kind": str(item.get("kind") or "update"),
                                    "ts": time.time(),
                                }
                            )
                            last_file_edit_seq = event_seq
                    if on_event:
                        friendly_map = {
                            "web_search": "🌐 联网检索学术/期刊资料",
                            "file_change": "✏️ 修改工作区文件",
                            "command_execution": "🔎 核查项目文件",
                            "reasoning": "🧠 深度思考推理中",
                        }
                        label_prefix = friendly_map.get(itype, itype)
                        preview = (item.get("text") or item.get("command") or itype)[:90]
                        on_event(
                            {
                                "type": f"{ev_type}:{itype}",
                                "activity_label": label_prefix,
                                "summary": f"{label_prefix}: {preview}",
                                "ts": time.time(),
                            }
                        )
                elif ev_type == "turn.completed":
                    turn_completed = True
                    usage_info = dict(ev.get("usage") or {})
                    tool_calls_done = sum(
                        1 for e in events
                        if e.get("type") == "item.completed"
                        and (e.get("item") or {}).get("type") in ("command_execution", "file_change", "mcp_tool_call", "web_search")
                    )
                    usage_info["internal_api_calls"] = max(1, 1 + tool_calls_done)
                    if on_event:
                        on_event(
                            {
                                "type": "turn.completed",
                                "summary": f"轮次完成 (out={usage_info.get('output_tokens', '?')} tokens)",
                                "ts": time.time(),
                            }
                        )
                elif ev_type in ("turn.failed", "error"):
                    err_obj = ev.get("error") or ev.get("message") or str(ev)
                    stream_error = str(err_obj)
                    if on_event:
                        on_event({"type": ev_type, "summary": f"执行错误: {stream_error[:140]}", "ts": time.time()})

        exit_code = proc.wait()
        stdin_thread.join(timeout=1.0)
        stderr_thread.join(timeout=2.0)

        with self._lock:
            self._active_procs.pop(key, None)

        aborted = bool(meta.get("abort_requested"))
        # Only treat stderr policy rejections as a fatal step block if the turn failed/exited non-zero
        # or produced no final output; intermediate rejected commands (e.g. Remove-Item) that the agent
        # gracefully works around in a multi-step turn must NOT discard a completed deliverable!
        policy_blocked = bool(
            policy_reasons and (not turn_completed or exit_code != 0 or not response_text.strip())
        )
        blocking_reason: Optional[str] = policy_reasons[0] if policy_blocked else None

        # Also check if model output explicitly surfaced a fatal policy block JSON error
        if not policy_blocked and response_text:
            try:
                parsed = json.loads(response_text)
                if isinstance(parsed, dict) and parsed.get("error") and "policy" in str(parsed.get("error")).lower():
                    policy_blocked = True
                    blocking_reason = f"模型报告工具受策略限制: {parsed.get('error')}"
            except Exception:
                pass

        error_msg = stream_error
        if meta.get("watchdog_timed_out"):
            error_msg = f"watchdog_idle_timeout: 连续 {int(idle_timeout_sec)} 秒无流式响应，已自动中断保护"
        elif not error_msg and exit_code != 0 and stderr_lines:
            error_msg = "\n".join(stderr_lines[-5:])

        accepted_final = dict(accepted_cfg)
        accepted_final["session_id"] = res_session_id

        partial_joined = partial_chunks[-1] if partial_chunks else response_text
        succeeded_now = (not aborted) and (not policy_blocked) and exit_code == 0 and turn_completed
        err_kind: Optional[str] = None
        if not succeeded_now and not aborted:
            err_kind, _sig = classify_turn_error(error_msg, stderr_lines, events)
            if policy_blocked:
                err_kind = "policy_blocked"
            elif session_missing_flag["value"] and err_kind != "context_length_exceeded":
                err_kind = "session_not_found"
        ctx_overflow = err_kind == "context_length_exceeded"
        if sandbox == "read-only":
            se_state = "read_only"
        elif changed_files or command_logs:
            se_state = "mutated_verified" if succeeded_now else "unknown_mutation"
        else:
            se_state = "none"

        return CodexExecutionResult(
            session_id=res_session_id,
            response_text=response_text,
            partial_text=partial_joined,
            exit_code=exit_code,
            raw_events=events,
            stderr_lines=stderr_lines,
            error=error_msg,
            turn_completed=turn_completed,
            aborted=aborted,
            policy_blocked=policy_blocked,
            blocking_reason=blocking_reason,
            session_not_found=session_missing_flag["value"],
            requested_config=requested_cfg,
            accepted_config=accepted_final,
            usage=usage_info,
            command_logs=command_logs,
            changed_files=changed_files,
            edit_after_check=(last_file_edit_seq > last_ok_cmd_seq and last_file_edit_seq >= 0),
            context_overflow=ctx_overflow,
            error_kind=err_kind,
            side_effect_state=se_state,
        )
