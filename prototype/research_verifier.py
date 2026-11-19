"""
Lock-Free Hash-Locked Mechanical Acceptance Verifier & Route-B External Import Engine (Pure Stdlib).
Always executed OUTSIDE TeamStateManager._lock so disk/subprocess checks never stall HTTP polling or state mutations!
"""

from __future__ import annotations

import hashlib
import json
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


def run_acceptance_checks(
    work_dir: str,
    research_spec: Dict[str, Any],
    before_inputs_snapshot: Optional[Dict[str, str]] = None,
    command_logs: Optional[List[Dict[str, Any]]] = None,
    edit_after_check: bool = False,
) -> Dict[str, Any]:
    """
    Execute hash-locked acceptance scripts (shell=False, no `|| true` spoofing!), verify readonly inputs,
    and run figure_qa.verify_figure_manifest on all declared figure manifests.
    Runs strictly outside state locks!
    """
    t0 = time.time()
    blocking: List[str] = []
    observations: List[str] = []
    checks_report: List[Dict[str, Any]] = []
    figure_reports: List[Dict[str, Any]] = []

    root_real = os.path.realpath(work_dir)

    # 1. Readonly raw-data integrity check
    if before_inputs_snapshot:
        viols = verify_readonly_inputs_intact(root_real, before_inputs_snapshot)
        if viols:
            blocking.extend(viols)
            checks_report.append({"check_id": "readonly_inputs_integrity", "ok": False, "details": viols})
        else:
            checks_report.append({
                "check_id": "readonly_inputs_integrity",
                "ok": True,
                "files_verified": len(before_inputs_snapshot),
            })

    # 2. Edit-After-Check & Non-zero inner command audit
    if edit_after_check:
        blocking.append("检测到 Edit-After-Check：末次文件修改发生在末次验收命令之后，必须重新运行验收脚本")
    if command_logs:
        nz = [c for c in command_logs if c.get("exit_code") not in (0, None)]
        if nz and research_spec.get("fail_on_inner_nonzero", False):
            blocking.append(f"存在 {len(nz)} 条非零退出码子命令（末条 exit_code={nz[-1].get('exit_code')}）")
        elif nz:
            observations.append(f"执行过程中共有 {len(nz)} 条中间子命令返回非零退出码（已记入旁路账本供复核）")

    # 3. Hash-locked deterministic acceptance scripts (shell=False -> prevents `|| true` shell wrapping!)
    seed_val = str(research_spec.get("seed", 42))
    base_env = os.environ.copy()
    base_env["MPLBACKEND"] = "Agg"
    base_env["PYTHONHASHSEED"] = seed_val
    base_env["PYTHONUTF8"] = "1"
    base_env["SOURCE_DATE_EPOCH"] = str(research_spec.get("source_date_epoch", "1700000000"))

    for chk in research_spec.get("acceptance_checks") or []:
        cid = str(chk.get("check_id") or "check")
        script_rel = str(chk.get("script_path") or "").strip()
        exp_sha = str(chk.get("expected_sha256") or "").strip().lower()
        runner = str(chk.get("runner") or "python").strip().lower()
        timeout_s = float(chk.get("timeout_sec") or 60.0)
        args = [str(a) for a in (chk.get("args") or [])]

        if not script_rel:
            blocking.append(f"验收项 {cid} 缺少 script_path")
            continue
        script_abs = os.path.realpath(os.path.join(root_real, script_rel))
        if not _is_within_root(root_real, script_abs) or not os.path.isfile(script_abs):
            blocking.append(f"验收脚本 {cid} 不存在或越出项目根目录: {script_rel}")
            checks_report.append({"check_id": cid, "ok": False, "error": "missing_or_escaped_script"})
            continue

        actual_sha = sha256_file(script_abs).lower()
        if exp_sha and actual_sha != exp_sha:
            blocking.append(f"验收脚本 {cid} 已被篡改 (SHA-256 不匹配，拒绝执行): {script_rel}")
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
                blocking.append(f"验收项 {cid} 需要 Rscript，但本机 PATH 未找到 Rscript")
                checks_report.append({"check_id": cid, "ok": False, "error": "rscript_missing"})
                continue
            argv = [exe, script_abs] + args
        else:
            argv = [sys.executable, script_abs] + args

        try:
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
                blocking.append(f"锁定验收脚本 {cid} 失败 (exit_code={proc.returncode}): {(proc.stderr or proc.stdout).strip()[:180]}")
        except subprocess.TimeoutExpired:
            blocking.append(f"锁定验收脚本 {cid} 执行超时 ({timeout_s}s)")
            checks_report.append({"check_id": cid, "ok": False, "error": "timeout"})
        except Exception as exc:
            blocking.append(f"锁定验收脚本 {cid} 启动异常: {exc}")
            checks_report.append({"check_id": cid, "ok": False, "error": str(exc)})

    # 4. Figure Manifest QA checks
    for mf_rel in research_spec.get("figure_manifests") or []:
        mf_abs = os.path.realpath(os.path.join(root_real, str(mf_rel)))
        if not _is_within_root(root_real, mf_abs):
            blocking.append(f"图件契约路径越界: {mf_rel}")
            continue
        fq = verify_figure_manifest(mf_abs, root_real)
        figure_reports.append(fq)
        if not fq.get("ok"):
            for b in fq.get("blocking") or []:
                blocking.append(f"[Figure QA] {b}")
        observations.extend(fq.get("observations") or [])

    passed = len(blocking) == 0
    return {
        "ok": passed,
        "status": "passed" if passed else "failed",
        "mode": research_spec.get("mode", "managed_exec"),
        "elapsed_sec": round(time.time() - t0, 2),
        "blocking": blocking,
        "observations": observations,
        "checks": checks_report,
        "figure_reports": figure_reports,
        "verified_at": time.time(),
    }
