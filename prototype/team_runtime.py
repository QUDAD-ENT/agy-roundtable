"""
Team Collaboration Runtime & Orchestrator v4:
Supports 4 Anthropomorphic Execution Topologies + Live Inter-Agent @Mention Consultation:
1. 'roundtable'     — 群聊圆桌自主协作 (Host framing -> Parallel proposals -> Cross-critique -> Synthesis)
2. 'chief_dispatch' — 总管自动点将编排 (Chief inspects project & roster -> Dynamic parallel/serial DAG -> Sign-off)
3. 'custom_dag'     — 自定义多步串并行搭积木 (User-defined multi-stage parallel/serial/review swimlanes)
4. 'single_member'  — 单人极速专办 (1-on-1 specialist desk; plus backward-compatible 'coordinator_auto')
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agy_adapter import AgyAdapter
from codex_adapter import CodexAdapter, CodexExecutionResult, get_runtime_dirs
from context_engine import (
    BlobStore,
    ContextBudgetConfig,
    build_handoff_checkpoint,
    classify_turn_error,
    compile_turn_prompt,
    estimate_tokens,
    extract_deterministic_anchors,
    is_context_overflow_error,
    mask_observation_blocks,
    normalize_usage,
    seal_round_snapshot,
    select_jit_playbooks,
)
from figure_qa import inspect_any_figure, probe_plotting_backend, sha256_file, verify_figure_manifest
from memory_index import ProjectMemoryIndex, build_structural_repomap
from openscience_playbooks import auto_select_playbooks, get_playbooks_catalog, render_playbooks_prompt
from project_discovery import browse_directory, discover_projects, pick_folder_native_dialog
from provenance_store import TaskProvenanceStore
from research_verifier import recompute_signoff_summary, run_acceptance_checks, snapshot_readonly_inputs
from team_state_manager import (
    SPECIALIST_TEMPLATES,
    STATE_FILE_PATH,
    TeamStateManager,
    enrich_member_metadata,
    normalize_win_path,
)
from worktree_manager import (
    cleanup_worktree,
    create_step_worktree,
    detect_git_repo,
    inspect_worktree_diff,
    merge_worktree_to_main,
)


SUPPORTED_CLIENTS = ("codex", "antigravity")
_SANDBOX_RANK = {"read-only": 0, "workspace-write": 1}

_QUOTA_ERROR_PATTERNS = (
    "quota",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "resource_exhausted",
    "resource exhausted",
    "insufficient_quota",
    "usage limit",
    "usage_limit",
    "credit balance",
    "out of credits",
    "too many requests",
    "capacity",
    "overloaded",
    "额度",
    "配额",
    "限流",
)

_FLAGSHIP_BACKUP_PRIORITY: Dict[str, List[str]] = {
    "antigravity": [
        "gemini-4-neon",
        "gemini-4-carbon",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "gemini-4-hydrogen",
    ],
    "codex": [
        "gpt-6.2-sol",
        "gpt-6.2-astra",
    ],
}


def _is_quota_or_rate_limit_error(error_text: Optional[str], stderr_lines: Optional[List[str]] = None) -> bool:
    """Detect model quota exhaustion, 429 rate-limiting, or capacity errors (strictly AFTER excluding context overflow!)."""
    if is_context_overflow_error(error_text, stderr_lines):
        return False
    kind, _ = classify_turn_error(error_text, stderr_lines)
    if kind == "quota_rate_limit":
        return True
    parts = [str(error_text or "")]
    if stderr_lines:
        parts.extend(str(x) for x in stderr_lines[-12:])
    combined = "\n".join(parts).lower()
    if not combined.strip():
        return False
    return any(pat in combined for pat in _QUOTA_ERROR_PATTERNS)


def _strip_model_parens(name: str) -> str:
    """Strip trailing '(Model Name)' or '[替补 · ...]' suffix so backup names stay clean."""
    s = re.sub(r"\s*\[(?:替补|Backup)[^\]]*\]\s*$", "", (name or "").strip())
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    return s or (name or "").strip()


def _short_model_id_token(model_slug: str) -> str:
    tok = re.sub(r"[^a-z0-9]+", "_", (model_slug or "alt").lower()).strip("_")
    return tok[:18] or "alt"


def _effective_sandbox(ceiling: Optional[str], requested: Optional[str], read_only: bool = False) -> str:
    """P0-0 Hard Sandbox Trust Root: strictly allow only read-only / workspace-write and clamp by ceiling."""
    ceil_val = (ceiling or "workspace-write").strip().lower()
    if ceil_val not in _SANDBOX_RANK:
        raise ValueError(f"非法上限 sandbox 策略: {ceil_val}")
    req_val = (requested or ceil_val).strip().lower()
    if req_val not in _SANDBOX_RANK:
        raise ValueError(f"非法 sandbox 策略（仅允许 read-only / workspace-write，拒绝 {req_val}）")
    if read_only:
        return "read-only"
    return "read-only" if min(_SANDBOX_RANK[ceil_val], _SANDBOX_RANK[req_val]) == 0 else "workspace-write"


def _resolve_workdir(proj_root: str, rel_scope: Optional[str]) -> str:
    """P0-0 Path-Escape Guard: resolve rel_scope under proj_root and reject .., absolute, cross-drive & symlink escapes."""
    root_real = os.path.realpath(os.path.abspath(proj_root))
    raw_rel = (rel_scope or ".").strip() or "."
    if os.path.isabs(raw_rel):
        raise ValueError(f"workspace_scope 禁止使用绝对路径: {raw_rel}")
    cand = os.path.realpath(os.path.join(root_real, raw_rel))
    try:
        common = os.path.commonpath([os.path.normcase(root_real), os.path.normcase(cand)])
    except ValueError:
        raise ValueError(f"workspace_scope 跨盘符越界: {raw_rel}")
    if common != os.path.normcase(root_real):
        raise ValueError(f"workspace_scope 越出绑定项目目录: {raw_rel}")
    return cand


def _write_sidecar_ledger(task_id: str, attempt_id: str, payload: Dict[str, Any]) -> str:
    """P0-2 Lock-free sidecar JSONL ledger so team_state.json stays compact without O(N^2) bloat."""
    tmp_dir, _ = get_runtime_dirs()
    ledger_dir = os.path.join(tmp_dir, "team_ledgers", task_id)
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, f"{attempt_id}.jsonl")
    with open(ledger_path, "w", encoding="utf-8") as lf:
        lf.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return ledger_path


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _extract_json_obj(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"```(?:json|chief_plan)?\s*(\{.*?\})\s*```", stripped, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(stripped[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def _extract_team_consult(text: str, valid_members: Dict[str, Any], self_id: str) -> Tuple[Optional[Dict[str, str]], str]:
    """Extract ```team_consult {"to": "member_id", "question": "...", "claim_to_verify": "...", "evidence_needed": "..."} ``` block."""
    if not text:
        return None, ""
    m = re.search(r"```team_consult\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            to_id = str(obj.get("to") or "").strip().lstrip("@")
            q_txt = str(obj.get("question") or "").strip()
            claim_txt = str(obj.get("claim_to_verify") or obj.get("claim") or "").strip()
            ev_txt = str(obj.get("evidence_needed") or obj.get("evidence") or "").strip()
            # Match by ID or display name
            resolved_id = None
            if to_id in valid_members and to_id != self_id:
                resolved_id = to_id
            else:
                for mid, minfo in valid_members.items():
                    if mid != self_id and minfo.get("name") == to_id:
                        resolved_id = mid
                        break
            if not q_txt and claim_txt:
                q_txt = f"请独立核验以下声明：{claim_txt}" + (f"（所需证据：{ev_txt}）" if ev_txt else "")
            if resolved_id and q_txt:
                cleaned = (text[: m.start()] + text[m.end() :]).strip()
                return {
                    "to": resolved_id,
                    "question": q_txt,
                    "claim_to_verify": claim_txt,
                    "evidence_needed": ev_txt,
                }, cleaned
        except Exception:
            pass
    return None, text


def _build_model_family_tuning(client: str, model: str) -> str:
    """Return model-family-specific prompt dialect calibration distilled from OpenScience & empirical benchmarks."""
    low_m = (model or "").lower()
    low_c = (client or "").lower()

    if "claude" in low_m:
        return (
            "<dialect_calibration model_family=\"claude\">\n"
            "- 发挥你最擅长的深度批判性审稿、方法学纠偏与高密度学术表达：结论前置，避免客套铺垫与空泛免责声明。\n"
            "- 发现缺陷时必须明确区分 `BLOCKING`（阻断结论成立的硬伤）与 `OBSERVATION`（优化建议），并先 Steelman 对方最强逻辑再反驳。\n"
            "- 遵循 OpenScience 原则：严禁以“无法确定/观察性数据无法推断”为借口回避机理与转化解读；请在给出客观实证后，显式以 `[机制假说/深度外推]` 提出可证伪机制与下一步判别实验。\n"
            "</dialect_calibration>"
        )
    if "hydrogen" in low_m or "flash" in low_m:
        return (
            "【模型族协同调优 · Gemini 4 Hydrogen 极速勘察模式】\n"
            "- 发挥极速多文件并行扫描优势（借鉴 OpenScience explore.txt）：优先并行读取关键文件、数据表与核心符号，输出紧凑的《证据索引卡》。\n"
            "- 严格逐条给出精确路径、行号、原样数值与单位，明确区分 `[实证观察]`、推断与证据缺口；零过程废话，文末给出 1~3 条最关键发现与最小下一步动作。"
        )
    if "carbon" in low_m:
        return (
            "【模型族协同调优 · Gemini 4 Carbon 顶刊绘图架构与视觉质检模式】\n"
            "- 发挥高密度多模态/空间版面推理与科研绘图架构优势（对齐 Nature/Science/Cell 投稿规范）：逐项审查图件版面网格（单栏 89mm / 双栏 183mm）、字体层级（≥6pt/7pt 最终尺寸）、色盲友好调色板（Okabe-Ito / viridis / 灰度可辨）、纯白底板对比度、误差棒与 n 值标注完整性。\n"
            "- 严查图数一致性：交叉比对图件坐标轴/显著性星号与原始冻结 CSV/TSV 中的 `p_adj`/`FDR`/组别标签，明确区分 `BLOCKING`（阻断投稿的数据错位、DPI 伪造、字号重叠）与 `OBSERVATION`（审美与排版优化）。"
        )
    if "gemini" in low_m or (low_c == "antigravity" and "oss" not in low_m):
        return (
            "【模型族协同调优 · Gemini 旗舰广域综合与假说演化模式】\n"
            "- 发挥超长上下文全局映射与跨学科综合优势（借鉴 OpenScience brainstorming/hypotheses + AI Co-Scientist）：先测绘全局事实与矛盾点，再使用 5 类创新算子（Gap / Transfer / Inversion / Constraint / Failure）生成正交备选路线。\n"
            "- 综合多方观点时请输出清晰的【多维仲裁矩阵】，在收敛主推荐方案的同时保留具有独特机理价值的亚军路径与最小判别实验，切勿抹平多样性。"
        )
    if "astra" in low_m:
        return (
            "【模型族协同调优 · GPT-6.2-Astra OpenScience 行动执行与高密核验模式 (gpt-astra.txt)】\n"
            "- 发挥 GPT-6.2-Astra 高自主深度执行优势：结论先行、并行执行独立只读/计算工具调用、改前必读文件；不写“不是X而是Y”等空洞铺垫，不做半途敷衍的“够用就行”答案。\n"
            "- 遵循 OpenScience specialist 交接格式（仅保留有实质内容的节）：Outcome / Findings / Evidence / Changes & Outputs / Verification（精确命令+退出码+所证结论）/ Limitations / Next Action。"
        )
    return (
        "【模型族协同调优 · Codex / GPT-6.2-Sol 工具先行与机械验收模式 (codex.txt + acceptance-checks)】\n"
        "- 发挥精确代码/数据执行与机械核验优势：先读文件、并行检索、对关键数值/边界条件做独立复算，绝不凭直觉猜测。\n"
        "- 工程与分析坚持非破坏性重跑与最小精准改动原则（不覆盖原数据目录）；修改或计算后必须复跑验收检查并明确区分已实测验证区间与未覆盖边界。"
    )


def _extract_ai_questions(text: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    if not text:
        return None, ""
    m = re.search(r"```team_questions\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            qs = _normalize_questions_list(data.get("questions"))
            if qs:
                cleaned = (text[: m.start()] + text[m.end() :]).strip()
                return qs, cleaned or "请根据下方卡片确认你的选项后继续执行。"
        except Exception:
            pass

    parsed = _extract_json_obj(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list) and "verdict" not in parsed and "steps" not in parsed:
        qs = _normalize_questions_list(parsed.get("questions"))
        if qs:
            cleaned = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", text, flags=re.S).strip()
            if cleaned.startswith("{") and cleaned.endswith("}"):
                cleaned = ""
            return qs, cleaned or "AI 需要先确认以下选项再继续执行："

    if "跳过了选择" in text:
        return [
            {
                "id": "q_fallback",
                "question": "AI 刚才尝试发起交互选择但被命令行无头模式跳过，请在下方补充你的偏好或直接让 AI 按推荐项继续：",
                "multi_select": False,
                "options": ["(推荐) 按当前项目最稳妥的标准方案自动决定并继续"],
            }
        ], text

    return None, text


def _normalize_questions_list(raw_qs: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_qs, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, q in enumerate(raw_qs[:6]):
        if not isinstance(q, dict):
            continue
        q_text = str(q.get("question") or q.get("title") or "").strip()
        if not q_text:
            continue
        raw_opts = q.get("options") or []
        opts = [str(o).strip() for o in raw_opts if str(o).strip()][:8]
        if not opts:
            opts = ["(推荐) 按推荐方案继续", "暂不采用，见下方补充说明"]
        out.append(
            {
                "id": str(q.get("id") or f"q{idx + 1}"),
                "question": q_text,
                "multi_select": bool(q.get("multi_select") or q.get("is_multi_select", False)),
                "options": opts,
            }
        )
    return out


def _scopes_overlap(scope_a: List[str], scope_b: List[str], project_root: str) -> bool:
    if not scope_a or not scope_b:
        return True
    for a in scope_a:
        if a in ("*", "."):
            return True
        pa = normalize_win_path(os.path.join(project_root, a))
        for b in scope_b:
            if b in ("*", "."):
                return True
            pb = normalize_win_path(os.path.join(project_root, b))
            if pa == pb or pa.startswith(pb + os.sep) or pb.startswith(pa + os.sep):
                return True
    return False


def _smart_clip(text: str, limit: int = 8000) -> str:
    if not text or len(text) <= limit:
        return text
    head = int(limit * 0.68)
    tail = limit - head
    return (
        text[:head]
        + f"\n\n...[中间省略 {len(text) - limit} 字，前表与末尾结论均已完整保留]...\n\n"
        + text[-tail:]
    )


def _extract_peer_ranking(text: str) -> Optional[Dict[str, Any]]:
    """Extract optional ```peer_ranking {...}``` Borda ranking JSON from Round 2+ anonymous critique."""
    if not text:
        return None
    m = re.search(r"```peer_ranking\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    obj = None
    if m:
        try:
            obj = json.loads(m.group(1))
        except Exception:
            obj = None
    if not isinstance(obj, dict):
        return None
    ranking = obj.get("ranking")
    if not isinstance(ranking, list) or not ranking:
        return None
    clean_rank = [str(x).strip() for x in ranking if str(x).strip()]
    if not clean_rank:
        return None
    try:
        conf = float(obj.get("confidence", 0.75))
        conf = max(0.0, min(1.0, conf))
    except Exception:
        conf = 0.75
    return {
        "ranking": clean_rank[:12],
        "confidence": round(conf, 3),
        "rationale": str(obj.get("rationale") or "").strip()[:280],
    }


def _compute_borda_scoreboard(task: Dict[str, Any], members: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministically tally Borda Count scores across anonymized Round 2+ peer rankings."""
    meta = task.get("mode_meta") or {}
    anon_map: Dict[str, str] = dict(meta.get("anon_map") or {})
    anon_rev: Dict[str, str] = {v.strip().lower(): k for k, v in anon_map.items()}
    participants: List[str] = list(meta.get("participants") or list(anon_map.keys()))
    n_seats = max(2, len(participants))

    points: Dict[str, float] = {mid: 0.0 for mid in participants}
    first_place: Dict[str, int] = {mid: 0 for mid in participants}
    votes_received: Dict[str, int] = {mid: 0 for mid in participants}
    confs: List[float] = []

    ballots = [b for b in (task.get("peer_rankings") or []) if isinstance(b, dict)]
    for blt in ballots:
        voter_id = blt.get("voter_id")
        confs.append(float(blt.get("confidence", 0.75)))
        seen_in_ballot: set = set()
        rank_pos = 0
        for raw_label in blt.get("ranking") or []:
            lbl = str(raw_label).strip()
            low = lbl.lower()
            target_mid = anon_rev.get(low)
            if not target_mid:
                # Also tolerate "Seat A" / "A" / direct member_id
                for albl, mid_k in anon_rev.items():
                    letter = albl.split()[-1] if albl else ""
                    if low == letter or low == f"seat {letter}" or low == mid_k.lower():
                        target_mid = mid_k
                        break
            if not target_mid or target_mid not in points or target_mid in seen_in_ballot:
                continue
            # Prevent self-voting inflation in Borda tally!
            if target_mid == voter_id:
                continue
            seen_in_ballot.add(target_mid)
            pts = max(1, n_seats - rank_pos)
            points[target_mid] += pts
            votes_received[target_mid] += 1
            if rank_pos == 0:
                first_place[target_mid] += 1
            rank_pos += 1

    rows: List[Dict[str, Any]] = []
    for mid in participants:
        m = members.get(mid) or {}
        rows.append(
            {
                "member_id": mid,
                "name": m.get("name") or mid,
                "model": m.get("model") or "",
                "anon_label": anon_map.get(mid, ""),
                "borda_points": int(points[mid]),
                "first_place_votes": int(first_place[mid]),
                "votes_received": int(votes_received[mid]),
            }
        )
    rows.sort(key=lambda r: (-r["borda_points"], -r["first_place_votes"], r["member_id"]))
    return {
        "ballots_count": len(ballots),
        "mean_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
        "anonymous_enabled": bool(meta.get("anonymous_peer_review")),
        "rows": rows,
        "winner_member_id": rows[0]["member_id"] if rows and rows[0]["borda_points"] > 0 else None,
        "updated_at": time.time(),
    }


def _extract_visual_qa_verdict(text: str) -> Dict[str, Any]:
    """Fail-Closed parser for VLM Visual Figure Critique JSON (PASS | REVISE | INVALID_FORMAT)."""
    parsed = _extract_json_obj(text)
    if not isinstance(parsed, dict):
        return {
            "verdict": "INVALID_FORMAT",
            "visual_score": 0.0,
            "defects": [{"severity": "BLOCKING", "region": "format", "issue": "视觉质检输出缺少合法 JSON verdict", "fix": "请返回 PASS/REVISE JSON"}],
            "summary": (text or "")[:240],
        }
    verdict = str(parsed.get("verdict") or "").strip().upper()
    if verdict not in ("PASS", "REVISE"):
        return {
            "verdict": "INVALID_FORMAT",
            "visual_score": 0.0,
            "defects": [{"severity": "BLOCKING", "region": "verdict", "issue": f"非法视觉判定: {verdict}", "fix": "verdict 必须为 PASS 或 REVISE"}],
            "summary": str(parsed.get("summary") or text[:200]),
        }
    try:
        score = float(parsed.get("visual_score", 8.0 if verdict == "PASS" else 5.0))
        score = max(0.0, min(10.0, round(score, 2)))
    except Exception:
        score = 8.0 if verdict == "PASS" else 5.0
    raw_defs = parsed.get("defects") if isinstance(parsed.get("defects"), list) else []
    clean_defs: List[Dict[str, str]] = []
    has_blocking = False
    for d in raw_defs[:12]:
        if isinstance(d, dict):
            sev = str(d.get("severity") or "MINOR").strip().upper()
            if sev not in ("BLOCKING", "MINOR"):
                sev = "BLOCKING" if "block" in sev.lower() else "MINOR"
            if sev == "BLOCKING":
                has_blocking = True
            clean_defs.append(
                {
                    "severity": sev,
                    "region": str(d.get("region") or "figure").strip()[:80],
                    "issue": str(d.get("issue") or "").strip()[:240],
                    "fix": str(d.get("fix") or "").strip()[:240],
                }
            )
    if has_blocking:
        verdict = "REVISE"
    return {
        "verdict": verdict,
        "visual_score": score,
        "defects": clean_defs,
        "summary": str(parsed.get("summary") or "").strip()[:300],
    }


