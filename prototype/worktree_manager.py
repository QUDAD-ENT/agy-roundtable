"""
AGY Roundtable v5.4 Native Git Worktree Parallel Write Isolation Manager (Pure Stdlib).

Inspired by BloopAI/vibe-kanban, enables multiple writable specialists to edit code
simultaneously in completely isolated Git worktrees under D:\\gemini\\tmp\\team_worktrees\\
without clobbering each other or the user's active working tree, with safe diff inspection,
conflict-checked merge, and automatic cleanup.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _get_worktree_base_root() -> str:
    env_tmp = os.environ.get("AGY_RUNTIME_TMP") or os.environ.get("TEAM_RUNTIME_TMP")
    if env_tmp:
        return os.path.join(env_tmp, "team_worktrees")
    if os.path.isdir(r"D:\gemini\tmp"):
        return os.path.join(r"D:\gemini\tmp", "team_worktrees")
    return os.path.join(tempfile.gettempdir(), "team_worktrees")


def _run_git(args: List[str], cwd: str, timeout: float = 25.0) -> Tuple[int, str, str]:
    try:
        hk: Dict[str, Any] = {}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            hk = {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            **hk,
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except FileNotFoundError:
        return 127, "", "git executable not found in PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args[:3])} timed out ({timeout}s)"
    except Exception as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"


def detect_git_repo(project_path: str) -> Dict[str, Any]:
    """Check whether project_path is inside a Git repository with a valid HEAD commit."""
    if not project_path or not os.path.isdir(project_path):
        return {"is_git": False, "reason": "目录不存在"}
    rc, out, err = _run_git(["rev-parse", "--show-toplevel"], cwd=project_path)
    if rc != 0 or not out.strip():
        return {"is_git": False, "reason": (err or "非 Git 仓库").strip()[:160]}
    repo_root = os.path.normpath(out.strip())
    rc_h, head_sha, err_h = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if rc_h != 0 or not head_sha.strip():
        return {"is_git": False, "repo_root": repo_root, "reason": "仓库尚无初始提交 (HEAD 不存在)"}
    rc_b, br_name, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    return {
        "is_git": True,
        "repo_root": repo_root,
        "head_commit": head_sha.strip(),
        "current_branch": (br_name or "HEAD").strip(),
    }


def create_step_worktree(
    project_path: str,
    task_id: str,
    step_index: int,
    member_id: str,
    *,
    base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an isolated Git worktree + temporary branch for a writable step."""
    info = detect_git_repo(project_path)
    if not info.get("is_git"):
        raise ValueError(f"无法创建 Git Worktree：{info.get('reason')}")

    repo_root = info["repo_root"]
    head_sha = info["head_commit"]
    safe_tid = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(task_id or "task"))[:32]
    safe_mid = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(member_id or "worker"))[:24]
    rand_tag = uuid.uuid4().hex[:6]
    branch = f"agy-wt/{safe_tid}-s{step_index}-{rand_tag}"

    wt_base = base_root if base_root else _get_worktree_base_root()
    wt_dir = os.path.join(wt_base, safe_tid, f"step_{step_index}_{safe_mid}_{rand_tag}")
    os.makedirs(os.path.dirname(wt_dir), exist_ok=True)

    rc, out, err = _run_git(["worktree", "add", "-b", branch, wt_dir, head_sha], cwd=repo_root)
    if rc != 0:
        raise ValueError(f"git worktree add 失败: {(err or out).strip()[:240]}")

    # Compute relative subpath if project_path is a subdirectory inside repo_root
    rel_sub = os.path.relpath(os.path.realpath(project_path), os.path.realpath(repo_root))
    effective_work_dir = wt_dir if rel_sub in (".", "") else os.path.normpath(os.path.join(wt_dir, rel_sub))

    return {
        "repo_root": repo_root,
        "worktree_path": wt_dir,
        "effective_work_dir": effective_work_dir,
        "branch": branch,
        "base_commit": head_sha,
        "base_branch": info.get("current_branch", "HEAD"),
        "status": "active",
        "created_at": time.time(),
    }


