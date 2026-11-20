"""
Lock-Free Hash-Locked Mechanical Acceptance Verifier & Structured Signoff Aggregator (Pure Stdlib).
Always executed OUTSIDE TeamStateManager._lock so disk/subprocess checks never stall HTTP polling or state mutations!
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from figure_qa import sha256_file, verify_figure_manifest


def _is_within_root(root: str, target: str) -> bool:
    r = os.path.normcase(os.path.realpath(root))
    t = os.path.normcase(os.path.realpath(target))
    try:
        return os.path.commonpath([r, t]) == r
    except ValueError:
        return False


def snapshot_readonly_inputs(work_dir: str, rel_paths: List[str]) -> Dict[str, str]:
    """Compute SHA-256 fingerprints of all files under declared readonly_inputs before execution."""
    snaps: Dict[str, str] = {}
    root_real = os.path.realpath(work_dir)
    for rel in rel_paths or []:
        if not rel or not str(rel).strip():
            continue
        target = os.path.realpath(os.path.join(root_real, str(rel).strip()))
        if not _is_within_root(root_real, target) or not os.path.exists(target):
            continue
        if os.path.isfile(target):
            key = os.path.relpath(target, root_real).replace("\\", "/")
            snaps[key] = sha256_file(target)
        elif os.path.isdir(target):
            for cur_root, _dirs, files in os.walk(target):
                for fn in sorted(files):
                    fp = os.path.join(cur_root, fn)
                    key = os.path.relpath(fp, root_real).replace("\\", "/")
                    snaps[key] = sha256_file(fp)
    return snaps


def verify_readonly_inputs_intact(work_dir: str, before_snapshot: Dict[str, str]) -> List[str]:
    """Detect any unauthorized modification or deletion of readonly_inputs during a writable run."""
    violations: List[str] = []
    root_real = os.path.realpath(work_dir)
    for rel, old_hash in (before_snapshot or {}).items():
        fp = os.path.join(root_real, rel)
        if not os.path.isfile(fp):
            violations.append(f"只读原始输入文件被删除或移动: {rel}")
            continue
        cur_hash = sha256_file(fp)
        if cur_hash != old_hash:
            violations.append(f"只读原始输入文件被篡改 (SHA-256 变更): {rel}")
    return violations


def recompute_signoff_summary(signoff: Dict[str, Any]) -> Dict[str, Any]:
    """
    Single source of truth for aggregating structured blocking_items + figure_reports into
    canonical blocking, observations, ok, and status (fixes P0-c one-way failure lock!).
    """
    raw_items = signoff.get("blocking_items")
    non_fig_items: List[Dict[str, Any]] = []
    seen_non_fig_msgs: set = set()
    seen_non_fig_objs: set = set()
    if isinstance(raw_items, list):
        for it in raw_items:
            if isinstance(it, dict) and it.get("source") != "figure_qa":
                msg = str(it.get("message") or "")
                oid = str(it.get("object_id") or "")
                if oid:
                    seen_non_fig_objs.add(oid)
                if msg and msg not in seen_non_fig_msgs:
                    seen_non_fig_msgs.add(msg)
                    non_fig_items.append(it)
    else:
        # Backfill legacy signoff dicts that only had flat blocking strings
        for msg in signoff.get("blocking") or []:
            s = str(msg)
            if not s.startswith("[Figure QA]") and s not in seen_non_fig_msgs:
                seen_non_fig_msgs.add(s)
                non_fig_items.append({"source": "legacy", "object_id": "legacy", "message": s})

    # Also honor any failed non-figure check recorded under signoff["checks"] (avoiding duplicate generic noise)!
    for chk in signoff.get("checks") or []:
        if not isinstance(chk, dict) or chk.get("ok") is not False:
            continue
        kind = str(chk.get("kind") or chk.get("check_id") or "check")
        if kind == "figure_qa":
            continue
        oid = str(chk.get("object_id") or chk.get("check_id") or kind)
        chk_blocks = chk.get("blocking") or ([chk["error"]] if chk.get("error") else ([] if oid in seen_non_fig_objs else [f"{kind} 校验未通过"]))
        for b in chk_blocks:
            s = str(b)
            if s and s not in seen_non_fig_msgs:
                seen_non_fig_msgs.add(s)
                non_fig_items.append({"source": kind, "object_id": oid, "message": s})

    fig_reports = [r for r in (signoff.get("figure_reports") or []) if isinstance(r, dict)]
    fig_items: List[Dict[str, Any]] = []
    obs_list: List[str] = []
    seen_obs: set = set()

    for base_o in signoff.get("base_observations") or []:
        s = str(base_o)
        if s and s not in seen_obs:
            seen_obs.add(s)
            obs_list.append(s)

    total_checked_artifacts = 0
    any_not_verifiable = False
    for fr in fig_reports:
        mp = str(fr.get("manifest_path") or fr.get("figure_id") or "manifest")
        total_checked_artifacts += int(fr.get("checked_artifacts") or len(fr.get("artifacts") or []))
        if fr.get("status") == "not_verifiable":
            any_not_verifiable = True
        if not fr.get("ok"):
            for b in fr.get("blocking") or []:
                fig_items.append({
                    "source": "figure_qa",
                    "object_id": mp,
                    "message": f"[Figure QA] {b}",
                })
            if not (fr.get("blocking") or []):
                fig_items.append({
                    "source": "figure_qa",
                    "object_id": mp,
                    "message": f"[Figure QA] 图件契约验收未通过: {os.path.basename(mp)}",
                })
        for o in fr.get("observations") or []:
            s = str(o)
            if s and s not in seen_obs:
                seen_obs.add(s)
                obs_list.append(s)

    all_items = non_fig_items + fig_items
    flat_blocking: List[str] = []
    seen_b: set = set()
    for it in all_items:
        msg = str(it.get("message") or "").strip()
        if msg and msg not in seen_b:
            seen_b.add(msg)
            flat_blocking.append(msg)

    signoff["blocking_items"] = all_items
    signoff["blocking"] = flat_blocking
    signoff["observations"] = obs_list
    signoff["checked_artifacts"] = total_checked_artifacts

    checks_count = len(signoff.get("checks") or [])
    if flat_blocking:
        signoff["ok"] = False
        signoff["status"] = "failed"
    elif checks_count == 0 and total_checked_artifacts == 0:
        # Prevent fake green "passed / 0 checks / 0 figures"!
        strict_block = bool(signoff.get("block_on_failed_signoff", False))
        signoff["ok"] = not strict_block
        signoff["status"] = "pending_artifacts"
        note = "尚未产出可核验的图件契约或锁定验收项（待出图或补齐 figure_manifest.json）"
        if note not in seen_obs:
            obs_list.append(note)
    elif any_not_verifiable:
        # Never promote an unverified quantitative figure to "passed" just because another check passed!
        strict_block = bool(signoff.get("block_on_failed_signoff", False))
        signoff["ok"] = not strict_block
        signoff["status"] = "not_verifiable"
    else:
        signoff["ok"] = True
        signoff["status"] = "passed"
    signoff["can_release"] = signoff["status"] == "passed"
    return signoff


def run_acceptance_checks(
    work_dir: str,
    research_spec: Dict[str, Any],
    before_inputs_snapshot: Optional[Dict[str, str]] = None,
    command_logs: Optional[List[Dict[str, Any]]] = None,
    edit_after_check: bool = False,
) -> Dict[str, Any]:
    """
    Execute hash-locked acceptance scripts (shell=False, no `|| true` spoofing!), verify readonly inputs,
    and run per-manifest exception-isolated figure_qa.verify_figure_manifest on all declared figure manifests.
    Runs strictly outside state locks!
    """
    t0 = time.time()
    blocking_items: List[Dict[str, Any]] = []
    base_observations: List[str] = []
    checks_report: List[Dict[str, Any]] = []
    figure_reports: List[Dict[str, Any]] = []

    def _add_block(source: str, obj_id: str, msg: str) -> None:
        blocking_items.append({"source": source, "object_id": obj_id, "message": msg})

    root_real = os.path.realpath(work_dir)
    spec = research_spec if isinstance(research_spec, dict) else {}

    # 1. Readonly raw-data integrity check
    if before_inputs_snapshot:
        viols = verify_readonly_inputs_intact(root_real, before_inputs_snapshot)
        if viols:
            for v in viols:
                _add_block("readonly_inputs", "readonly_inputs_integrity", v)
            checks_report.append({"check_id": "readonly_inputs_integrity", "ok": False, "details": viols})
        else:
            checks_report.append({
                "check_id": "readonly_inputs_integrity",
                "ok": True,
                "files_verified": len(before_inputs_snapshot),
            })

    # 2. Edit-After-Check & Non-zero inner command audit
    if edit_after_check:
        _add_block("edit_after_check", "edit_after_check", "检测到 Edit-After-Check：末次文件修改发生在末次验收命令之后，必须重新运行验收脚本")
    if command_logs:
        nz = [c for c in command_logs if c.get("exit_code") not in (0, None)]
        if nz and spec.get("fail_on_inner_nonzero", False):
            _add_block("inner_nonzero", "command_logs", f"存在 {len(nz)} 条非零退出码子命令（末条 exit_code={nz[-1].get('exit_code')}）")
        elif nz:
            base_observations.append(f"执行过程中共有 {len(nz)} 条中间子命令返回非零退出码（已记入旁路账本供复核）")

    # 3. Hash-locked deterministic acceptance scripts (shell=False -> prevents `|| true` shell wrapping!)
    seed_val = str(spec.get("seed", 42))
    base_env = os.environ.copy()
    base_env["MPLBACKEND"] = "Agg"
    base_env["PYTHONHASHSEED"] = seed_val
    base_env["PYTHONUTF8"] = "1"
    base_env["SOURCE_DATE_EPOCH"] = str(spec.get("source_date_epoch", "1700000000"))

    for chk in spec.get("acceptance_checks") or []:
        if not isinstance(chk, dict):
            _add_block("acceptance_check", "invalid", f"acceptance_checks 条目类型非法 ({type(chk).__name__})")
            continue
        cid = str(chk.get("check_id") or "check")
        script_rel = str(chk.get("script_path") or "").strip()
        exp_sha = str(chk.get("expected_sha256") or "").strip().lower()
        runner = str(chk.get("runner") or "python").strip().lower()
        timeout_s = float(chk.get("timeout_sec") or 60.0)
        args = [str(a) for a in (chk.get("args") or [])]

        if not script_rel:
            _add_block("acceptance_check", cid, f"验收项 {cid} 缺少 script_path")
            continue
        script_abs = os.path.realpath(os.path.join(root_real, script_rel))
        if not _is_within_root(root_real, script_abs) or not os.path.isfile(script_abs):
            _add_block("acceptance_check", cid, f"验收脚本 {cid} 不存在或越出项目根目录: {script_rel}")
            checks_report.append({"check_id": cid, "ok": False, "error": "missing_or_escaped_script"})
            continue

        actual_sha = sha256_file(script_abs).lower()
        if exp_sha and actual_sha != exp_sha:
            _add_block("acceptance_check", cid, f"验收脚本 {cid} 已被篡改 (SHA-256 不匹配，拒绝执行): {script_rel}")
            checks_report.append({
                "check_id": cid,
                "ok": False,
                "error": "script_sha256_mismatch",
                "expected_sha256": exp_sha,
                "actual_sha256": actual_sha,
            })
            continue

        if runner == "rscript":
            exe = shutil.which("Rscript.exe") or shutil.which("Rscript")
            if not exe:
                _add_block("acceptance_check", cid, f"验收项 {cid} 需要 Rscript，但本机 PATH 未找到 Rscript")
                checks_report.append({"check_id": cid, "ok": False, "error": "rscript_missing"})
                continue
            argv = [exe, script_abs] + args
        else:
            argv = [sys.executable, script_abs] + args

        try:
            _hk: Dict[str, Any] = {}
            if os.name == "nt":
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
                except Exception:
                    pass
                _si = subprocess.STARTUPINFO()
                _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                _si.wShowWindow = 0
                _hk = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), "startupinfo": _si}
            proc = subprocess.run(
                argv,
                cwd=root_real,
                env=base_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                shell=False,
                **_hk,
            )
            ok_rc = proc.returncode in (chk.get("allowed_exit_codes") or [0])
            rec = {
                "check_id": cid,
                "ok": ok_rc,
                "exit_code": proc.returncode,
                "script_path": script_rel,
                "script_sha256": actual_sha,
                "stdout_tail": (proc.stdout or "")[-600:],
                "stderr_tail": (proc.stderr or "")[-600:],
            }
            checks_report.append(rec)
            if not ok_rc:
                _add_block(
                    "acceptance_check",
                    cid,
                    f"锁定验收脚本 {cid} 失败 (exit_code={proc.returncode}): {(proc.stderr or proc.stdout).strip()[:180]}",
                )
        except subprocess.TimeoutExpired:
            _add_block("acceptance_check", cid, f"锁定验收脚本 {cid} 执行超时 ({timeout_s}s)")
            checks_report.append({"check_id": cid, "ok": False, "error": "timeout"})
        except Exception as exc:
            _add_block("acceptance_check", cid, f"锁定验收脚本 {cid} 启动异常: {exc}")
            checks_report.append({"check_id": cid, "ok": False, "error": str(exc)})

    # 4. Per-Manifest Exception-Isolated Figure QA checks
    for mf_rel in spec.get("figure_manifests") or []:
        mf_str = str(mf_rel or "").strip()
        if not mf_str:
            continue
        mf_abs = os.path.realpath(os.path.join(root_real, mf_str))
        if not _is_within_root(root_real, mf_abs):
            figure_reports.append({
                "ok": False,
                "status": "failed",
                "verification_level": "failed",
                "figure_id": os.path.basename(mf_str) or "escaped_manifest",
                "manifest_path": mf_abs,
                "manifest_rel_path": mf_str,
                "checked_artifacts": 0,
                "checked_panels": 0,
                "blocking": [f"图件契约路径越界: {mf_str}"],
                "observations": [],
                "manual_checks": [],
                "artifacts": [],
            })
            continue
        try:
            fq = verify_figure_manifest(mf_abs, root_real)
        except Exception as exc:
            fq = {
                "ok": False,
                "status": "failed",
                "verification_level": "failed",
                "figure_id": os.path.basename(mf_abs),
                "manifest_path": mf_abs,
                "checked_artifacts": 0,
                "checked_panels": 0,
                "blocking": [f"figure_qa 内部异常: {type(exc).__name__}: {exc}"],
                "observations": [],
                "manual_checks": [],
                "artifacts": [],
            }
        figure_reports.append(fq)

    result: Dict[str, Any] = {
        "ok": True,
        "status": "passed",
        "mode": spec.get("mode", "managed_exec"),
        "advisory": bool(spec.get("advisory", False)),
        "block_on_failed_signoff": bool(spec.get("block_on_failed_signoff", False)),
        "elapsed_sec": round(time.time() - t0, 2),
        "blocking_items": blocking_items,
        "blocking": [],
        "base_observations": base_observations,
        "observations": [],
        "checks": checks_report,
        "figure_reports": figure_reports,
        "verified_at": time.time(),
    }
    return recompute_signoff_summary(result)