class TeamOrchestrator:
    def __init__(
        self,
        state_file: str = STATE_FILE_PATH,
        adapter: Optional[CodexAdapter] = None,
        agy_adapter: Optional[AgyAdapter] = None,
        auto_start_scheduler: bool = True,
    ):
        self.store = TeamStateManager(state_file=state_file)
        self.adapter = adapter or CodexAdapter()
        self.agy_adapter = agy_adapter or AgyAdapter()
        self._sched_lock = threading.RLock()
        self._wakeup = threading.Event()
        self._stop_event = threading.Event()
        self._running_attempts: Dict[str, Dict[str, Any]] = {}
        self._scheduler_thread: Optional[threading.Thread] = None
        self._plotting_backends_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        self._figure_cache_lock = threading.RLock()
        self._figure_inspect_cache: Dict[Tuple[str, int, int], Tuple[Dict[str, Any], str]] = {}
        if auto_start_scheduler:
            self.start_scheduler()

    def start_scheduler(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="TeamScheduler", daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._stop_event.set()
        self._wakeup.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=2.0)
        try:
            self.store.close()
        except Exception:
            pass

    def touch_coordinator_heartbeat(self, source: str = "antigravity_native") -> None:
        def _upd(st: Dict[str, Any]) -> None:
            st["binding"]["last_coordinator_heartbeat"] = time.time()
            st["binding"]["last_coordinator_source"] = source

        self.store.mutate(_upd, persist="debounced")

    def _append_room_chat(
        self,
        st: Dict[str, Any],
        task: Dict[str, Any],
        sender_id: str,
        kind: str,
        content: str,
        to_member_id: Optional[str] = None,
        step_index: Optional[int] = None,
        round_label: str = "",
    ) -> Dict[str, Any]:
        if not content or not content.strip():
            return {}
        mem = st.get("members", {}).get(sender_id, {})
        role_name = st.get("roles", {}).get(mem.get("role", ""), {}).get("name", "总管/用户" if sender_id == "user" else "成员")
        to_name = st.get("members", {}).get(to_member_id, {}).get("name", to_member_id) if to_member_id else None
        entry = {
            "id": _new_id("CHAT"),
            "ts": time.time(),
            "sender_id": sender_id,
            "sender_name": "项目负责人 (你)" if sender_id == "user" else (mem.get("name") or sender_id),
            "sender_role": "负责人" if sender_id == "user" else role_name,
            "sender_client": "human" if sender_id == "user" else mem.get("client", "codex"),
            "sender_model": "" if sender_id == "user" else f"{mem.get('model', '')}/{mem.get('reasoning_effort', '')}",
            "kind": kind,
            "to_member_id": to_member_id,
            "to_member_name": to_name,
            "step_index": step_index,
            "round_label": round_label,
            "content": content.strip(),
        }
        task.setdefault("chat_room", []).append(entry)
        try:
            # Seal Blind Round 1 disk side-channel: do not write Round 1 peer speeches/consults into
            # the shared markdown transcript file until ALL Round 1 roundtable steps have succeeded!
            r1_still_blind = (
                task.get("execution_mode") == "roundtable"
                and any(
                    s.get("step_type") == "discussion"
                    and int(s.get("stage_index") or 0) == 1
                    and s.get("status") != "succeeded"
                    for s in task.get("steps", [])
                )
            )
            tmp_dir, _ = get_runtime_dirs()
            rooms_dir = os.path.join(tmp_dir, "team_rooms")
            os.makedirs(rooms_dir, exist_ok=True)
            room_md_path = os.path.join(rooms_dir, f"{task.get('id', 'task')}.md")
            md_lines = [f"# 协作任务实时群聊完整纪要：{task.get('title', '')}\n"]
            for cm in task.get("chat_room", []):
                if r1_still_blind and cm.get("kind") not in ("user_goal", "user_interjection", "user_choice", "ruling"):
                    if not (cm.get("step_index") == 0 and (task.get("mode_meta") or {}).get("enable_host_opening")):
                        continue
                to_s = f" -> @{cm['to_member_name']}" if cm.get("to_member_name") else ""
                md_lines.append(f"## [{cm.get('round_label') or cm.get('kind')}] {cm.get('sender_name')}{to_s}\n\n{cm.get('content', '')}\n")
            with open(room_md_path, "w", encoding="utf-8") as rf:
                rf.write("\n---\n".join(md_lines))
        except Exception:
            pass
        return entry

    # ------------------------------------------------------------------
    # Capabilities & Free Project Management
    # ------------------------------------------------------------------

    def get_capabilities(self, refresh_cli: bool = False) -> Dict[str, Any]:
        codex_caps = self.adapter.discover_capabilities(refresh_cli=refresh_cli)
        agy_caps = self.agy_adapter.discover_capabilities(refresh_cli=refresh_cli)
        return {
            "cli": codex_caps["cli"],
            "models": codex_caps["models"],
            "clients": {
                "codex": codex_caps,
                "antigravity": agy_caps,
            },
        }

    def _validate_client_model_effort(self, client: str, model: Optional[str], effort: Optional[str]) -> None:
        if client == "codex":
            ok, err, _ = self.adapter.validate_model_effort(model, effort)
            if not ok:
                raise ValueError(err or "无效的 Codex 模型或思考强度组合")
        elif client == "antigravity":
            ok, err, _, _ = self.agy_adapter.resolve_full_model_slug(model, effort)
            if not ok:
                raise ValueError(err or "无效的 Antigravity 模型或思考强度组合")
        else:
            raise ValueError(f"不支持的客户端类型: {client}")

    def list_projects(self) -> Dict[str, Any]:
        snap = self.store.snapshot()
        cat = discover_projects(recent_paths=snap["binding"].get("recent_projects", []))
        cat["current_project_path"] = snap["binding"]["project_path"]
        cat["current_project_name"] = snap["binding"].get("project_name")
        return cat

    def switch_project(self, project_path: str) -> Dict[str, Any]:
        if not project_path or not project_path.strip():
            raise ValueError("项目路径不能为空")
        abs_p = os.path.normpath(os.path.abspath(project_path.strip()))
        if not os.path.isdir(abs_p):
            raise ValueError(f"所选项目目录不存在: {abs_p}")
        norm_p = normalize_win_path(abs_p)

        def _sw(st: Dict[str, Any]) -> Dict[str, Any]:
            b = st["binding"]
            b["project_path"] = abs_p
            b["project_name"] = os.path.basename(abs_p) or abs_p
            b["normalized_project_path"] = norm_p
            b["path_verified"] = True
            b["verified_at"] = time.time()

            rec = [p for p in b.get("recent_projects", []) if normalize_win_path(p) != norm_p]
            rec.insert(0, abs_p)
            b["recent_projects"] = rec[:16]
            b["antigravity_project_id"] = b.setdefault("project_agy_map", {}).get(norm_p, "")

            proj_active = [
                t for t in st.get("tasks", [])
                if not t.get("archived", False) and normalize_win_path(t.get("project_path", "")) == norm_p
            ]
            st["active_task_id"] = proj_active[-1]["id"] if proj_active else None
            return {"binding": b, "active_task_id": st["active_task_id"]}

        return self.store.mutate(_sw)

    def browse_fs(self, path: Optional[str] = None) -> Dict[str, Any]:
        return browse_directory(path)

    def pick_project_native(self) -> Dict[str, Any]:
        cur = self.store.snapshot()["binding"].get("project_path")
        picked = pick_folder_native_dialog(cur)
        if not picked.get("cancelled") and picked.get("path"):
            sw = self.switch_project(picked["path"])
            return {"cancelled": False, "path": picked["path"], **sw}
        return picked

    def update_binding(
        self,
        project_path: Optional[str] = None,
        remote_control_url: Optional[str] = None,
        max_concurrency: Optional[int] = None,
        default_max_auto_repairs: Optional[int] = None,
        auto_spawn_quota_backup: Optional[bool] = None,
        context_budget_chars: Optional[int] = None,
        context_session_mode: Optional[str] = None,
        auto_context_rollover: Optional[bool] = None,
        max_turns_per_session: Optional[int] = None,
        enable_cross_task_memory: Optional[bool] = None,
        enable_ast_repomap: Optional[bool] = None,
        enable_git_worktree_isolation: Optional[bool] = None,
        task_token_budget: Optional[int] = None,
        max_sandbox: Optional[str] = None,
        default_task_sandbox: Optional[str] = None,
        roundtable_allow_member_writes: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if project_path is not None:
            self.switch_project(project_path)

        def _upd(st: Dict[str, Any]) -> Dict[str, Any]:
            b = st["binding"]
            s = st.setdefault("settings", {})
            if remote_control_url is not None:
                b["remote_control_url"] = remote_control_url.strip()
            if max_sandbox is not None:
                ms_val = str(max_sandbox).strip().lower()
                if ms_val not in _SANDBOX_RANK:
                    raise ValueError(f"非法全局文件权限上限 max_sandbox（仅允许 read-only / workspace-write）: {ms_val}")
                b["max_sandbox"] = ms_val
                if ms_val == "read-only":
                    s["default_task_sandbox"] = "read-only"
            if default_task_sandbox is not None:
                ds_val = str(default_task_sandbox).strip().lower()
                if ds_val not in _SANDBOX_RANK:
                    raise ValueError(f"非法默认任务权限 default_task_sandbox: {ds_val}")
                b_max = b.get("max_sandbox", "workspace-write")
                s["default_task_sandbox"] = _effective_sandbox(b_max, ds_val)
            if roundtable_allow_member_writes is not None:
                s["roundtable_allow_member_writes"] = bool(roundtable_allow_member_writes)
            if max_concurrency is not None:
                mc = max(1, min(12, int(max_concurrency)))
                st["max_concurrency"] = mc
                s["max_concurrency"] = mc
            if default_max_auto_repairs is not None:
                mr = max(0, min(5, int(default_max_auto_repairs)))
                s["default_max_auto_repairs"] = mr
            if auto_spawn_quota_backup is not None:
                s["auto_spawn_quota_backup"] = bool(auto_spawn_quota_backup)
            if context_budget_chars is not None:
                s["context_budget_chars"] = max(12_000, min(160_000, int(context_budget_chars)))
            if context_session_mode is not None:
                cm = str(context_session_mode).strip().lower()
                if cm in ("bounded_native", "stateless_replay"):
                    s["context_session_mode"] = cm
            if auto_context_rollover is not None:
                s["auto_context_rollover"] = bool(auto_context_rollover)
            if max_turns_per_session is not None:
                s["max_turns_per_session"] = max(3, min(30, int(max_turns_per_session)))
            if enable_cross_task_memory is not None:
                s["enable_cross_task_memory"] = bool(enable_cross_task_memory)
            if enable_ast_repomap is not None:
                s["enable_ast_repomap"] = bool(enable_ast_repomap)
            if enable_git_worktree_isolation is not None:
                s["enable_git_worktree_isolation"] = bool(enable_git_worktree_isolation)
            if task_token_budget is not None:
                tb = int(task_token_budget)
                s["task_token_budget"] = 0 if tb <= 0 else max(50_000, min(20_000_000, tb))
            return b

        res = self.store.mutate(_upd)
        self._wakeup.set()
        return res

    def set_members_sandbox(
        self,
        sandbox: str,
        member_id: Optional[str] = None,
        client: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Configure file-edit permission (read-only / workspace-write) for a member or an entire engine (e.g. 'codex')."""
        sb_req = str(sandbox or "").strip().lower()
        if sb_req not in _SANDBOX_RANK:
            raise ValueError(f"非法 sandbox 权限（仅允许 read-only / workspace-write）: {sb_req}")

        def _upd(st: Dict[str, Any]) -> Dict[str, Any]:
            b_max = st.get("binding", {}).get("max_sandbox") or "workspace-write"
            if sb_req == "workspace-write" and b_max == "read-only":
                raise ValueError("当前全局文件权限上限已锁定为只读 (max_sandbox=read-only)，请先在设置中解锁全局可写上限")
            eff_sb = _effective_sandbox(b_max, sb_req)
            updated: List[str] = []
            queued: List[str] = []
            members = st.get("members") or {}
            targets: List[str] = []
            if member_id:
                if member_id not in members:
                    raise ValueError(f"未找到成员: {member_id}")
                targets = [member_id]
            else:
                want_cl = (client or "codex").strip().lower()
                targets = [mid for mid, m in members.items() if (m.get("client") or "").lower() == want_cl]
            for mid in targets:
                m = members[mid]
                if m.get("status") == "working":
                    pc = dict(m.get("pending_config") or {})
                    pc.setdefault("name", m.get("name"))
                    pc.setdefault("client", m.get("client"))
                    pc.setdefault("role", m.get("role"))
                    pc.setdefault("model", m.get("model"))
                    pc.setdefault("reasoning_effort", m.get("reasoning_effort"))
                    pc.setdefault("workspace_scope", m.get("workspace_scope", "."))
                    pc["sandbox"] = eff_sb
                    m["pending_config"] = pc
                    queued.append(mid)
                else:
                    m["sandbox"] = eff_sb
                    updated.append(mid)
            return {"sandbox": eff_sb, "updated": updated, "queued_next_round": queued}

        return self.store.mutate(_upd)

    # ------------------------------------------------------------------
    # Roles & Members Management
    # ------------------------------------------------------------------

    def upsert_role(self, role_id: str, name: str, description: str, set_as_coordinator: bool = False) -> Dict[str, Any]:
        role_id = role_id.strip()
        name = name.strip()
        if not role_id or not name:
            raise ValueError("角色 ID 与名称不能为空")

        def _upd(st: Dict[str, Any]) -> Dict[str, Any]:
            existing = st["roles"].get(role_id, {})
            st["roles"][role_id] = {
                "id": role_id,
                "name": name,
                "description": description.strip(),
                "is_coordinator": bool(existing.get("is_coordinator", False)),
                "builtin": bool(existing.get("builtin", False)),
            }
            if set_as_coordinator:
                for rid, r in st["roles"].items():
                    r["is_coordinator"] = rid == role_id
                st["binding"]["coordinator_role_id"] = role_id
            return st["roles"][role_id]

        return self.store.mutate(_upd)

    def delete_role(self, role_id: str) -> Dict[str, Any]:
        def _del(st: Dict[str, Any]) -> Dict[str, Any]:
            if role_id not in st["roles"]:
                raise ValueError(f"角色不存在: {role_id}")
            in_use = [mid for mid, m in st["members"].items() if m.get("role") == role_id]
            if in_use:
                raise ValueError(f"角色仍被成员使用，无法删除: {', '.join(in_use)}")
            if st["binding"].get("coordinator_role_id") == role_id:
                raise ValueError("当前担任总管职责的角色不能直接删除")
            return st["roles"].pop(role_id)

        return self.store.mutate(_del)

    def get_specialist_catalog(self) -> Dict[str, Any]:
        return {
            "templates": SPECIALIST_TEMPLATES,
            "openscience_playbooks": get_playbooks_catalog(),
            "squad_presets": [
                {
                    "id": "openscience_discovery",
                    "name_zh": "OpenScience 科研发现闭环",
                    "name_en": "OpenScience Discovery Loop",
                    "desc_zh": "盲评假说发散（科研首席+统计复算+顶刊审稿）→ 对抗锦标赛互评 → 多维仲裁演化与溯源终稿",
                    "desc_en": "Blind hypothesis generation -> adversarial peer-review & data audit -> M-MAD evolutionary synthesis",
                    "mode": "roundtable",
                    "rounds": 2,
                    "sandbox": "read-only",
                    "preferred_tags": ["openscience", "hypothesis", "review", "fact_check"],
                    "playbooks": ["experimental_design", "statistical_rigor"],
                    "target_count": 4,
                },
                {
                    "id": "scientific_coding_plotting",
                    "name_zh": "科研代码与顶刊绘图闭环",
                    "name_en": "Scientific Code & Publication Plots",
                    "desc_zh": "GPT-6.2-Astra/Sol 编写并跑通 Python/R 统计与出版级矢量/600DPI 绘图 → Gemini 4 Carbon + Claude Opus 5.5 视觉与数据联合审稿",
                    "desc_en": "GPT-6.2 scientific coding & vector/600-DPI plotting + Gemini 4 Carbon & Claude Opus 5.5 visual/statistical QA",
                    "mode": "roundtable",
                    "rounds": 2,
                    "sandbox": "workspace-write",
                    "preferred_tags": ["plotting", "openscience", "implementation", "review"],
                    "playbooks": ["publication_figures_schematics", "execution_hygiene_code"],
                    "target_count": 4,
                },
                {
                    "id": "openscience_autoresearch",
                    "name_zh": "OpenScience 自动科研实验循环",
                    "name_en": "OpenScience Autoresearch Loop",
                    "desc_zh": "锁定单一北极星指标与基线 (Baseline) → 假设驱动消融/调优实验 → 对比 Delta 榜与内容寻址溯源谱系",
                    "desc_en": "Single-metric baseline + hypothesis-driven experiment queue + delta ledger & provenance DAG",
                    "mode": "roundtable",
                    "rounds": 2,
                    "sandbox": "workspace-write",
                    "preferred_tags": ["openscience", "implementation", "fact_check", "review"],
                    "playbooks": ["autoresearch_loop", "execution_hygiene_code"],
                    "target_count": 4,
                },
                {
                    "id": "dual_redteam",
                    "name_zh": "双引擎红队核查",
                    "name_en": "Dual-Engine Red-Team",
                    "desc_zh": "Antigravity + Codex 异构交叉验证，强制包含独立审查与事实核查视角",
                    "desc_en": "Cross-engine Antigravity + Codex debate with dedicated verification",
                    "mode": "roundtable",
                    "rounds": 2,
                    "sandbox": "read-only",
                    "preferred_tags": ["implementation", "review", "fact_check"],
                    "target_count": 3,
                },
                {
                    "id": "research_submission",
                    "name_zh": "科研投稿与统计核查",
                    "name_en": "Research & Stats Panel",
                    "desc_zh": "方法学审稿 + 数据表复算 + 稿件统筹，三层认知标定兼顾严谨与机理深度",
                    "desc_en": "Methodologist + data audit + synthesis strategist with 3-tier epistemic calibration",
                    "mode": "roundtable",
                    "rounds": 2,
                    "sandbox": "read-only",
                    "preferred_tags": ["research", "review", "writing"],
                    "target_count": 3,
                },
                {
                    "id": "code_arch_audit",
                    "name_zh": "代码架构与安全审计",
                    "name_en": "Arch & Security Audit",
                    "desc_zh": "系统架构统筹 + 工程实施验收 + 独立安全与边界审计",
                    "desc_en": "System architect + implementation + edge-case security audit",
                    "mode": "roundtable",
                    "rounds": 2,
                    "sandbox": "read-only",
                    "preferred_tags": ["engineering", "implementation", "review"],
                    "target_count": 3,
                },
                {
                    "id": "fast_cross_check",
                    "name_zh": "极速双核对照",
                    "name_en": "Fast Dual Cross-Check",
                    "desc_zh": "1 位 Antigravity + 1 位 Codex 单轮并行直出对照结论，省时防单模盲区",
                    "desc_en": "1 Antigravity + 1 Codex fast 1-round cross-check",
                    "mode": "roundtable",
                    "rounds": 1,
                    "sandbox": "read-only",
                    "preferred_tags": ["implementation", "review"],
                    "target_count": 2,
                },
            ],
        }

    def get_plotting_backends(self, refresh: bool = False) -> Dict[str, Any]:
        """Return cached live Python (matplotlib/seaborn) & R (ggplot2/patchwork/ComplexHeatmap) runtime status."""
        now = time.time()
        if not refresh and self._plotting_backends_cache and (now - self._plotting_backends_cache[0]) < 60.0:
            return self._plotting_backends_cache[1]
        data = probe_plotting_backend("all")
        data["probed_at"] = now
        self._plotting_backends_cache = (now, data)
        return data

    def _cached_inspect_figure(self, abs_p: str, allow_raster_panels: bool = False) -> Tuple[Dict[str, Any], str]:
        """Thread-safe (abs_path, mtime_ns, size) cache for inspect_any_figure + sha256 to eliminate repeat I/O."""
        st_stat = os.stat(abs_p)
        key = (os.path.normcase(os.path.realpath(abs_p)), int(st_stat.st_mtime_ns), int(st_stat.st_size))
        with self._figure_cache_lock:
            hit = self._figure_inspect_cache.get(key)
            if hit is not None:
                return dict(hit[0]), hit[1]
        insp = inspect_any_figure(abs_p, allow_raster_panels=allow_raster_panels)
        digest = sha256_file(abs_p).lower()
        with self._figure_cache_lock:
            if len(self._figure_inspect_cache) >= 512:
                # Evict oldest quarter
                for old_k in list(self._figure_inspect_cache.keys())[:128]:
                    self._figure_inspect_cache.pop(old_k, None)
            self._figure_inspect_cache[key] = (dict(insp), digest)
        return insp, digest

    def get_task_artifacts(self, task_id: str) -> Dict[str, Any]:
        """P0-5 Version-bound task artifacts, bounded recursive discovery (depth<=3), LRU inspect cache & exact SHA-256 QA binding."""
        snap = self.store.snapshot()
        task = self._find_task(snap, task_id)
        proj_abs = os.path.realpath(os.path.abspath(task.get("project_path") or snap["binding"]["project_path"]))
        root_nc = os.path.normcase(proj_abs)
        tmp_dir, _ = get_runtime_dirs()
        ledger_dir = os.path.join(tmp_dir, "team_ledgers", task_id)

        def _within_proj(cand_abs: str) -> bool:
            try:
                return os.path.commonpath([root_nc, os.path.normcase(cand_abs)]) == root_nc
            except ValueError:
                return False

        def _to_rel(raw_p: str, base_dir: Optional[str] = None) -> Optional[str]:
            if not raw_p or not str(raw_p).strip():
                return None
            p_str = str(raw_p).strip()
            if os.path.isabs(p_str):
                cand = os.path.realpath(p_str)
            elif base_dir:
                c1 = os.path.realpath(os.path.join(base_dir, p_str))
                c2 = os.path.realpath(os.path.join(proj_abs, p_str))
                cand = c1 if (_within_proj(c1) and os.path.isfile(c1)) else c2
            else:
                cand = os.path.realpath(os.path.join(proj_abs, p_str))
            if not _within_proj(cand) or not os.path.isfile(cand):
                return None
            return os.path.relpath(cand, proj_abs).replace("\\", "/")

        attempts_ledger: List[Dict[str, Any]] = []
        changed_rel_set: set = set()
        for step in task.get("steps", []):
            mid = step.get("member_id", "")
            mname = snap.get("members", {}).get(mid, {}).get("name", mid)
            for att in step.get("attempts", []):
                aid = att.get("attempt_id", "")
                l_path = os.path.join(ledger_dir, f"{aid}.jsonl")
                cmd_logs: List[Dict[str, Any]] = []
                chg_files: List[Dict[str, Any]] = []
                edit_after = False
                if os.path.isfile(l_path):
                    try:
                        with open(l_path, "r", encoding="utf-8", errors="replace") as lf:
                            line = lf.readline().strip()
                            if line:
                                obj = json.loads(line)
                                cmd_logs = list(obj.get("command_logs") or [])
                                chg_files = list(obj.get("changed_files") or [])
                                edit_after = bool(obj.get("edit_after_check", False))
                    except Exception:
                        pass
                for cf in chg_files:
                    rel = _to_rel(cf.get("path", ""))
                    if rel:
                        changed_rel_set.add(rel)
                attempts_ledger.append(
                    {
                        "attempt_id": aid,
                        "step_index": step.get("step_index"),
                        "step_title": step.get("title", ""),
                        "member_id": mid,
                        "member_name": mname,
                        "status": att.get("status"),
                        "ended_at": att.get("ended_at"),
                        "edit_after_check": edit_after,
                        "command_logs": cmd_logs,
                        "changed_files": chg_files,
                        "ledger_summary": att.get("ledger_summary"),
                    }
                )

        # Bounded recursive workspace discovery (max_depth=3, starting from workspace_scope + common output roots)
        skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv", ".codex", ".pytest_cache",
            "states", "source_snapshot_1618", "evidence_snapshot", "evidence_snapshot_final",
        }
        manifest_rels: List[str] = []
        seen_mf: set = set()

        def _add_mf(rel: Optional[str]) -> None:
            if rel and rel not in seen_mf and len(manifest_rels) < 20:
                seen_mf.add(rel)
                manifest_rels.append(rel)

        rspec = task.get("research_spec") if isinstance(task.get("research_spec"), dict) else {}
        explicit_manifest_rels: set = set()
        for m_rel in rspec.get("figure_manifests") or []:
            rm = _to_rel(str(m_rel))
            if rm:
                explicit_manifest_rels.add(rm)
            _add_mf(rm)
        for rel in sorted(changed_rel_set):
            if rel.lower().endswith(".json") and "manifest" in os.path.basename(rel).lower():
                _add_mf(rel)

        fig_exts = {".svg", ".pdf", ".png", ".tif", ".tiff", ".jpg", ".jpeg"}
        tbl_exts = {".csv", ".tsv"}
        walk_figs: List[str] = []
        walk_tbls: List[str] = []
        scan_truncated = False
        visited_files = 0
        max_walk_files = 600
        max_depth = 3

        scope_rel = (task.get("workspace_scope") or ".").strip() or "."
        try:
            scan_root = _resolve_workdir(proj_abs, scope_rel)
        except Exception:
            scan_root = proj_abs

        if os.path.isdir(scan_root):
            base_depth = scan_root.rstrip("\\/").count(os.sep)
            for cur_root, dirs, files in os.walk(scan_root, followlinks=False):
                cur_depth = cur_root.rstrip("\\/").count(os.sep) - base_depth
                # Prune noisy/snapshot dirs & cap recursion depth
                dirs[:] = sorted(
                    d for d in dirs
                    if d not in skip_dirs and not d.startswith(".") and cur_depth < max_depth
                )
                for fn in sorted(files):
                    visited_files += 1
                    if visited_files > max_walk_files:
                        scan_truncated = True
                        break
                    fp = os.path.join(cur_root, fn)
                    ext = os.path.splitext(fn)[1].lower()
                    if ext == ".json" and "manifest" in fn.lower():
                        _add_mf(_to_rel(fp))
                    elif ext in fig_exts and len(walk_figs) < 80:
                        r = _to_rel(fp)
                        if r:
                            walk_figs.append(r)
                    elif ext in tbl_exts and len(walk_tbls) < 40:
                        r = _to_rel(fp)
                        if r:
                            walk_tbls.append(r)
                if scan_truncated:
                    break

        signoff = task.get("research_signoff") if isinstance(task.get("research_signoff"), dict) else None
        qa_by_mf: Dict[str, Dict[str, Any]] = {}
        if signoff:
            for fr in signoff.get("figure_reports") or []:
                if not isinstance(fr, dict):
                    continue
                mp = str(fr.get("manifest_rel_path") or fr.get("manifest_path") or "")
                rel_mp = _to_rel(mp) if mp else None
                if rel_mp:
                    qa_by_mf[rel_mp] = fr

        manifests_out: List[Dict[str, Any]] = []
        manifest_fig_meta: Dict[str, Dict[str, Any]] = {}
        manifest_tables: set = set()
        for m_rel in manifest_rels[:20]:
            m_abs = os.path.join(proj_abs, m_rel)
            m_dir = os.path.dirname(m_abs)
            parsed_m: Dict[str, Any] = {}
            try:
                with open(m_abs, "r", encoding="utf-8") as mf:
                    parsed_m = json.load(mf)
                    if not isinstance(parsed_m, dict):
                        parsed_m = {}
            except Exception:
                parsed_m = {}
            qa_rep = qa_by_mf.get(m_rel)
            is_explicit_mf = (m_rel in explicit_manifest_rels) or (qa_rep is not None)
            has_fig_schema = any(k in parsed_m for k in ("exports", "outputs", "figure_id", "panel_sources", "target_width_mm"))
            if not is_explicit_mf and not has_fig_schema:
                continue
            manifests_out.append(
                {
                    "rel_path": m_rel,
                    "figure_id": parsed_m.get("figure_id") or os.path.splitext(os.path.basename(m_rel))[0],
                    "title": parsed_m.get("title") or "",
                    "backend": parsed_m.get("backend") or "",
                    "qa_report": qa_rep,
                }
            )
            src_tables_m: List[str] = []
            for p_obj in (parsed_m.get("panel_sources") or parsed_m.get("panels") or []):
                if isinstance(p_obj, dict):
                    raw_tbl = p_obj.get("source_table") or p_obj.get("source_data")
                    if raw_tbl:
                        t_rel = _to_rel(str(raw_tbl), base_dir=m_dir)
                        if t_rel:
                            manifest_tables.add(t_rel)
                            if t_rel not in src_tables_m:
                                src_tables_m.append(t_rel)
            if parsed_m.get("source_data_path"):
                t_rel = _to_rel(str(parsed_m["source_data_path"]), base_dir=m_dir)
                if t_rel:
                    manifest_tables.add(t_rel)
                    if t_rel not in src_tables_m:
                        src_tables_m.append(t_rel)

            # Build exact (project_rel_path -> expected_sha256) map from QA report artifacts first!
            qa_art_sha_by_rel: Dict[str, str] = {}
            if isinstance(qa_rep, dict):
                for art in qa_rep.get("artifacts") or []:
                    if not isinstance(art, dict):
                        continue
                    art_rel = art.get("project_rel_path") or _to_rel(str(art.get("path") or art.get("rel_path") or ""), base_dir=m_dir)
                    if art_rel and art.get("sha256"):
                        qa_art_sha_by_rel[art_rel] = str(art["sha256"]).lower()

            raw_exp = parsed_m.get("exports") or parsed_m.get("outputs") or {}
            if isinstance(raw_exp, list):
                exp_vals = [(x.get("path") if isinstance(x, dict) else (x if isinstance(x, str) else "")) for x in raw_exp]
            elif isinstance(raw_exp, dict):
                exp_vals = [(v.get("path") if isinstance(v, dict) else (v if isinstance(v, str) else "")) for v in raw_exp.values()]
            else:
                exp_vals = []

            bound_rels = list(qa_art_sha_by_rel.keys())
            for f_raw in exp_vals:
                if not f_raw:
                    continue
                f_rel = _to_rel(str(f_raw), base_dir=m_dir)
                if f_rel and f_rel not in bound_rels:
                    bound_rels.append(f_rel)

            for f_rel in bound_rels:
                manifest_fig_meta[f_rel] = {
                    "manifest_rel": m_rel,
                    "figure_id": parsed_m.get("figure_id") or "",
                    "title": parsed_m.get("title") or "",
                    "source_tables": list(src_tables_m),
                    "qa_report": qa_rep,
                    "qa_expected_sha256": qa_art_sha_by_rel.get(f_rel),
                }

        fig_sources: Dict[str, str] = {}
        tbl_sources: Dict[str, str] = {}

        for rel in manifest_fig_meta:
            fig_sources[rel] = "manifest"
        for rel in manifest_tables:
            tbl_sources[rel] = "manifest"
        for rel in sorted(changed_rel_set):
            ext = os.path.splitext(rel)[1].lower()
            if ext in fig_exts and rel not in fig_sources:
                fig_sources[rel] = "changed"
            elif ext in tbl_exts and rel not in tbl_sources:
                tbl_sources[rel] = "changed"
        for rel in walk_figs:
            if rel not in fig_sources and len(fig_sources) < 80:
                fig_sources[rel] = "workspace"
        for rel in walk_tbls:
            if rel not in tbl_sources and len(tbl_sources) < 40:
                tbl_sources[rel] = "workspace"

        # Group figure files by (dir, stem) and verify exact disk SHA-256 against QA report!
        groups_map: Dict[str, Dict[str, Any]] = {}
        fmt_order = {"svg": 0, "png": 1, "pdf": 2, "tiff": 3, "tif": 4, "jpg": 5, "jpeg": 6}
        for rel, src_tag in fig_sources.items():
            abs_p = os.path.join(proj_abs, rel)
            stem_key = os.path.splitext(rel)[0]
            ext_clean = os.path.splitext(rel)[1].lstrip(".").lower()
            fmt_key = "tiff" if ext_clean == "tif" else ext_clean
            insp, disk_sha = self._cached_inspect_figure(abs_p)
            st_stat = os.stat(abs_p)
            mmeta = manifest_fig_meta.get(rel, {})
            qa_rep = mmeta.get("qa_report")
            exp_sha = mmeta.get("qa_expected_sha256")

            # Compute honest per-file QA status (ZERO false green PASS on stale or shadowed files!)
            file_qa_status = "unverified"
            file_qa_ok: Optional[bool] = None
            file_blocking: List[str] = list(insp.get("blocking") or [])
            file_warnings: List[str] = list(insp.get("warnings") or [])
            if not insp.get("ok") and insp.get("error") and insp.get("error") not in file_blocking:
                file_blocking.append(str(insp["error"]))

            if isinstance(qa_rep, dict):
                if exp_sha and exp_sha != disk_sha:
                    file_qa_status = "stale"
                    file_qa_ok = None
                    file_warnings.append("文件磁盘内容 SHA-256 已较上次质检变更，请重新运行图件 QA")
                elif exp_sha is None and qa_rep.get("artifacts"):
                    # Manifest report exists, but this specific file wasn't in the verified artifact set!
                    file_qa_status = "stale"
                    file_qa_ok = None
                else:
                    rep_st = qa_rep.get("status")
                    if rep_st == "passed":
                        file_qa_status = "passed"
                        file_qa_ok = True
                    elif rep_st == "not_verifiable":
                        file_qa_status = "not_verifiable"
                        file_qa_ok = True
                    else:
                        file_qa_status = "failed"
                        file_qa_ok = False
                    for b in qa_rep.get("blocking") or []:
                        if b not in file_blocking:
                            file_blocking.append(b)
                    for o in qa_rep.get("observations") or []:
                        if o not in file_warnings:
                            file_warnings.append(o)
            else:
                if file_blocking:
                    file_qa_status = "failed"
                    file_qa_ok = False
                elif file_warnings:
                    file_qa_status = "warn"
                    file_qa_ok = None

            grp = groups_map.setdefault(
                stem_key,
                {
                    "id": stem_key,
                    "stem": os.path.basename(stem_key),
                    "dir": os.path.dirname(rel) or ".",
                    "title": mmeta.get("title") or os.path.basename(stem_key),
                    "figure_id": mmeta.get("figure_id") or os.path.basename(stem_key),
                    "manifest_rel": mmeta.get("manifest_rel") or "",
                    "qa_ok": file_qa_ok,
                    "qa_status": file_qa_status,
                    "qa_blocking": [],
                    "qa_warnings": [],
                    "checked_artifacts": int(qa_rep.get("checked_artifacts") or 0) if isinstance(qa_rep, dict) else 0,
                    "checked_panels": int(qa_rep.get("checked_panels") or 0) if isinstance(qa_rep, dict) else 0,
                    "source": src_tag,
                    "source_tables": list(mmeta.get("source_tables") or []),
                    "candidate_tables": [],
                    "mtime": st_stat.st_mtime,
                    "formats": {},
                },
            )
            if src_tag == "manifest" or (src_tag == "changed" and grp["source"] == "workspace"):
                grp["source"] = src_tag
            if mmeta.get("title") and grp["title"] == grp["stem"]:
                grp["title"] = mmeta["title"]
            if mmeta.get("manifest_rel") and not grp["manifest_rel"]:
                grp["manifest_rel"] = mmeta["manifest_rel"]
            # Merge group-level QA status by worst severity (failed > stale > not_verifiable > warn > passed > unverified)
            sev = {"failed": 5, "stale": 4, "not_verifiable": 3, "warn": 2, "passed": 1, "unverified": 0}
            if sev.get(file_qa_status, 0) > sev.get(grp["qa_status"], 0):
                grp["qa_status"] = file_qa_status
                grp["qa_ok"] = file_qa_ok
            for b in file_blocking:
                if b not in grp["qa_blocking"]:
                    grp["qa_blocking"].append(b)
            for w in file_warnings:
                if w not in grp["qa_warnings"]:
                    grp["qa_warnings"].append(w)
            for stbl in mmeta.get("source_tables") or []:
                if stbl not in grp["source_tables"]:
                    grp["source_tables"].append(stbl)
            if isinstance(qa_rep, dict):
                grp["checked_artifacts"] = max(grp["checked_artifacts"], int(qa_rep.get("checked_artifacts") or 0))
                grp["checked_panels"] = max(grp["checked_panels"], int(qa_rep.get("checked_panels") or 0))
            grp["mtime"] = max(grp["mtime"], st_stat.st_mtime)
            grp["formats"][fmt_key] = {
                "format": fmt_key,
                "rel_path": rel,
                "size": st_stat.st_size,
                "mtime": st_stat.st_mtime,
                "sha256": disk_sha,
                "inspect": insp,
            }

        # Distinguish explicitly bound manifest source tables vs filename-similar candidate tables!
        for grp in groups_map.values():
            stem_low = grp["stem"].lower()
            for t_rel in tbl_sources:
                t_base = os.path.splitext(os.path.basename(t_rel))[0].lower()
                if (t_base == stem_low or t_base.startswith(stem_low + "_")) and t_rel not in grp["source_tables"]:
                    if t_rel not in grp["candidate_tables"]:
                        grp["candidate_tables"].append(t_rel)
            grp["format_list"] = sorted(grp["formats"].keys(), key=lambda x: fmt_order.get(x, 99))

        src_priority = {"manifest": 0, "changed": 1, "workspace": 2}
        figures_out = sorted(
            groups_map.values(),
            key=lambda g: (src_priority.get(g["source"], 9), -g["mtime"], g["id"]),
        )
        tables_out = [
            {
                "rel_path": t_rel,
                "name": os.path.basename(t_rel),
                "source": t_src,
                "size": os.path.getsize(os.path.join(proj_abs, t_rel)),
                "mtime": os.path.getmtime(os.path.join(proj_abs, t_rel)),
            }
            for t_rel, t_src in sorted(tbl_sources.items(), key=lambda kv: (src_priority.get(kv[1], 9), kv[0]))
        ]
        return {
            "task_id": task_id,
            "project_path": proj_abs,
            "scan_truncated": scan_truncated,
            "openscience_playbooks": list(task.get("openscience_playbooks") or []),
            "research_spec": task.get("research_spec"),
            "research_signoff": signoff,
            "attempts_ledger": attempts_ledger,
            "figures": figures_out,
            "tables": tables_out,
            "manifests": manifests_out,
            "provenance": self.get_task_provenance(task_id),
        }

    def get_task_provenance(self, task_id: str) -> Dict[str, Any]:
        """Return OpenScience content-addressed Provenance DAG (nodes + edges) & Experiment Runs."""
        tmp_dir, _ = get_runtime_dirs()
        store = TaskProvenanceStore(os.path.join(tmp_dir, "team_ledgers"), task_id)
        return store.get_summary()

    def verify_task_figure(self, task_id: str, manifest_path: str) -> Dict[str, Any]:
        """
        Run lock-free publication-grade figure_qa.verify_figure_manifest on a task's manifest
        and recompute task research_signoff cleanly (fixes P0-c one-way failure lock & recovers needs_ruling!).
        """
        snap = self.store.snapshot()
        task = self._find_task(snap, task_id)
        proj_abs = os.path.realpath(os.path.abspath(task.get("project_path") or snap["binding"]["project_path"]))
        mf_abs = _resolve_workdir(proj_abs, manifest_path)
        if not os.path.isfile(mf_abs):
            raise ValueError(f"图件契约文件不存在: {manifest_path}")
        try:
            report = verify_figure_manifest(mf_abs, proj_abs)
        except Exception as exc:
            report = {
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
        report["verified_at"] = time.time()

        def _save_qa(st: Dict[str, Any]) -> None:
            t = self._find_task(st, task_id)
            so = t.get("research_signoff")
            if not isinstance(so, dict):
                so = {
                    "ok": True,
                    "status": "pending_artifacts",
                    "mode": "manual_figure_qa",
                    "checks": [],
                    "figure_reports": [],
                    "blocking_items": [],
                    "blocking": [],
                    "base_observations": [],
                    "observations": [],
                    "verified_at": report["verified_at"],
                }
                t["research_signoff"] = so
            target_mp_nc = os.path.normcase(os.path.realpath(mf_abs))
            reports = [
                r for r in (so.get("figure_reports") or [])
                if isinstance(r, dict)
                and os.path.normcase(os.path.realpath(str(r.get("manifest_path") or ""))) != target_mp_nc
            ]
            reports.append(report)
            so["figure_reports"] = reports
            so["verified_at"] = report["verified_at"]
            recompute_signoff_summary(so)

            # Recover task/step ONLY when full signoff is genuinely "passed" (NEVER on not_verifiable or pending_artifacts!)
            recovered = False
            if so.get("status") == "passed" and t.get("status") == "needs_ruling":
                b_reason = str(t.get("blocking_reason") or "")
                if "机械验收未通过" in b_reason or "Figure QA" in b_reason:
                    for s in t.get("steps") or []:
                        if s.get("status") == "needs_ruling" and ("机械验收未通过" in str(s.get("blocking_reason") or "") or "Figure QA" in str(s.get("blocking_reason") or "")):
                            s["status"] = "succeeded"
                            s["blocking_reason"] = None
                            recovered = True
                    if recovered:
                        self._refresh_task_overall_status_unlocked(st, t)

            t["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "figure.qa.recovered" if recovered else "figure.qa",
                    "summary": (
                        f"科研图复检通过并恢复放行: {os.path.basename(mf_abs)}"
                        if recovered
                        else f"科研图质检 {report.get('status')}: {os.path.basename(mf_abs)}"
                    ),
                }
            )
            t["recent_events"] = t["recent_events"][-40:]
            t["updated_at"] = time.time()

        self.store.mutate(_save_qa)
        return report

    def compact_task_context(self, task_id: str, reset_sessions: bool = True) -> Dict[str, Any]:
        """
        Lock-free manual context compaction & optional clean session rollover:
        Seals an immutable deterministic Round State Snapshot from completed chat history,
        writes a Checkpoint Handoff Pack to BlobStore, and optionally clears non-running step
        worker_session_ids so the next followup/turn begins on a fresh KV-clean session!
        """
        snap = self.store.snapshot()
        task = self._find_task(snap, task_id)
        msgs = list(task.get("chat_room") or [])
        if not msgs:
            raise ValueError("当前任务尚无讨论上下文可压缩")

        blob_store = BlobStore(task_id)
        tmp_dir, _ = get_runtime_dirs()
        room_md_path = os.path.join(tmp_dir, "team_rooms", f"{task_id}.md")
        max_stage = max([int(s.get("stage_index") or 0) for s in (task.get("steps") or [])] or [0])
        sealed = seal_round_snapshot(
            msgs,
            visibility_scope="manual_compact",
            stage_index=max_stage,
            cutoff_seq=len(msgs),
            task_meta=task,
            blob_store=blob_store,
            round_label="手动压缩快照",
        )
        ckpt = build_handoff_checkpoint(
            task,
            (task.get("steps") or [{}])[-1],
            msgs,
            blob_store,
            room_md_path,
            reason="manual_compact",
            level="L1",
        )

        def _commit(st: Dict[str, Any]) -> Dict[str, Any]:
            t = self._find_task(st, task_id)
            now = time.time()
            snaps = t.setdefault("round_snapshots", [])
            if not any(x.get("snapshot_sha256") == sealed["snapshot_sha256"] for x in snaps):
                snaps.append(sealed)
                t["round_snapshots"] = snaps[-12:]
            t["latest_checkpoint"] = {
                "blob_path": ckpt["blob_path"],
                "sha256": ckpt["sha256"],
                "reason": "manual_compact",
                "anchors_count": ckpt["anchors_count"],
                "created_at": now,
            }
            cleared = 0
            if reset_sessions:
                for s in t.get("steps") or []:
                    if s.get("status") != "running" and s.get("worker_session_id"):
                        mid = s.get("member_id")
                        if mid and mid in st.get("members", {}):
                            st["members"][mid].setdefault("session_history", []).append(s["worker_session_id"])
                        s["worker_session_id"] = None
                        s["session_epoch"] = int(s.get("session_epoch") or 0) + 1
                        cleared += 1
            cs = t.setdefault("context_stats", {})
            cs["manual_compactions"] = int(cs.get("manual_compactions") or 0) + 1
            cs["snapshot_count"] = len(t.get("round_snapshots") or [])
            cs["snapshots_count"] = len(t.get("round_snapshots") or [])
            cs["last_compacted_at"] = now
            t["recent_events"].append({
                "ts": now,
                "type": "context.compacted",
                "summary": f"已手动封存不可变状态卡 ({sealed['anchor_count']} 锚点 / {sealed['blocker_count']} 阻断)"
                + (f" 并重置 {cleared} 个非运行会话" if cleared else ""),
            })
            t["recent_events"] = t["recent_events"][-40:]
            t["updated_at"] = now
            return {
                "snapshot": {k: v for k, v in sealed.items() if k != "rendered_md"},
                "checkpoint_path": ckpt["blob_path"],
                "cleared_sessions": cleared,
                "reset_sessions_count": cleared,
                "snapshots_count": len(t.get("round_snapshots") or []),
            }

        res_c = self.store.mutate(_commit)
        # Lock-free FTS5 indexing of manual snapshot into project memory
        try:
            proj_p = task.get("project_path") or snap["binding"]["project_path"]
            ProjectMemoryIndex(proj_p).upsert_doc(
                doc_id=f"{task_id}:snap:{sealed['snapshot_sha256'][:12]}",
                task_id=task_id,
                kind="snapshot",
                title=f"{task.get('title', task_id)} · 手动压缩快照",
                content=sealed["rendered_md"],
                blob_path=sealed["blob_path"],
                anchors=extract_deterministic_anchors(sealed["rendered_md"]),
                stage_index=max_stage,
            )
        except Exception:
            pass
        return res_c

    def search_project_memory(
        self,
        query: str,
        *,
        project_path: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: int = 8,
    ) -> Dict[str, Any]:
        """Search project-scoped SQLite FTS5 + CJK bi-gram memory index (snapshots, blobs, deliverables, rulings)."""
        snap = self.store.snapshot()
        proj_p = project_path or snap["binding"]["project_path"]
        idx = ProjectMemoryIndex(proj_p)
        hits = idx.search(query, limit=limit, task_id=task_id)
        return {"query": query, "project_path": proj_p, "hits": hits, "stats": idx.stats()}

    def get_memory_stats(self, project_path: Optional[str] = None) -> Dict[str, Any]:
        """Return project-scoped FTS5 memory index statistics and Git repository worktree readiness."""
        snap = self.store.snapshot()
        proj_p = project_path or snap["binding"]["project_path"]
        idx = ProjectMemoryIndex(proj_p)
        return {
            "project_path": proj_p,
            "memory": idx.stats(),
            "git": detect_git_repo(proj_p),
        }

    def get_task_worktrees(self, task_id: str) -> Dict[str, Any]:
        """Inspect all isolated Git worktrees and live unified diffs for a task (strictly lock-free)."""
        snap = self.store.snapshot()
        task = self._find_task(snap, task_id)
        proj_p = task.get("project_path") or snap["binding"]["project_path"]
        items: List[Dict[str, Any]] = []
        for s in task.get("steps") or []:
            wt = s.get("worktree_info")
            if not isinstance(wt, dict) or not wt.get("worktree_path"):
                continue
            live_diff = inspect_worktree_diff(wt["worktree_path"], wt.get("base_commit", ""))
            items.append(
                {
                    "step_index": s["step_index"],
                    "title": s.get("title", ""),
                    "member_id": s.get("member_id", ""),
                    "member_name": snap["members"].get(s.get("member_id", ""), {}).get("name", s.get("member_id", "")),
                    "step_status": s.get("status"),
                    **wt,
                    "live_diff": live_diff,
                }
            )
        return {"task_id": task_id, "git": detect_git_repo(proj_p), "worktrees": items}

    def merge_task_worktree(self, task_id: str, step_index: int, cleanup_after: bool = True) -> Dict[str, Any]:
        """Safely merge an isolated step worktree branch into main repo (conflict-checked, lock-free)."""
        snap = self.store.snapshot()
        task = self._find_task(snap, task_id)
        step = task["steps"][int(step_index)]
        wt = step.get("worktree_info")
        if not isinstance(wt, dict) or not wt.get("worktree_path"):
            raise ValueError(f"步骤 {step_index} 未启用或不存在隔离 Git Worktree")
        if step.get("status") == "running":
            raise ValueError("该步骤仍在运行中，请等待完成后再合并")

        msg = f"AGY Roundtable merge {task_id} step #{step_index} ({step.get('title', '')})"
        merge_res = merge_worktree_to_main(wt["repo_root"], wt["worktree_path"], wt["branch"], commit_message=msg)
        clean_res = None
        if cleanup_after:
            clean_res = cleanup_worktree(wt["repo_root"], wt["worktree_path"], wt["branch"])

        def _mark_merged(st: Dict[str, Any]) -> None:
            t = self._find_task(st, task_id)
            s = t["steps"][int(step_index)]
            if isinstance(s.get("worktree_info"), dict):
                s["worktree_info"]["status"] = "merged_and_cleaned" if cleanup_after else "merged"
                s["worktree_info"]["merged_head"] = merge_res.get("new_head")
                s["worktree_info"]["merged_at"] = time.time()
            t["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "worktree.merged",
                    "summary": f"已合并步骤 #{step_index} 隔离分支 {wt['branch']} -> HEAD ({str(merge_res.get('new_head', ''))[:8]})",
                }
            )
            t["recent_events"] = t["recent_events"][-40:]
            t["updated_at"] = time.time()

        self.store.mutate(_mark_merged)
        return {"merge": merge_res, "cleanup": clean_res}

    def cleanup_task_worktree(self, task_id: str, step_index: int) -> Dict[str, Any]:
        """Discard and clean up an isolated step worktree & temporary branch."""
        snap = self.store.snapshot()
        task = self._find_task(snap, task_id)
        step = task["steps"][int(step_index)]
        wt = step.get("worktree_info")
        if not isinstance(wt, dict) or not wt.get("worktree_path"):
            raise ValueError(f"步骤 {step_index} 不存在隔离 Git Worktree")
        if step.get("status") == "running":
            raise ValueError("该步骤正在运行中，请先中止再清理 Worktree")

        clean_res = cleanup_worktree(wt["repo_root"], wt["worktree_path"], wt.get("branch", ""))

        def _mark_cleaned(st: Dict[str, Any]) -> None:
            t = self._find_task(st, task_id)
            s = t["steps"][int(step_index)]
            if isinstance(s.get("worktree_info"), dict):
                s["worktree_info"]["status"] = "cleaned"
                s["worktree_info"]["cleaned_at"] = time.time()
            t["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "worktree.cleaned",
                    "summary": f"已清理步骤 #{step_index} 隔离 Worktree ({wt.get('branch', '')})",
                }
            )
            t["recent_events"] = t["recent_events"][-40:]
            t["updated_at"] = time.time()

        self.store.mutate(_mark_cleaned)
        return clean_res

    def list_backup_candidates(
        self,
        source_member_id: str,
        require_same_client: bool = False,
        prefer_cross_engine: bool = False,
    ) -> Dict[str, Any]:
        """Rank live alternative models (excluding source model & gpt-6.1-terra) for role-preserving quota backup."""
        snap = self.store.snapshot()
        mem = snap.get("members", {}).get(source_member_id)
        if not mem:
            raise ValueError(f"成员不存在: {source_member_id}")
        caps = self.get_capabilities(refresh_cli=False)
        src_client = mem.get("client", "codex")
        src_model = (mem.get("model") or "").strip().lower()
        src_effort = mem.get("reasoning_effort") or "high"
        other_client = "antigravity" if src_client == "codex" else "codex"
        client_order = [src_client] if require_same_client else (
            [other_client, src_client] if prefer_cross_engine else [src_client, other_client]
        )

        candidates: List[Dict[str, Any]] = []
        seen_keys: set = set()
        for cl in client_order:
            avail = (caps.get("clients") or {}).get(cl, {}).get("models") or []
            by_slug = {m.get("slug"): m for m in avail if m.get("slug")}
            prio = list(_FLAGSHIP_BACKUP_PRIORITY.get(cl, []))
            # Append any remaining live non-terra models on this client
            for m in avail:
                sl = m.get("slug") or ""
                if sl and sl not in prio and "terra" not in sl.lower():
                    prio.append(sl)
            for slug in prio:
                if "terra" in slug.lower():
                    continue
                if cl == src_client and slug.lower() == src_model:
                    continue
                mobj = by_slug.get(slug)
                if not mobj:
                    continue
                key = (cl, slug)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                effs = mobj.get("supported_reasoning_efforts") or []
                eff = src_effort if src_effort in effs else (
                    "high" if "high" in effs else (mobj.get("default_reasoning_effort") or (effs[0] if effs else "medium"))
                )
                candidates.append(
                    {
                        "client": cl,
                        "model": slug,
                        "display_name": mobj.get("display_name") or slug,
                        "reasoning_effort": eff,
                        "supported_reasoning_efforts": effs,
                        "same_engine": cl == src_client,
                    }
                )
        return {
            "source_member_id": source_member_id,
            "source_client": src_client,
            "source_model": mem.get("model"),
            "candidates": candidates,
            "recommended": candidates[0] if candidates else None,
        }

    def bind_quota_backup(
        self,
        source_member_id: str,
        backup_member_id: Optional[str] = None,
        backup_client: Optional[str] = None,
        backup_model: Optional[str] = None,
        backup_effort: Optional[str] = None,
        prefer_cross_engine: bool = False,
        clear: bool = False,
    ) -> Dict[str, Any]:
        """Persistently bind (or clear / auto-pick) a quota-exhaustion backup specialist or model for a member."""
        snap = self.store.snapshot()
        src_mem = snap.get("members", {}).get(source_member_id)
        if not src_mem:
            raise ValueError(f"原专家不存在: {source_member_id}")

        binding_obj: Optional[Dict[str, Any]] = None
        if not clear:
            if backup_member_id:
                if backup_member_id == source_member_id:
                    raise ValueError("不能将专家自身绑定为自己的额度替补")
                bm = snap.get("members", {}).get(backup_member_id)
                if not bm:
                    raise ValueError(f"替补成员不存在: {backup_member_id}")
                binding_obj = {
                    "mode": "member",
                    "member_id": backup_member_id,
                    "client": bm.get("client", "codex"),
                    "model": bm.get("model", ""),
                    "reasoning_effort": bm.get("reasoning_effort", "high"),
                    "display_name": bm.get("name") or backup_member_id,
                    "updated_at": time.time(),
                }
            else:
                cands_info = self.list_backup_candidates(source_member_id, prefer_cross_engine=prefer_cross_engine)
                cands = cands_info.get("candidates") or []
                if backup_model:
                    ch_cl = (backup_client or src_mem.get("client", "codex")).strip().lower()
                    if ch_cl not in SUPPORTED_CLIENTS:
                        raise ValueError(f"不支持的替补引擎: {ch_cl}")
                    if ch_cl == src_mem.get("client") and backup_model.strip().lower() == str(src_mem.get("model", "")).lower():
                        raise ValueError("绑定的替补模型必须与原模型不同")
                    matched = next((c for c in cands if c["client"] == ch_cl and c["model"] == backup_model.strip()), None)
                    ch_md = backup_model.strip()
                    ch_ef = backup_effort or (matched["reasoning_effort"] if matched else (src_mem.get("reasoning_effort") or "high"))
                    ch_disp = matched["display_name"] if matched else ch_md
                    self._validate_client_model_effort(ch_cl, ch_md, ch_ef)
                else:
                    if not cands:
                        raise ValueError("当前没有可用的不同旗舰模型可绑定为额度替补")
                    rec = cands[0]
                    ch_cl, ch_md, ch_ef, ch_disp = rec["client"], rec["model"], rec["reasoning_effort"], rec["display_name"]
                binding_obj = {
                    "mode": "model",
                    "member_id": None,
                    "client": ch_cl,
                    "model": ch_md,
                    "reasoning_effort": ch_ef,
                    "display_name": ch_disp,
                    "updated_at": time.time(),
                }

        def _bind(st: Dict[str, Any]) -> Dict[str, Any]:
            m = st["members"].get(source_member_id)
            if not m:
                raise ValueError(f"原专家不存在: {source_member_id}")
            m["quota_backup"] = binding_obj
            return {"member_id": source_member_id, "quota_backup": binding_obj, "member": m}

        return self.store.mutate(_bind)

    def auto_bind_all_quota_backups(
        self,
        prefer_cross_engine: bool = False,
        member_ids: Optional[List[str]] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Auto-bind optimal different-model quota backups for all (or specified) roster members in 1 click."""
        snap = self.store.snapshot()
        target_ids = list(member_ids) if member_ids else [
            mid for mid, m in snap.get("members", {}).items()
            if m.get("client") in SUPPORTED_CLIENTS and not m.get("backup_of")
        ]
        plan: Dict[str, Dict[str, Any]] = {}
        for mid in target_ids:
            m = snap.get("members", {}).get(mid)
            if not m or m.get("client") not in SUPPORTED_CLIENTS:
                continue
            if m.get("quota_backup") and not overwrite:
                continue
            cinfo = self.list_backup_candidates(mid, prefer_cross_engine=prefer_cross_engine)
            rec = cinfo.get("recommended")
            if rec:
                plan[mid] = {
                    "mode": "model",
                    "member_id": None,
                    "client": rec["client"],
                    "model": rec["model"],
                    "reasoning_effort": rec["reasoning_effort"],
                    "display_name": rec["display_name"],
                    "updated_at": time.time(),
                }

        def _apply(st: Dict[str, Any]) -> Dict[str, Any]:
            bound_list = []
            for mid, bobj in plan.items():
                if mid in st["members"]:
                    st["members"][mid]["quota_backup"] = bobj
                    bound_list.append({"member_id": mid, "name": st["members"][mid].get("name"), "quota_backup": bobj})
            return {"bound": bound_list, "count": len(bound_list)}

        return self.store.mutate(_apply)

    def spawn_quota_backup_member(
        self,
        source_member_id: str,
        new_client: Optional[str] = None,
        new_model: Optional[str] = None,
        new_effort: Optional[str] = None,
        prefer_cross_engine: bool = False,
        task_id: Optional[str] = None,
        step_index: Optional[int] = None,
        auto_reassign: bool = False,
        bind_as_default: bool = True,
        reason: str = "模型额度预警，补充同角色跨模型替补",
    ) -> Dict[str, Any]:
        """Spawn (or reuse idle) backup specialist inheriting 100% of source role/persona/grounding on a different model."""
        snap = self.store.snapshot()
        src_mem = snap.get("members", {}).get(source_member_id)
        if not src_mem:
            raise ValueError(f"原专家不存在: {source_member_id}")

        req_same_client = False
        if task_id is not None and step_index is not None:
            try:
                t_chk = self._find_task(snap, task_id)
                s_chk = t_chk["steps"][int(step_index)]
                req_same_client = bool(s_chk.get("requires_shell", False))
            except Exception:
                pass

        cands_info = self.list_backup_candidates(
            source_member_id,
            require_same_client=req_same_client,
            prefer_cross_engine=prefer_cross_engine,
        )
        cands = cands_info.get("candidates") or []

        if new_model:
            chosen_client = (new_client or src_mem.get("client", "codex")).strip().lower()
            if chosen_client not in SUPPORTED_CLIENTS:
                raise ValueError(f"不支持的替补引擎: {chosen_client}")
            if chosen_client == src_mem.get("client") and new_model.strip().lower() == str(src_mem.get("model", "")).lower():
                raise ValueError("替补角色必须使用与原角色不同的模型（或跨引擎模型）")
            if req_same_client and chosen_client == "antigravity":
                raise ValueError("当前步骤需要 Shell/脚本执行权限，替补必须选用 Codex 引擎下的不同模型")
            matched_c = next((c for c in cands if c["client"] == chosen_client and c["model"] == new_model), None)
            chosen_model = new_model.strip()
            chosen_effort = new_effort or (matched_c["reasoning_effort"] if matched_c else (src_mem.get("reasoning_effort") or "high"))
            chosen_disp = matched_c["display_name"] if matched_c else chosen_model
            self._validate_client_model_effort(chosen_client, chosen_model, chosen_effort)
        else:
            if not cands:
                raise ValueError("当前没有可用的不同旗舰模型可作为额度替补")
            rec = cands[0]
            chosen_client = rec["client"]
            chosen_model = rec["model"]
            chosen_effort = new_effort or rec["reasoning_effort"]
            chosen_disp = rec["display_name"]

        def _spawn(st: Dict[str, Any]) -> Dict[str, Any]:
            src = st["members"].get(source_member_id)
            if not src:
                raise ValueError(f"原专家不存在: {source_member_id}")
            # Reuse existing idle backup with exact same source + target (client, model) to avoid clutter
            for ex_id, ex_m in st["members"].items():
                if (
                    ex_m.get("backup_of") == source_member_id
                    and ex_m.get("client") == chosen_client
                    and ex_m.get("model") == chosen_model
                    and ex_m.get("status") != "working"
                ):
                    # Refresh inherited persona/specialty in case source was tuned
                    ex_m["role"] = src.get("role", ex_m.get("role"))
                    ex_m["specialty"] = src.get("specialty", "")
                    ex_m["persona_prompt"] = src.get("persona_prompt", "")
                    ex_m["grounding_mode"] = src.get("grounding_mode", "strict")
                    ex_m["tags"] = list(src.get("tags") or [])
                    ex_m["reasoning_effort"] = chosen_effort
                    ex_m["backup_reason"] = reason
                    if bind_as_default:
                        src["quota_backup"] = {
                            "mode": "member",
                            "member_id": ex_id,
                            "client": chosen_client,
                            "model": chosen_model,
                            "reasoning_effort": chosen_effort,
                            "display_name": ex_m.get("name") or chosen_disp,
                            "updated_at": time.time(),
                        }
                    return {"member": ex_m, "reused": True}

            base_token = _short_model_id_token(chosen_model)
            root_id = src.get("backup_of") or source_member_id
            cand_id = f"{root_id}_bk_{base_token}"
            seq = 2
            while cand_id in st["members"]:
                cand_id = f"{root_id}_bk_{base_token}_{seq}"
                seq += 1

            base_name = _strip_model_parens(src.get("name") or source_member_id)
            bk_name = f"{base_name} [替补 · {chosen_disp}]"
            eff_sb = _effective_sandbox(st["binding"].get("max_sandbox", "workspace-write"), src.get("sandbox", "read-only"))
            new_mem = {
                "id": cand_id,
                "name": bk_name,
                "role": src.get("role", "implementation"),
                "client": chosen_client,
                "model": chosen_model,
                "reasoning_effort": chosen_effort,
                "specialty": src.get("specialty", ""),
                "persona_prompt": src.get("persona_prompt", ""),
                "grounding_mode": src.get("grounding_mode", "strict"),
                "tags": list(src.get("tags") or []),
                "workspace_scope": src.get("workspace_scope", "."),
                "sandbox": eff_sb,
                "backup_of": source_member_id,
                "backup_reason": reason,
                "session_id": None,
                "session_history": [],
                "config_history": [
                    {
                        "changed_at": time.time(),
                        "from": {"source_member_id": source_member_id, "client": src.get("client"), "model": src.get("model")},
                        "to": {"client": chosen_client, "model": chosen_model, "reasoning_effort": chosen_effort},
                        "reason": reason,
                    }
                ],
                "pending_config": None,
                "status": "idle",
                "live_activity": "",
                "current_task_id": None,
                "current_attempt_id": None,
            }
            enrich_member_metadata(new_mem)
            st["members"][cand_id] = new_mem
            if bind_as_default:
                src["quota_backup"] = {
                    "mode": "member",
                    "member_id": cand_id,
                    "client": chosen_client,
                    "model": chosen_model,
                    "reasoning_effort": chosen_effort,
                    "display_name": bk_name,
                    "updated_at": time.time(),
                }
            return {"member": new_mem, "reused": False}

        out = self.store.mutate(_spawn)
        bk_id = out["member"]["id"]

        if auto_reassign and task_id is not None and step_index is not None:
            reassigned_task = self.reassign_step(
                task_id=task_id,
                step_index=int(step_index),
                new_member_id=bk_id,
                reason=f"模型额度预警/耗尽，自动交接同角色替补 {out['member']['name']} ({chosen_client}/{chosen_model})",
            )
            out["task"] = reassigned_task
        return out

    def optimize_roster(self, apply_names: bool = True, refresh_cli: bool = False) -> Dict[str, Any]:
        """Auto-calibrate all idle members against live CLI capabilities & benchmark-proven OpenScience/Engineering roles."""
        caps = self.get_capabilities(refresh_cli=refresh_cli)
        tpl_map = {t["template_id"]: t for t in SPECIALIST_TEMPLATES}

        def _pick_live_model(client: str, preferred: List[str], pref_effort: str) -> Tuple[Optional[str], str]:
            avail = (caps.get("clients") or {}).get(client, {}).get("models") or []
            by_slug = {m["slug"]: m for m in avail if m.get("slug")}
            chosen_obj = None
            for slug in preferred:
                if slug in by_slug:
                    chosen_obj = by_slug[slug]
                    break
            if not chosen_obj and avail:
                chosen_obj = avail[0]
            if not chosen_obj:
                return (preferred[0] if preferred else None), pref_effort
            effs = chosen_obj.get("supported_reasoning_efforts") or []
            if pref_effort in effs:
                eff = pref_effort
            elif "high" in effs and pref_effort in ("xhigh", "max"):
                eff = "high"
            elif "medium" in effs:
                eff = "medium"
            elif effs:
                eff = effs[0]
            else:
                eff = chosen_obj.get("default_reasoning_effort") or "medium"
            return chosen_obj["slug"], eff

        def _classify_member(mid: str, mem: Dict[str, Any]) -> Tuple[str, str]:
            low = mid.lower()
            client = mem.get("client", "codex")
            role = mem.get("role", "implementation")
            if "carbon" in low or "visual" in low:
                return "visual_figure_qa", "科研绘图架构与视觉质检官 (Gemini 4 Carbon)"
            if "flash" in low or "scout" in low or "hydrogen" in low:
                return "openscience_scout", "极速证据勘察员 (Gemini 4 Hydrogen)"
            if client == "antigravity" and (role == "review" or "claude" in low or "review" in low):
                return "journal_reviewer", "顶刊方法学与代码审稿官 (Claude Opus 5.5)"
            if client == "antigravity" and ("gemini" in low or "pi" in low or role == "implementation"):
                return "openscience_pi", "OpenScience 科研首席 (Gemini 4 Neon)"
            if client == "codex" and (role == "review" or "review" in low or "fact" in low):
                return "fact_checker", "红队事实核查与代码安全官 (GPT-6.2-Astra)"
            if role == "documentation" or "doc" in low or "writ" in low:
                return "academic_strategist", "学术撰写与交付策略官 (GPT-6.2-Sol)"
            if "plot" in low:
                return "scientific_plotter", "顶刊科研代码绘图专家 (GPT-6.2-Astra)"
            if "2" in low or "stat" in low or "data" in low or "biostat" in low:
                return "biostat_auditor", "统计复算与顶刊科研绘图官 (GPT-6.2-Astra)"
            return "openscience_experimenter", "科研代码与可复现工程官 (GPT-6.2-Sol)"

        changes: List[Dict[str, Any]] = []
        skipped: List[str] = []

        def _opt(st: Dict[str, Any]) -> Dict[str, Any]:
            # Ensure Gemini 4 Carbon visual/figure QA specialist seat exists if available in Antigravity!
            agy_slugs = {m.get("slug") for m in (caps.get("clients") or {}).get("antigravity", {}).get("models", [])}
            if "gemini-4-carbon" in agy_slugs and "agy_carbon_plotter" not in st.get("members", {}):
                vtpl = tpl_map.get("visual_figure_qa") or SPECIALIST_TEMPLATES[0]
                st.setdefault("members", {})["agy_carbon_plotter"] = {
                    "id": "agy_carbon_plotter",
                    "name": "科研绘图架构与视觉质检官 (Gemini 4 Carbon)",
                    "role": "review",
                    "client": "antigravity",
                    "model": "gemini-4-carbon",
                    "reasoning_effort": "high",
                    "specialty": vtpl["specialty_zh"],
                    "persona_prompt": vtpl["persona_prompt"],
                    "grounding_mode": "strict",
                    "tags": list(vtpl["tags"]),
                    "workspace_scope": ".",
                    "sandbox": "read-only",
                    "session_id": None,
                    "session_history": [],
                    "config_history": [],
                    "pending_config": None,
                    "status": "idle",
                    "live_activity": "",
                    "current_task_id": None,
                    "current_attempt_id": None,
                }
            for mid, mem in st.get("members", {}).items():
                if mem.get("client") not in SUPPORTED_CLIENTS:
                    continue
                if mem.get("backup_of"):
                    continue
                if mem.get("status") == "working":
                    skipped.append(mid)
                    continue
                tpl_id, default_label = _classify_member(mid, mem)
                tpl = tpl_map.get(tpl_id) or SPECIALIST_TEMPLATES[0]
                client = mem.get("client", tpl.get("recommended_client", "codex"))
                new_model, new_effort = _pick_live_model(
                    client,
                    list(tpl.get("recommended_models") or []),
                    tpl.get("recommended_effort") or "medium",
                )
                old_snapshot = {
                    "name": mem.get("name"),
                    "model": mem.get("model"),
                    "reasoning_effort": mem.get("reasoning_effort"),
                    "specialty": mem.get("specialty"),
                }
                if new_model:
                    mem["model"] = new_model
                mem["reasoning_effort"] = new_effort
                mem["specialty"] = tpl["specialty_zh"]
                mem["persona_prompt"] = tpl["persona_prompt"]
                mem["grounding_mode"] = "strict"
                mem["tags"] = list(tpl["tags"])
                if apply_names:
                    # Reflect actual live model slug cleanly in label if fallback happened
                    short_base = default_label.split("(")[0].strip()
                    live_disp = next(
                        (
                            m.get("display_name") or new_model
                            for m in (caps.get("clients") or {}).get(client, {}).get("models", [])
                            if m.get("slug") == new_model
                        ),
                        new_model or "",
                    )
                    mem["name"] = f"{short_base} ({live_disp})" if live_disp else default_label
                mem["pending_config"] = None
                mem.setdefault("config_history", []).append(
                    {
                        "changed_at": time.time(),
                        "from": old_snapshot,
                        "to": {
                            "name": mem.get("name"),
                            "model": mem.get("model"),
                            "reasoning_effort": mem.get("reasoning_effort"),
                            "specialty": mem.get("specialty"),
                            "template_id": tpl_id,
                        },
                        "reason": "OpenScience + 实测能力基准一键校准",
                    }
                )
                changes.append(
                    {
                        "member_id": mid,
                        "template_id": tpl_id,
                        "from": old_snapshot,
                        "to": {
                            "name": mem.get("name"),
                            "client": client,
                            "model": mem.get("model"),
                            "reasoning_effort": mem.get("reasoning_effort"),
                            "specialty": mem.get("specialty"),
                        },
                    }
                )
            return {"changes": changes, "skipped_working": skipped, "count": len(changes)}

        return self.store.mutate(_opt)

    def upsert_member(
        self,
        member_id: str,
        role_id: str,
        name: Optional[str] = None,
        client: str = "codex",
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = "low",
        workspace_scope: str = ".",
        sandbox: str = "read-only",
        specialty: Optional[str] = None,
        persona_prompt: Optional[str] = None,
        grounding_mode: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        member_id = member_id.strip()
        if not member_id:
            raise ValueError("成员 ID 不能为空")
        client = client.strip().lower()
        if client not in SUPPORTED_CLIENTS:
            raise ValueError(f"不支持的客户端: {client}")
        use_model = model or ("gpt-6.2-sol" if client == "codex" else "gemini-4-carbon")
        self._validate_client_model_effort(client, use_model, reasoning_effort)

        def _upd(st: Dict[str, Any]) -> Dict[str, Any]:
            if role_id not in st["roles"]:
                raise ValueError(f"未知角色 ID: {role_id}")
            existing = st["members"].get(member_id)
            if existing and existing.get("status") == "working":
                raise ValueError("成员正在运行中，请使用安全配置切换接口")
            gm = (grounding_mode or (existing.get("grounding_mode") if existing else "strict") or "strict").strip().lower()
            if gm not in ("strict", "balanced"):
                gm = "strict"
            clean_tags = [str(x).strip() for x in (tags if tags is not None else (existing.get("tags") if existing else [])) if str(x).strip()]
            clean_scope = workspace_scope.strip() or "."
            _resolve_workdir(st["binding"]["project_path"], clean_scope)
            eff_sb = _effective_sandbox(st["binding"].get("max_sandbox", "workspace-write"), sandbox)
            new_mem = {
                "id": member_id,
                "name": (name or (existing.get("name") if existing else member_id)).strip(),
                "role": role_id,
                "client": client,
                "model": use_model,
                "reasoning_effort": reasoning_effort or "medium",
                "specialty": (specialty if specialty is not None else (existing.get("specialty", "") if existing else "")).strip(),
                "persona_prompt": (persona_prompt if persona_prompt is not None else (existing.get("persona_prompt", "") if existing else "")).strip(),
                "grounding_mode": gm,
                "tags": clean_tags,
                "workspace_scope": clean_scope,
                "sandbox": eff_sb,
                "session_id": existing.get("session_id") if existing and existing.get("client") == client else None,
                "session_history": existing.get("session_history", []) if existing else [],
                "config_history": existing.get("config_history", []) if existing else [],
                "pending_config": None,
                "status": "idle",
                "live_activity": "",
                "current_task_id": None,
                "current_attempt_id": None,
            }
            enrich_member_metadata(new_mem)
            st["members"][member_id] = new_mem
            return st["members"][member_id]

        return self.store.mutate(_upd)

    def delete_member(self, member_id: str) -> Dict[str, Any]:
        def _del(st: Dict[str, Any]) -> Dict[str, Any]:
            m = st["members"].get(member_id)
            if not m:
                raise ValueError(f"成员不存在: {member_id}")
            if m.get("status") == "working":
                raise ValueError("成员正在执行任务，请先中止当前尝试后再移除")
            return st["members"].pop(member_id)

        return self.store.mutate(_del)

    def switch_member_config(
        self,
        member_id: str,
        name: Optional[str] = None,
        client: Optional[str] = None,
        role_id: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        workspace_scope: Optional[str] = None,
        sandbox: Optional[str] = None,
        specialty: Optional[str] = None,
        persona_prompt: Optional[str] = None,
        grounding_mode: Optional[str] = None,
        tags: Optional[List[str]] = None,
        mode: str = "next_round",
        reason: str = "用户调整成员配置",
    ) -> Dict[str, Any]:
        snap = self.store.snapshot()
        m = snap["members"].get(member_id)
        if not m:
            raise ValueError(f"成员不存在: {member_id}")

        clean_name = name.strip() if (name is not None and name.strip()) else m.get("name", member_id)
        target_client = (client or m.get("client", "codex")).strip().lower()
        target_model = model if model is not None else m.get("model")
        target_effort = reasoning_effort if reasoning_effort is not None else m.get("reasoning_effort")
        m_check = None if target_model in ("client_default", "client_current_setting") else target_model
        e_check = None if target_effort in ("client_default", "client_current_setting") else target_effort
        self._validate_client_model_effort(target_client, m_check, e_check)

        clean_gm = (grounding_mode or m.get("grounding_mode", "strict")).strip().lower()
        if clean_gm not in ("strict", "balanced"):
            clean_gm = "strict"
        clean_spec = specialty.strip() if specialty is not None else m.get("specialty", "")
        clean_persona = persona_prompt.strip() if persona_prompt is not None else m.get("persona_prompt", "")
        clean_tags = [str(x).strip() for x in tags if str(x).strip()] if tags is not None else list(m.get("tags") or [])
        clean_ws = (workspace_scope if workspace_scope is not None else m.get("workspace_scope", ".")).strip() or "."
        _resolve_workdir(snap["binding"]["project_path"], clean_ws)
        eff_sb = _effective_sandbox(
            snap["binding"].get("max_sandbox", "workspace-write"),
            sandbox if sandbox is not None else m.get("sandbox", "read-only"),
        )

        new_cfg = {
            "name": clean_name,
            "client": target_client,
            "role": role_id or m.get("role"),
            "model": target_model,
            "reasoning_effort": target_effort,
            "workspace_scope": clean_ws,
            "sandbox": eff_sb,
            "specialty": clean_spec,
            "persona_prompt": clean_persona,
            "grounding_mode": clean_gm,
            "tags": clean_tags,
            "reason": reason,
            "requested_at": time.time(),
        }

        if m.get("status") == "working":
            if mode == "abort_now":
                cur_att = m.get("current_attempt_id")
                cur_task = m.get("current_task_id")
                if cur_att:
                    self.abort_attempt(task_id=cur_task, attempt_id=cur_att, reason=f"中止并切换配置: {reason}")
                deadline = time.time() + 4.0
                while time.time() < deadline:
                    if self.store.snapshot()["members"].get(member_id, {}).get("status") != "working":
                        break
                    time.sleep(0.1)
            else:
                def _set_pending(st: Dict[str, Any]) -> Dict[str, Any]:
                    mem = st["members"][member_id]
                    mem["name"] = clean_name
                    mem["specialty"] = clean_spec
                    mem["persona_prompt"] = clean_persona
                    mem["grounding_mode"] = clean_gm
                    mem["tags"] = clean_tags
                    exec_changed = any(
                        new_cfg[k] != mem.get(k)
                        for k in ("client", "role", "model", "reasoning_effort", "workspace_scope", "sandbox")
                    )
                    if exec_changed:
                        mem["pending_config"] = new_cfg
                        return {"applied": "next_round", "member": mem}
                    return {"applied": "immediate", "member": mem}

                return self.store.mutate(_set_pending)

        def _apply_now(st: Dict[str, Any]) -> Dict[str, Any]:
            mem = st["members"][member_id]
            if new_cfg["role"] not in st["roles"]:
                raise ValueError(f"未知角色: {new_cfg['role']}")
            client_changed = mem.get("client") != new_cfg["client"]
            mem.setdefault("config_history", []).append(
                {
                    "changed_at": time.time(),
                    "from": {
                        "name": mem.get("name"),
                        "client": mem.get("client"),
                        "role": mem.get("role"),
                        "model": mem.get("model"),
                        "reasoning_effort": mem.get("reasoning_effort"),
                    },
                    "to": new_cfg,
                    "reason": reason,
                }
            )
            if client_changed and mem.get("session_id"):
                mem.setdefault("session_history", []).append(mem["session_id"])
                mem["session_id"] = None
            mem["name"] = clean_name
            mem["client"] = new_cfg["client"]
            mem["role"] = new_cfg["role"]
            mem["model"] = new_cfg["model"]
            mem["reasoning_effort"] = new_cfg["reasoning_effort"]
            mem["workspace_scope"] = new_cfg["workspace_scope"]
            mem["sandbox"] = new_cfg["sandbox"]
            mem["specialty"] = clean_spec
            mem["persona_prompt"] = clean_persona
            mem["grounding_mode"] = clean_gm
            mem["tags"] = clean_tags
            mem["pending_config"] = None
            return {"applied": "immediate", "member": mem}

        return self.store.mutate(_apply_now)

    # ------------------------------------------------------------------
    # Multi-Topology Task Creation (Roundtable / Chief / Custom DAG / Solo)
    # ------------------------------------------------------------------

    def set_focus_task(self, task_id: Optional[str]) -> Dict[str, Any]:
        def _foc(st: Dict[str, Any]) -> Dict[str, Any]:
            if task_id is None:
                st["active_task_id"] = None
                return {"active_task_id": None}
            t = self._find_task(st, task_id)
            t["archived"] = False
            st["active_task_id"] = task_id
            return {"active_task_id": task_id, "task": t}

        return self.store.mutate(_foc)

    def archive_task(self, task_id: str, archived: bool = True) -> Dict[str, Any]:
        def _arch(st: Dict[str, Any]) -> Dict[str, Any]:
            t = self._find_task(st, task_id)
            t["archived"] = bool(archived)
            if archived and st.get("active_task_id") == task_id:
                cur_norm = st["binding"]["normalized_project_path"]
                others = [
                    x for x in st["tasks"]
                    if not x.get("archived", False)
                    and x["id"] != task_id
                    and normalize_win_path(x.get("project_path", "")) == cur_norm
                ]
                st["active_task_id"] = others[-1]["id"] if others else None
            elif not archived:
                st["active_task_id"] = task_id
            return {"active_task_id": st.get("active_task_id"), "task": t}

        return self.store.mutate(_arch)

    def archive_completed_tasks(self, keep_active: bool = False) -> Dict[str, Any]:
        def _sweep(st: Dict[str, Any]) -> Dict[str, Any]:
            count = 0
            active_id = st.get("active_task_id")
            for t in st["tasks"]:
                if t.get("archived"):
                    continue
                if keep_active and t["id"] == active_id:
                    continue
                if t["status"] in ("succeeded", "failed", "aborted"):
                    t["archived"] = True
                    count += 1
                    if st.get("active_task_id") == t["id"]:
                        st["active_task_id"] = None
            return {"archived_count": count, "active_task_id": st.get("active_task_id")}

        return self.store.mutate(_sweep)

    def create_task(
        self,
        title: str,
        goal: str,
        dispatch_mode: str = "single_member",
        execution_mode: Optional[str] = None,
        target_member_id: Optional[str] = "codex_author",
        reviewer_member_id: Optional[str] = "agy_claude_reviewer",
        chief_member_id: Optional[str] = None,
        host_member_id: Optional[str] = None,
        participants: Optional[List[str]] = None,
        discussion_rounds: int = 2,
        enable_host_opening: bool = False,
        stages: Optional[List[List[Dict[str, Any]]]] = None,
        auto_template: str = "standard_review_loop",
        steps: Optional[List[Dict[str, Any]]] = None,
        project_path: Optional[str] = None,
        workspace_scope: Optional[str] = None,
        sandbox: str = "read-only",
        max_auto_repairs: Optional[int] = None,
        require_manual_ruling: bool = False,
        reuse_member_session: bool = False,
        archive_previous_done: bool = True,
        research_spec: Optional[Dict[str, Any]] = None,
        openscience_playbooks: Optional[List[str]] = None,
        squad_preset: str = "",
        chief_max_members: Optional[int] = None,
        anonymous_peer_review: bool = False,
        enable_worktree_isolation: Optional[bool] = None,
        task_token_budget: Optional[int] = None,
        allow_member_writes: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if project_path:
            self.switch_project(project_path)

        title = title.strip() or goal.strip().splitlines()[0][:36] or "未命名任务"
        goal = goal.strip()
        if not goal:
            raise ValueError("任务目标不能为空")

        mode = (execution_mode or dispatch_mode or "single_member").strip()
        now = time.time()
        task_id = _new_id("TASK")

        def _create(st: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal mode, sandbox
            if archive_previous_done and st.get("active_task_id"):
                try:
                    prev_t = self._find_task(st, st["active_task_id"])
                    if prev_t["status"] in ("succeeded", "aborted", "failed"):
                        prev_t["archived"] = True
                except Exception:
                    pass

            proj_abs = st["binding"]["project_path"]
            proj_norm = st["binding"]["normalized_project_path"]
            proj_name = st["binding"].get("project_name") or os.path.basename(proj_abs)
            max_sb = st["binding"].get("max_sandbox", "workspace-write")
            sandbox = _effective_sandbox(max_sb, sandbox)
            default_repairs = st.get("settings", {}).get("default_max_auto_repairs", 2)
            repairs_cap = default_repairs if max_auto_repairs is None else max(0, min(5, int(max_auto_repairs)))

            built_steps: List[Dict[str, Any]] = []
            mode_meta: Dict[str, Any] = {}
            scope_default = (workspace_scope or ".").strip() or "."
            _resolve_workdir(proj_abs, scope_default)

            if mode == "roundtable":
                # 1. Roundtable Conference Collaboration
                valid_ids = [m for m in (participants or []) if m in st["members"]]
                if len(valid_ids) < 2:
                    raise ValueError("圆桌协作请至少勾选 2 位参会成员")
                host_id = host_member_id if (host_member_id and host_member_id in st["members"]) else valid_ids[0]
                rounds = max(1, min(3, int(discussion_rounds or 2)))
                anon_enabled = bool(anonymous_peer_review)
                anon_map: Dict[str, str] = {}
                if anon_enabled:
                    # Deterministic per-task hash permutation so seat labels (席位 A/B/C...) don't leak model order!
                    perm_ids = sorted(
                        valid_ids,
                        key=lambda x: hashlib.sha256(f"{task_id}:{x}".encode("utf-8")).hexdigest(),
                    )
                    for ai, am_id in enumerate(perm_ids):
                        letter = chr(ord("A") + (ai % 26))
                        anon_map[am_id] = f"席位 {letter}"
                mode_meta = {
                    "host_member_id": host_id,
                    "participants": valid_ids,
                    "discussion_rounds": rounds,
                    "enable_host_opening": bool(enable_host_opening),
                    "anonymous_peer_review": anon_enabled,
                    "anon_map": anon_map,
                }

                roster_desc = "、".join(f"@{mid}({st['members'][mid]['name']})" for mid in valid_ids)
                prev_round_indices: List[int] = []

                if enable_host_opening:
                    built_steps.append(
                        self._make_step(
                            step_index=0,
                            title="主持人开场破题与议题分工",
                            step_type="opening",
                            stage_index=0,
                            round_label="开场定调",
                            member_id=host_id,
                            prompt=(
                                f"你是本次圆桌会议的主持人。参会成员有：{roster_desc}。\n"
                                f"总任务目标：{goal}\n"
                                "请先快速查看项目现状，用简洁清晰的语言拆解核心争点，并点名每位参会成员接下来重点核查/攻关的角度（暂不输出最终定稿）。"
                            ),
                            depends_on=[],
                            review_of=None,
                            read_only=True,
                            write_scope=[],
                            workspace_scope=scope_default,
                            sandbox="read-only",
                        )
                    )
                    prev_round_indices = [0]

                for r_num in range(1, rounds + 1):
                    cur_round_indices: List[int] = []
                    for mid in valid_ids:
                        idx = len(built_steps)
                        cur_round_indices.append(idx)
                        m_info = st["members"][mid]
                        m_name = m_info["name"]
                        m_role = m_info.get("role", "implementation")
                        m_tags = set(m_info.get("tags") or [])
                        if r_num == 1:
                            r_title = f"首轮盲评独立立论 ({m_name})"
                            r_label = "第 1 轮 · 盲评立论"
                            r_prompt = (
                                f"你是本次圆桌正式参会成员（同席参会者：{roster_desc}）。当前处于【第 1 轮独立盲评阶段（Blind Round 1）】，你看不到其他队友的本轮草稿，以杜绝早期锚定偏倚。\n"
                                "请结合你的专业职责独立检查当前项目材料，并按以下高质量科研/工程标准作答：\n"
                                "1. 事实锚定：先读后引，所有项目内文件、代码行号与统计数值均标注 `[文件名](路径)`；\n"
                                "2. 多路径发散与合理深度外推：若任务涉及方案选择、机理阐释或科研创新，请提出 ≥2 条真正正交的竞争假说/架构路线（含更简洁的竞争性解释），并用 `[实证观察]` / `[统计推断]` / `[机制假说/深度外推 · 证伪条件]` 清晰分层，拒绝回避解读；\n"
                                "3. 如有必须由本场特定队友核验的关键声明，可在末尾附 `team_consult` 发起结构化核验。\n"
                                f"总目标：{goal}"
                            )
                        else:
                            r_title = f"第{r_num}轮对抗质询与锦标赛演化 ({m_name})"
                            r_label = f"第 {r_num} 轮 · 对抗互评"
                            if m_role == "review" or "fact_check" in m_tags:
                                role_lens = (
                                    "【你的本轮认知分工：红队审稿与证伪官（Skeptical Challenger）】\n"
                                    "- 先用 1~2 句 Steelman 精准复述队友最强论点，再亲自打开工作区文件独立复核队友引用的 ≥2 处关键数字、路径或前提假设；\n"
                                    "- 明确列出 `BLOCKING`（动摇核心结论的硬伤/幻觉/统计或逻辑漏洞）与 `OBSERVATION`（优化建议），并检验是否存在更简洁的竞争性解释或未覆盖边界。"
                                )
                            elif "openscience" in m_tags or "hypothesis" in m_tags or m_role == "documentation":
                                role_lens = (
                                    "【你的本轮认知分工：假说锦标赛评比与演化官（Evolution & Tournament Lead）】\n"
                                    "- 对各队友第 1 轮提出的核心方案/假说做横向成对比较（证据强度 × 创新深度 × 证伪成本），指出谁的论据被实证打穿、谁提出了最值得嫁接的跨学科/架构亮点；\n"
                                    "- 遵循多样性保留原则：在进化出更优综合方案的同时，保留有独特价值的亚军正交路径及最小判别实验。"
                                )
                            else:
                                role_lens = (
                                    "【你的本轮认知分工：实证复算与工程现实核验官（Empirical & Engineering Verifier）】\n"
                                    "- 亲自检查队友方案的可落地性、数据/统计口径一致性（防伪重复/泄漏）、代码接口兼容性与边界条件；\n"
                                    "- 至少验证 2 处队友声明并给出可执行的机械验收标准（Acceptance Checks）或最小判别实验脚本设计。"
                                )
                            r_prompt = (
                                "请仔细阅读共享群聊中其他参会队友上一轮的全部发言，并执行严格交叉核验：\n"
                                f"{role_lens}\n"
                                "【强制沟通纪律】\n"
                                "1. 严禁盲目附和或复读队友观点；发现数字偏差、未读先猜、过度外推或怯懦回避解读时，必须直接 `@名字`（匿名模式下点名 `席位 X`）纠错并附真实证据锚点；\n"
                                "2. 给出经过质询压力测试后修正并演化的收敛建议。"
                            )
                            if anon_enabled:
                                peer_labels = [anon_map[p] for p in valid_ids if p != mid and p in anon_map]
                                r_prompt += (
                                    "\n\n【匿名互评与 Borda 共识计票协议（已隐藏所有队友的模型与姓名，消除名望偏见）】\n"
                                    f"你在本场被匿名化为 `{anon_map.get(mid, '本席')}`，其余待评席位为：{', '.join(peer_labels)}。\n"
                                    "请仅基于论据扎实度、代码/数据核验质量与假说创新度，在文末附上结构化排序票：\n"
                                    "```peer_ranking\n"
                                    f'{{"ranking":{json.dumps(peer_labels, ensure_ascii=False)},"confidence":0.85,"rationale":"简述排名第一席位的核心实证优势"}}\n'
                                    "```"
                                )
                        eff_rt_writes = (
                            bool(allow_member_writes)
                            if allow_member_writes is not None
                            else bool(st.get("settings", {}).get("roundtable_allow_member_writes", False))
                        )
                        mem_can_write = (
                            sandbox == "workspace-write"
                            and eff_rt_writes
                            and st.get("members", {}).get(mid, {}).get("sandbox") == "workspace-write"
                        )
                        built_steps.append(
                            self._make_step(
                                step_index=idx,
                                title=r_title,
                                step_type="discussion",
                                stage_index=r_num,
                                round_label=r_label,
                                member_id=mid,
                                prompt=r_prompt,
                                depends_on=list(prev_round_indices),
                                review_of=None,
                                read_only=not mem_can_write,
                                write_scope=[scope_default] if mem_can_write else [],
                                workspace_scope=scope_default,
                                sandbox="workspace-write" if mem_can_write else "read-only",
                            )
                        )
                    prev_round_indices = cur_round_indices

                # Final Host Synthesis & Execution
                fin_idx = len(built_steps)
                ro_final = sandbox == "read-only"
                built_steps.append(
                    self._make_step(
                        step_index=fin_idx,
                        title="主持人多维仲裁与终稿演化交付",
                        step_type="synthesis",
                        stage_index=rounds + 1,
                        round_label="终稿拍板与交付",
                        member_id=host_id,
                        prompt=(
                            "作为圆桌主持人，请综合群聊中全体成员的多轮讨论与质询意见，明确说明采纳点与分歧裁决，并完成最终任务交付（若开启了写入权限请直接落地修改文件并核验）：\n"
                            f"{goal}\n\n"
                            "【终稿质量与 OpenScience 演化交付要求】\n"
                            "1. 结论先行、不和稀泥：给出最受证据支持的主推荐路线/结论、置信度，以及“什么新观测/实验结果会推翻该结论”；\n"
                            "2. 多维仲裁与演化矩阵（M-MAD Grid）：用简洁 Markdown 表对比各候选方案/假说在「实证扎实度」「方法/工程严谨度」「创新与合理外推价值」「执行与证伪成本」上的表现，说明采纳了谁的实证纠错、驳回了哪些缺陷论点，并保留 1~2 条有价值的亚军正交备选路线；\n"
                            "3. 认知三层标定与完整溯源：完整保留已核实的数据表与 `[文件名](路径)` 真实链接，严格区分 `[实证观察]`、`[统计/逻辑推断]` 与 `[机制假说/深度外推 · 证伪条件]`，文末附【证据溯源与外推假说台账】。"
                        ),
                        depends_on=list(prev_round_indices),
                        review_of=None,
                        read_only=ro_final,
                        write_scope=[] if ro_final else [scope_default],
                        workspace_scope=scope_default,
                        sandbox=sandbox,
                    )
                )
                initial_status = "queued" if st.get("paused") else "running"

            elif mode == "chief_dispatch":
                # 2. Chief Dynamic Roster Selection & DAG Dispatch
                runnable = [k for k, v in st["members"].items() if v.get("client") in SUPPORTED_CLIENTS]
                chief_id = chief_member_id if (chief_member_id and chief_member_id in st["members"]) else (runnable[0] if runnable else "codex_author")
                chief_cap = max(1, min(12, int(chief_max_members))) if chief_max_members is not None else min(6, max(1, len(runnable)))
                mode_meta = {"chief_member_id": chief_id, "chief_max_members": chief_cap, "recruited_members": []}

                roster_lines = [
                    f'- id="{k}" | 姓名="{st["members"][k]["name"]}" | 职责="{st["roles"].get(st["members"][k]["role"], {}).get("name", "")}" | 引擎={st["members"][k]["client"]}/{st["members"][k]["model"]}'
                    for k in runnable
                ]
                built_steps.append(
                    self._make_step(
                        step_index=0,
                        title="主帅察看项目与动态点将排班",
                        step_type="chief_plan",
                        stage_index=0,
                        round_label="主帅点将",
                        member_id=chief_id,
                        prompt=(
                            f"你是本次任务的主帅总管。请先快速查看当前项目目录概况，再从以下真实团队花名册中按需点选最合适的 1~{chief_cap} 名成员，编排串行/并行协作与审查步骤（无需所有人都上场）：\n"
                            + "\n".join(roster_lines)
                            + f"\n\n【用户任务目标】{goal}\n"
                            + f"【用户授予的文件权限】sandbox={sandbox}（若为 read-only 则所有步骤 read_only 必须为 true；若为 workspace-write 则实施写文件步骤可设 read_only=false）\n\n"
                            + "请先用一段话说明你的【点将理由与战术分工】，随后在末尾严格附上如下 JSON 代码块：\n"
                            + "```chief_plan\n"
                            + '{"rationale":"一句话战术总结","steps":[\n'
                            + '  {"title":"步骤名","member_id":"花名册中的id","step_type":"execution 或 discussion 或 review","depends_on":[],"review_of":null,"read_only":true,"prompt":"给该成员的具体指令"}\n'
                            + "]}\n```"
                        ),
                        depends_on=[],
                        review_of=None,
                        read_only=True,
                        write_scope=[],
                        workspace_scope=scope_default,
                        sandbox="read-only",
                    )
                )
                initial_status = "queued" if st.get("paused") else "running"

            elif mode == "custom_dag" and stages:
                # 3. User-Built Multi-Stage Parallel/Serial Blocks
                prev_stage_step_indices: List[int] = []
                last_exec_step_idx: Optional[int] = None
                stage_meta_list: List[List[int]] = []

                for stg_i, stage_nodes in enumerate(stages):
                    if not stage_nodes:
                        continue
                    cur_indices: List[int] = []
                    for node in stage_nodes:
                        mid = node.get("member_id") or "codex_author"
                        if mid not in st["members"]:
                            raise ValueError(f"搭积木阶段 {stg_i + 1} 中的成员不存在: {mid}")
                        ntype = node.get("step_type") or "execution"
                        idx = len(built_steps)
                        cur_indices.append(idx)

                        rev_target = None
                        if ntype == "review" and last_exec_step_idx is not None and len(stage_nodes) == 1:
                            rev_target = last_exec_step_idx

                        req_shell = bool(node.get("requires_shell", False))
                        if req_shell and st["members"][mid].get("client") == "antigravity":
                            raise ValueError(
                                f"能力路由拦截：搭积木阶段 {stg_i + 1} 节点需要 Shell/脚本执行权限 (requires_shell=True)，不能指派给无头 Antigravity 节点 {mid}"
                            )
                        ro_node = True if ntype in ("review", "discussion") else (
                            bool(node["read_only"]) if "read_only" in node else (sandbox == "read-only")
                        )
                        node_sb = _effective_sandbox(sandbox, node.get("sandbox") or ("read-only" if ro_node else sandbox), read_only=ro_node)
                        ro_node = node_sb == "read-only"
                        node_ws = (node.get("workspace_scope") or scope_default).strip() or "."
                        _resolve_workdir(proj_abs, node_ws)
                        custom_p = (node.get("prompt") or "").strip()
                        node_prompt = (
                            f"{custom_p}\n\n（所属总任务目标：{goal}）"
                            if custom_p
                            else (
                                f"请对照总目标独立复核前序阶段成果：{goal}"
                                if ntype == "review"
                                else f"请按你的角色职责完成本阶段工作（可参考前序阶段队友成果，必要时用 team_consult @队友协商）：\n{goal}"
                            )
                        )
                        built_steps.append(
                            self._make_step(
                                step_index=idx,
                                title=(node.get("title") or "").strip() or f"第{stg_i + 1}站 · {st['members'][mid]['name']}",
                                step_type=ntype,
                                stage_index=stg_i,
                                round_label=f"第 {stg_i + 1} 阶段",
                                member_id=mid,
                                prompt=node_prompt,
                                depends_on=list(prev_stage_step_indices),
                                review_of=rev_target,
                                read_only=ro_node,
                                write_scope=[] if ro_node else [node_ws],
                                workspace_scope=node_ws,
                                sandbox=node_sb,
                                requires_shell=req_shell,
                            )
                        )
                        if ntype in ("execution", "synthesis"):
                            last_exec_step_idx = idx
                    stage_meta_list.append(cur_indices)
                    prev_stage_step_indices = cur_indices

                if not built_steps:
                    raise ValueError("自定义流水线至少需要包含 1 个工位节点")
                mode_meta = {"stage_groups": stage_meta_list}
                initial_status = "queued" if st.get("paused") else "running"

            elif steps:
                mode = "custom_dag" if execution_mode == "custom_dag" else mode
                for idx, raw_s in enumerate(steps):
                    mid = raw_s["member_id"]
                    if mid not in st["members"]:
                        raise ValueError(f"步骤 {idx} 指定的成员不存在: {mid}")
                    req_shell = bool(raw_s.get("requires_shell", False))
                    if req_shell and st["members"][mid].get("client") == "antigravity":
                        raise ValueError(
                            f"能力路由拦截：步骤 {idx} 需要 Shell/脚本执行权限 (requires_shell=True)，不能指派给无头 Antigravity 节点 {mid}，请改派 Codex 成员或走 external_import 模式"
                        )
                    ro = bool(raw_s.get("read_only", True))
                    step_sb = _effective_sandbox(sandbox, raw_s.get("sandbox") or ("read-only" if ro else sandbox), read_only=ro)
                    ro = step_sb == "read-only"
                    step_ws = (raw_s.get("workspace_scope") or scope_default).strip() or "."
                    _resolve_workdir(proj_abs, step_ws)
                    w_scope = [] if ro else (raw_s.get("write_scope") or raw_s.get("file_scope") or [])
                    built_steps.append(
                        self._make_step(
                            step_index=idx,
                            title=raw_s.get("title") or f"步骤 {idx}",
                            step_type=raw_s.get("step_type") or ("review" if raw_s.get("review_of") is not None else "execution"),
                            stage_index=int(raw_s.get("stage_index", idx)),
                            round_label=raw_s.get("round_label", f"步骤 {idx + 1}"),
                            member_id=mid,
                            prompt=raw_s["prompt"],
                            depends_on=[int(x) for x in raw_s.get("depends_on", [])],
                            review_of=int(raw_s["review_of"]) if raw_s.get("review_of") is not None else None,
                            read_only=ro,
                            write_scope=w_scope,
                            workspace_scope=step_ws,
                            sandbox=step_sb,
                            initial_session_id=raw_s.get("session_id"),
                            requires_shell=req_shell,
                        )
                    )
                initial_status = "queued" if st.get("paused") else "running"

            elif mode == "single_member":
                mid = target_member_id or "codex_author"
                if mid not in st["members"]:
                    raise ValueError(f"指定成员不存在: {mid}")
                mem_sess = st["members"][mid].get("session_id") if reuse_member_session else None
                built_steps.append(
                    self._make_step(
                        step_index=0,
                        title=title,
                        step_type="execution",
                        stage_index=0,
                        round_label="专员直办",
                        member_id=mid,
                        prompt=goal,
                        depends_on=[],
                        review_of=None,
                        read_only=(sandbox == "read-only"),
                        write_scope=[] if sandbox == "read-only" else [scope_default],
                        workspace_scope=scope_default,
                        sandbox=sandbox,
                        initial_session_id=mem_sess,
                    )
                )
                mode_meta = {"specialist_id": mid}
                initial_status = "queued" if st.get("paused") else "running"

            elif auto_template == "standard_review_loop":
                exec_mid = target_member_id or "codex_author"
                rev_mid = reviewer_member_id or ("agy_claude_reviewer" if "agy_claude_reviewer" in st["members"] else "codex_reviewer")
                if exec_mid not in st["members"] or rev_mid not in st["members"]:
                    raise ValueError("执行成员或审查成员不存在")
                built_steps.append(
                    self._make_step(
                        step_index=0,
                        title="方案实施与结果输出",
                        step_type="execution",
                        stage_index=0,
                        round_label="实施交付",
                        member_id=exec_mid,
                        prompt=goal,
                        depends_on=[],
                        review_of=None,
                        read_only=(sandbox == "read-only"),
                        write_scope=[] if sandbox == "read-only" else [scope_default],
                        workspace_scope=scope_default,
                        sandbox=sandbox,
                    )
                )
                built_steps.append(
                    self._make_step(
                        step_index=1,
                        title="独立质量与证据复核",
                        step_type="review",
                        stage_index=1,
                        round_label="独立复核",
                        member_id=rev_mid,
                        prompt=f"请对照原始目标独立审查步骤 0 的交付物是否完整、准确、无偏离。\n原始目标：{goal}",
                        depends_on=[0],
                        review_of=0,
                        read_only=True,
                        write_scope=[],
                        workspace_scope=scope_default,
                        sandbox="read-only",
                    )
                )
                initial_status = "queued" if st.get("paused") else "running"
            else:
                initial_status = "awaiting_coordinator"

            task_obj: Dict[str, Any] = {
                "id": task_id,
                "title": title,
                "goal": goal,
                "project_path": proj_abs,
                "normalized_project_path": proj_norm,
                "project_name": proj_name,
                "archived": False,
                "dispatch_mode": dispatch_mode,
                "execution_mode": mode,
                "mode_meta": mode_meta,
                "sandbox": sandbox,
                "workspace_scope": scope_default,
                "worktree_isolation": (
                    bool(enable_worktree_isolation)
                    if enable_worktree_isolation is not None
                    else bool(st.get("settings", {}).get("enable_git_worktree_isolation", False))
                ),
                "task_token_budget": (
                    max(0, int(task_token_budget))
                    if task_token_budget is not None
                    else int(st.get("settings", {}).get("task_token_budget", 0) or 0)
                ),
                "auto_template": auto_template,
                "require_manual_ruling": require_manual_ruling,
                "max_auto_repairs": repairs_cap,
                "status": initial_status,
                "current_step_index": 0 if built_steps else None,
                "blocking_reason": "当前派发已暂停，排队等待" if st.get("paused") else None,
                "steps": built_steps,
                "chat_room": [],
                "deliverables": [],
                "recent_events": [
                    {
                        "ts": now,
                        "type": "task.created",
                        "summary": f"启动 [{mode}] 协作任务（初始共 {len(built_steps)} 个节点）",
                    }
                ],
                "pending_questions": None,
                "messages": [],
                "reviews": [],
                "rulings": [],
                "openscience_playbooks": (
                    [str(x) for x in openscience_playbooks if str(x).strip()]
                    if openscience_playbooks is not None
                    else auto_select_playbooks(goal, execution_mode=mode, squad_preset=squad_preset)
                ),
                "research_spec": research_spec if isinstance(research_spec, dict) else None,
                "research_signoff": (
                    {"status": "pending", "checks": [], "figure_reports": [], "blocking": [], "observations": []}
                    if isinstance(research_spec, dict)
                    else None
                ),
                "summary": "",
                "created_at": now,
                "updated_at": now,
            }
            self._append_room_chat(
                st,
                task_obj,
                sender_id="user",
                kind="user_goal",
                content=goal,
                round_label="任务发起",
            )
            st["tasks"].append(task_obj)
            st["active_task_id"] = task_id
            return task_obj

        created = self.store.mutate(_create)
        self._wakeup.set()
        return created

    def _make_step(
        self,
        step_index: int,
        title: str,
        step_type: str,
        member_id: str,
        prompt: str,
        depends_on: List[int],
        review_of: Optional[int],
        read_only: bool,
        write_scope: List[str],
        workspace_scope: str,
        sandbox: str,
        stage_index: int = 0,
        round_label: str = "",
        initial_session_id: Optional[str] = None,
        requires_shell: bool = False,
    ) -> Dict[str, Any]:
        return {
            "step_index": step_index,
            "title": title,
            "step_type": step_type,
            "stage_index": stage_index,
            "round_label": round_label,
            "member_id": member_id,
            "prompt": prompt,
            "depends_on": depends_on,
            "review_of": review_of,
            "read_only": read_only,
            "requires_shell": bool(requires_shell),
            "write_scope": write_scope,
            "workspace_scope": workspace_scope,
            "sandbox": sandbox,
            "worker_session_id": initial_session_id,
            "status": "pending",
            "repair_count": 0,
            "consult_count": 0,
            "pending_repair_brief": None,
            "pending_user_answer": None,
            "pending_questions": None,
            "handoff_context": None,
            "blocking_reason": None,
            "accepted_attempt_id": None,
            "latest_result_text": "",
            "attempts": [],
        }

    # ------------------------------------------------------------------
    # Live User Chat Interjection, Answer Questions, Followup & Rulings
    # ------------------------------------------------------------------

    def interject_chat(
        self,
        task_id: str,
        content: str,
        target_member_id: Optional[str] = None,
        trigger_reply: bool = True,
    ) -> Dict[str, Any]:
        """Allow human user to post directly into the live Team Chat Room and optionally @wake a member!"""
        content = content.strip()
        if not content:
            raise ValueError("发言内容不能为空")

        def _interj(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            self._append_room_chat(
                st,
                task,
                sender_id="user",
                kind="user_interjection",
                content=content,
                to_member_id=target_member_id or None,
                round_label="负责人插话",
            )
            task["updated_at"] = time.time()

            # If task is currently not running (or user explicitly @picked an idle member to respond), append a live reply turn!
            if trigger_reply and task["status"] not in ("running",):
                chosen_mid = target_member_id
                if not chosen_mid or chosen_mid not in st["members"]:
                    chosen_mid = (
                        task.get("mode_meta", {}).get("host_member_id")
                        or task.get("mode_meta", {}).get("chief_member_id")
                        or (task["steps"][-1]["member_id"] if task.get("steps") else "codex_author")
                    )
                prev_same_sess = next(
                    (s.get("worker_session_id") for s in reversed(task.get("steps", [])) if s["member_id"] == chosen_mid and s.get("worker_session_id")),
                    None,
                )
                prev_done_indices = [s["step_index"] for s in task.get("steps", []) if s["status"] == "succeeded"]
                new_idx = len(task["steps"])
                ro = task.get("sandbox", "read-only") == "read-only"
                sc = task.get("workspace_scope", ".")
                task["steps"].append(
                    self._make_step(
                        step_index=new_idx,
                        title=f"回应负责人指示 ({st['members'][chosen_mid]['name']})",
                        step_type="execution",
                        stage_index=new_idx,
                        round_label="回应负责人",
                        member_id=chosen_mid,
                        prompt=f"项目负责人在群聊中向你发出新指示：\n{content}\n\n请结合当前项目与群聊上下文直接回应或执行交付。",
                        depends_on=prev_done_indices[-2:] if prev_done_indices else [],
                        review_of=None,
                        read_only=ro,
                        write_scope=[] if ro else [sc],
                        workspace_scope=sc,
                        sandbox=task.get("sandbox", "read-only"),
                        initial_session_id=prev_same_sess,
                    )
                )
                task["current_step_index"] = new_idx
                task["archived"] = False
                st["active_task_id"] = task["id"]
                task["status"] = "queued" if st.get("paused") else "running"
                task["blocking_reason"] = None
            return task

        res = self.store.mutate(_interj)
        self._wakeup.set()
        return res

    def _pick_cross_engine_checker(
        self,
        st: Dict[str, Any],
        author_id: Optional[str],
        author_client: Optional[str],
    ) -> str:
        runnable = [k for k, v in st["members"].items() if v.get("client") in SUPPORTED_CLIENTS]
        if not runnable:
            raise ValueError("当前没有可用的执行成员")

        def _score(mid: str) -> Tuple[int, int, int, int]:
            m = st["members"][mid]
            opp_engine = 1 if (author_client and m.get("client") != author_client) else 0
            diff_member = 1 if mid != author_id else 0
            tags = set(m.get("tags") or [])
            is_fc = 1 if ("fact_check" in tags or m.get("role") == "review") else 0
            is_idle = 1 if m.get("status") == "idle" else 0
            return (opp_engine, is_fc, diff_member, is_idle)

        return sorted(runnable, key=_score, reverse=True)[0]

    def fact_check_task(
        self,
        task_id: str,
        checker_member_id: Optional[str] = None,
        target_chat_id: Optional[str] = None,
        focus_note: str = "",
        auto_revise: bool = False,
    ) -> Dict[str, Any]:
        """Dispatch an independent, clean-session (preferentially cross-engine) anti-hallucination fact-check audit."""
        focus_note = (focus_note or "").strip()

        def _fc(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            target_text = ""
            target_label = "当前终稿交付"
            author_id: Optional[str] = None
            author_client: Optional[str] = None

            if target_chat_id:
                cm = next((c for c in task.get("chat_room", []) if c.get("id") == target_chat_id), None)
                if cm:
                    target_text = cm.get("content", "")
                    author_id = cm.get("sender_id")
                    author_client = cm.get("sender_client")
                    target_label = f"{cm.get('sender_name', author_id)} 的发言 ({cm.get('round_label') or cm.get('kind')})"

            if not target_text:
                target_text = task.get("summary") or ""
                if not target_text:
                    non_fc = [s for s in task.get("steps", []) if s.get("step_type") != "fact_check" and s.get("latest_result_text")]
                    if non_fc:
                        target_text = non_fc[-1]["latest_result_text"]
                        author_id = non_fc[-1]["member_id"]
                if not author_id:
                    synth = [s for s in task.get("steps", []) if s.get("step_type") == "synthesis"]
                    if synth:
                        author_id = synth[-1]["member_id"]
                    elif task.get("steps"):
                        author_id = task["steps"][-1]["member_id"]
                if author_id and author_id in st["members"]:
                    author_client = st["members"][author_id].get("client")

            if not target_text.strip():
                raise ValueError("当前任务尚无可核查的输出文本")

            if checker_member_id:
                if checker_member_id not in st["members"]:
                    raise ValueError(f"指定的核查专家不存在: {checker_member_id}")
                chosen_checker = checker_member_id
            else:
                chosen_checker = self._pick_cross_engine_checker(st, author_id, author_client)

            checker_mem = st["members"][chosen_checker]
            cross_engine = bool(author_client and checker_mem.get("client") != author_client)
            prev_done = [s["step_index"] for s in task.get("steps", []) if s["status"] == "succeeded"]
            check_idx = len(task["steps"])
            sc = task.get("workspace_scope", ".")

            focus_block = f"\n【负责人重点怀疑/要求核查点】\n{focus_note}\n" if focus_note else ""
            audit_prompt = (
                f"你现在以独立事实核查官身份（{'异构跨引擎交叉校验' if cross_engine else '独立会话复核'}），对【{target_label}】执行严格的反幻觉与事实核查审计。\n"
                f"【原始总目标】{task['goal']}{focus_block}\n"
                f"【待核查文本（已截取关键内容）】\n{_smart_clip(target_text, 9000)}\n\n"
                "【严格核查执行要求】\n"
                "1. 亲自读取文本中引用到的工作区真实文件、CSV/统计结果表或源码，逐项核对文件名、样本量、数字、代码符号与逻辑推导；\n"
                "2. 请严格按以下三段结构输出《事实核查与纠错审计报告》：\n"
                "   - **一、核查结论摘要**：给出核查项总数、已证实项数、发现硬错误/幻觉项数、推断待核实项数，以及是否建议立即修订终稿；\n"
                "   - **二、逐项证据对照表**（Markdown 表格）：列包含 `声明/数据` | `核查分级 (已核实 / 错误或幻觉 / 推断待核实)` | `真实文件证据链接 [文件名](路径)` | `纠错修订建议`；\n"
                "   - **三、必改错误与去幻觉清单**：按优先级列出必须删除、修正或降级为 `[推断/待外部核实]` 的条目（若全部准确请明确说明“未发现事实性错误”）。"
            )

            fc_step = self._make_step(
                step_index=check_idx,
                title=f"独立事实核查与纠错 ({checker_mem['name']})",
                step_type="fact_check",
                stage_index=check_idx,
                round_label="事实核查与纠错",
                member_id=chosen_checker,
                prompt=audit_prompt,
                depends_on=prev_done[-2:] if prev_done else [],
                review_of=None,
                read_only=True,
                write_scope=[],
                workspace_scope=sc,
                sandbox="read-only",
                initial_session_id=None,  # Clean unbiased session!
            )
            fc_step["audit_target_author_id"] = author_id
            fc_step["audit_cross_engine"] = cross_engine
            task["steps"].append(fc_step)

            if auto_revise:
                editor_id = (
                    (task.get("mode_meta") or {}).get("host_member_id")
                    or (author_id if author_id in st["members"] else None)
                    or chosen_checker
                )
                rev_idx = len(task["steps"])
                ro = task.get("sandbox", "read-only") == "read-only"
                task["steps"].append(
                    self._make_step(
                        step_index=rev_idx,
                        title=f"依据纠错审计修订终稿 ({st['members'][editor_id]['name']})",
                        step_type="synthesis",
                        stage_index=rev_idx,
                        round_label="依据核查修订终稿",
                        member_id=editor_id,
                        prompt=(
                            "请严格对照刚刚完成的《事实核查与纠错审计报告》，输出修正后的完整终稿：\n"
                            "1. 逐条修复审计指出的全部数字误差、路径错误或逻辑漏洞；\n"
                            "2. 对审计标记为「推断待核实」的外部声明，要么删除要么显式标注 `[推断/待外部核实]`；\n"
                            "3. 完整保留所有已核实的表格与 `[文件名](路径)` 证据链接，并在文首简要列出本次修订变更点。"
                        ),
                        depends_on=[check_idx],
                        review_of=None,
                        read_only=ro,
                        write_scope=[] if ro else [sc],
                        workspace_scope=sc,
                        sandbox=task.get("sandbox", "read-only"),
                        initial_session_id=None,
                    )
                )

            task["current_step_index"] = check_idx
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            task["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "task.fact_check_dispatched",
                    "summary": f"已派发独立事实核查：{checker_mem['name']} ({'异构跨引擎' if cross_engine else '独立会话'})",
                }
            )
            return {"task": task, "checker_member_id": chosen_checker, "cross_engine": cross_engine, "step_index": check_idx}

        res = self.store.mutate(_fc)
        self._wakeup.set()
        return res

    def revise_from_fact_check(
        self,
        task_id: str,
        editor_member_id: Optional[str] = None,
        extra_note: str = "",
    ) -> Dict[str, Any]:
        """Generate a clean revised deliverable addressing every issue in the latest fact-check audit."""
        extra_note = (extra_note or "").strip()

        def _rev(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            fc = task.get("latest_fact_check")
            if not fc or not (fc.get("content") or "").strip():
                raise ValueError("当前任务尚无已完成的事实核查报告")

            chosen_editor = editor_member_id if (editor_member_id and editor_member_id in st["members"]) else (
                (task.get("mode_meta") or {}).get("host_member_id")
                or fc.get("target_author_id")
                or fc.get("checker_id")
            )
            if not chosen_editor or chosen_editor not in st["members"]:
                chosen_editor = next(k for k, v in st["members"].items() if v.get("client") in SUPPORTED_CLIENTS)

            rev_idx = len(task["steps"])
            ro = task.get("sandbox", "read-only") == "read-only"
            sc = task.get("workspace_scope", ".")
            extra_block = f"\n【负责人补充修订指示】\n{extra_note}\n" if extra_note else ""
            task["steps"].append(
                self._make_step(
                    step_index=rev_idx,
                    title=f"按纠错清单修订终稿 ({st['members'][chosen_editor]['name']})",
                    step_type="synthesis",
                    stage_index=rev_idx,
                    round_label="依据核查修订终稿",
                    member_id=chosen_editor,
                    prompt=(
                        f"请严格依据独立事实核查官（{fc.get('checker_name')}）的《事实核查与纠错审计报告》修订终稿：{extra_block}\n"
                        f"【核查纠错报告全文】\n{_smart_clip(fc['content'], 8000)}\n\n"
                        f"【原终稿基准】\n{_smart_clip(task.get('summary') or '', 7000)}\n\n"
                        "要求：\n"
                        "1. 文首列出【本轮纠错修订摘要】（逐条回应审计发现的错误/幻觉如何修正）；\n"
                        "2. 输出完整、干净、可直接使用的修订后终稿，保留所有已核实的表格与 `[文件名](路径)` 真实证据链接；\n"
                        "3. 凡外部经验推断统一保留 `[推断/待外部核实]` 标注，严禁再次引入未核实数字。"
                    ),
                    depends_on=[fc["step_index"]] if isinstance(fc.get("step_index"), int) else [],
                    review_of=None,
                    read_only=ro,
                    write_scope=[] if ro else [sc],
                    workspace_scope=sc,
                    sandbox=task.get("sandbox", "read-only"),
                    initial_session_id=None,
                )
            )
            task["current_step_index"] = rev_idx
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return task

        res = self.store.mutate(_rev)
        self._wakeup.set()
        return res

    def reanswer_with_specialist(
        self,
        task_id: str,
        new_member_id: str,
        target_chat_id: Optional[str] = None,
        instruction: str = "",
        as_deliverable: bool = True,
    ) -> Dict[str, Any]:
        """One-click switch specialist on a deliverable or chat entry to produce an independent answer/revision."""
        instruction = (instruction or "").strip()

        def _reans(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            if new_member_id not in st["members"]:
                raise ValueError(f"目标专家不存在: {new_member_id}")
            mem = st["members"][new_member_id]
            ref_block = ""
            if target_chat_id:
                cm = next((c for c in task.get("chat_room", []) if c.get("id") == target_chat_id), None)
                if cm:
                    ref_block = f"\n【负责人指定的参考前序发言（来自 {cm.get('sender_name')}）】\n{_smart_clip(cm.get('content', ''), 5000)}\n"

            extra_inst = f"\n【负责人补充要求】\n{instruction}\n" if instruction else ""
            new_idx = len(task["steps"])
            ro = task.get("sandbox", "read-only") == "read-only"
            sc = task.get("workspace_scope", ".")
            task["steps"].append(
                self._make_step(
                    step_index=new_idx,
                    title=f"切换专家独立作答 ({mem['name']})",
                    step_type="synthesis" if as_deliverable else "discussion",
                    stage_index=new_idx,
                    round_label=f"切换专家 · {mem['name']}",
                    member_id=new_member_id,
                    prompt=(
                        f"项目负责人已切换指定由你（{mem['name']}）结合自身专长独立重新研判并回答本任务：\n"
                        f"【总目标】{task['goal']}{ref_block}{extra_inst}\n"
                        "请亲自核对工作区真实文件，给出比前序更严谨、证据更扎实、无幻觉的独立结论与完整方案。"
                    ),
                    depends_on=[],
                    review_of=None,
                    read_only=ro,
                    write_scope=[] if ro else [sc],
                    workspace_scope=sc,
                    sandbox=task.get("sandbox", "read-only"),
                    initial_session_id=None,
                )
            )
            task["current_step_index"] = new_idx
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            task["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "task.specialist_switched",
                    "summary": f"已切换专家由 {mem['name']} ({mem['client']}/{mem['model']}) 独立作答",
                }
            )
            return task

        res = self.store.mutate(_reans)
        self._wakeup.set()
        return res

    def answer_questions(
        self,
        task_id: str,
        answers: Optional[List[Dict[str, Any]]] = None,
        custom_note: str = "",
        use_recommended: bool = False,
    ) -> Dict[str, Any]:
        def _ans(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            pq = task.get("pending_questions")
            step_idx = pq["step_index"] if isinstance(pq, dict) and "step_index" in pq else (task.get("current_step_index") or 0)
            step = task["steps"][int(step_idx)]
            if step["status"] == "running":
                raise ValueError("该步骤已在运行中，请勿重复提交")

            qs_list = (pq.get("questions") if isinstance(pq, dict) else None) or step.get("pending_questions") or []
            lines = ["【用户已完成问题与选项确认，请沿用当前上下文直接完成交付（不要再次重复提问）】"]

            if use_recommended:
                for q in qs_list:
                    rec_opt = (q.get("options") or ["按推荐方案继续"])[0]
                    lines.append(f"- {q.get('question')} → 用户确认采用：{rec_opt}")
                if not qs_list:
                    lines.append("- 用户确认：请按你推荐的最优默认方案直接继续并完成交付。")
            else:
                ans_by_id = {str(a.get("id")): a for a in (answers or []) if isinstance(a, dict)}
                for idx, q in enumerate(qs_list):
                    qid = str(q.get("id") or f"q{idx + 1}")
                    a_obj = ans_by_id.get(qid, {})
                    selected = a_obj.get("selected")
                    sel_str = "；".join(str(x) for x in selected if str(x).strip()) if isinstance(selected, list) else str(selected or "").strip()
                    other_txt = str(a_obj.get("other") or "").strip()
                    combined = " | ".join(x for x in (sel_str, f"补充: {other_txt}" if other_txt else "") if x)
                    if not combined and q.get("options"):
                        combined = q["options"][0]
                    lines.append(f"- {q.get('question')} → 用户选择：{combined or '见整体补充说明'}")

            if custom_note and custom_note.strip():
                lines.append(f"- 用户补充说明：{custom_note.strip()}")

            formatted_reply = "\n".join(lines)
            now = time.time()

            step["pending_user_answer"] = formatted_reply
            step["pending_questions"] = None
            step["status"] = "pending"
            step["blocking_reason"] = None

            task["pending_questions"] = None
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["current_step_index"] = step["step_index"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = now

            self._append_room_chat(
                st,
                task,
                sender_id="user",
                kind="user_choice",
                content=formatted_reply,
                to_member_id=step["member_id"],
                step_index=step["step_index"],
                round_label="选项确认",
            )
            task["messages"].append(
                {
                    "id": _new_id("MSG"),
                    "ts": now,
                    "sender_id": "user",
                    "msg_type": "handoff",
                    "step_index": step["step_index"],
                    "content": formatted_reply,
                }
            )
            task["recent_events"].append(
                {"ts": now, "type": "user.answered_questions", "summary": f"用户已提交选项确认，恢复步骤 {step['step_index']} 原会话继续"}
            )
            return task

        res = self.store.mutate(_ans)
        self._wakeup.set()
        return res

    def attach_plan(self, task_id: str, steps: List[Dict[str, Any]], coordinator_note: str = "") -> Dict[str, Any]:
        if not steps or len(steps) > 8:
            raise ValueError("计划步骤数量需在 1 到 8 步之间")
        self.touch_coordinator_heartbeat()

        def _attach(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            if task["steps"]:
                raise ValueError("该任务已包含执行步骤")
            proj_abs = task.get("project_path") or st["binding"]["project_path"]
            task_sb = _effective_sandbox(st["binding"].get("max_sandbox", "workspace-write"), task.get("sandbox", "read-only"))
            built = []
            for idx, raw_s in enumerate(steps):
                mid = raw_s["member_id"]
                if mid not in st["members"]:
                    raise ValueError(f"成员不存在: {mid}")
                req_shell = bool(raw_s.get("requires_shell", False))
                if req_shell and st["members"][mid].get("client") == "antigravity":
                    raise ValueError(f"能力路由拦截：步骤 {idx} 需要 Shell 执行权限，不能指派给 Antigravity 节点 {mid}")
                ro_req = bool(raw_s.get("read_only", True))
                step_sb = _effective_sandbox(task_sb, raw_s.get("sandbox") or ("read-only" if ro_req else task_sb), read_only=ro_req)
                ro = step_sb == "read-only"
                step_ws = (raw_s.get("workspace_scope") or ".").strip() or "."
                _resolve_workdir(proj_abs, step_ws)
                built.append(
                    self._make_step(
                        step_index=idx,
                        title=raw_s.get("title") or f"步骤 {idx}",
                        step_type=raw_s.get("step_type") or ("review" if raw_s.get("review_of") is not None else "execution"),
                        member_id=mid,
                        prompt=raw_s["prompt"],
                        depends_on=[int(x) for x in raw_s.get("depends_on", [])],
                        review_of=int(raw_s["review_of"]) if raw_s.get("review_of") is not None else None,
                        read_only=ro,
                        write_scope=[] if ro else (raw_s.get("write_scope") or raw_s.get("file_scope") or []),
                        workspace_scope=step_ws,
                        sandbox=step_sb,
                        requires_shell=req_shell,
                    )
                )
            task["steps"] = built
            task["current_step_index"] = 0
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return task

        res = self.store.mutate(_attach)
        self._wakeup.set()
        return res

    def post_message(
        self,
        task_id: str,
        sender_id: str,
        msg_type: str,
        content: str,
        step_index: Optional[int] = None,
        require_ruling: bool = False,
    ) -> Dict[str, Any]:
        if msg_type not in ("question", "deviation", "handoff", "coordinator_note"):
            msg_type = "question"
        content = content.strip()
        if not content:
            raise ValueError("消息内容不能为空")

        def _post(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            msg = {
                "id": _new_id("MSG"),
                "ts": time.time(),
                "sender_id": sender_id,
                "msg_type": msg_type,
                "step_index": step_index,
                "content": content,
            }
            task["messages"].append(msg)
            self._append_room_chat(st, task, sender_id=sender_id, kind=msg_type, content=content, step_index=step_index)
            if require_ruling:
                task["status"] = "needs_ruling"
                task["blocking_reason"] = f"成员 {sender_id} 提出问题/偏离待裁决: {content[:100]}"
            task["updated_at"] = msg["ts"]
            return msg

        return self.store.mutate(_post)

    def followup_task(
        self,
        task_id: str,
        prompt: str,
        step_index: Optional[int] = None,
        target_member_id: Optional[str] = None,
        sandbox: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("追问内容不能为空")

        def _fup(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            if not task["steps"]:
                raise ValueError("任务尚无执行步骤，无法追问")
            target_idx = (len(task["steps"]) - 1) if step_index is None else int(step_index)
            base_step = task["steps"][target_idx]
            if base_step["status"] == "running":
                raise ValueError("该步骤正在运行中，请勿重复提交")

            if target_member_id and target_member_id in st["members"]:
                mid = target_member_id
                sess = base_step.get("worker_session_id") if mid == base_step["member_id"] else None
            else:
                mid = base_step["member_id"]
                sess = base_step.get("worker_session_id")

            req_sb = (sandbox or "").strip().lower()
            max_sb = st["binding"].get("max_sandbox", "workspace-write")
            if req_sb in _SANDBOX_RANK:
                eff_sb = _effective_sandbox(max_sb, req_sb)
            else:
                eff_sb = _effective_sandbox(max_sb, base_step.get("sandbox", "read-only"))
            is_ro = eff_sb == "read-only"
            if not is_ro and st["members"][mid].get("client") == "antigravity":
                # Antigravity headless cannot execute workspace-write scripts -> prefer Codex plotter if explicitly requesting write
                codex_ids = [k for k, v in st["members"].items() if v.get("client") == "codex"]
                if codex_ids:
                    mid = next((k for k in codex_ids if "plotting" in (st["members"][k].get("tags") or [])), codex_ids[0])
                    sess = None

            for other in st["tasks"]:
                if other["id"] == task_id:
                    continue
                if other["created_at"] > task["created_at"] and other["status"] in (
                    "running",
                    "queued",
                    "needs_ruling",
                    "blocked",
                    "needs_verification",
                ):
                    for os_step in other.get("steps", []):
                        if os_step.get("member_id") == mid and (
                            (sess and os_step.get("worker_session_id") == sess)
                            or os_step.get("status") in ("pending", "running", "needs_ruling", "needs_verification", "blocked")
                        ):
                            raise ValueError(
                                f"冲突拦截 (conflict_requires_ruling)：成员 {mid} 在更新的任务 {other['id']} 中存在未结束轮次 ({other['status']})，不得直接恢复旧轮次"
                            )

            new_idx = len(task["steps"])
            ws_scope = base_step.get("workspace_scope", ".")
            is_repair_step = base_step.get("status") in ("needs_ruling", "failed", "aborted", "blocked")
            new_step = self._make_step(
                step_index=new_idx,
                title=(title or "").strip() or f"续聊追问 #{new_idx}",
                step_type="execution",
                stage_index=new_idx,
                round_label="视觉修图迭代" if (title and "修图" in title) else ("定向返修" if is_repair_step else "同会话追问"),
                member_id=mid,
                prompt=prompt,
                depends_on=[target_idx],
                review_of=None,
                read_only=is_ro,
                write_scope=[] if is_ro else (list(base_step.get("write_scope", [])) or [ws_scope]),
                workspace_scope=ws_scope,
                sandbox=eff_sb,
                initial_session_id=sess,
                requires_shell=not is_ro,
            )
            if is_repair_step:
                new_step["repair_of"] = target_idx
                new_step["allow_dep_statuses"] = ["succeeded", "needs_ruling", "failed", "aborted", "blocked"]
            task["steps"].append(new_step)
            self._append_room_chat(
                st,
                task,
                sender_id="user",
                kind="user_interjection",
                content=prompt,
                to_member_id=mid,
                step_index=new_idx,
                round_label="发回修图" if (title and "修图" in title) else "追问",
            )
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["current_step_index"] = new_idx
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return task

        res = self.store.mutate(_fup)
        self._wakeup.set()
        return res

    def submit_ruling(
        self,
        task_id: str,
        step_index: int,
        decision: str,
        reason: str,
        repair_instructions: Optional[str] = None,
        decided_by: str = "ag_coordinator",
    ) -> Dict[str, Any]:
        if decision not in ("accept", "reject", "defer"):
            raise ValueError("裁决动作必须为 accept / reject / defer")
        reason = reason.strip()
        if not reason:
            raise ValueError("总管裁决必须记录明确理由")
        self.touch_coordinator_heartbeat(decided_by)

        def _rule(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            now = time.time()
            task["rulings"].append(
                {"id": _new_id("RULE"), "ts": now, "step_index": int(step_index), "decision": decision, "reason": reason, "decided_by": decided_by}
            )
            self._append_room_chat(st, task, sender_id="user", kind="ruling", content=f"裁决 [{decision}]: {reason}", round_label="总管裁决")
            if decision == "accept" and "预算熔断" in str(task.get("blocking_reason") or ""):
                task["token_budget_overridden"] = True
                task["blocking_reason"] = None
                task["status"] = "queued" if st.get("paused") else "running"
                task["updated_at"] = now
                return task

            if step.get("review_of") is not None:
                rev_step = step
                target_step = task["steps"][int(step["review_of"])]
            else:
                target_step = step
                rev_step = next((s for s in task["steps"] if s.get("review_of") == target_step["step_index"]), None)

            if decision == "accept":
                last_rev = next((r for r in reversed(task["reviews"]) if r.get("target_step_index") == target_step["step_index"]), None)
                brief = repair_instructions or (
                    f"审查问题：{last_rev.get('problem')}\n证据：{last_rev.get('evidence')}\n期望修复：{last_rev.get('expected_fix')}"
                    if last_rev
                    else reason
                )
                target_step["repair_count"] = int(target_step.get("repair_count", 0)) + 1
                target_step["pending_repair_brief"] = f"【总管裁决采纳审查意见 - 定向修复】\n裁决理由：{reason}\n{brief}"
                target_step["status"] = "pending"
                target_step["blocking_reason"] = None
                if rev_step is not None:
                    rev_step["status"] = "pending"
                    rev_step["blocking_reason"] = None
                task["status"] = "queued" if st.get("paused") else "running"
                task["blocking_reason"] = None
                task["current_step_index"] = target_step["step_index"]
            elif decision == "reject":
                if target_step["attempts"]:
                    target_step["accepted_attempt_id"] = target_step["attempts"][-1]["attempt_id"]
                target_step["status"] = "succeeded"
                target_step["blocking_reason"] = None
                if rev_step is not None:
                    rev_step["status"] = "succeeded"
                    rev_step["blocking_reason"] = None
                # P0-1 Decoupling: human override rejecting a review NEVER grants automated research_signoff = passed!
                if isinstance(task.get("research_signoff"), dict) and task["research_signoff"].get("status") != "passed":
                    if task["research_signoff"].get("status") != "failed":
                        task["research_signoff"]["status"] = "needs_ruling"
                    task["research_signoff"]["human_override_noted"] = True
                task["blocking_reason"] = None
                self._refresh_task_overall_status_unlocked(st, task)
            else:
                task["status"] = "needs_ruling"
                task["blocking_reason"] = f"总管暂缓裁决：{reason}"

            task["updated_at"] = now
            return task

        res = self.store.mutate(_rule)
        self._wakeup.set()
        return res

    def reassign_step(self, task_id: str, step_index: int, new_member_id: str, reason: str = "总管改派成员接手") -> Dict[str, Any]:
        def _reassign(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            if step["status"] == "running":
                raise ValueError("该步骤正在运行，请先中止当前尝试再改派")
            if new_member_id not in st["members"]:
                raise ValueError(f"新成员不存在: {new_member_id}")
            if step.get("requires_shell") and st["members"][new_member_id].get("client") == "antigravity":
                raise ValueError(f"能力路由拦截：该步骤需要 Shell 执行权限，不可改派给无头 Antigravity 节点 {new_member_id}")

            old_member = step["member_id"]
            step["member_id"] = new_member_id
            step["worker_session_id"] = None
            step["handoff_context"] = {
                "reassigned_from": old_member,
                "reassigned_to": new_member_id,
                "reason": reason,
                "goal": task["goal"],
                "constraints": f"read_only={step['read_only']}, workspace_scope={step['workspace_scope']}, sandbox={step['sandbox']}",
                "completed_steps": [
                    f"- 步骤 {s['step_index']} ({s['title']}): {s['latest_result_text'][:200]}"
                    for s in task["steps"]
                    if s["status"] == "succeeded" and s["step_index"] != step["step_index"]
                ],
                "prior_outputs": [
                    f"- 尝试 {a['attempt_id']} (成员={a['member_id']}, 状态={a['status']}): {(a.get('result_text') or a.get('partial_text') or '')[:200]}"
                    for a in step["attempts"]
                ],
                "open_issues_and_evidence": [
                    f"- 审查问题: {r.get('problem')} | 证据: {r.get('evidence')}"
                    for r in task["reviews"]
                    if r.get("target_step_index") == step["step_index"] and r.get("verdict") == "REJECT"
                ],
                "ts": time.time(),
            }
            step["status"] = "pending"
            step["blocking_reason"] = None
            self._append_room_chat(
                st,
                task,
                sender_id="user",
                kind="handoff",
                content=f"将 [{step['title']}] 改派由 @{st['members'][new_member_id]['name']} 接手（{reason}）",
                to_member_id=new_member_id,
                step_index=int(step_index),
            )
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return task

        res = self.store.mutate(_reassign)
        self._wakeup.set()
        return res

    def swap_task_seat(
        self,
        task_id: str,
        old_member_id: str,
        new_member_id: Optional[str] = None,
        spawn_backup_client: Optional[str] = None,
        spawn_backup_model: Optional[str] = None,
        spawn_backup_effort: Optional[str] = None,
        abort_running: bool = True,
        trigger_fresh_turn: bool = False,
        bind_as_quota_backup: bool = False,
        reason: str = "现场快速换人",
    ) -> Dict[str, Any]:
        """Hot-swap a seated specialist in a task at ANY time (mid-run, waiting, failed, or completed)."""
        snap = self.store.snapshot()
        task_snap = self._find_task(snap, task_id)
        if old_member_id not in snap["members"]:
            raise ValueError(f"原席位专家不存在: {old_member_id}")

        # 1. Resolve or spawn target replacement specialist
        spawned_info: Optional[Dict[str, Any]] = None
        if spawn_backup_model:
            spawned_info = self.spawn_quota_backup_member(
                source_member_id=old_member_id,
                new_client=spawn_backup_client,
                new_model=spawn_backup_model,
                new_effort=spawn_backup_effort,
                bind_as_default=bind_as_quota_backup,
                reason=f"{reason}（同角色跨模型替补）",
            )
            target_mid = spawned_info["member"]["id"]
        else:
            if not new_member_id:
                raise ValueError("请指定要替换上场的专家或跨模型替补")
            target_mid = new_member_id.strip()
            if target_mid == old_member_id:
                raise ValueError("新旧专家不能相同")
            if target_mid not in snap["members"]:
                raise ValueError(f"目标专家不存在: {target_mid}")
            if bind_as_quota_backup:
                self.bind_quota_backup(old_member_id, backup_member_id=target_mid)

        # Validate requires_shell capability before aborting anything!
        snap2 = self.store.snapshot()
        target_mem = snap2["members"][target_mid]
        task_now = self._find_task(snap2, task_id)
        for s in task_now.get("steps", []):
            if s["member_id"] == old_member_id and s["status"] != "succeeded":
                if s.get("requires_shell") and target_mem.get("client") == "antigravity":
                    raise ValueError(
                        f"能力路由拦截：步骤 #{s['step_index']} 需要 Shell 执行权限，不可切换为无头 Antigravity 专家 {target_mid}"
                    )

        # 2. If old_member_id is actively running right now and abort_running=True, cleanly abort & wait briefly!
        aborted_step_indices: List[int] = []
        running_steps = [s for s in task_now.get("steps", []) if s["member_id"] == old_member_id and s["status"] == "running"]
        if running_steps and abort_running:
            for rs in running_steps:
                aborted_step_indices.append(rs["step_index"])
                self.abort_attempt(task_id=task_id, step_index=rs["step_index"], reason=f"现场换人中止原席位: {reason}")
            deadline = time.time() + 3.5
            while time.time() < deadline:
                cur_t = self._find_task(self.store.snapshot(), task_id)
                still_run = any(
                    cur_t["steps"][idx]["status"] == "running"
                    for idx in aborted_step_indices
                    if idx < len(cur_t["steps"])
                )
                if not still_run:
                    break
                time.sleep(0.08)

        # 3. Atomically swap roster seat + pending/aborted/failed steps (+ optional fresh turn if task was already done!)
        def _do_swap(st: Dict[str, Any]) -> Dict[str, Any]:
            t = self._find_task(st, task_id)
            old_m = st["members"][old_member_id]
            new_m = st["members"][target_mid]
            now = time.time()

            # Update mode_meta participants / host / chief / specialist cleanly
            meta = t.setdefault("mode_meta", {})
            if isinstance(meta.get("participants"), list):
                new_parts: List[str] = []
                replaced = False
                for p in meta["participants"]:
                    if p == old_member_id:
                        if target_mid not in new_parts:
                            new_parts.append(target_mid)
                        replaced = True
                    else:
                        if p not in new_parts:
                            new_parts.append(p)
                if not replaced and target_mid not in new_parts:
                    new_parts.append(target_mid)
                meta["participants"] = new_parts
            if meta.get("host_member_id") == old_member_id:
                meta["host_member_id"] = target_mid
            if meta.get("chief_member_id") == old_member_id:
                meta["chief_member_id"] = target_mid
            if meta.get("specialist_id") == old_member_id:
                meta["specialist_id"] = target_mid

            swapped_steps: List[int] = []
            still_running_skipped: List[int] = []
            restarted_any = False

            for s in t.get("steps", []):
                if s["member_id"] != old_member_id:
                    continue
                st_val = s["status"]
                if st_val == "succeeded":
                    continue
                if st_val == "running":
                    still_running_skipped.append(s["step_index"])
                    continue
                if s.get("requires_shell") and new_m.get("client") == "antigravity":
                    continue
                s["member_id"] = target_mid
                s["worker_session_id"] = None
                if st_val in ("aborted", "failed", "blocked", "needs_ruling", "needs_verification", "awaiting_user"):
                    s["handoff_context"] = {
                        "reassigned_from": f"{old_m.get('name')} ({old_m.get('model')})",
                        "reassigned_to": f"{new_m.get('name')} ({new_m.get('model')})",
                        "reason": reason,
                        "completed_steps": [x["step_index"] for x in t["steps"] if x["status"] == "succeeded"],
                        "prior_outputs": [
                            (a.get("result_text") or a.get("partial_text") or "")[:600]
                            for a in s.get("attempts", [])[-1:]
                            if (a.get("result_text") or a.get("partial_text"))
                        ],
                        "open_issues_and_evidence": [s.get("blocking_reason") or reason],
                    }
                    s["status"] = "pending"
                    s["blocking_reason"] = None
                    s["pending_questions"] = None
                    restarted_any = True
                swapped_steps.append(s["step_index"])

            # If task had no unfinished steps for old_member_id and user requested a fresh turn from new specialist
            fresh_step_idx: Optional[int] = None
            if trigger_fresh_turn and not restarted_any and not still_running_skipped:
                new_idx = len(t["steps"])
                fresh_step_idx = new_idx
                ro = t.get("sandbox", "read-only") == "read-only"
                sc = t.get("workspace_scope", ".")
                t["steps"].append(
                    self._make_step(
                        step_index=new_idx,
                        title=f"新席位补充研判 ({new_m['name']})",
                        step_type="synthesis" if meta.get("host_member_id") == target_mid else "discussion",
                        stage_index=new_idx,
                        round_label="现场换人接力",
                        member_id=target_mid,
                        prompt=(
                            f"项目负责人已将原席位 [{old_m['name']}] 切换由你 ({new_m['name']}) 接替。\n"
                            f"换人说明：{reason}\n"
                            f"任务总目标：{t['goal']}\n\n"
                            "请审阅上方已有讨论与交付物，结合你的专长与模型优势给出独立复核、纠偏或增量深化方案。"
                        ),
                        depends_on=[],
                        review_of=None,
                        read_only=ro,
                        write_scope=[] if ro else [sc],
                        workspace_scope=sc,
                        sandbox=t.get("sandbox", "read-only"),
                    )
                )
                restarted_any = True
                swapped_steps.append(new_idx)

            if restarted_any:
                t["archived"] = False
                st["active_task_id"] = t["id"]
                t["status"] = "queued" if st.get("paused") else "running"
                t["blocking_reason"] = None
                if swapped_steps:
                    t["current_step_index"] = swapped_steps[0]

            note_parts = [f"现场换人：将 **{old_m['name']}** 切换为 **{new_m['name']}** ({new_m['client']}/{new_m['model']})"]
            if aborted_step_indices:
                note_parts.append(f"已中止并交接运行中步骤 #{aborted_step_indices}")
            elif still_running_skipped:
                note_parts.append(f"当前运行中步骤 #{still_running_skipped} 说完后生效，后续步骤已切换")
            elif fresh_step_idx is not None:
                note_parts.append("已触发新席位补充研判")
            if bind_as_quota_backup:
                note_parts.append("并已绑定为额度耗尽默认替补")
            summary_msg = " · ".join(note_parts)

            self._append_room_chat(
                st,
                t,
                sender_id="user",
                kind="handoff",
                content=summary_msg,
                to_member_id=target_mid,
                round_label="现场换人",
            )
            t["recent_events"].append({"ts": now, "type": "seat.swapped", "summary": summary_msg})
            t["recent_events"] = t["recent_events"][-40:]
            t["updated_at"] = now
            return {
                "task": t,
                "old_member_id": old_member_id,
                "new_member_id": target_mid,
                "swapped_steps": swapped_steps,
                "still_running_skipped": still_running_skipped,
                "applied": "next_round" if (still_running_skipped and not aborted_step_indices) else "immediate",
                "spawned_backup": spawned_info["member"] if spawned_info else None,
            }

        res = self.store.mutate(_do_swap)
        self._wakeup.set()
        return res

    def add_task_seat(
        self,
        task_id: str,
        member_id: str,
        trigger_turn: bool = True,
        instruction: str = "",
    ) -> Dict[str, Any]:
        """Add an extra specialist seat into an active or completed task at any time."""
        def _add(st: Dict[str, Any]) -> Dict[str, Any]:
            t = self._find_task(st, task_id)
            if member_id not in st["members"]:
                raise ValueError(f"专家不存在: {member_id}")
            mem = st["members"][member_id]
            meta = t.setdefault("mode_meta", {})
            parts = list(meta.get("participants") or [s["member_id"] for s in t.get("steps", [])])
            dedup_parts: List[str] = []
            for p in parts:
                if p not in dedup_parts:
                    dedup_parts.append(p)
            if member_id not in dedup_parts:
                dedup_parts.append(member_id)
            meta["participants"] = dedup_parts

            now = time.time()
            added_step_idx: Optional[int] = None
            if trigger_turn:
                added_step_idx = len(t["steps"])
                ro = t.get("sandbox", "read-only") == "read-only"
                sc = t.get("workspace_scope", ".")
                extra = f"\n补充指示：{instruction.strip()}" if instruction.strip() else ""
                t["steps"].append(
                    self._make_step(
                        step_index=added_step_idx,
                        title=f"增补席位发言 ({mem['name']})",
                        step_type="discussion",
                        stage_index=added_step_idx,
                        round_label="增补专家",
                        member_id=member_id,
                        prompt=(
                            f"项目负责人新邀请你 ({mem['name']}) 加入本次协作讨论。\n"
                            f"总目标：{t['goal']}{extra}\n\n"
                            "请审阅上方已有发言与项目文件，从你的专业视角提供独立洞察、盲区核查或关键补充建议。"
                        ),
                        depends_on=[],
                        review_of=None,
                        read_only=ro,
                        write_scope=[] if ro else [sc],
                        workspace_scope=sc,
                        sandbox=t.get("sandbox", "read-only"),
                    )
                )
                t["archived"] = False
                st["active_task_id"] = t["id"]
                t["current_step_index"] = added_step_idx
                t["status"] = "queued" if st.get("paused") else "running"
                t["blocking_reason"] = None

            msg = f"现场增补专家 **{mem['name']}** ({mem['client']}/{mem['model']}) 加入讨论席位"
            self._append_room_chat(st, t, sender_id="user", kind="handoff", content=msg, to_member_id=member_id, round_label="增补专家")
            t["recent_events"].append({"ts": now, "type": "seat.added", "summary": msg})
            t["recent_events"] = t["recent_events"][-40:]
            t["updated_at"] = now
            return {"task": t, "member_id": member_id, "step_index": added_step_idx}

        res = self.store.mutate(_add)
        self._wakeup.set()
        return res

    def set_pause(self, paused: bool) -> Dict[str, Any]:
        def _p(st: Dict[str, Any]) -> Dict[str, Any]:
            st["paused"] = bool(paused)
            now = time.time()
            for t in st["tasks"]:
                if paused and t["status"] == "running":
                    if not any(s["status"] == "running" for s in t["steps"]):
                        t["status"] = "queued"
                        t["blocking_reason"] = "已暂停新任务派发，等待恢复"
                elif not paused and t["status"] == "queued":
                    t["status"] = "running"
                    t["blocking_reason"] = None
                    t["updated_at"] = now
            return {"paused": st["paused"]}

        res = self.store.mutate(_p)
        self._wakeup.set()
        return res

    def abort_attempt(
        self,
        task_id: Optional[str] = None,
        step_index: Optional[int] = None,
        attempt_id: Optional[str] = None,
        reason: str = "用户请求中止当前尝试",
    ) -> Dict[str, Any]:
        snap = self.store.snapshot()
        target_att_ids: List[str] = [attempt_id] if attempt_id else []
        if not target_att_ids and task_id is not None:
            task = self._find_task(snap, task_id)
            for s in task["steps"]:
                if step_index is not None and s["step_index"] != int(step_index):
                    continue
                for a in s["attempts"]:
                    if a["status"] == "running":
                        target_att_ids.append(a["attempt_id"])

        if not target_att_ids:
            return {"aborted": False, "message": "未找到正在运行的尝试"}

        last_kill: Dict[str, Any] = {}
        for aid in target_att_ids:
            k1 = self.adapter.abort_attempt(aid)
            k2 = self.agy_adapter.abort_attempt(aid)
            last_kill = k1 if k1.get("found") else k2

        self._wakeup.set()
        return {"aborted": True, "attempt_ids": target_att_ids, **last_kill}

    def continue_task(self, task_id: str) -> Dict[str, Any]:
        def _cont(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            if any(s["status"] == "running" for s in task["steps"]):
                return {"status": "already_running", "message": "任务已有步骤正在执行", "task": task}

            proj_root = task.get("project_path") or st["binding"]["project_path"]
            if not os.path.isdir(proj_root):
                raise ValueError(f"任务绑定项目目录不可用: {proj_root}")

            for s in task["steps"]:
                if s["status"] == "succeeded":
                    continue
                mid = s["member_id"]
                sess = s.get("worker_session_id")
                for other in st["tasks"]:
                    if other["id"] == task_id:
                        continue
                    if other["created_at"] > task["created_at"] and other["status"] in (
                        "running",
                        "queued",
                        "needs_ruling",
                        "needs_verification",
                        "blocked",
                    ):
                        for os_step in other.get("steps", []):
                            if os_step.get("member_id") == mid and (
                                (sess and os_step.get("worker_session_id") == sess)
                                or os_step.get("status") in ("pending", "running", "needs_ruling", "needs_verification", "blocked")
                            ):
                                raise ValueError(
                                    f"冲突拦截 (conflict_requires_ruling)：成员 {mid} 在更新任务 {other['id']} 中有未结束轮次，请先处理冲突"
                                )

            resumed_steps = []
            for s in task["steps"]:
                if s["status"] in ("aborted", "failed", "needs_verification", "blocked"):
                    if s["status"] in ("failed", "needs_verification"):
                        # Clear broken/overflowed session ID so retry starts cleanly without re-hitting overflow
                        s["worker_session_id"] = None
                    s["status"] = "pending"
                    s["blocking_reason"] = None
                    resumed_steps.append(s["step_index"])

            if not resumed_steps and all(s["status"] == "succeeded" for s in task["steps"]):
                task["status"] = "succeeded"
                return {"status": "already_completed", "message": "所有步骤均已完成", "task": task}

            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            return {"status": "resumed", "resumed_steps": resumed_steps, "task": task}

        res = self.store.mutate(_cont)
        self._wakeup.set()
        return res

    def accept_step_partial(self, task_id: str, step_index: int) -> Dict[str, Any]:
        """Adopt already-streamed partial/result text of an interrupted/failed step as its official deliverable."""
        def _adopt(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            if step["status"] == "running":
                raise ValueError("该步骤正在运行中，请先等待结束或中止")
            text_candidate = ""
            chosen_att = None
            for att in reversed(step.get("attempts", [])):
                cand = (att.get("result_text") or att.get("partial_text") or "").strip()
                if cand:
                    text_candidate = cand
                    chosen_att = att
                    break
            if not text_candidate:
                raise ValueError("该步骤尚无已生成的流式文本可供采纳")

            now = time.time()
            if chosen_att:
                chosen_att["result_text"] = text_candidate
                chosen_att["status"] = "succeeded"
                chosen_att["blocking_reason"] = None
                step["accepted_attempt_id"] = chosen_att["attempt_id"]
            step["latest_result_text"] = text_candidate
            step["status"] = "succeeded"
            step["blocking_reason"] = None
            # P0-1 Decoupling: adopting a partial streamed draft NEVER grants research_signoff = passed!
            if isinstance(task.get("research_signoff"), dict) and task["research_signoff"].get("status") != "passed":
                task["research_signoff"]["status"] = "needs_ruling"

            chat_kind = "deliverable" if step.get("step_type") in ("synthesis", "execution") else "speech"
            self._append_room_chat(
                st,
                task,
                sender_id=step["member_id"],
                kind=chat_kind,
                content=text_candidate,
                step_index=step["step_index"],
                round_label=step.get("round_label") or step["title"],
            )
            task.setdefault("deliverables", []).append(
                {
                    "step_index": step["step_index"],
                    "title": step["title"],
                    "member_id": step["member_id"],
                    "attempt_id": chosen_att["attempt_id"] if chosen_att else "manual_adopt",
                    "session_id": chosen_att.get("session_id") if chosen_att else None,
                    "preview": text_candidate[:240],
                    "ts": now,
                }
            )
            task["recent_events"].append(
                {
                    "ts": now,
                    "type": "step.partial_adopted",
                    "summary": f"已直接采纳步骤 {step['step_index']} ({step['title']}) 已生成的 {len(text_candidate)} 字成果",
                }
            )
            self._refresh_task_overall_status_unlocked(st, task)
            task["updated_at"] = now
            return task

        res = self.store.mutate(_adopt)
        self._wakeup.set()
        return res

    def retry_single_step(
        self,
        task_id: str,
        step_index: int,
        reset_session: bool = True,
        new_member_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retry a single failed/blocked/aborted step cleanly (with fresh session by default)."""
        def _retry(st: Dict[str, Any]) -> Dict[str, Any]:
            task = self._find_task(st, task_id)
            step = task["steps"][int(step_index)]
            if step["status"] == "running":
                raise ValueError("该步骤已在运行中")
            if new_member_id:
                if new_member_id not in st["members"]:
                    raise ValueError(f"成员不存在: {new_member_id}")
                step["member_id"] = new_member_id
                step["worker_session_id"] = None
            elif reset_session:
                step["worker_session_id"] = None

            step["status"] = "pending"
            step["blocking_reason"] = None
            task["archived"] = False
            st["active_task_id"] = task["id"]
            task["current_step_index"] = step["step_index"]
            task["status"] = "queued" if st.get("paused") else "running"
            task["blocking_reason"] = None
            task["updated_at"] = time.time()
            task["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "step.retried",
                    "summary": f"已开启干净会话重跑步骤 {step['step_index']} ({step['title']})",
                }
            )
            return task

        res = self.store.mutate(_retry)
        self._wakeup.set()
        return res

    # ------------------------------------------------------------------
    # Internal Scheduler, @Mention Consultation & Execution
    # ------------------------------------------------------------------

    def _find_task(self, st: Dict[str, Any], task_id: str) -> Dict[str, Any]:
        for t in st.get("tasks", []):
            if t.get("id") == task_id:
                return t
        raise ValueError(f"未找到任务: {task_id}")

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wakeup.wait(timeout=1.0)
            self._wakeup.clear()
            if self._stop_event.is_set():
                break
            try:
                self._schedule_tick()
            except Exception:
                pass

    def _schedule_tick(self) -> None:
        with self._sched_lock:
            # Fast zero-copy pre-check: NEVER deep-copy or fsync team_state.json when idle or no step is ready!
            def _has_schedulable_work(st: Dict[str, Any]) -> bool:
                if st.get("paused"):
                    return False
                max_conc = int(st.get("max_concurrency", 3))
                if len(self._running_attempts) >= max_conc:
                    return False
                busy_members = {mid for mid, m in st.get("members", {}).items() if m.get("status") == "working"}
                busy_sessions = {info["session_id"] for info in self._running_attempts.values() if info.get("session_id")}
                active_writes = [
                    (info["project_root"], info["write_scope"], bool(info.get("worktree_isolated", False)))
                    for info in self._running_attempts.values()
                    if not info.get("read_only", True)
                ]
                for task in st.get("tasks", []):
                    if task.get("status") not in ("running", "queued"):
                        continue
                    tok_cap = int(task.get("task_token_budget") or 0)
                    tok_used = int((task.get("context_stats") or {}).get("total_input_tokens") or 0)
                    if tok_cap > 0 and tok_used >= tok_cap and not task.get("token_budget_overridden"):
                        return True
                    proj_root = task.get("project_path") or st["binding"]["project_path"]
                    task_wt_iso = bool(task.get("worktree_isolation", False))
                    steps = task.get("steps", [])
                    for step in steps:
                        if step.get("status") != "pending":
                            continue
                        allow_st = set(step.get("allow_dep_statuses") or ["succeeded"])
                        if step.get("repair_of") is not None:
                            allow_st.update(("succeeded", "needs_ruling", "failed", "aborted", "blocked"))
                        if not all(0 <= d < len(steps) and steps[d].get("status") in allow_st for d in step.get("depends_on", [])):
                            continue
                        mid = step["member_id"]
                        mem = st.get("members", {}).get(mid)
                        if not mem or mem.get("client") not in SUPPORTED_CLIENTS or mid in busy_members:
                            continue
                        sess = step.get("worker_session_id")
                        if sess and sess in busy_sessions:
                            continue
                        step_wt_iso = bool(task_wt_iso and not step.get("read_only", True) and step.get("sandbox") == "workspace-write")
                        if not step.get("read_only", True):
                            cand_scope = step.get("write_scope") or []
                            if any(
                                (not (step_wt_iso and aw_wt))
                                and normalize_win_path(proj_root) == normalize_win_path(pr)
                                and _scopes_overlap(cand_scope, ws, proj_root)
                                for pr, ws, aw_wt in active_writes
                            ):
                                continue
                        return True
                return False

            if not self.store.read(_has_schedulable_work):
                return

            to_launch: List[Tuple[str, int, str]] = []

            def _pick_candidates(st: Dict[str, Any]) -> None:
                if st.get("paused"):
                    return
                max_conc = int(st.get("max_concurrency", 3))
                active_count = len(self._running_attempts)
                if active_count >= max_conc:
                    return

                busy_members = {mid for mid, m in st["members"].items() if m.get("status") == "working"}
                busy_sessions = {info["session_id"] for info in self._running_attempts.values() if info.get("session_id")}
                active_writes: List[Tuple[str, List[str], bool]] = [
                    (info["project_root"], info["write_scope"], bool(info.get("worktree_isolated", False)))
                    for info in self._running_attempts.values()
                    if not info.get("read_only", True)
                ]

                for task in st.get("tasks", []):
                    if task.get("status") not in ("running", "queued"):
                        continue
                    # Pillar 5: Per-task Token Budget Hard Ceiling (pauses cleanly in needs_ruling before runaway spend!)
                    tok_cap = int(task.get("task_token_budget") or 0)
                    tok_used = int((task.get("context_stats") or {}).get("total_input_tokens") or 0)
                    if tok_cap > 0 and tok_used >= tok_cap and not task.get("token_budget_overridden"):
                        task["status"] = "needs_ruling"
                        task["blocking_reason"] = (
                            f"任务累计输入 Token ({tok_used:,}) 已达预算上限 ({tok_cap:,})，已触发预算熔断暂停派发，请裁决放行或精简收敛。"
                        )
                        task["recent_events"].append(
                            {"ts": time.time(), "type": "budget.token_cap_hit", "summary": task["blocking_reason"][:150]}
                        )
                        task["recent_events"] = task["recent_events"][-40:]
                        task["updated_at"] = time.time()
                        continue

                    proj_root = task.get("project_path") or st["binding"]["project_path"]
                    task_wt_iso = bool(task.get("worktree_isolation", False))
                    steps = task.get("steps", [])
                    for step in steps:
                        if active_count >= max_conc:
                            return
                        if step.get("status") != "pending":
                            continue
                        allow_st = set(step.get("allow_dep_statuses") or ["succeeded"])
                        if step.get("repair_of") is not None:
                            allow_st.update(("succeeded", "needs_ruling", "failed", "aborted", "blocked"))
                        deps_ok = all(
                            0 <= d < len(steps) and steps[d].get("status") in allow_st
                            for d in step.get("depends_on", [])
                        )
                        if not deps_ok:
                            continue

                        mid = step["member_id"]
                        mem = st["members"].get(mid)
                        if not mem or mem.get("client") not in SUPPORTED_CLIENTS:
                            continue
                        if mid in busy_members:
                            continue
                        sess = step.get("worker_session_id")
                        if sess and sess in busy_sessions:
                            continue
                        step_wt_iso = bool(task_wt_iso and not step.get("read_only", True) and step.get("sandbox") == "workspace-write")
                        if not step.get("read_only", True):
                            cand_scope = step.get("write_scope") or []
                            conflict = any(
                                (not (step_wt_iso and aw_wt))
                                and normalize_win_path(proj_root) == normalize_win_path(pr)
                                and _scopes_overlap(cand_scope, ws, proj_root)
                                for pr, ws, aw_wt in active_writes
                            )
                            if conflict:
                                continue

                        att_id = _new_id("ATT")
                        now = time.time()
                        req_cfg = {
                            "client": mem["client"],
                            "model": mem["model"],
                            "reasoning_effort": mem["reasoning_effort"],
                            "sandbox": step.get("sandbox", mem.get("sandbox", "read-only")),
                        }
                        step["attempts"].append(
                            {
                                "attempt_id": att_id,
                                "round_num": len(step["attempts"]) + 1,
                                "member_id": mid,
                                "session_id": sess,
                                "is_fallback_session": False,
                                "fallback_of_session_id": None,
                                "requested_config": req_cfg,
                                "accepted_config": {},
                                "prompt_sent": "",
                                "partial_text": "",
                                "result_text": "",
                                "events_summary": [],
                                "stderr_excerpt": "",
                                "status": "running",
                                "blocking_reason": None,
                                "started_at": now,
                                "ended_at": None,
                                "pid": None,
                            }
                        )
                        step["status"] = "running"
                        step["blocking_reason"] = None
                        task["status"] = "running"
                        task["current_step_index"] = step["step_index"]
                        task["blocking_reason"] = None
                        task["updated_at"] = now

                        mem["status"] = "working"
                        mem["live_activity"] = step.get("round_label") or step["title"]
                        mem["current_task_id"] = task["id"]
                        mem["current_attempt_id"] = att_id

                        busy_members.add(mid)
                        if sess:
                            busy_sessions.add(sess)
                        if not step.get("read_only", True):
                            active_writes.append((proj_root, step.get("write_scope") or [], step_wt_iso))

                        self._running_attempts[att_id] = {
                            "task_id": task["id"],
                            "step_index": step["step_index"],
                            "member_id": mid,
                            "session_id": sess,
                            "project_root": proj_root,
                            "read_only": step.get("read_only", True),
                            "write_scope": step.get("write_scope") or [],
                            "worktree_isolated": step_wt_iso,
                        }
                        active_count += 1
                        to_launch.append((task["id"], step["step_index"], att_id))

            self.store.mutate(_pick_candidates)

            for task_id, step_index, att_id in to_launch:
                threading.Thread(
                    target=self._execute_attempt_worker,
                    args=(task_id, step_index, att_id),
                    name=f"Worker-{att_id}",
                    daemon=True,
                ).start()

    def _visible_messages_for_step(self, task: Dict[str, Any], step: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
        """Compute Blind Round 1 visibility-filtered chat messages + optional Anonymous Peer Review masking."""
        is_blind_r1 = (
            task.get("execution_mode") == "roundtable"
            and step.get("step_type") == "discussion"
            and int(step.get("stage_index") or 0) == 1
        )
        room_msgs = list(task.get("chat_room") or [])
        if is_blind_r1:
            visible_msgs = [
                cm
                for cm in room_msgs
                if cm.get("kind") in ("user_goal", "user_interjection", "user_choice", "ruling")
                or (cm.get("step_index") == 0 and (task.get("mode_meta") or {}).get("enable_host_opening"))
                or (cm.get("sender_id") == step["member_id"] and cm.get("step_index") == step["step_index"])
            ]
            return visible_msgs, True

        # Pillar 5: Anonymous Peer Review seat masking during Round 2+ cross-critique (unmasked in synthesis!)
        meta = task.get("mode_meta") or {}
        anon_map = meta.get("anon_map") if isinstance(meta.get("anon_map"), dict) else {}
        if (
            meta.get("anonymous_peer_review")
            and anon_map
            and step.get("step_type") == "discussion"
            and int(step.get("stage_index") or 0) >= 2
        ):
            masked_msgs: List[Dict[str, Any]] = []
            for cm in room_msgs:
                cp = dict(cm)
                sid = cp.get("sender_id")
                if sid in anon_map:
                    lbl = anon_map[sid]
                    cp["sender_name"] = f"{lbl} (你自己)" if sid == step["member_id"] else lbl
                tid_to = cp.get("to_member_id")
                if tid_to in anon_map:
                    cp["to_member_name"] = anon_map[tid_to]
                masked_msgs.append(cp)
            return masked_msgs, False

        return room_msgs, False

    def _compile_step_turn(
        self,
        st: Dict[str, Any],
        task: Dict[str, Any],
        step: Dict[str, Any],
        work_dir: str,
        *,
        session_id: Optional[str] = None,
        compression_level: str = "L0",
        force_full_prompt: bool = False,
        extra_handoff_md: str = "",
    ) -> Dict[str, Any]:
        """
        Compile Three-Zone KV-Cache Prefix-Stable prompt with Four-Tier Hard Budget Governance (v5.3).
        Preserves pending_user_answer passthrough, Blind R1 isolation, and all 22 existing test contracts.
        """
        cfg = ContextBudgetConfig.from_settings(st.get("settings"))
        blob_store = BlobStore(str(task.get("id") or "task"))
        mem = st["members"][step["member_id"]]

        if step.get("pending_user_answer"):
            pua = str(step["pending_user_answer"])
            return {
                "prompt": pua,
                "prefix_sha256": "pua_passthrough",
                "is_delta": True,
                "compression_level": "L0",
                "prompt_chars": len(pua),
                "est_tokens": max(1, len(pua) // 2),
                "budget_chars": cfg.max_prompt_chars,
                "budget_decision": "send",
                "rollover_reason": None,
                "chat_cursor_id": str((task.get("chat_room") or [{}])[-1].get("id") or ""),
                "masked_blocks": 0,
                "used_snapshots": 0,
                "omitted_refs": [],
                "window_tokens": 200_000,
                "window_source": "passthrough",
            }

        role_obj = st["roles"].get(mem["role"], {})
        specialty = (mem.get("specialty") or "").strip()
        persona_prompt = (mem.get("persona_prompt") or "").strip()
        grounding_mode = (mem.get("grounding_mode") or "strict").strip().lower()

        id_line = f"【你的身份】{mem.get('name')} (ID: {step['member_id']}) · 职责：{role_obj.get('name', '成员')}"
        if specialty:
            id_line += f" · 专长：{specialty}"
        if role_obj.get("description"):
            id_line += f" — {role_obj.get('description', '')}"

        # ===== ZONE A: Byte-Frozen Static Prefix (KV-Cache Friendly!) =====
        zone_a_blocks: List[str] = [
            f"【当前项目工作目录】{work_dir}",
            id_line,
        ]
        if persona_prompt:
            zone_a_blocks.append(f"【专家专属视角与核查清单】\n{persona_prompt}")
        dialect_block = _build_model_family_tuning(mem.get("client", "codex"), mem.get("model", ""))
        if dialect_block:
            zone_a_blocks.append(dialect_block)
        if grounding_mode == "strict":
            zone_a_blocks.append(
                "【OpenScience 证据核证、科学诚实与反幻觉规约 (Strict Grounding + science.txt)】\n"
                "1. 事实层零编造与科学诚实：严禁虚构工作区不存在的文件路径、代码行号、数据样本量、统计值(P值/CI/HR)、文献作者或 DOI；引用项目内任何数字、表格结论或代码实现必须先读后引，并用 `[文件名](真实路径)`（代码附 `#L起始-L结束`）标出锚点；若实证数据反驳预设假说或用户预期，必须以精确数字坦诚报告而非迎合预期；\n"
                "2. 非破坏性重跑与数据直绘：严禁覆盖或删除原始数据与既有结果目录（重跑写入新目录或版本化文件名）；统计图必须直接由脚本从实测数据绘制，AI 生成位图绝不可作为实验证据；\n"
                "3. 推演层鼓励合理深度外推（三层认知标定）：遵循 OpenScience 原则，严禁以“观察性数据无法推断/无法确定”为借口回避机理阐释、跨学科迁移或前瞻假说；所有核心陈述须显式分层：\n"
                "   - `[实证观察 Observed]`：直接由工作区文件/数据表/代码或实测命令证实的客观事实；\n"
                "   - `[统计/逻辑推断 Inferred]`：基于实证推导出的方向性判断（注明置信度与会推翻该判断的证据边界）；\n"
                "   - `[机制假说/深度外推 Hypothesis]`：有科研/工程创新价值的机制解释、迁移路径或前瞻外推（每条必须附带具体的“可证伪检验/最小判别实验”）；\n"
                "4. 覆盖边界与代码验收纪律：禁止将局部子集上测得的结论宣称为普适结果；若涉及代码/脚本改动，改动后必须重新运行验证并如实报告实测状态（严禁空口宣称已通过）；\n"
                "5. 反回音室与 Steelman 沟通：阅读其他队友发言时严禁盲目附和；反驳前先准确复述对方最强论点，再用真实文件证据或更简洁的竞争性解释 `@队友` 纠错；\n"
                "6. 末尾附简短【证据溯源与外推假说台账】（列出本条直接核验过的真实文件/命令，以及关键外推假说及其证伪条件/待外部核实项）。"
            )
        else:
            zone_a_blocks.append(
                "【证据规范 (Balanced Grounding)】\n"
                "引用项目文件与数据时请优先读取真实文件并用 `[文件名](路径)` 标注来源；鼓励给出机理与前瞻推演，但未实证项须注明 `[推断/假说]` 与验证路径，切勿编造具体数据或文献。"
            )
        if step.get("read_only", True):
            zone_a_blocks.append("【权限边界】当前为只读模式 (read-only)：可读取检查项目内文件，不得修改或删除文件。")

        # ===== ZONE B: Semi-Static JIT Playbooks + Figure Contract + Immutable Sealed Snapshots =====
        stype = step.get("step_type", "execution")
        is_rev = step.get("review_of") is not None
        fc = (task.get("research_spec") or {}).get("figure_contract") if isinstance(task.get("research_spec"), dict) else None
        has_fc = isinstance(fc, dict) and bool(fc)
        jit_pbs = select_jit_playbooks(
            task.get("openscience_playbooks"),
            step_type=stype,
            sandbox=str(step.get("sandbox") or task.get("sandbox") or "read-only"),
            has_figure_contract=has_fc,
            is_review=is_rev,
        )
        zone_b_static: List[str] = []
        pb_block = render_playbooks_prompt(jit_pbs)
        if pb_block:
            zone_b_static.append(pb_block)

        if has_fc and stype not in ("opening", "chief_plan"):
            be = str(fc.get("backend") or "python").strip().lower()
            be_rule = (
                "Python (matplotlib / seaborn)，强制设置 `mpl.rcParams['svg.fonttype']='none'` 与 `mpl.rcParams['pdf.fonttype']=42`，严禁混用 R 代绘"
                if be == "python"
                else "R (ggplot2 / patchwork / ComplexHeatmap)，使用 `svglite` / `cairo_pdf` / `ragg::agg_tiff`，严禁混用 Python 代绘"
            )
            tw = fc.get("target_width_mm") or 89
            jname = fc.get("journal_preset") or "Nature"
            mf_pt = fc.get("min_font_pt") or 6.0
            pal = fc.get("palette") or "Okabe-Ito"
            fmts = ", ".join(fc.get("required_formats") or fc.get("formats") or ["svg", "pdf", "png", "tiff"])
            claim = str(fc.get("core_claim") or "").strip()
            drawio_note = (
                "- 可编辑机制图导出：同时写出标准 `.drawio` (mxGraph XML) 源文件与矢量 SVG/PDF，支持在 Draw.io 中二次编辑。\n"
                if (str(fc.get("figure_type") or "").lower() == "schematic" or fc.get("export_drawio"))
                else ""
            )
            zone_b_static.append(
                "【科研出图契约 (Publication Figure Contract · 必须严格遵守)】\n"
                f"- 排他后端：{be_rule}\n"
                f"- 版面与字体：目标刊物 {jname}，物理终印宽 `{tw} mm`，纯白底板 `#FFFFFF`，终稿有效字号 `>= {mf_pt} pt`，配色方案 `{pal}`\n"
                f"- 导出格式包：`{fmts}`（PNG >= 300 DPI，TIFF >= 600 DPI 且像素宽匹配物理毫米）\n"
                + (f"- 核心科学论断 (Hero Claim)：{claim}\n" if claim else "")
                + drawio_note
                + "- 机械验收契约：脚本输出图件同时必须写出 `figure_manifest.json`（含 `figure_id`, `target_width_mm`, `min_dpi`, `script_path`, `script_sha256`, `source_data_path`, `source_data_sha256`, `exports`, `panel_sources`），以便 `figure_qa.py` 自动机械核验。"
            )

        visible_msgs, is_blind_r1 = self._visible_messages_for_step(task, step)

        # Pillar 2: Cross-Task SQLite FTS5 Memory Recall + Pure-Stdlib AST Structural RepoMap (fresh sessions only!)
        # Strictly skip cross-task recall during Blind Round 1 to guarantee zero anchoring bias!
        settings_map = st.get("settings") if isinstance(st.get("settings"), dict) else {}
        if not session_id and compression_level == "L0" and stype not in ("visual_qa",):
            if not is_blind_r1 and settings_map.get("enable_cross_task_memory", True):
                try:
                    proj_mem_root = task.get("project_path") or st["binding"]["project_path"]
                    recalled = ProjectMemoryIndex(proj_mem_root).recall_cross_task_memory(
                        str(task.get("goal") or ""),
                        str(task.get("id") or ""),
                        max_chars=1500,
                        limit=3,
                    )
                    if recalled:
                        zone_b_static.append(recalled)
                except Exception:
                    pass
            # Inject AST RepoMap into Codex fresh sessions (Antigravity already injects it in Zone C!)
            if mem.get("client") == "codex" and settings_map.get("enable_ast_repomap", True):
                try:
                    focus_toks = re.findall(r"[A-Za-z0-9_]{3,32}", str(task.get("goal") or ""))[:8]
                    rmap = build_structural_repomap(work_dir, max_chars=1600, focus_terms=focus_toks)
                    if rmap:
                        zone_b_static.append(rmap)
                except Exception:
                    pass

        # Pillar 5: Inject deterministic Borda Count scoreboard into final Host Synthesis when available!
        if stype == "synthesis" and isinstance(task.get("borda_scoreboard"), dict) and (task["borda_scoreboard"].get("ballots_count") or 0) > 0:
            bs = task["borda_scoreboard"]
            b_rows = [
                f"- {r['name']} ({r.get('anon_label') or r['member_id']}): {r['borda_points']} 分 (首推 {r['first_place_votes']} 票)"
                for r in (bs.get("rows") or [])
            ]
            zone_b_static.append(
                f"【匿名互评 Borda 共识计票结果（已揭晓真实席位 · 共 {bs['ballots_count']} 份有效选票 · 平均置信度 {bs['mean_confidence']}）】\n"
                + "\n".join(b_rows)
            )

        tmp_dir, _ = get_runtime_dirs()
        room_md_path = os.path.join(tmp_dir, "team_rooms", f"{task.get('id', 'task')}.md")

        dep_fallback = ""
        if len(visible_msgs) <= 1 and step.get("depends_on") and not is_blind_r1:
            dep_blocks = []
            for d_idx in step["depends_on"]:
                if 0 <= d_idx < len(task.get("steps") or []):
                    d_step = task["steps"][d_idx]
                    dep_blocks.append(
                        f"--- 前序步骤 {d_idx} ({d_step['title']} / {st['members'].get(d_step['member_id'], {}).get('name', d_step['member_id'])}) 成果 ---\n{d_step.get('latest_result_text', '')}"
                    )
            dep_fallback = "\n".join(dep_blocks)

        handoff_parts: List[str] = []
        if extra_handoff_md and extra_handoff_md.strip():
            handoff_parts.append(extra_handoff_md.strip())
        if step.get("handoff_context"):
            hc = step["handoff_context"]
            handoff_parts.append(
                "【改派交接摘要】\n"
                f"- 原负责人：{hc.get('reassigned_from')} → 现负责人：{hc.get('reassigned_to')}（原因：{hc.get('reason')}）\n"
                f"- 已完成步骤：{json.dumps(hc.get('completed_steps', []), ensure_ascii=False)}\n"
                f"- 前序产物：{json.dumps(hc.get('prior_outputs', []), ensure_ascii=False)}\n"
                f"- 待解决问题：{json.dumps(hc.get('open_issues_and_evidence', []), ensure_ascii=False)}"
            )
        handoff_block = "\n\n".join(handoff_parts)
        step_prompt_block = step["pending_repair_brief"] if step.get("pending_repair_brief") else f"【本步任务】\n{step['prompt']}"

        # ===== JIT Output Protocol Contracts (Zero-bloat per step_type!) =====
        protocol_blocks: List[str] = []
        if stype == "visual_qa":
            protocol_blocks.append(
                "【VLM 科研图件视觉质检输出格式契约 (Fail-Closed JSON)】\n"
                "请使用 read_file 亲自查看渲染生成的 PNG/SVG 图件，重点检查：文字/图例/散点是否遮挡重叠、字号可读性、配色对比与留白均衡、是否直观支撑核心论断。\n"
                "严格输出如下 JSON 对象（可置于 ```json 代码块中）：\n"
                '{"verdict":"PASS 或 REVISE","visual_score":8.5,"defects":[{"severity":"BLOCKING 或 MINOR","region":"面板/区域","issue":"具体视觉缺陷","fix":"可直接修改绘图脚本的精确建议"}],"summary":"一句话视觉评价"}'
            )
        elif is_rev:
            protocol_blocks.append(
                "【独立审查输出格式契约】\n"
                "请严格核对被审查步骤的实际输出证据，仅输出如下 JSON 对象：\n"
                '{"verdict": "ACCEPT 或 REJECT", "problem": "具体问题（通过填为空字符串）", '
                '"evidence": "可定位的具体证据", "impact": "问题影响", '
                '"expected_fix": "限定原范围的具体修复要求", "scope_expanded": false}'
            )
        elif stype != "chief_plan":
            seated_ids = set((task.get("mode_meta") or {}).get("participants") or [])
            in_room_peers = []
            standby_peers = []
            for pid, pinfo in st["members"].items():
                if pid == step["member_id"] or pinfo.get("client") not in SUPPORTED_CLIENTS:
                    continue
                desc = f"{pid} ({pinfo['name']} · {st['roles'].get(pinfo['role'], {}).get('name', '')})"
                if not seated_ids or pid in seated_ids or pid == (task.get("mode_meta") or {}).get("host_member_id"):
                    in_room_peers.append(desc)
                else:
                    standby_peers.append(desc)

            if (in_room_peers or standby_peers) and stype in ("execution", "discussion", "synthesis") and int(step.get("consult_count") or 0) < 3:
                roster_block = f"本场在席队友：{', '.join(in_room_peers) if in_room_peers else '无'}"
                if standby_peers and compression_level == "L0":
                    roster_block += f"\n场外支援专员（未参加本场圆桌发言，仅可请教具体技术/文献核查，切勿向其索要本场发言记录）：{', '.join(standby_peers)}"
                protocol_blocks.append(
                    "【团队内部 @ 结构化核验协商协议（可选）】\n"
                    f"{roster_block}\n"
                    "注意：上方共享群聊记录已包含队友的完整发言，无需为索要聊天记录而提问。仅当需要某位队友结合其专长独立核验某条关键声明、数据表或代码边界时，可在末尾附上结构化代码块唤醒TA答复：\n"
                    "```team_consult\n"
                    '{"to":"队友id","claim_to_verify":"待核验的核心声明或数值（可选）","evidence_needed":"希望读取核对的具体文件或口径（可选）","question":"具体请教或要求协助核对的问题"}\n'
                    "```"
                )
            if stype not in ("fact_check", "opening"):
                protocol_blocks.append(
                    "【向人类负责人提问确认协议（可选）】\n"
                    "严禁调用内置 ask_question 弹窗工具。仅当存在必须由人类负责人拍板的方向选择时，在末尾附上如下提问块暂停等待点选：\n"
                    "```team_questions\n"
                    '{"questions":[{"id":"q1","question":"需要确认的问题","multi_select":false,"options":["(推荐) 方案A","方案B"]}]}\n'
                    "```"
                )

        sess_metrics_map = task.get("session_metrics") if isinstance(task.get("session_metrics"), dict) else {}
        sess_metric = sess_metrics_map.get(session_id) if session_id else None
        cur_stage = int(step.get("stage_index") or 0)
        sealed_snaps = [] if is_blind_r1 else list(task.get("round_snapshots") or [])

        return compile_turn_prompt(
            zone_a_blocks=zone_a_blocks,
            zone_b_static_blocks=zone_b_static,
            sealed_snapshots=sealed_snaps,
            visible_msgs=visible_msgs,
            is_blind_r1=is_blind_r1,
            current_stage_index=cur_stage,
            step_prompt_block=step_prompt_block,
            handoff_block=handoff_block,
            protocol_blocks=protocol_blocks,
            room_md_path=room_md_path,
            blob_store=blob_store,
            session_id=session_id,
            session_metric=sess_metric,
            model_slug=mem.get("model"),
            cfg=cfg,
            compression_level=compression_level,
            force_full_prompt=force_full_prompt,
            dep_fallback_block=dep_fallback,
        )

    def _build_step_prompt(self, st: Dict[str, Any], task: Dict[str, Any], step: Dict[str, Any], work_dir: str) -> str:
        """Backward-compatible prompt builder delegating to v5.3 Three-Zone Context Compiler."""
        plan = self._compile_step_turn(st, task, step, work_dir, session_id=None, compression_level="L0", force_full_prompt=True)
        return plan["prompt"]

    def _invoke_member_turn(
        self,
        mem: Dict[str, Any],
        prompt_text: str,
        session_id: Optional[str],
        work_dir: str,
        sandbox_arg: str,
        agy_proj_id: Optional[str],
        attempt_id: str,
        on_start: Any = None,
        on_event: Any = None,
        on_partial_text: Any = None,
        lightweight_effort: bool = False,
    ) -> CodexExecutionResult:
        client_kind = mem.get("client", "codex")
        model_arg = None if mem["model"] in ("client_default", "client_current_setting") else mem["model"]
        effort_arg = None if mem["reasoning_effort"] in ("client_default", "client_current_setting") else mem["reasoning_effort"]
        if lightweight_effort and effort_arg in ("high", "xhigh", "max"):
            try:
                self._validate_client_model_effort(client_kind, model_arg, "medium")
                effort_arg = "medium"
            except Exception:
                pass
        if client_kind == "antigravity":
            return self.agy_adapter.execute_turn(
                prompt=prompt_text,
                session_id=session_id,
                model=model_arg,
                reasoning_effort=effort_arg,
                cwd=work_dir,
                sandbox=sandbox_arg,
                agy_project_id=agy_proj_id,
                attempt_id=attempt_id,
                on_start=on_start,
                on_event=on_event,
                on_partial_text=on_partial_text,
            )
        return self.adapter.execute_turn(
            prompt=prompt_text,
            session_id=session_id,
            model=model_arg,
            reasoning_effort=effort_arg,
            cwd=work_dir,
            sandbox=sandbox_arg,
            attempt_id=attempt_id,
            on_start=on_start,
            on_event=on_event,
            on_partial_text=on_partial_text,
        )

    def _execute_attempt_worker(self, task_id: str, step_index: int, attempt_id: str) -> None:
        try:
            snap = self.store.snapshot()
            task = self._find_task(snap, task_id)
            step = task["steps"][step_index]
            mem = snap["members"][step["member_id"]]
            proj_root = task.get("project_path") or snap["binding"]["project_path"]
            rel_scope = step.get("workspace_scope") or mem.get("workspace_scope") or "."

            # P0-0 Secondary Guard: validate path confinement, hard sandbox ceiling & shell capability BEFORE spawning CLI!
            try:
                work_dir = _resolve_workdir(proj_root, rel_scope)
                max_sb = snap["binding"].get("max_sandbox", "workspace-write")
                task_sb = _effective_sandbox(max_sb, task.get("sandbox", "read-only"))
                sandbox_arg = _effective_sandbox(
                    task_sb,
                    step.get("sandbox") or mem.get("sandbox") or "read-only",
                    read_only=bool(step.get("read_only", True)),
                )
                if step.get("requires_shell") and mem.get("client") == "antigravity":
                    raise ValueError(
                        f"能力路由拦截：步骤 {step_index} 需要 Shell/脚本执行权限 (requires_shell=True)，不能指派给无头 Antigravity 节点 {step['member_id']}"
                    )
            except ValueError as sec_exc:
                blocked_res = CodexExecutionResult(
                    session_id=step.get("worker_session_id"),
                    response_text="",
                    partial_text="",
                    exit_code=1,
                    turn_completed=False,
                    policy_blocked=True,
                    blocking_reason=f"安全/能力策略拦截: {sec_exc}",
                    requested_config={},
                    accepted_config={},
                )
                self._finalize_attempt(task_id, step_index, attempt_id, blocked_res, False, None)
                return

            # Pillar 4: Optional Native Git Worktree Isolation for writable steps (strictly outside state lock!)
            wt_info: Optional[Dict[str, Any]] = None
            if (
                task.get("worktree_isolation")
                and sandbox_arg == "workspace-write"
                and not step.get("read_only", True)
            ):
                existing_wt = step.get("worktree_info") if isinstance(step.get("worktree_info"), dict) else None
                if existing_wt and existing_wt.get("worktree_path") and os.path.isdir(existing_wt["worktree_path"]):
                    wt_info = dict(existing_wt)
                    work_dir = wt_info.get("effective_work_dir") or wt_info["worktree_path"]
                else:
                    git_det = detect_git_repo(work_dir)
                    if git_det.get("is_git"):
                        try:
                            wt_info = create_step_worktree(work_dir, task_id, step_index, step["member_id"])
                            work_dir = wt_info["effective_work_dir"]
                            def _save_wt_start(st: Dict[str, Any]) -> None:
                                t = self._find_task(st, task_id)
                                t["steps"][step_index]["worktree_info"] = dict(wt_info)
                                t["recent_events"].append(
                                    {"ts": time.time(), "type": "worktree.created", "summary": f"步骤 {step_index} 已分配隔离 Git Worktree ({wt_info['branch']})"}
                                )
                                t["recent_events"] = t["recent_events"][-40:]
                            self.store.mutate(_save_wt_start)
                        except Exception:
                            wt_info = None

            # P0-3 Lock-Free pre-execution SHA-256 snapshot of readonly_inputs (strictly outside state lock!)
            research_spec = task.get("research_spec") if isinstance(task.get("research_spec"), dict) else None
            before_inputs: Dict[str, str] = {}
            if research_spec and research_spec.get("readonly_inputs"):
                before_inputs = snapshot_readonly_inputs(work_dir, list(research_spec.get("readonly_inputs") or []))

            # v5.3 Context Engine: Three-Zone KV-Cache Prefix-Stable Compilation + Hard Budget Governance
            ctx_cfg = ContextBudgetConfig.from_settings(snap.get("settings"))
            blob_store = BlobStore(task_id)
            tmp_dir_ctx, _ = get_runtime_dirs()
            room_md_path = os.path.join(tmp_dir_ctx, "team_rooms", f"{task_id}.md")

            sess_to_use = None if ctx_cfg.session_mode == "stateless_replay" else step.get("worker_session_id")
            agy_proj_id = snap["binding"].get("project_agy_map", {}).get(normalize_win_path(work_dir)) or (
                snap["binding"].get("antigravity_project_id")
                if normalize_win_path(work_dir) == normalize_win_path(snap["binding"]["project_path"]) and snap["binding"].get("antigravity_project_id")
                else None
            )

            turn_plan = self._compile_step_turn(
                snap, task, step, work_dir, session_id=sess_to_use, compression_level="L0"
            )
            used_fallback = False
            fallback_of: Optional[str] = None
            rollover_events: List[Dict[str, Any]] = []
            archived_session_ids: List[str] = []

            # Hard budget ceiling guard: NEVER invoke CLI when P0 alone exceeds ceiling!
            if turn_plan.get("budget_decision") == "budget_blocked":
                b_reason = "; ".join(turn_plan.get("omitted_refs") or ["P0 必选任务指令超过硬上下文预算上限"])
                blocked_res = CodexExecutionResult(
                    session_id=sess_to_use,
                    response_text="",
                    partial_text="",
                    exit_code=1,
                    turn_completed=False,
                    context_overflow=True,
                    error_kind="context_length_exceeded",
                    error=f"硬预算阻断 (budget_blocked): {b_reason}",
                    requested_config={},
                    accepted_config={},
                )
                self._finalize_attempt(
                    task_id,
                    step_index,
                    attempt_id,
                    blocked_res,
                    False,
                    None,
                    turn_plan=turn_plan,
                    rollover_events=rollover_events,
                )
                return

            # Proactive Watermark Rollover (M1): retire aging session before hitting CLI context ceiling!
            if sess_to_use and turn_plan.get("budget_decision") == "proactive_rollover":
                ro_reason = str(turn_plan.get("rollover_reason") or "token_watermark")
                archived_session_ids.append(sess_to_use)
                fallback_of = sess_to_use
                used_fallback = True
                vis_now, _ = self._visible_messages_for_step(task, step)
                ckpt = build_handoff_checkpoint(
                    task,
                    step,
                    vis_now,
                    blob_store,
                    room_md_path,
                    reason=ro_reason,
                    level="L1",
                )
                rollover_events.append(
                    {
                        "kind": "proactive",
                        "from_session_id": sess_to_use,
                        "reason": ro_reason,
                        "level": "L0",
                        "checkpoint_path": ckpt["blob_path"],
                        "ts": time.time(),
                    }
                )
                sess_to_use = None
                turn_plan = self._compile_step_turn(
                    snap,
                    task,
                    step,
                    work_dir,
                    session_id=None,
                    compression_level="L0",
                    force_full_prompt=True,
                    extra_handoff_md=ckpt["checkpoint_md"],
                )

            prompt_text = turn_plan["prompt"]
            prompt_ledger_path = ""
            try:
                p_meta = blob_store.put_text(prompt_text, subdir="prompts", ext=".md")
                prompt_ledger_path = p_meta["path"]
            except Exception:
                prompt_ledger_path = ""

            def _make_telemetry(plan: Dict[str, Any], ro_list: List[Dict[str, Any]], p_path: str) -> Dict[str, Any]:
                return {
                    "prefix_sha256": plan.get("prefix_sha256"),
                    "is_delta": bool(plan.get("is_delta", False)),
                    "compression_level": plan.get("compression_level", "L0"),
                    "prompt_chars": int(plan.get("prompt_chars", 0)),
                    "est_tokens": int(plan.get("est_tokens", 0)),
                    "budget_chars": int(plan.get("budget_chars", 0)),
                    "masked_blocks": int(plan.get("masked_blocks", 0)),
                    "used_snapshots": int(plan.get("used_snapshots", 0)),
                    "omitted_refs": list(plan.get("omitted_refs") or [])[:8],
                    "window_tokens": int(plan.get("window_tokens", 200_000)),
                    "window_source": plan.get("window_source", "default"),
                    "session_mode": ctx_cfg.session_mode,
                    "rollover_count": len(ro_list),
                    "rollover_events": list(ro_list),
                    "prompt_ledger_path": p_path,
                }

            def _save_prompt(st: Dict[str, Any]) -> None:
                t = self._find_task(st, task_id)
                att_ref = next((a for a in t["steps"][step_index]["attempts"] if a["attempt_id"] == attempt_id), t["steps"][step_index]["attempts"][-1])
                att_ref["prompt_sent"] = (
                    prompt_text[:4000] + (f"\n…[完整 Prompt ({len(prompt_text)} chars) 见 {prompt_ledger_path}]" if len(prompt_text) > 4000 else "")
                )
                att_ref["prompt_ledger_path"] = prompt_ledger_path
                att_ref["context_telemetry"] = _make_telemetry(turn_plan, rollover_events, prompt_ledger_path)
                if archived_session_ids:
                    hist = st["members"][step["member_id"]].setdefault("session_history", [])
                    for sid in archived_session_ids:
                        if sid and sid not in hist:
                            hist.append(sid)
                    t["steps"][step_index]["session_epoch"] = int(t["steps"][step_index].get("session_epoch") or 0) + len(rollover_events)
                    t["recent_events"].append(
                        {
                            "ts": time.time(),
                            "type": "context.rollover",
                            "summary": f"步骤 {step_index} ({mem['name']}) 触发预防性无缝换乘新会话 ({rollover_events[-1]['reason']})",
                        }
                    )
                    t["recent_events"] = t["recent_events"][-40:]

            self.store.mutate(_save_prompt)

            def _is_attempt_still_active() -> bool:
                def _chk(st: Dict[str, Any]) -> bool:
                    c_task = self._find_task(st, task_id)
                    c_step = c_task["steps"][step_index]
                    if c_step.get("status") != "running" or c_step.get("member_id") != step["member_id"]:
                        return False
                    c_att = next((a for a in c_step.get("attempts", []) if a.get("attempt_id") == attempt_id), None)
                    return bool(c_att and c_att.get("status") not in ("aborted", "cancelled"))
                try:
                    return bool(self.store.read(_chk))
                except Exception:
                    return False

            def _on_start(pid: int) -> None:
                def _upd_pid(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    att = next(a for a in t["steps"][step_index]["attempts"] if a["attempt_id"] == attempt_id)
                    att["pid"] = pid
                    t["recent_events"].append(
                        {"ts": time.time(), "type": "worker.started", "summary": f"步骤 {step_index} ({mem['name']}) 已启动 PID={pid}"}
                    )
                    t["recent_events"] = t["recent_events"][-40:]

                self.store.mutate(_upd_pid, persist="debounced")

            last_partial_flush = {"ts": 0.0}

            def _on_partial_text(txt: str) -> None:
                now = time.time()
                if now - last_partial_flush["ts"] < 0.65:
                    return
                last_partial_flush["ts"] = now
                capped = txt[-4000:] if len(txt) > 4000 else txt

                def _upd_p(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    att = next(a for a in t["steps"][step_index]["attempts"] if a["attempt_id"] == attempt_id)
                    att["partial_text"] = capped
                    t["updated_at"] = now

                self.store.mutate(_upd_p, persist="debounced")

            def _on_event(ev: Dict[str, Any]) -> None:
                def _upd_ev(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    s = t["steps"][step_index]
                    att = next(a for a in s["attempts"] if a["attempt_id"] == attempt_id)
                    ev_type = ev.get("type", "event")
                    if ev_type != "thinking.heartbeat":
                        att["events_summary"].append(ev)
                        att["events_summary"] = att["events_summary"][-30:]
                    if ev.get("session_id"):
                        att["session_id"] = ev["session_id"]
                        s["worker_session_id"] = ev["session_id"]
                    if ev.get("activity_label") and st["members"].get(step["member_id"], {}).get("status") == "working":
                        st["members"][step["member_id"]]["live_activity"] = ev["activity_label"]
                    # Replace consecutive thinking.heartbeat tail instead of flooding recent_events
                    if ev_type == "thinking.heartbeat" and t["recent_events"] and t["recent_events"][-1].get("type") == "thinking.heartbeat" and mem["name"] in str(t["recent_events"][-1].get("summary", "")):
                        t["recent_events"][-1] = {
                            "ts": ev.get("ts", time.time()),
                            "type": ev_type,
                            "summary": f"[{mem['name']}] {ev.get('summary', '')}",
                        }
                    else:
                        t["recent_events"].append(
                            {"ts": ev.get("ts", time.time()), "type": ev_type, "summary": f"[{mem['name']}] {ev.get('summary', '')}"}
                        )
                        t["recent_events"] = t["recent_events"][-40:]
                    t["updated_at"] = time.time()

                self.store.mutate(_upd_ev, persist="debounced")

            step_exec_started_at = time.time()
            is_light = step.get("step_type") in ("opening", "chief_plan")
            res = self._invoke_member_turn(
                mem, prompt_text, sess_to_use, work_dir, sandbox_arg, agy_proj_id, attempt_id, _on_start, _on_event, _on_partial_text, lightweight_effort=is_light
            )

            # Reactive Context-Overflow Rollover: monotonic L0 -> L1 -> L2 (hard cap 2 retries) + write-side-effect safety gate!
            is_ovf = bool(getattr(res, "context_overflow", False)) or is_context_overflow_error(
                res.error, res.stderr_lines, getattr(res, "raw_events", None)
            )
            if is_ovf and not res.aborted and not res.policy_blocked:
                res.context_overflow = True
                res.error_kind = "context_length_exceeded"
                se_state = str(getattr(res, "side_effect_state", "none") or "none")
                has_mutated_risk = (sandbox_arg != "read-only") and (
                    se_state == "unknown_mutation" or bool(getattr(res, "changed_files", None))
                )
                if has_mutated_risk:
                    res.side_effect_state = "unknown_mutation"
                    try:
                        vis_m_mut, _ = self._visible_messages_for_step(task, step)
                        ckpt_mut = build_handoff_checkpoint(
                            task, step, vis_m_mut, blob_store, room_md_path, reason="overflow_with_mutation", level="L1"
                        )
                        res.error = (
                            f"上下文超限且工作区可能已发生写入变更 (side_effect={se_state})，"
                            f"为防止重复副作用已暂停自动重放并保存检查点 {ckpt_mut['blob_path']}，请核查工作区后继续或重置。"
                        )
                    except Exception:
                        pass
                elif ctx_cfg.auto_rollover:
                    for next_lvl in ("L1", "L2"):
                        if not _is_attempt_still_active():
                            break
                        if sess_to_use and sess_to_use not in archived_session_ids:
                            archived_session_ids.append(sess_to_use)
                        if sess_to_use:
                            fallback_of = fallback_of or sess_to_use
                        used_fallback = True
                        sess_to_use = None

                        cur_snap_ro = self.store.snapshot()
                        cur_task_ro = self._find_task(cur_snap_ro, task_id)
                        cur_step_ro = cur_task_ro["steps"][step_index]
                        vis_ro, _ = self._visible_messages_for_step(cur_task_ro, cur_step_ro)
                        ckpt_ro = build_handoff_checkpoint(
                            cur_task_ro,
                            cur_step_ro,
                            vis_ro,
                            blob_store,
                            room_md_path,
                            reason=f"reactive_overflow_{next_lvl}",
                            level=next_lvl,
                        )
                        next_plan = self._compile_step_turn(
                            cur_snap_ro,
                            cur_task_ro,
                            cur_step_ro,
                            work_dir,
                            session_id=None,
                            compression_level=next_lvl,
                            force_full_prompt=True,
                            extra_handoff_md=ckpt_ro["checkpoint_md"],
                        )
                        if next_plan.get("budget_decision") == "budget_blocked":
                            turn_plan = next_plan
                            res.error = f"上下文超限且降级至 {next_lvl} 仍触发硬预算阻断: {'; '.join(next_plan.get('omitted_refs') or [])}"
                            break

                        turn_plan = next_plan
                        prompt_text = turn_plan["prompt"]
                        try:
                            prompt_ledger_path = blob_store.put_text(prompt_text, subdir="prompts", ext=".md")["path"]
                        except Exception:
                            pass
                        rollover_events.append(
                            {
                                "kind": "reactive_overflow",
                                "from_session_id": fallback_of,
                                "reason": f"context_length_exceeded -> {next_lvl}",
                                "level": next_lvl,
                                "checkpoint_path": ckpt_ro["blob_path"],
                                "ts": time.time(),
                            }
                        )

                        def _log_reactive_ro(st: Dict[str, Any]) -> None:
                            t = self._find_task(st, task_id)
                            t["steps"][step_index]["session_epoch"] = int(t["steps"][step_index].get("session_epoch") or 0) + 1
                            t["recent_events"].append(
                                {
                                    "ts": time.time(),
                                    "type": "context.rollover",
                                    "summary": f"步骤 {step_index} ({mem['name']}) 上下文超限，自动压缩至 {next_lvl} 并无损换乘新会话重试",
                                }
                            )
                            t["recent_events"] = t["recent_events"][-40:]

                        self.store.mutate(_log_reactive_ro)
                        res = self._invoke_member_turn(
                            mem, prompt_text, None, work_dir, sandbox_arg, agy_proj_id, attempt_id, _on_start, _on_event, _on_partial_text, lightweight_effort=is_light
                        )
                        still_ovf = bool(getattr(res, "context_overflow", False)) or is_context_overflow_error(
                            res.error, res.stderr_lines, getattr(res, "raw_events", None)
                        )
                        if not still_ovf:
                            break
                        res.context_overflow = True
                        res.error_kind = "context_length_exceeded"
                        se2 = str(getattr(res, "side_effect_state", "none") or "none")
                        if (sandbox_arg != "read-only") and (se2 == "unknown_mutation" or bool(getattr(res, "changed_files", None))):
                            res.side_effect_state = "unknown_mutation"
                            break

            # Existing session_not_found fallback (replaces raw prior_texts[-2:] dump with Checkpoint Handoff Pack!)
            if (
                sess_to_use
                and not res.aborted
                and not res.policy_blocked
                and not getattr(res, "context_overflow", False)
                and not _is_quota_or_rate_limit_error(res.error, res.stderr_lines)
                and (res.session_not_found or (res.exit_code != 0 and not res.turn_completed))
                and _is_attempt_still_active()
            ):
                fallback_of = sess_to_use
                used_fallback = True
                if sess_to_use not in archived_session_ids:
                    archived_session_ids.append(sess_to_use)
                sess_to_use = None
                vis_snf, _ = self._visible_messages_for_step(task, step)
                ckpt_snf = build_handoff_checkpoint(
                    task, step, vis_snf, blob_store, room_md_path, reason="session_not_found", level="L1"
                )
                rollover_events.append(
                    {
                        "kind": "session_not_found_fallback",
                        "from_session_id": fallback_of,
                        "reason": "session_not_found",
                        "level": "L1",
                        "checkpoint_path": ckpt_snf["blob_path"],
                        "ts": time.time(),
                    }
                )
                snf_plan = self._compile_step_turn(
                    snap,
                    task,
                    step,
                    work_dir,
                    session_id=None,
                    compression_level="L1",
                    force_full_prompt=True,
                    extra_handoff_md=ckpt_snf["checkpoint_md"],
                )
                turn_plan = snf_plan
                prompt_text = snf_plan["prompt"]
                res = self._invoke_member_turn(
                    mem, prompt_text, None, work_dir, sandbox_arg, agy_proj_id, attempt_id, _on_start, _on_event, _on_partial_text, lightweight_effort=is_light
                )

            # Live Peer @Mention Consultation Loop (up to 3 consultations per step, deduped + reply-capped!)
            consult_loops = 0
            seen_consult_keys: set = set()
            while res.final_status == "succeeded" and consult_loops < 3:
                cur_snap = self.store.snapshot()
                consult_req, cleaned_before_consult = _extract_team_consult(
                    res.response_text, cur_snap["members"], step["member_id"]
                )
                if not consult_req:
                    break
                peer_id = consult_req["to"]
                peer_q = consult_req["question"]
                peer_claim = consult_req.get("claim_to_verify", "")
                peer_ev_need = consult_req.get("evidence_needed", "")
                ckey = (
                    step["member_id"],
                    peer_id,
                    hashlib.sha256((peer_claim or "").strip().encode("utf-8")).hexdigest()[:12],
                    hashlib.sha256((peer_q or "").strip().encode("utf-8")).hexdigest()[:12],
                )
                if ckey in seen_consult_keys:
                    break
                seen_consult_keys.add(ckey)
                consult_loops += 1
                peer_mem = cur_snap["members"][peer_id]
                peer_role_name = cur_snap.get("roles", {}).get(peer_mem.get("role", ""), {}).get("name", "专员")

                ask_display = peer_q
                if peer_claim or peer_ev_need:
                    extra_lines = []
                    if peer_claim:
                        extra_lines.append(f"- 待核验声明：{peer_claim}")
                    if peer_ev_need:
                        extra_lines.append(f"- 需核对证据：{peer_ev_need}")
                    if peer_q and peer_q not in peer_claim:
                        extra_lines.append(f"- 具体问题：{peer_q}")
                    ask_display = "\n".join(extra_lines)

                def _log_ask(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    if cleaned_before_consult:
                        self._append_room_chat(
                            st, t, sender_id=step["member_id"], kind="speech", content=cleaned_before_consult,
                            step_index=step_index, round_label=step.get("round_label") or step["title"]
                        )
                    self._append_room_chat(
                        st, t, sender_id=step["member_id"], kind="mention_ask", content=ask_display,
                        to_member_id=peer_id, step_index=step_index, round_label="@队友结构化协商"
                    )
                    st["members"][step["member_id"]]["live_activity"] = f"正在等待 @{peer_mem['name']} 答复..."
                    if st["members"][peer_id]["status"] == "idle":
                        st["members"][peer_id]["status"] = "working"
                        st["members"][peer_id]["live_activity"] = f"正在回应 @{mem['name']} 的提问"
                    t["recent_events"].append(
                        {"ts": time.time(), "type": "agent.mention_ask", "summary": f"{mem['name']} @{peer_mem['name']} 发起结构化协商"}
                    )

                self.store.mutate(_log_ask)

                peer_spec = (peer_mem.get("specialty") or "").strip()
                peer_persona = (peer_mem.get("persona_prompt") or "").strip()
                peer_dialect = _build_model_family_tuning(peer_mem.get("client", "codex"), peer_mem.get("model", ""))
                peer_parts = [
                    f"【当前项目工作目录】{work_dir}",
                    f"【你的专员身份】{peer_mem.get('name')} (ID: {peer_id}) · 职责：{peer_role_name}" + (f" · 专长：{peer_spec}" if peer_spec else ""),
                ]
                if peer_persona:
                    peer_parts.append(f"【你的专业核查视角】\n{peer_persona}")
                if peer_dialect:
                    peer_parts.append(peer_dialect)
                peer_parts.append(
                    f"【团队实时协助请求】队友 {mem['name']} 在执行“{task['title']}”时向你 (@{peer_mem['name']}) 发起只读独立核验：\n"
                    f"{ask_display}\n\n"
                    "【结构化交接契约（OpenScience Specialist Handoff，严禁再发起新的 team_consult）】\n"
                    "请亲自只读检查相关真实文件或数据，按以下紧凑结构直接答复：\n"
                    "- **判定 (Verdict)**：`CONFIRMED`（证实）/ `REFUTED`（证伪）/ `PARTIAL`（部分成立）/ `UNVERIFIABLE`（现有材料无法核实）\n"
                    "- **核查证据 (Evidence)**：精确 `[文件名](路径#L行号)` 与实测数值/代码行为（绝不猜测）\n"
                    "- **最小修正或竞争性解释 (Correction / Alternative)**：若存在偏差或更优解释，给出可直接采纳的纠偏建议或下一步最小判别检验"
                )
                peer_prompt = "\n\n".join(peer_parts)
                peer_res = self._invoke_member_turn(
                    peer_mem, peer_prompt, None, work_dir, "read-only", agy_proj_id, f"{attempt_id}-c{consult_loops}", lightweight_effort=True
                )
                raw_peer_txt = (peer_res.response_text or "").strip()
                if peer_res.final_status != "succeeded" or not raw_peer_txt:
                    err_detail = peer_res.error or peer_res.blocking_reason or f"status={peer_res.final_status}, exit={peer_res.exit_code}"
                    peer_reply = (
                        f"**判定 (Verdict)**：`UNVERIFIABLE`（协商核验调用未成功，严禁视为“无异议/通过”）\n"
                        f"**失败原因**：{err_detail}\n"
                        "**后续要求**：请将相关声明保留为 `[待核实]`，切勿依据失败的协商调用声称已获队友确认。"
                    )
                else:
                    peer_reply = raw_peer_txt

                def _log_reply(st: Dict[str, Any]) -> None:
                    t = self._find_task(st, task_id)
                    t["steps"][step_index]["consult_count"] = int(t["steps"][step_index].get("consult_count", 0)) + 1
                    self._append_room_chat(
                        st, t, sender_id=peer_id, kind="mention_reply", content=peer_reply,
                        to_member_id=step["member_id"], step_index=step_index, round_label="@协商答复"
                    )
                    if st["members"][peer_id].get("live_activity", "").startswith("正在回应"):
                        st["members"][peer_id]["status"] = "idle"
                        st["members"][peer_id]["live_activity"] = ""
                    st["members"][step["member_id"]]["live_activity"] = f"收到 @{peer_mem['name']} 反馈，继续定稿"
                    t["recent_events"].append(
                        {"ts": time.time(), "type": "agent.mention_reply", "summary": f"{peer_mem['name']} 已回复 @{mem['name']}"}
                    )

                self.store.mutate(_log_reply)

                # Cap injected peer reply to <= 3,500 chars while preserving anchors & full BlobStore pointer!
                injected_peer = peer_reply
                if len(injected_peer) > 3500:
                    pr_blob = blob_store.put_text(peer_reply, subdir="consults", ext=".md")
                    pr_masked, _ = mask_observation_blocks(peer_reply, blob_store, min_code_lines=16, min_table_lines=14, min_block_chars=700)
                    injected_peer = pr_masked[:3100] + f"\n…[完整协商回复已落盘：{pr_blob['path']} · 完整群聊见 {room_md_path}]"

                base_re_ctx = ""
                if not res.session_id:
                    base_re_ctx = (prompt_text[:12000] + "\n…[上下文已截断，完整 Prompt 见 " + prompt_ledger_path + "]\n\n") if len(prompt_text) > 12000 else f"{prompt_text}\n\n"
                followup_to_caller = (
                    base_re_ctx
                    + f"【收到队友 @{peer_mem['name']} 的协商回复】\n{injected_peer}\n\n"
                    + "请结合该反馈直接完成你的本步最终交付（不要重复发起相同提问）。"
                )
                res = self._invoke_member_turn(
                    mem, followup_to_caller, res.session_id, work_dir, sandbox_arg, agy_proj_id, attempt_id, _on_start, _on_event, _on_partial_text
                )

            # P0-2 Lock-Free Sidecar Execution Ledger (keeps team_state.json lean & avoids O(N^2) bloat)
            ledger_summary: Optional[Dict[str, Any]] = None
            cmd_logs = list(getattr(res, "command_logs", None) or [])
            chg_files = list(getattr(res, "changed_files", None) or [])
            edit_after = bool(getattr(res, "edit_after_check", False))
            if cmd_logs or chg_files or research_spec:
                ledger_path = ""
                try:
                    ledger_path = _write_sidecar_ledger(
                        task_id,
                        attempt_id,
                        {
                            "task_id": task_id,
                            "step_index": step_index,
                            "attempt_id": attempt_id,
                            "member_id": step["member_id"],
                            "client": mem.get("client"),
                            "model": mem.get("model"),
                            "sandbox": sandbox_arg,
                            "final_status": res.final_status,
                            "exit_code": res.exit_code,
                            "edit_after_check": edit_after,
                            "command_logs": cmd_logs,
                            "changed_files": chg_files,
                            "recorded_at": time.time(),
                        },
                    )
                except Exception:
                    ledger_path = ""
                nonzero_cmds = sum(1 for c in cmd_logs if c.get("exit_code") not in (0, None))
                ledger_summary = {
                    "command_count": len(cmd_logs),
                    "nonzero_command_count": nonzero_cmds,
                    "changed_files_count": len(chg_files),
                    "changed_files_preview": [c.get("path", "") for c in chg_files[:12] if c.get("path")],
                    "edit_after_check": edit_after,
                    "ledger_path": ledger_path,
                }

            # P0-3 Lock-Free Mechanical Acceptance Checks & Advisory Auto-Figure-QA (STRICTLY OUTSIDE TeamStateManager._lock!)
            # 3-layer exception isolation: QA failure NEVER destroys a succeeded specialist's response_text `res`!
            verif_report: Optional[Dict[str, Any]] = None
            if (
                res.final_status == "succeeded"
                and step.get("step_type") in ("execution", "synthesis")
                and step.get("review_of") is None
            ):
                def _collect_step_manifests() -> List[str]:
                    def _looks_like_fig_mf(fp_abs: str) -> bool:
                        try:
                            with open(fp_abs, "r", encoding="utf-8") as mf_f:
                                obj = json.load(mf_f)
                            return isinstance(obj, dict) and any(
                                k in obj for k in ("exports", "outputs", "figure_id", "panel_sources", "target_width_mm")
                            )
                        except Exception:
                            return False

                    found: List[str] = []
                    wd_real = os.path.realpath(work_dir)
                    for cf in chg_files:
                        cp = str(cf.get("path") or "").strip().replace("\\", "/")
                        if cp.lower().endswith(".json") and "manifest" in os.path.basename(cp).lower():
                            cand_abs = os.path.realpath(os.path.join(wd_real, cp))
                            if cp not in found and _looks_like_fig_mf(cand_abs):
                                found.append(cp)
                    # Also detect manifests written directly by plotting scripts via shell (bounded depth-3, fresh mtime)
                    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
                    try:
                        if os.path.isdir(wd_real):
                            for cur_r, subdirs, fns in os.walk(wd_real):
                                rel_d = os.path.relpath(cur_r, wd_real)
                                depth = 0 if rel_d == "." else rel_d.count(os.sep) + 1
                                subdirs[:] = [d for d in subdirs if d not in skip_dirs and not d.startswith(".")] if depth < 3 else []
                                for fn in fns:
                                    if fn.lower().endswith(".json") and "manifest" in fn.lower():
                                        fp = os.path.join(cur_r, fn)
                                        try:
                                            if os.path.getmtime(fp) >= step_exec_started_at - 2.0 and _looks_like_fig_mf(fp):
                                                rel_p = os.path.relpath(fp, wd_real).replace("\\", "/")
                                                if rel_p not in found:
                                                    found.append(rel_p)
                                        except OSError:
                                            pass
                                    if len(found) >= 8:
                                        break
                                if len(found) >= 8:
                                    break
                    except Exception:
                        pass
                    return found[:8]

                if research_spec:
                    # Merge any newly created/modified manifests in this step if figure_contract requested auto-discovery
                    eff_spec = dict(research_spec)
                    if eff_spec.get("figure_contract") and not eff_spec.get("figure_manifests"):
                        auto_mfs = _collect_step_manifests()
                        if auto_mfs:
                            eff_spec["figure_manifests"] = auto_mfs
                    try:
                        verif_report = run_acceptance_checks(
                            work_dir=work_dir,
                            research_spec=eff_spec,
                            before_inputs_snapshot=before_inputs,
                            command_logs=cmd_logs,
                            edit_after_check=edit_after,
                        )
                    except Exception as qa_exc:
                        verif_report = {
                            "ok": False,
                            "status": "error",
                            "mode": eff_spec.get("mode", "managed_exec"),
                            "blocking_items": [{"source": "verifier_error", "object_id": "run_acceptance_checks", "message": f"验收引擎异常: {type(qa_exc).__name__}: {qa_exc}"}],
                            "blocking": [f"验收引擎异常: {type(qa_exc).__name__}: {qa_exc}"],
                            "observations": [],
                            "checks": [],
                            "figure_reports": [],
                            "verified_at": time.time(),
                        }
                elif sandbox_arg == "workspace-write":
                    # P0-d Advisory Auto-Figure-QA fallback for ordinary writable UI plotting tasks (strictly non-blocking!)
                    disc_mfs = _collect_step_manifests()
                    if disc_mfs:
                        try:
                            verif_report = run_acceptance_checks(
                                work_dir=work_dir,
                                research_spec={
                                    "mode": "auto_figure_advisory",
                                    "advisory": True,
                                    "block_on_failed_signoff": False,
                                    "figure_manifests": disc_mfs[:8],
                                },
                                before_inputs_snapshot=before_inputs,
                                command_logs=cmd_logs,
                                edit_after_check=edit_after,
                            )
                        except Exception as qa_exc:
                            verif_report = {
                                "ok": False,
                                "status": "error",
                                "mode": "auto_figure_advisory",
                                "advisory": True,
                                "blocking_items": [{"source": "verifier_error", "object_id": "auto_figure_advisory", "message": f"自动图件质检异常: {type(qa_exc).__name__}: {qa_exc}"}],
                                "blocking": [f"自动图件质检异常: {type(qa_exc).__name__}: {qa_exc}"],
                                "observations": [],
                                "checks": [],
                                "figure_reports": [],
                                "verified_at": time.time(),
                            }

            # OpenScience Content-Addressed Provenance DAG & Experiment Runs (STRICTLY OUTSIDE state lock!)
            try:
                tmp_dir_p, _ = get_runtime_dirs()
                prov_store = TaskProvenanceStore(os.path.join(tmp_dir_p, "team_ledgers"), task_id)
                prov_store.record_attempt_lineage(
                    step_index=step_index,
                    attempt_id=attempt_id,
                    member_id=step["member_id"],
                    member_name=mem.get("name") or step["member_id"],
                    client=mem.get("client", "codex"),
                    model=mem.get("model", ""),
                    sandbox=sandbox_arg,
                    final_status=res.final_status,
                    readonly_snapshots=before_inputs,
                    command_logs=cmd_logs,
                    changed_files=chg_files,
                    verif_report=verif_report,
                    response_text=res.response_text or "",
                    work_dir=work_dir,
                    sha_fn=sha256_file,
                )
            except Exception:
                pass

            # Pillar 4: Lock-free Git Worktree diff inspection & patch offload
            if wt_info and wt_info.get("worktree_path"):
                try:
                    wdiff = inspect_worktree_diff(wt_info["worktree_path"], wt_info.get("base_commit", ""))
                    diff_blob_path = ""
                    if wdiff.get("diff_text"):
                        diff_blob_path = blob_store.put_text(wdiff["diff_text"], subdir="worktree_diffs", ext=".diff")["path"]
                    wt_info = {
                        **wt_info,
                        "has_changes": bool(wdiff.get("has_changes")),
                        "changed_files": list(wdiff.get("changed_files") or [])[:40],
                        "diff_stat": str(wdiff.get("diff_stat") or "")[:1200],
                        "commits_ahead": int(wdiff.get("commits_ahead") or 0),
                        "diff_blob_path": diff_blob_path,
                        "inspected_at": time.time(),
                    }
                except Exception:
                    pass

            self._finalize_attempt(
                task_id,
                step_index,
                attempt_id,
                res,
                used_fallback,
                fallback_of,
                ledger_summary=ledger_summary,
                verif_report=verif_report,
                turn_plan=turn_plan,
                rollover_events=rollover_events,
                prompt_ledger_path=prompt_ledger_path,
                archived_session_ids=archived_session_ids,
                worktree_info=wt_info,
            )
            if res.final_status == "succeeded":
                try:
                    self._maybe_seal_completed_stage_snapshot(task_id, step_index)
                except Exception:
                    pass
                # Pillar 2: Lock-free SQLite FTS5 indexing of completed deliverable/audit/visual_qa into project memory!
                try:
                    stype_ix = str(step.get("step_type") or "execution")
                    if stype_ix in ("execution", "synthesis", "fact_check", "visual_qa") and (res.response_text or "").strip():
                        mem_kind = (
                            "fact_check" if stype_ix == "fact_check"
                            else ("visual_qa" if stype_ix == "visual_qa" else "deliverable")
                        )
                        ProjectMemoryIndex(proj_root).upsert_doc(
                            doc_id=f"{task_id}:step:{step_index}:{attempt_id}",
                            task_id=task_id,
                            kind=mem_kind,
                            title=f"{task.get('title', task_id)} · {step.get('title', f'Step {step_index}')}",
                            content=res.response_text or "",
                            blob_path=room_md_path,
                            anchors=extract_deterministic_anchors(res.response_text or ""),
                            stage_index=int(step.get("stage_index") or 0),
                        )
                except Exception:
                    pass
        except Exception as exc:
            # Never leave a step permanently stuck in "running" or a member stuck in "working" on OS/adapter errors!
            try:
                err_res = CodexExecutionResult(
                    session_id=None,
                    response_text="",
                    partial_text="",
                    exit_code=1,
                    turn_completed=False,
                    error=f"工作线程执行异常: {type(exc).__name__}: {exc}",
                    requested_config={},
                    accepted_config={},
                )
                self._finalize_attempt(task_id, step_index, attempt_id, err_res, False, None)
            except Exception:
                pass
        finally:
            with self._sched_lock:
                self._running_attempts.pop(attempt_id, None)
            self._wakeup.set()

    def _maybe_seal_completed_stage_snapshot(self, task_id: str, completed_step_index: int) -> Optional[Dict[str, Any]]:
        """
        Lock-free automatic sealing of an IMMUTABLE Round State Snapshot once every step in a stage
        has succeeded and downstream stages remain. Runs strictly outside TeamStateManager._lock!
        """
        snap = self.store.snapshot()
        task = self._find_task(snap, task_id)
        steps = task.get("steps") or []
        if not (0 <= completed_step_index < len(steps)):
            return None
        cur_step = steps[completed_step_index]
        stage_idx = int(cur_step.get("stage_index") or 0)

        stage_steps = [s for s in steps if int(s.get("stage_index") or 0) == stage_idx]
        if not stage_steps or not all(s.get("status") == "succeeded" for s in stage_steps):
            return None
        has_later_stage = any(int(s.get("stage_index") or 0) > stage_idx for s in steps)
        if not has_later_stage:
            return None

        existing_snaps = task.get("round_snapshots") or []
        if any(int(sn.get("stage_index", -1)) == stage_idx for sn in existing_snaps):
            return None

        stage_step_idxs = {int(s["step_index"]) for s in stage_steps}
        chat_room = task.get("chat_room") or []
        stage_events = [
            m for m in chat_room
            if m.get("step_index") in stage_step_idxs or (stage_idx == 0 and m.get("kind") == "user_goal")
        ]
        if not stage_events:
            return None

        r_label = str(cur_step.get("round_label") or f"Stage {stage_idx}")
        sealed = seal_round_snapshot(
            stage_events,
            visibility_scope=f"stage_{stage_idx}_all",
            stage_index=stage_idx,
            cutoff_seq=len(chat_room),
            task_meta=task,
            blob_store=BlobStore(task_id),
            round_label=r_label,
        )

        def _commit_snap(st: Dict[str, Any]) -> None:
            t = self._find_task(st, task_id)
            snaps = t.setdefault("round_snapshots", [])
            if any(int(sn.get("stage_index", -1)) == stage_idx for sn in snaps):
                return
            snaps.append(sealed)
            cs = t.setdefault("context_stats", {})
            cs["snapshot_count"] = len(snaps)
            cs["updated_at"] = time.time()
            t["recent_events"].append(
                {
                    "ts": time.time(),
                    "type": "context.snapshot_sealed",
                    "summary": f"已封存不可变轮次状态卡 Stage {stage_idx} ({r_label}) · 锁定 {sealed.get('anchor_count', 0)} 条证据锚点 / {sealed.get('blocker_count', 0)} 项阻断",
                }
            )
            t["recent_events"] = t["recent_events"][-40:]

        self.store.mutate(_commit_snap)
        # Pillar 2: Lock-free FTS5 indexing of newly sealed immutable snapshot
        try:
            proj_p = task.get("project_path") or snap["binding"]["project_path"]
            ProjectMemoryIndex(proj_p).upsert_doc(
                doc_id=f"{task_id}:snap:stage{stage_idx}:{sealed['snapshot_sha256'][:10]}",
                task_id=task_id,
                kind="snapshot",
                title=f"{task.get('title', task_id)} · Stage {stage_idx} ({r_label})",
                content=sealed["rendered_md"],
                blob_path=sealed["blob_path"],
                anchors=extract_deterministic_anchors(sealed["rendered_md"]),
                stage_index=stage_idx,
            )
        except Exception:
            pass
        return sealed

    def _expand_chief_plan_unlocked(self, st: Dict[str, Any], task: Dict[str, Any], chief_step: Dict[str, Any], raw_text: str) -> None:
        parsed = _extract_json_obj(raw_text) or {}
        raw_steps = parsed.get("steps") if isinstance(parsed.get("steps"), list) else []
        rationale = str(parsed.get("rationale") or "").strip()

        clean_speech = re.sub(r"```(?:chief_plan|json)?\s*\{.*?\}\s*```", "", raw_text, flags=re.S).strip()
        display_plan = clean_speech or rationale or "已完成项目研判并下发点将分工计划。"
        self._append_room_chat(
            st, task, sender_id=chief_step["member_id"], kind="plan", content=display_plan,
            step_index=0, round_label="主帅点将令"
        )

        valid_members = [k for k, v in st["members"].items() if v.get("client") in SUPPORTED_CLIENTS]
        codex_members = [k for k in valid_members if st["members"][k].get("client") == "codex"]
        max_sb = st["binding"].get("max_sandbox", "workspace-write")
        task_sb = _effective_sandbox(max_sb, task.get("sandbox", "read-only"))
        sc = task.get("workspace_scope", ".")

        if not raw_steps:
            # Fallback sensible 2-step dispatch if Chief wrote prose without JSON block
            exec_m = next((k for k in valid_members if k != chief_step["member_id"]), chief_step["member_id"])
            raw_steps = [
                {
                    "title": "专项执行交付",
                    "member_id": exec_m,
                    "step_type": "execution",
                    "depends_on": [],
                    "read_only": (task_sb == "read-only"),
                    "prompt": f"请根据主帅安排完成任务：{task['goal']}\n主帅指示：{display_plan}",
                }
            ]

        recruited: List[str] = []
        offset = len(task["steps"])  # 1 (since chief_plan is step 0)
        chief_cap = int((task.get("mode_meta") or {}).get("chief_max_members") or 6)
        max_expand = max(6, min(14, chief_cap + 2))
        for local_i, rs in enumerate(raw_steps[:max_expand]):
            mid = str(rs.get("member_id") or "").strip()
            if mid not in st["members"]:
                mid = valid_members[local_i % len(valid_members)]
            req_shell = bool(rs.get("requires_shell", False))
            if req_shell and st["members"][mid].get("client") == "antigravity" and codex_members:
                mid = codex_members[local_i % len(codex_members)]
            if mid not in recruited:
                recruited.append(mid)

            global_idx = offset + local_i
            raw_deps = [int(d) + offset for d in (rs.get("depends_on") or []) if isinstance(d, int) and 0 <= int(d) < local_i]
            rev_of_raw = rs.get("review_of")
            rev_of = (int(rev_of_raw) + offset) if (isinstance(rev_of_raw, int) and 0 <= int(rev_of_raw) < local_i) else None
            if rev_of is not None and rev_of not in raw_deps:
                raw_deps.append(rev_of)
            deps = raw_deps if raw_deps else [0]

            stype = str(rs.get("step_type") or ("review" if rev_of is not None else "execution"))
            ro = True if (task_sb == "read-only" or rev_of is not None or stype in ("review", "discussion")) else bool(rs.get("read_only", False))
            raw_sb_val = str(rs.get("sandbox") or "").strip().lower()
            step_sb = _effective_sandbox(task_sb, raw_sb_val if raw_sb_val in _SANDBOX_RANK else ("read-only" if ro else task_sb), read_only=ro)
            ro = step_sb == "read-only"

            task["steps"].append(
                self._make_step(
                    step_index=global_idx,
                    title=str(rs.get("title") or f"点将任务 #{local_i + 1}"),
                    step_type=stype,
                    stage_index=1 if raw_deps == [] else 2,
                    round_label="点将执行" if rev_of is None else "独立审计",
                    member_id=mid,
                    prompt=str(rs.get("prompt") or task["goal"]),
                    depends_on=deps,
                    review_of=rev_of,
                    read_only=ro,
                    write_scope=[] if ro else [sc],
                    workspace_scope=sc,
                    sandbox=step_sb,
                    requires_shell=req_shell,
                )
            )

        # Final Chief Sign-Off Summary Step depending on all worker steps!
        worker_indices = list(range(offset, len(task["steps"])))
        signoff_idx = len(task["steps"])
        task["steps"].append(
            self._make_step(
                step_index=signoff_idx,
                title="主帅终审汇总与签发交付",
                step_type="synthesis",
                stage_index=3,
                round_label="主帅签发",
                member_id=chief_step["member_id"],
                prompt=f"请作为主帅审阅全体被点将成员的执行与复核成果，向项目负责人输出清晰、完整的最终汇总交付：\n原始目标：{task['goal']}",
                depends_on=worker_indices,
                review_of=None,
                read_only=True,
                write_scope=[],
                workspace_scope=sc,
                sandbox="read-only",
                initial_session_id=chief_step.get("worker_session_id"),
            )
        )
        task.setdefault("mode_meta", {})["recruited_members"] = recruited
        task["mode_meta"]["rationale"] = rationale or display_plan[:160]

    def _finalize_attempt(
        self,
        task_id: str,
        step_index: int,
        attempt_id: str,
        res: CodexExecutionResult,
        used_fallback: bool,
        fallback_of: Optional[str],
        ledger_summary: Optional[Dict[str, Any]] = None,
        verif_report: Optional[Dict[str, Any]] = None,
        turn_plan: Optional[Dict[str, Any]] = None,
        rollover_events: Optional[List[Dict[str, Any]]] = None,
        prompt_ledger_path: str = "",
        archived_session_ids: Optional[List[str]] = None,
        worktree_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Normalize token & KV-cache usage outside lock
        is_ctx_overflow = bool(getattr(res, "context_overflow", False)) or is_context_overflow_error(
            res.error, res.stderr_lines, getattr(res, "raw_events", None)
        )
        if is_ctx_overflow:
            res.context_overflow = True
            res.error_kind = "context_length_exceeded"
        norm_usage = normalize_usage(
            getattr(res, "usage", None),
            prompt_chars=int((turn_plan or {}).get("prompt_chars") or 0),
            response_chars=len(res.response_text or res.partial_text or ""),
        )
        usage_src = "cli_reported" if norm_usage.get("has_provider_usage") else "estimated"
        norm_usage["usage_source"] = usage_src

        # Pre-compute resilient auto-failover backup candidate OUTSIDE TeamStateManager._lock!
        # Covers: (1) Quota / 429 rate-limit, (2) watchdog_idle_timeout stall,
        #         (3) Codex Windows read-only shell policy block (prefer cross-engine Antigravity read_file!).
        # Priority rule: NEVER misroute context_length_exceeded or hard security violations (exit_code==2) to failover!
        quota_failover_candidate: Optional[Dict[str, Any]] = None
        failover_trigger_kind: Optional[str] = None
        is_quota_err = (
            res.final_status not in ("succeeded", "aborted", "blocked")
            and not is_ctx_overflow
            and _is_quota_or_rate_limit_error(res.error, res.stderr_lines)
        )
        is_watchdog_stall = (
            res.final_status not in ("succeeded", "aborted", "blocked")
            and not is_ctx_overflow
            and "watchdog_idle_timeout" in str(res.error or "").lower()
        )
        is_codex_ro_shell_block = (
            res.final_status == "blocked"
            and bool(res.policy_blocked)
            and res.exit_code != 2
            and ("策略拒绝" in str(res.blocking_reason or "") or "blocked by policy" in str(res.blocking_reason or "").lower())
        )
        if is_quota_err or is_watchdog_stall or is_codex_ro_shell_block:
            try:
                pre_snap = self.store.snapshot()
                if pre_snap.get("settings", {}).get("auto_spawn_quota_backup", True):
                    pre_task = self._find_task(pre_snap, task_id)
                    pre_step = pre_task["steps"][step_index]
                    pre_src = pre_snap.get("members", {}).get(pre_step["member_id"], {})
                    # For Codex read-only policy block, only auto-failover if step is truly read-only on codex
                    allow_this_failover = (
                        is_quota_err
                        or is_watchdog_stall
                        or (is_codex_ro_shell_block and bool(pre_step.get("read_only", True)) and pre_src.get("client") == "codex")
                    )
                    if allow_this_failover and int(pre_step.get("quota_failover_count", 0)) < 2:
                        failover_trigger_kind = (
                            "quota" if is_quota_err else ("codex_ro_policy" if is_codex_ro_shell_block else "watchdog_stall")
                        )
                        req_sh = bool(pre_step.get("requires_shell", False))
                        prefer_cross = bool(is_codex_ro_shell_block and not req_sh)
                        exhausted = set(pre_step.get("exhausted_quota_models") or [])
                        if pre_src.get("model"):
                            exhausted.add(f"{pre_src.get('client')}:{pre_src.get('model')}".lower())
                        qb = pre_src.get("quota_backup") if isinstance(pre_src.get("quota_backup"), dict) else None
                        if qb and not prefer_cross:
                            b_mid = qb.get("member_id")
                            if qb.get("mode") == "member" and b_mid and b_mid in pre_snap.get("members", {}):
                                bm = pre_snap["members"][b_mid]
                                bkey = f"{bm.get('client')}:{bm.get('model')}".lower()
                                if bkey not in exhausted and (not req_sh or bm.get("client") == "codex"):
                                    quota_failover_candidate = {
                                        "bound_member_id": b_mid,
                                        "client": bm.get("client", "codex"),
                                        "model": bm.get("model", ""),
                                        "reasoning_effort": bm.get("reasoning_effort", "high"),
                                        "display_name": bm.get("name") or b_mid,
                                    }
                            elif qb.get("client") and qb.get("model"):
                                bkey = f"{qb['client']}:{qb['model']}".lower()
                                if bkey not in exhausted and (not req_sh or qb.get("client") == "codex"):
                                    quota_failover_candidate = {
                                        "client": qb["client"],
                                        "model": qb["model"],
                                        "reasoning_effort": qb.get("reasoning_effort") or "high",
                                        "display_name": qb.get("display_name") or qb["model"],
                                    }
                        if not quota_failover_candidate:
                            cinfo = self.list_backup_candidates(
                                pre_step["member_id"],
                                require_same_client=req_sh,
                                prefer_cross_engine=prefer_cross,
                            )
                            for cand in cinfo.get("candidates") or []:
                                if prefer_cross and cand.get("client") != "antigravity":
                                    continue
                                ckey = f"{cand['client']}:{cand['model']}".lower()
                                if ckey not in exhausted:
                                    quota_failover_candidate = cand
                                    break
            except Exception:
                quota_failover_candidate = None

        def _fin(st: Dict[str, Any]) -> None:
            now = time.time()
            task = self._find_task(st, task_id)
            step = task["steps"][step_index]
            mem = st["members"][step["member_id"]]
            att = next(a for a in step["attempts"] if a["attempt_id"] == attempt_id)

            att["session_id"] = res.session_id
            att["is_fallback_session"] = used_fallback
            att["fallback_of_session_id"] = fallback_of
            att["accepted_config"] = res.accepted_config
            att["partial_text"] = res.partial_text
            att["result_text"] = res.response_text
            att["stderr_excerpt"] = "\n".join(res.stderr_lines[-8:])
            att["status"] = res.final_status
            att["blocking_reason"] = res.blocking_reason or res.error
            att["ended_at"] = now
            att["usage"] = norm_usage
            att["error_kind"] = getattr(res, "error_kind", None)
            att["side_effect_state"] = getattr(res, "side_effect_state", "none")
            att["context_overflow"] = bool(is_ctx_overflow)
            if prompt_ledger_path:
                att["prompt_ledger_path"] = prompt_ledger_path
            if ledger_summary is not None:
                att["ledger_summary"] = ledger_summary
            if worktree_info is not None:
                step["worktree_info"] = dict(worktree_info)

            # v5.3 Context Telemetry & Session Metrics Persistence
            ro_list = list(rollover_events or [])
            tp = turn_plan if isinstance(turn_plan, dict) else {}
            prev_tel = att.get("context_telemetry") if isinstance(att.get("context_telemetry"), dict) else {}
            att["context_telemetry"] = {
                **prev_tel,
                "prefix_sha256": tp.get("prefix_sha256", prev_tel.get("prefix_sha256")),
                "is_delta": bool(tp.get("is_delta", prev_tel.get("is_delta", False))),
                "compression_level": tp.get("compression_level", prev_tel.get("compression_level", "L0")),
                "prompt_chars": int(tp.get("prompt_chars", prev_tel.get("prompt_chars", 0))),
                "est_tokens": int(tp.get("est_tokens", prev_tel.get("est_tokens", 0))),
                "budget_chars": int(tp.get("budget_chars", prev_tel.get("budget_chars", 0))),
                "masked_blocks": int(tp.get("masked_blocks", prev_tel.get("masked_blocks", 0))),
                "used_snapshots": int(tp.get("used_snapshots", prev_tel.get("used_snapshots", 0))),
                "rollover_count": len(ro_list),
                "rollover_events": ro_list,
                "usage": norm_usage,
            }

            if archived_session_ids:
                hist_m = mem.setdefault("session_history", [])
                for asid in archived_session_ids:
                    if asid and asid not in hist_m:
                        hist_m.append(asid)

            if res.session_id:
                old_mem_sess = mem.get("session_id")
                if old_mem_sess and old_mem_sess != res.session_id:
                    mem.setdefault("session_history", []).append(old_mem_sess)
                mem["session_id"] = res.session_id
                step["worker_session_id"] = res.session_id

                # Update per-session rolling telemetry for Proactive Watermark & Delta Frame tracking
                smap = task.setdefault("session_metrics", {})
                sm = smap.get(res.session_id) if isinstance(smap.get(res.session_id), dict) else {}
                prev_est_cum = int(sm.get("estimated_cumulative_tokens") or 0)
                in_tok = int(norm_usage["input_tokens"] if norm_usage.get("input_tokens") is not None else (norm_usage.get("estimated_input_tokens") or 0))
                peak_tok = int(norm_usage["peak_context_tokens"] if norm_usage.get("peak_context_tokens") is not None else in_tok)
                cached_tok = int(norm_usage.get("cached_input_tokens") or 0)
                out_tok = int(norm_usage["output_tokens"] if norm_usage.get("output_tokens") is not None else (norm_usage.get("estimated_output_tokens") or 0))
                if norm_usage.get("usage_source") == "cli_reported" and peak_tok > 0:
                    est_cum = max(peak_tok + out_tok, prev_est_cum + out_tok)
                else:
                    est_cum = prev_est_cum + in_tok + out_tok
                sm.update(
                    {
                        "session_id": res.session_id,
                        "member_id": step["member_id"],
                        "model": mem.get("model"),
                        "turns": int(sm.get("turns") or 0) + 1,
                        "last_input_tokens": in_tok,
                        "last_peak_context_tokens": peak_tok,
                        "last_internal_api_calls": int(norm_usage.get("internal_api_calls") or 1),
                        "last_cached_input_tokens": cached_tok,
                        "last_output_tokens": out_tok,
                        "cache_hit_ratio": float(norm_usage.get("cache_hit_ratio") or 0.0),
                        "total_input_tokens": int(sm.get("total_input_tokens") or 0) + in_tok,
                        "total_cached_input_tokens": int(sm.get("total_cached_input_tokens") or 0) + cached_tok,
                        "total_output_tokens": int(sm.get("total_output_tokens") or 0) + out_tok,
                        "estimated_cumulative_tokens": est_cum,
                        "usage_source": norm_usage.get("usage_source", "estimated"),
                        "prefix_sha256": tp.get("prefix_sha256") or sm.get("prefix_sha256"),
                        "session_epoch": int(step.get("session_epoch") or 0),
                        "rollover_count": int(sm.get("rollover_count") or 0) + len(ro_list),
                        "updated_at": now,
                    }
                )
                smap[res.session_id] = sm

            # Update task-level aggregated Context Engine telemetry
            cs = task.setdefault("context_stats", {})
            eff_in_tok = int(norm_usage["input_tokens"] if norm_usage.get("input_tokens") is not None else (norm_usage.get("estimated_input_tokens") or 0))
            eff_peak_tok = int(norm_usage["peak_context_tokens"] if norm_usage.get("peak_context_tokens") is not None else eff_in_tok)
            eff_cached_tok = int(norm_usage.get("cached_input_tokens") or 0)
            eff_out_tok = int(norm_usage["output_tokens"] if norm_usage.get("output_tokens") is not None else (norm_usage.get("estimated_output_tokens") or 0))
            tot_in = int(cs.get("total_input_tokens") or 0) + eff_in_tok
            tot_cached = int(cs.get("total_cached_input_tokens") or 0) + eff_cached_tok
            tot_out = int(cs.get("total_output_tokens") or 0) + eff_out_tok
            cs["max_peak_context_tokens"] = max(int(cs.get("max_peak_context_tokens") or 0), eff_peak_tok)
            cs.update(
                {
                    "total_input_tokens": tot_in,
                    "total_cached_input_tokens": tot_cached,
                    "total_output_tokens": tot_out,
                    "cache_hit_ratio": round(tot_cached / tot_in, 4) if tot_in > 0 else 0.0,
                    "usage_source": (
                        "cli_reported"
                        if (norm_usage.get("usage_source") == "cli_reported" or cs.get("usage_source") == "cli_reported")
                        else "estimated"
                    ),
                    "rollover_count": int(cs.get("rollover_count") or 0) + len(ro_list),
                    "delta_turns": int(cs.get("delta_turns") or 0) + (1 if tp.get("is_delta") else 0),
                    "masked_blocks_total": int(cs.get("masked_blocks_total") or 0) + int(tp.get("masked_blocks") or 0),
                    "snapshot_count": len(task.get("round_snapshots") or []),
                    "last_prompt_chars": int(tp.get("prompt_chars") or cs.get("last_prompt_chars") or 0),
                    "last_budget_chars": int(tp.get("budget_chars") or cs.get("last_budget_chars") or 0),
                    "last_compression_level": tp.get("compression_level") or cs.get("last_compression_level") or "L0",
                    "session_mode": st.get("settings", {}).get("context_session_mode", "bounded_native"),
                    "updated_at": now,
                }
            )

            def _sync_session_cursor() -> None:
                if res.session_id and isinstance(task.get("session_metrics", {}).get(res.session_id), dict):
                    last_cid = str((task.get("chat_room") or [{}])[-1].get("id") or tp.get("chat_cursor_id") or "")
                    if last_cid:
                        task["session_metrics"][res.session_id]["last_synced_msg_id"] = last_cid

            mem["status"] = "idle"
            mem["live_activity"] = ""
            mem["current_task_id"] = None
            mem["current_attempt_id"] = None
            if mem.get("pending_config"):
                pc = mem["pending_config"]
                if pc.get("name"):
                    mem["name"] = pc["name"]
                if pc.get("client") and pc["client"] != mem.get("client"):
                    if mem.get("session_id"):
                        mem.setdefault("session_history", []).append(mem["session_id"])
                    mem["session_id"] = None
                    mem["client"] = pc["client"]
                mem["role"] = pc["role"]
                mem["model"] = pc["model"]
                mem["reasoning_effort"] = pc["reasoning_effort"]
                mem["workspace_scope"] = pc["workspace_scope"]
                mem["sandbox"] = pc["sandbox"]
                if "specialty" in pc:
                    mem["specialty"] = pc["specialty"]
                if "persona_prompt" in pc:
                    mem["persona_prompt"] = pc["persona_prompt"]
                if "grounding_mode" in pc:
                    mem["grounding_mode"] = pc["grounding_mode"]
                if "tags" in pc:
                    mem["tags"] = pc["tags"]
                mem["pending_config"] = None

            step["pending_repair_brief"] = None
            step["pending_user_answer"] = None
            step["handoff_context"] = None

            if res.final_status == "aborted":
                step["status"] = "aborted"
                step["blocking_reason"] = "尝试已确认中止，保留已有输出"
                task["status"] = "aborted"
                task["blocking_reason"] = f"步骤 {step_index} 已中止"
                task["updated_at"] = now
                return

            if res.final_status == "blocked" and not (
                failover_trigger_kind == "codex_ro_policy"
                and quota_failover_candidate
                and int(step.get("quota_failover_count", 0)) < 2
            ):
                step["status"] = "blocked"
                step["blocking_reason"] = res.blocking_reason or "策略拦截 (blocked by policy)"
                task["status"] = "blocked"
                task["blocking_reason"] = f"步骤 {step_index} 受阻: {step['blocking_reason']}"
                task["updated_at"] = now
                return

            if res.final_status != "succeeded":
                if is_ctx_overflow:
                    # Priority 1: Context overflow NEVER triggers quota 429 model swap!
                    se_s = str(getattr(res, "side_effect_state", "none") or "none")
                    if se_s == "unknown_mutation":
                        step["status"] = "needs_verification"
                        step["blocking_reason"] = res.error or "上下文超限且工作区可能存在未确认写入，已暂停自动重放，请核查后继续"
                        task["status"] = "needs_verification"
                        task["blocking_reason"] = f"步骤 {step_index} 需人工核查变更：{step['blocking_reason']}"
                    else:
                        step["status"] = "needs_ruling"
                        step["blocking_reason"] = res.error or "上下文超限（已达最大压缩换乘重试上限或触发硬预算阻断），请精简目标或点击封存换新会话"
                        task["status"] = "needs_ruling"
                        task["blocking_reason"] = f"步骤 {step_index} 上下文预算阻断：{step['blocking_reason']}"
                    task["recent_events"].append(
                        {
                            "ts": now,
                            "type": "context.overflow_stop",
                            "summary": f"步骤 {step_index} 上下文超限停机 ({step['status']})：{step['blocking_reason'][:140]}",
                        }
                    )
                    task["recent_events"] = task["recent_events"][-40:]
                    task["updated_at"] = now
                    return

                if quota_failover_candidate and int(step.get("quota_failover_count", 0)) < 2:
                    # Auto-spawn (or reuse idle / bound) same-role different-model backup specialist & hand off seamlessly!
                    src_id = step["member_id"]
                    src_m = st["members"][src_id]
                    root_orig_id = src_m.get("backup_of") or src_id
                    ex_list = list(step.get("exhausted_quota_models") or [])
                    cur_key = f"{src_m.get('client')}:{src_m.get('model')}".lower()
                    if cur_key not in ex_list:
                        ex_list.append(cur_key)
                    step["exhausted_quota_models"] = ex_list
                    ch_cl = quota_failover_candidate["client"]
                    ch_md = quota_failover_candidate["model"]
                    ch_ef = quota_failover_candidate["reasoning_effort"]
                    ch_disp = quota_failover_candidate.get("display_name") or ch_md
                    bk_id = None
                    bound_mid = quota_failover_candidate.get("bound_member_id")
                    if bound_mid and bound_mid in st["members"]:
                        bk_id = bound_mid
                    if not bk_id:
                        for ex_id, ex_m in st["members"].items():
                            if (
                                ex_m.get("backup_of") == src_id
                                and ex_m.get("client") == ch_cl
                                and ex_m.get("model") == ch_md
                                and ex_m.get("status") != "working"
                            ):
                                bk_id = ex_id
                                break
                    if not bk_id:
                        tok = _short_model_id_token(ch_md)
                        root_id = src_m.get("backup_of") or src_id
                        cand_id = f"{root_id}_bk_{tok}"
                        seq = 2
                        while cand_id in st["members"]:
                            cand_id = f"{root_id}_bk_{tok}_{seq}"
                            seq += 1
                        bk_id = cand_id
                        base_name = _strip_model_parens(src_m.get("name") or src_id)
                        eff_sb = _effective_sandbox(st["binding"].get("max_sandbox", "workspace-write"), src_m.get("sandbox", "read-only"))
                        bk_mem = {
                            "id": bk_id,
                            "name": f"{base_name} [替补 · {ch_disp}]",
                            "role": src_m.get("role", "implementation"),
                            "client": ch_cl,
                            "model": ch_md,
                            "reasoning_effort": ch_ef,
                            "specialty": src_m.get("specialty", ""),
                            "persona_prompt": src_m.get("persona_prompt", ""),
                            "grounding_mode": src_m.get("grounding_mode", "strict"),
                            "tags": list(src_m.get("tags") or []),
                            "workspace_scope": src_m.get("workspace_scope", "."),
                            "sandbox": eff_sb,
                            "backup_of": root_orig_id,
                            "backup_reason": f"原模型 {src_m.get('model')} 触发额度/限流，自动补充同角色替补",
                            "session_id": None,
                            "session_history": [],
                            "config_history": [],
                            "pending_config": None,
                            "status": "idle",
                            "live_activity": "",
                            "current_task_id": None,
                            "current_attempt_id": None,
                        }
                        enrich_member_metadata(bk_mem)
                        st["members"][bk_id] = bk_mem

                    bk_name = st["members"][bk_id]["name"]
                    step["member_id"] = bk_id
                    step["worker_session_id"] = None
                    step["quota_failover_count"] = int(step.get("quota_failover_count", 0)) + 1
                    # Also carry remaining pending steps & seat roster of this exhausted member over to backup!
                    for fut_s in task.get("steps", []):
                        if fut_s["step_index"] != step_index and fut_s["member_id"] == src_id and fut_s["status"] == "pending":
                            if not (fut_s.get("requires_shell") and ch_cl == "antigravity"):
                                fut_s["member_id"] = bk_id
                                fut_s["worker_session_id"] = None
                    meta_f = task.get("mode_meta") or {}
                    if isinstance(meta_f.get("participants"), list):
                        meta_f["participants"] = [bk_id if p == src_id else p for p in meta_f["participants"]]
                    if meta_f.get("host_member_id") == src_id:
                        meta_f["host_member_id"] = bk_id
                    if failover_trigger_kind == "codex_ro_policy":
                        fo_reason_short = "Codex 只读沙箱策略受阻（自动跨引擎切换原生文件读取替补）"
                        fo_event_type = "failover.codex_ro_policy"
                        fo_round_lbl = "只读沙箱自动切替补"
                    elif failover_trigger_kind == "watchdog_stall":
                        fo_reason_short = "长时静默触发看门狗超时保护（自动切换替补接力）"
                        fo_event_type = "failover.watchdog_stall"
                        fo_round_lbl = "超时自动切替补"
                    else:
                        fo_reason_short = "遇到额度/限流受阻，自动由同角色跨模型替补接力继续"
                        fo_event_type = "quota.auto_backup"
                        fo_round_lbl = "额度自动替补"
                    step["handoff_context"] = {
                        "reassigned_from": f"{src_m.get('name')} ({src_m.get('model')})",
                        "reassigned_to": f"{bk_name} ({ch_cl}/{ch_md})",
                        "reason": f"原模型 {src_m.get('model')} {fo_reason_short}",
                        "completed_steps": [s["step_index"] for s in task["steps"] if s["status"] == "succeeded"],
                        "prior_outputs": [(res.partial_text or "")[:1000]] if res.partial_text else [],
                        "open_issues_and_evidence": [str(res.blocking_reason or res.error or failover_trigger_kind or "quota/rate_limit")[:200]],
                    }
                    step["status"] = "pending"
                    step["blocking_reason"] = None
                    task["status"] = "queued" if st.get("paused") else "running"
                    task["blocking_reason"] = None
                    task["recent_events"].append(
                        {
                            "ts": now,
                            "type": fo_event_type,
                            "summary": f"检测到 {src_m.get('model')} {fo_reason_short}，已自动补充替补 {bk_name} ({ch_cl}/{ch_md}) 接力续跑步骤 {step_index}",
                        }
                    )
                    task["recent_events"] = task["recent_events"][-40:]
                    self._append_room_chat(
                        st,
                        task,
                        sender_id="user",
                        kind="handoff",
                        content=f"检测到 {src_m.get('name')} ({src_m.get('model')}) {fo_reason_short}，系统已自动切换同角色替补 **{bk_name}** ({ch_cl}/{ch_md}) 接力继续当前节点。",
                        to_member_id=bk_id,
                        step_index=step_index,
                        round_label=fo_round_lbl,
                    )
                    task["updated_at"] = now
                    return

                step["status"] = "failed"
                step["blocking_reason"] = res.error or f"进程非正常退出 (code={res.exit_code})"
                task["status"] = "failed"
                task["blocking_reason"] = f"步骤 {step_index} 执行失败: {step['blocking_reason']}"
                task["updated_at"] = now
                return

            step["latest_result_text"] = res.response_text
            step["accepted_attempt_id"] = attempt_id

            # P0-3 + BLK-5 Merge decoupled lock-free mechanical acceptance & figure QA signoff (never erase prior figure_reports!)
            if verif_report is not None:
                vcopy = dict(verif_report)
                vcopy["attempt_id"] = attempt_id
                vcopy["step_index"] = step_index
                prev_so = task.get("research_signoff") if isinstance(task.get("research_signoff"), dict) else None
                if prev_so:
                    merged_so = dict(prev_so)
                    merged_so["attempt_id"] = attempt_id
                    merged_so["step_index"] = step_index
                    merged_so["verified_at"] = vcopy.get("verified_at", now)
                    if vcopy.get("mode"):
                        merged_so["mode"] = vcopy["mode"]
                    merged_so["block_on_failed_signoff"] = bool(
                        prev_so.get("block_on_failed_signoff")
                        or vcopy.get("block_on_failed_signoff")
                        or (task.get("research_spec") or {}).get("block_on_failed_signoff", False)
                    )
                    # Merge figure_reports keyed by normalized manifest_path
                    fig_by_key: Dict[str, Dict[str, Any]] = {}
                    fig_order: List[str] = []
                    for src_list in (prev_so.get("figure_reports") or [], vcopy.get("figure_reports") or []):
                        for fr in src_list:
                            if not isinstance(fr, dict):
                                continue
                            fk = os.path.normcase(str(fr.get("manifest_path") or fr.get("figure_id") or "").replace("\\", "/"))
                            if not fk:
                                fk = f"anon_{len(fig_order)}"
                            if fk not in fig_by_key:
                                fig_order.append(fk)
                            fig_by_key[fk] = fr
                    merged_so["figure_reports"] = [fig_by_key[k] for k in fig_order]

                    # Merge checks keyed by check_id
                    chk_by_id: Dict[str, Dict[str, Any]] = {}
                    chk_order: List[str] = []
                    new_passed_cids: set = set()
                    for idx_c, chk in enumerate(prev_so.get("checks") or []):
                        if not isinstance(chk, dict):
                            continue
                        ck = str(chk.get("check_id") or chk.get("id") or f"prev_{idx_c}")
                        chk_order.append(ck)
                        chk_by_id[ck] = chk
                    for idx_c, chk in enumerate(vcopy.get("checks") or []):
                        if not isinstance(chk, dict):
                            continue
                        ck = str(chk.get("check_id") or chk.get("id") or f"cur_{idx_c}")
                        if ck not in chk_by_id:
                            chk_order.append(ck)
                        chk_by_id[ck] = chk
                        if chk.get("ok"):
                            new_passed_cids.add(ck)
                    merged_so["checks"] = [chk_by_id[k] for k in chk_order]

                    # Merge non-figure blocking items (retaining prior unresolved non-fig blockers unless newly passed)
                    merged_bi: List[Dict[str, Any]] = []
                    seen_bi: set = set()
                    for it in prev_so.get("blocking_items") or []:
                        if not isinstance(it, dict) or it.get("source") == "figure_qa":
                            continue
                        if str(it.get("object_id") or "") in new_passed_cids:
                            continue
                        sig = (it.get("source"), it.get("object_id"), it.get("message"))
                        if sig not in seen_bi:
                            seen_bi.add(sig)
                            merged_bi.append(it)
                    for it in vcopy.get("blocking_items") or []:
                        if not isinstance(it, dict) or it.get("source") == "figure_qa":
                            continue
                        sig = (it.get("source"), it.get("object_id"), it.get("message"))
                        if sig not in seen_bi:
                            seen_bi.add(sig)
                            merged_bi.append(it)
                    merged_so["blocking_items"] = merged_bi

                    # Union discovered_figure_manifests & base_observations
                    disc_union: List[str] = []
                    for dm in list(prev_so.get("discovered_figure_manifests") or []) + list(vcopy.get("discovered_figure_manifests") or []):
                        if dm and dm not in disc_union:
                            disc_union.append(dm)
                    merged_so["discovered_figure_manifests"] = disc_union
                    merged_so["base_observations"] = list(
                        dict.fromkeys(
                            [o for o in (prev_so.get("base_observations") or []) if o]
                            + [o for o in (vcopy.get("base_observations") or []) if o]
                        )
                    )
                    if vcopy.get("status") == "error":
                        recompute_signoff_summary(merged_so)
                        merged_so["status"] = "error"
                        merged_so["ok"] = False
                        merged_so["can_release"] = False
                    else:
                        recompute_signoff_summary(merged_so)
                    vcopy = merged_so

                task["research_signoff"] = vcopy
                task["recent_events"].append(
                    {
                        "ts": now,
                        "type": "research.signoff",
                        "summary": (
                            f"科研机械验收通过 ({len(vcopy.get('checks', []))} 项检查 / {len(vcopy.get('figure_reports', []))} 份图件契约)"
                            if vcopy.get("ok")
                            else f"科研机械验收拦截 ({vcopy.get('status')}): {'; '.join((vcopy.get('blocking') or [])[:2])[:160]}"
                        ),
                    }
                )
                task["recent_events"] = task["recent_events"][-40:]
                if not vcopy.get("ok") and (task.get("research_spec") or {}).get("block_on_failed_signoff", False):
                    b_msg = "; ".join((vcopy.get("blocking") or [])[:3]) or f"status={vcopy.get('status')}"
                    step["status"] = "needs_ruling"
                    step["blocking_reason"] = f"机械验收未通过（已拦截签发）：{b_msg}"
                    task["status"] = "needs_ruling"
                    task["blocking_reason"] = f"步骤 {step_index} 科研机械验收未通过：{b_msg}"
                    _sync_session_cursor()
                    task["updated_at"] = now
                    return

            # Pillar 3: Automated Dual-Gate VLM Visual Critique & Bounded Self-Repair Loop!
            if step.get("step_type") == "visual_qa":
                vqa = _extract_visual_qa_verdict(res.response_text)
                v_round = int(step.get("visual_qa_round") or 1)
                fc_cur = (task.get("research_spec") or {}).get("figure_contract") if isinstance(task.get("research_spec"), dict) else {}
                max_v_rep = max(1, min(3, int((fc_cur or {}).get("max_visual_repairs") or 2)))
                v_rec = {
                    "id": _new_id("VQA"),
                    "ts": now,
                    "step_index": step_index,
                    "target_step_index": step.get("visual_target_step_index"),
                    "critic_id": step["member_id"],
                    "critic_name": mem.get("name") or step["member_id"],
                    "critic_model": mem.get("model") or "",
                    "round": v_round,
                    "figure_paths": list(step.get("visual_figure_paths") or []),
                    "manifest_path": step.get("visual_manifest_path") or "",
                    **vqa,
                }
                task.setdefault("visual_qa_reports", []).append(v_rec)
                def_lines = [
                    f"- [{d['severity']}] ({d['region']}) {d['issue']} → 修复建议：{d['fix']}"
                    for d in vqa.get("defects") or []
                ]
                v_chat = (
                    f"✅ VLM 视觉质检通过 (评分 {vqa['visual_score']}/10)：{vqa['summary']}"
                    if vqa["verdict"] == "PASS"
                    else f"🎨 VLM 视觉质检打回 [{vqa['verdict']} · 评分 {vqa['visual_score']}/10]：{vqa['summary']}"
                    + ("\n" + "\n".join(def_lines) if def_lines else "")
                )
                self._append_room_chat(
                    st,
                    task,
                    sender_id=step["member_id"],
                    kind="review",
                    content=v_chat,
                    to_member_id=step.get("visual_target_member_id"),
                    step_index=step_index,
                    round_label=f"第{v_round}轮 VLM 视觉质检",
                )
                task["recent_events"].append(
                    {
                        "ts": now,
                        "type": "figure.visual_qa",
                        "summary": f"第 {v_round} 轮 VLM 视觉质检 {vqa['verdict']} ({vqa['visual_score']}/10)",
                    }
                )
                task["recent_events"] = task["recent_events"][-40:]

                if vqa["verdict"] == "PASS":
                    step["status"] = "succeeded"
                    step["blocking_reason"] = None
                    _sync_session_cursor()
                    self._refresh_task_overall_status_unlocked(st, task)
                    task["updated_at"] = now
                    return
                if vqa["verdict"] == "REVISE" and v_round < max_v_rep and step.get("visual_target_member_id") in st["members"]:
                    step["status"] = "succeeded"
                    step["blocking_reason"] = None
                    tgt_mid = step["visual_target_member_id"]
                    new_fix_idx = len(task["steps"])
                    sc_fix = step.get("workspace_scope") or task.get("workspace_scope") or "."
                    fix_step = self._make_step(
                        step_index=new_fix_idx,
                        title=f"VLM 视觉反馈自动修图 (#{v_round})",
                        step_type="execution",
                        stage_index=new_fix_idx,
                        round_label=f"视觉自动返修 #{v_round}",
                        member_id=tgt_mid,
                        prompt=(
                            f"【第 {v_round}/{max_v_rep} 轮 VLM 自动视觉挑错返修指令】\n"
                            f"质检官 {mem.get('name')} 评分 {vqa['visual_score']}/10，判定 REVISE。\n"
                            f"涉及图件：{', '.join(v_rec['figure_paths'])}\n"
                            + (f"Manifest：{v_rec['manifest_path']}\n" if v_rec["manifest_path"] else "")
                            + "必须解决的视觉缺陷：\n"
                            + ("\n".join(def_lines) if def_lines else f"- {vqa['summary']}")
                            + "\n\n请直接修改绘图脚本、重新运行导出全部格式并更新 `figure_manifest.json`，确保机械 QA 与视觉质量同时达标。"
                        ),
                        depends_on=[step_index],
                        review_of=None,
                        read_only=False,
                        write_scope=[sc_fix],
                        workspace_scope=sc_fix,
                        sandbox="workspace-write",
                        requires_shell=True,
                    )
                    fix_step["visual_qa_round"] = v_round
                    task["steps"].append(fix_step)
                    task["current_step_index"] = new_fix_idx
                    task["status"] = "queued" if st.get("paused") else "running"
                    task["blocking_reason"] = None
                    _sync_session_cursor()
                    task["updated_at"] = now
                    return

                # Fail-Closed on INVALID_FORMAT or max visual repair rounds exceeded!
                step["status"] = "needs_ruling"
                step["blocking_reason"] = (
                    f"VLM 视觉质检输出格式非法 ({vqa['summary'][:120]})"
                    if vqa["verdict"] == "INVALID_FORMAT"
                    else f"已达最大 VLM 视觉自动返修上限 ({max_v_rep} 轮)，当前评分 {vqa['visual_score']}/10，等待人工拍板"
                )
                task["status"] = "needs_ruling"
                task["blocking_reason"] = step["blocking_reason"]
                _sync_session_cursor()
                task["updated_at"] = now
                return

            # Chief Dynamic Plan Expansion!
            if step.get("step_type") == "chief_plan":
                step["status"] = "succeeded"
                step["blocking_reason"] = None
                self._expand_chief_plan_unlocked(st, task, step, res.response_text)
                _sync_session_cursor()
                self._refresh_task_overall_status_unlocked(st, task)
                task["updated_at"] = now
                return

            if step.get("review_of") is not None:
                target_idx = int(step["review_of"])
                target_step = task["steps"][target_idx]
                parsed_rev = _extract_json_obj(res.response_text)
                verdict = str((parsed_rev or {}).get("verdict", "")).strip().upper()
                if not isinstance(parsed_rev, dict) or verdict not in ("ACCEPT", "REJECT"):
                    # P0-1 Fail-Closed Review Gate: malformed/non-JSON output NEVER falls back to ACCEPT!
                    bad_record = {
                        "id": _new_id("REV"),
                        "ts": now,
                        "review_step_index": step_index,
                        "target_step_index": target_idx,
                        "reviewer_id": step["member_id"],
                        "reviewer_session_id": res.session_id,
                        "round": target_step.get("repair_count", 0) + 1,
                        "verdict": "INVALID_FORMAT",
                        "problem": "审查输出缺少合法结构化 JSON verdict (ACCEPT/REJECT)",
                        "evidence": (res.response_text or "")[:240],
                        "impact": "Fail-Closed 门禁已阻止自动放行",
                        "expected_fix": "请人工裁决或要求审稿人返回合法 JSON verdict",
                        "scope_expanded": False,
                    }
                    task["reviews"].append(bad_record)
                    self._append_room_chat(
                        st,
                        task,
                        sender_id=step["member_id"],
                        kind="review",
                        content=f"审查输出格式非法（未包含合法 JSON verdict: ACCEPT/REJECT），按 Fail-Closed 门禁拦截放行，等待人工裁决。\n原文节选：{(res.response_text or '')[:240]}",
                        to_member_id=target_step["member_id"],
                        step_index=step_index,
                        round_label="审查门禁拦截",
                    )
                    step["status"] = "needs_ruling"
                    step["blocking_reason"] = "审查输出缺少合法结构化 verdict (ACCEPT/REJECT)，按 Fail-Closed 门禁拦截等待人工裁决"
                    task["status"] = "needs_ruling"
                    task["blocking_reason"] = f"步骤 {step_index} 审查输出缺少合法 verdict，已拦截放行等待人工裁决"
                    task["updated_at"] = now
                    return

                rev_record = {
                    "id": _new_id("REV"),
                    "ts": now,
                    "review_step_index": step_index,
                    "target_step_index": target_idx,
                    "reviewer_id": step["member_id"],
                    "reviewer_session_id": res.session_id,
                    "round": target_step.get("repair_count", 0) + 1,
                    "verdict": verdict,
                    "problem": str(parsed_rev.get("problem") or ""),
                    "evidence": str(parsed_rev.get("evidence") or res.response_text[:200]),
                    "impact": str(parsed_rev.get("impact") or ""),
                    "expected_fix": str(parsed_rev.get("expected_fix") or ""),
                    "scope_expanded": bool(parsed_rev.get("scope_expanded", False)),
                }
                task["reviews"].append(rev_record)
                rev_chat_txt = (
                    f"✅ 审查通过：{rev_record['evidence']}"
                    if verdict == "ACCEPT"
                    else f"⚠️ 审查打回：{rev_record['problem']}\n证据：{rev_record['evidence']}\n修复要求：{rev_record['expected_fix']}"
                )
                self._append_room_chat(
                    st, task, sender_id=step["member_id"], kind="review", content=rev_chat_txt,
                    to_member_id=target_step["member_id"], step_index=step_index, round_label=f"第{rev_record['round']}轮独立审查"
                )

                if verdict == "ACCEPT":
                    step["status"] = "succeeded"
                    step["blocking_reason"] = None
                else:
                    prev_problems = [
                        r["problem"].strip()
                        for r in task["reviews"][:-1]
                        if r["target_step_index"] == target_idx and r["verdict"] == "REJECT" and r["problem"].strip()
                    ]
                    cur_prob = rev_record["problem"].strip()
                    is_repeat = bool(cur_prob and cur_prob in prev_problems)
                    cap = int(task.get("max_auto_repairs", 2))
                    cur_repairs = int(target_step.get("repair_count", 0))

                    if task.get("require_manual_ruling"):
                        step["status"] = "needs_ruling"
                        task["status"] = "needs_ruling"
                        task["blocking_reason"] = f"审查驳回步骤 {target_idx}，等待总管裁决：{cur_prob or rev_record['evidence'][:80]}"
                        task["updated_at"] = now
                        return

                    if rev_record["scope_expanded"] or is_repeat or cur_repairs >= cap:
                        why = (
                            "审查意见涉及范围扩大"
                            if rev_record["scope_expanded"]
                            else ("同一审查问题重复出现" if is_repeat else f"已达到最大自动修复轮数上限 ({cap} 轮)")
                        )
                        step["status"] = "needs_ruling"
                        step["blocking_reason"] = f"{why}，停止自动循环，等待总管裁决"
                        task["status"] = "needs_ruling"
                        task["blocking_reason"] = f"需要裁决：{why}（问题：{cur_prob[:80]}）"
                        task["updated_at"] = now
                        return

                    target_step["repair_count"] = cur_repairs + 1
                    task["rulings"].append(
                        {
                            "id": _new_id("RULE"),
                            "ts": now,
                            "step_index": step_index,
                            "decision": "accept",
                            "reason": f"第 {target_step['repair_count']}/{cap} 轮自动退回原作者 ({target_step['member_id']}) 原会话修复",
                            "decided_by": "coordinator_policy",
                        }
                    )
                    target_step["pending_repair_brief"] = (
                        f"【第 {target_step['repair_count']}/{cap} 轮审查退回修复】\n"
                        f"- 具体问题：{rev_record['problem']}\n"
                        f"- 审查证据：{rev_record['evidence']}\n"
                        f"- 影响：{rev_record['impact']}\n"
                        f"- 期望修复：{rev_record['expected_fix']}\n"
                        "请在你原有的上下文基础上直接输出修复后的完整交付物。"
                    )
                    target_step["status"] = "pending"
                    step["status"] = "pending"
                    task["current_step_index"] = target_idx
                    task["status"] = "queued" if st.get("paused") else "running"
                    task["updated_at"] = now
                    return
            else:
                ai_qs, cleaned_resp = _extract_ai_questions(res.response_text)
                if ai_qs:
                    step["latest_result_text"] = cleaned_resp
                    step["pending_questions"] = ai_qs
                    step["status"] = "awaiting_user"
                    step["blocking_reason"] = f"成员提出了 {len(ai_qs)} 个问题等待你选择确认后继续"
                    task["pending_questions"] = {
                        "step_index": step_index,
                        "member_id": step["member_id"],
                        "questions": ai_qs,
                        "ts": now,
                    }
                    self._append_room_chat(
                        st, task, sender_id=step["member_id"], kind="question_to_user",
                        content=cleaned_resp, step_index=step_index, round_label="请示负责人"
                    )
                    task["current_step_index"] = step_index
                    task["status"] = "awaiting_user"
                    task["blocking_reason"] = f"🙋 {mem['name']} 提出了 {len(ai_qs)} 个选项问题，请在下方点选确认后继续执行"
                    task["updated_at"] = now
                    return

                step["status"] = "succeeded"
                step["pending_questions"] = None
                step["blocking_reason"] = None
                task["pending_questions"] = None
                # BLK-6: if this step was a followup repair of a needs_ruling step and signoff is now passed (or absent), unlock target step!
                if step.get("repair_of") is not None:
                    so_cur = task.get("research_signoff") if isinstance(task.get("research_signoff"), dict) else None
                    if (so_cur is None) or (so_cur.get("status") == "passed"):
                        rep_idx = int(step["repair_of"])
                        if 0 <= rep_idx < len(task["steps"]) and task["steps"][rep_idx].get("status") == "needs_ruling":
                            task["steps"][rep_idx]["status"] = "succeeded"
                            task["steps"][rep_idx]["blocking_reason"] = None
                stype_done = step.get("step_type")
                if stype_done == "fact_check":
                    chat_kind = "fact_check"
                    task["latest_fact_check"] = {
                        "step_index": step_index,
                        "checker_id": step["member_id"],
                        "checker_name": mem.get("name") or step["member_id"],
                        "checker_client": mem.get("client"),
                        "checker_model": mem.get("model"),
                        "target_author_id": step.get("audit_target_author_id"),
                        "cross_engine": bool(step.get("audit_cross_engine")),
                        "content": res.response_text,
                        "ts": now,
                    }
                else:
                    chat_kind = "deliverable" if stype_done in ("synthesis", "execution") else "speech"
                self._append_room_chat(
                    st, task, sender_id=step["member_id"], kind=chat_kind,
                    content=res.response_text, step_index=step_index, round_label=step.get("round_label") or step["title"]
                )
                task["deliverables"].append(
                    {
                        "step_index": step_index,
                        "title": step["title"],
                        "member_id": step["member_id"],
                        "attempt_id": attempt_id,
                        "session_id": res.session_id,
                        "preview": res.response_text[:240],
                        "ts": now,
                    }
                )

                # Pillar 5: Parse Borda Peer Ranking ballot in Anonymous Roundtable Round 2+
                if (
                    stype_done == "discussion"
                    and (task.get("mode_meta") or {}).get("anonymous_peer_review")
                    and int(step.get("stage_index") or 0) >= 2
                ):
                    ballot = _extract_peer_ranking(res.response_text)
                    if ballot:
                        pr_list = task.setdefault("peer_rankings", [])
                        pr_list = [b for b in pr_list if not (b.get("step_index") == step_index and b.get("voter_id") == step["member_id"])]
                        pr_list.append(
                            {
                                "step_index": step_index,
                                "stage_index": int(step.get("stage_index") or 0),
                                "voter_id": step["member_id"],
                                "ts": now,
                                **ballot,
                            }
                        )
                        task["peer_rankings"] = pr_list
                        task["borda_scoreboard"] = _compute_borda_scoreboard(task, st["members"])

                # Pillar 3: Auto-dispatch VLM Visual Critique (Gate 2) right after Mechanical Gate 1 on writable plotting steps!
                fc_auto = (task.get("research_spec") or {}).get("figure_contract") if isinstance(task.get("research_spec"), dict) else None
                if (
                    isinstance(fc_auto, dict)
                    and fc_auto.get("auto_visual_qa")
                    and stype_done == "execution"
                    and step.get("sandbox") == "workspace-write"
                    and not step.get("visual_qa_dispatched")
                ):
                    max_v_rep = max(1, min(3, int(fc_auto.get("max_visual_repairs") or 2)))
                    cur_v_round = int(step.get("visual_qa_round") or 0)
                    so_now = task.get("research_signoff") if isinstance(task.get("research_signoff"), dict) else None
                    fig_reps = [fr for fr in ((so_now or {}).get("figure_reports") or []) if isinstance(fr, dict)]
                    fig_paths_v: List[str] = []
                    mf_rel_v = ""
                    for fr in fig_reps:
                        if not mf_rel_v and fr.get("manifest_path"):
                            mf_rel_v = str(fr.get("manifest_path"))
                        for art in fr.get("artifacts") or []:
                            ap = str(art.get("path") or "")
                            if ap and os.path.splitext(ap)[1].lower() in (".png", ".svg", ".jpg", ".jpeg") and ap not in fig_paths_v:
                                fig_paths_v.append(ap)
                    if fig_paths_v and cur_v_round < max_v_rep:
                        # Prefer dedicated multimodal visual figure QA specialist (never the plotter itself!)
                        critic_id = None
                        if "agy_carbon_plotter" in st["members"] and "agy_carbon_plotter" != step["member_id"]:
                            critic_id = "agy_carbon_plotter"
                        else:
                            for cid, cm in st["members"].items():
                                if cid != step["member_id"] and cm.get("client") == "antigravity" and ("plotting" in (cm.get("tags") or []) or "visual" in (cm.get("tags") or [])):
                                    critic_id = cid
                                    break
                            if not critic_id:
                                critic_id = next(
                                    (cid for cid, cm in st["members"].items() if cid != step["member_id"] and cm.get("client") in SUPPORTED_CLIENTS),
                                    None,
                                )
                        if critic_id:
                            step["visual_qa_dispatched"] = True
                            v_idx = len(task["steps"])
                            next_v_round = cur_v_round + 1
                            sc_v = step.get("workspace_scope") or task.get("workspace_scope") or "."
                            claim_v = str(fc_auto.get("core_claim") or task.get("goal") or "")[:300]
                            v_step = self._make_step(
                                step_index=v_idx,
                                title=f"VLM 科研图视觉质检 (#{next_v_round})",
                                step_type="visual_qa",
                                stage_index=v_idx,
                                round_label=f"VLM 视觉闭环 #{next_v_round}",
                                member_id=critic_id,
                                prompt=(
                                    f"请使用 read_file 直接检视以下已通过机械 QA 校验的科研图件，执行顶刊出版级视觉挑刺：\n"
                                    f"- 待检图件路径：\n" + "\n".join(f"  * {fp}" for fp in fig_paths_v[:4]) + "\n"
                                    + (f"- 对应 Manifest：{mf_rel_v}\n" if mf_rel_v else "")
                                    + f"- 核心科学论断 (Hero Claim)：{claim_v}\n\n"
                                    "请逐项核查：文字/图例/散点是否遮挡重叠、刻度与标签字号是否清晰、多面板对齐与留白是否均衡、配色是否色盲友好。"
                                ),
                                depends_on=[step_index],
                                review_of=None,
                                read_only=True,
                                write_scope=[],
                                workspace_scope=sc_v,
                                sandbox="read-only",
                            )
                            v_step["visual_qa_round"] = next_v_round
                            v_step["visual_target_member_id"] = step["member_id"]
                            v_step["visual_target_step_index"] = step_index
                            v_step["visual_figure_paths"] = fig_paths_v[:4]
                            v_step["visual_manifest_path"] = mf_rel_v
                            task["steps"].append(v_step)
                            task["recent_events"].append(
                                {
                                    "ts": now,
                                    "type": "figure.visual_qa.dispatched",
                                    "summary": f"已自动派发第 {next_v_round} 轮 VLM 视觉质检 ({st['members'][critic_id]['name']})",
                                }
                            )
                            task["recent_events"] = task["recent_events"][-40:]

            _sync_session_cursor()
            self._refresh_task_overall_status_unlocked(st, task)
            task["updated_at"] = now

        self.store.mutate(_fin)

    def _refresh_task_overall_status_unlocked(self, st: Dict[str, Any], task: Dict[str, Any]) -> None:
        steps = task.get("steps", [])
        if not steps:
            return
        if all(s["status"] == "succeeded" for s in steps):
            task["status"] = "succeeded"
            task["blocking_reason"] = None
            synth = [s for s in steps if s.get("step_type") == "synthesis" and s.get("latest_result_text")]
            exec_steps = [
                s for s in steps
                if s.get("review_of") is None and s.get("step_type") not in ("fact_check", "visual_qa") and s.get("latest_result_text")
            ]
            chosen = synth[-1] if synth else (exec_steps[-1] if exec_steps else steps[-1])
            task["summary"] = chosen.get("latest_result_text", "")
            task["recent_events"].append(
                {"ts": time.time(), "type": "task.succeeded", "summary": f"协作任务全部 {len(steps)} 个节点已圆满完成"}
            )
        elif any(s["status"] == "awaiting_user" for s in steps):
            task["status"] = "awaiting_user"
        elif any(s["status"] == "needs_verification" for s in steps):
            task["status"] = "needs_verification"
        elif any(s["status"] == "needs_ruling" for s in steps):
            task["status"] = "needs_ruling"
        elif any(s["status"] == "blocked" for s in steps):
            task["status"] = "blocked"
        elif any(s["status"] == "failed" for s in steps):
            task["status"] = "failed"
        elif any(s["status"] == "aborted" for s in steps):
            task["status"] = "aborted"
        elif any(s["status"] in ("running", "pending") for s in steps):
            next_p = next((s["step_index"] for s in steps if s["status"] in ("running", "pending")), 0)
            task["current_step_index"] = next_p
            task["status"] = "queued" if st.get("paused") else "running"