def inspect_worktree_diff(
    worktree_path: str,
    base_commit: str = "",
    *,
    max_diff_chars: int = 24000,
) -> Dict[str, Any]:
    """Read-only inspection of changed/untracked files and unified diff inside a worktree."""
    if not worktree_path or not os.path.isdir(worktree_path):
        return {
            "exists": False,
            "has_changes": False,
            "changed_files": [],
            "diff_stat": "",
            "diff_text": "",
            "commits_ahead": 0,
        }

    rc_s, status_out, _ = _run_git(["status", "--porcelain"], cwd=worktree_path)
    changed_files: List[Dict[str, str]] = []
    untracked_paths: List[str] = []
    if rc_s == 0:
        for ln in status_out.splitlines():
            if len(ln) < 4:
                continue
            code = ln[:2].strip()
            rel_p = ln[3:].strip().strip('"')
            if not rel_p:
                continue
            changed_files.append({"status": code or "M", "path": rel_p.replace("\\", "/")})
            if code == "??":
                untracked_paths.append(rel_p)

    commits_ahead = 0
    diff_ref = base_commit.strip() if base_commit else "HEAD"
    if base_commit:
        rc_c, cnt_out, _ = _run_git(["rev-list", "--count", f"{base_commit}..HEAD"], cwd=worktree_path)
        if rc_c == 0 and cnt_out.strip().isdigit():
            commits_ahead = int(cnt_out.strip())

    rc_st, stat_out, _ = _run_git(["diff", "--stat", diff_ref], cwd=worktree_path)
    rc_df, diff_out, _ = _run_git(["diff", diff_ref], cwd=worktree_path)

    # Also capture committed file list if agent committed directly in worktree
    if commits_ahead > 0 and base_commit:
        rc_nm, nm_out, _ = _run_git(["diff", "--name-status", base_commit, "HEAD"], cwd=worktree_path)
        if rc_nm == 0:
            existing_p = {c["path"] for c in changed_files}
            for ln in nm_out.splitlines():
                parts = ln.split("\t", 1)
                if len(parts) == 2 and parts[1].replace("\\", "/") not in existing_p:
                    changed_files.append({"status": parts[0].strip(), "path": parts[1].replace("\\", "/")})
                    existing_p.add(parts[1].replace("\\", "/"))

    # Append compact preview of untracked text files so diff view is complete
    extra_parts: List[str] = []
    for up in untracked_paths[:10]:
        abs_u = os.path.join(worktree_path, up)
        if os.path.isfile(abs_u) and os.path.getsize(abs_u) <= 64_000:
            try:
                with open(abs_u, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read(3000)
                if "\x00" not in txt[:256]:
                    extra_parts.append(f"+++ new file: {up}\n" + "\n".join("+" + l for l in txt.splitlines()[:60]))
            except OSError:
                pass

    full_diff = (diff_out or "").strip()
    if extra_parts:
        full_diff = (full_diff + "\n\n" + "\n\n".join(extra_parts)).strip()
    truncated = len(full_diff) > max_diff_chars
    if truncated:
        full_diff = full_diff[:max_diff_chars] + f"\n…[diff 已截断，共 {len(diff_out)} 字符]"

    return {
        "exists": True,
        "has_changes": bool(changed_files or commits_ahead > 0),
        "changed_files": changed_files[:60],
        "diff_stat": (stat_out or "").strip(),
        "diff_text": full_diff,
        "diff_truncated": truncated,
        "commits_ahead": commits_ahead,
    }


def merge_worktree_to_main(
    repo_root: str,
    worktree_path: str,
    branch: str,
    *,
    commit_message: str = "AGY Roundtable worktree merge",
) -> Dict[str, Any]:
    """
    Safely commit any pending changes inside the isolated worktree onto its temp branch,
    verify the main working tree has no overlapping dirty edits, and merge with --no-ff.
    On conflict, aborts merge cleanly so the user's main repo is NEVER left broken!
    """
    if not os.path.isdir(repo_root):
        raise ValueError(f"主仓库目录不存在: {repo_root}")
    if not os.path.isdir(worktree_path):
        raise ValueError(f"Worktree 目录不存在: {worktree_path}")

    # 1. Stage & commit uncommitted work inside the isolated worktree
    rc_s, st_wt, _ = _run_git(["status", "--porcelain"], cwd=worktree_path)
    if rc_s == 0 and st_wt.strip():
        rc_a, _, err_a = _run_git(["add", "-A"], cwd=worktree_path)
        if rc_a != 0:
            raise ValueError(f"Worktree git add 失败: {err_a.strip()[:200]}")
        rc_c, out_c, err_c = _run_git(
            [
                "-c", "user.name=AGY Roundtable",
                "-c", "user.email=agy-roundtable@local",
                "commit", "-m", commit_message,
            ],
            cwd=worktree_path,
        )
        if rc_c != 0 and "nothing to commit" not in (out_c + err_c).lower():
            raise ValueError(f"Worktree 提交失败: {(err_c or out_c).strip()[:200]}")

    # 2. Protect uncommitted edits in the main working tree!
    rc_ms, main_st, _ = _run_git(["status", "--porcelain"], cwd=repo_root)
    if rc_ms == 0 and main_st.strip():
        raise ValueError("主仓库当前有未提交的本地修改，为防覆盖冲突，请先 commit 或 stash 后再一键合并 Worktree。")

    # 3. Merge temp branch into main repo cleanly
    rc_m, out_m, err_m = _run_git(
        [
            "-c", "user.name=AGY Roundtable",
            "-c", "user.email=agy-roundtable@local",
            "merge", "--no-ff", branch, "-m", commit_message,
        ],
        cwd=repo_root,
    )
    if rc_m != 0:
        _run_git(["merge", "--abort"], cwd=repo_root)
        raise ValueError(f"合并存在冲突，已自动回滚 (git merge --abort)：{(err_m or out_m).strip()[:240]}")

    rc_h, new_head, _ = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    return {
        "merged": True,
        "branch": branch,
        "new_head": new_head.strip() if rc_h == 0 else "",
        "summary": (out_m or "合并成功").strip()[:300],
    }


def cleanup_worktree(
    repo_root: str,
    worktree_path: str,
    branch: str = "",
) -> Dict[str, Any]:
    """Remove isolated worktree directory and temporary branch cleanly."""
    removed_wt = False
    removed_br = False
    notes: List[str] = []

    if os.path.isdir(repo_root) and worktree_path:
        rc, out, err = _run_git(["worktree", "remove", "--force", worktree_path], cwd=repo_root)
        if rc == 0:
            removed_wt = True
        else:
            notes.append((err or out).strip()[:160])
        _run_git(["worktree", "prune"], cwd=repo_root)

    if worktree_path and os.path.isdir(worktree_path):
        try:
            shutil.rmtree(worktree_path, ignore_errors=True)
            removed_wt = True
        except Exception as exc:
            notes.append(str(exc))

    if os.path.isdir(repo_root) and branch:
        rc_b, out_b, err_b = _run_git(["branch", "-D", branch], cwd=repo_root)
        if rc_b == 0:
            removed_br = True
        else:
            notes.append((err_b or out_b).strip()[:160])

    return {
        "removed_worktree": removed_wt,
        "removed_branch": removed_br,
        "notes": [n for n in notes if n],
    }
