"""
AGY Roundtable v5.3 Deterministic Context Governance, Compaction & Session Rollover Engine (Pure Stdlib).

Architecture (M1 Primary + M2 Stateless Replay Mode + M3 Optional Non-Authoritative Digest Slot):
1. Three-Zone KV-Cache Prefix-Stable Prompt Layout:
   - Zone A (Byte-Frozen Static Prefix): workspace root, identity/persona, model dialect, grounding rules.
   - Zone B (Append-Only Semi-Static): JIT-selected playbooks, figure contract, immutable sealed Round State Snapshots.
   - Zone C (Dynamic Tail): changed-files delta, protected recent round (or Resumed-Session Delta Frame),
     step prompt / repair brief / handoff checkpoint, and JIT output protocol contracts.
2. Hybrid Observation Masking + Immutable Deterministic Round State Snapshots (zero hallucination):
   - Large code blocks / wide tables in older turns are offloaded lock-free to content-addressed BlobStore
     under D:\\gemini\\tmp\\team_blobs\\<task_id>\\ and replaced by anchor-preserving pointer stubs.
   - Sealed Round Snapshots deterministically preserve file:line refs, exact numeric/statistical literals,
     BLOCKING items, rulings, and consult verdicts (never mutated retroactively -> preserves KV-cache prefix!).
3. Structured Error Classifier + Dual-Track Session Rollover:
   - Strictly classifies `context_length_exceeded` BEFORE `quota_rate_limit` (eliminates 429 misrouting).
   - Proactive Watermark Rollover (token fill ratio OR session turn cap) + Reactive Monotonic Rollover (L0 -> L1 -> L2)
     with write-side-effect safety gating.
4. Four-Tier Hard Budget Governor (P0 mandatory floor -> P1 core evidence -> P2 recent/delta -> P3 older masked/dropped).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _get_blob_base_root() -> str:
    env_tmp = os.environ.get("AGY_RUNTIME_TMP") or os.environ.get("TEAM_RUNTIME_TMP")
    if env_tmp:
        return os.path.join(env_tmp, "team_blobs")
    if os.path.isdir(r"D:\gemini\tmp"):
        return os.path.join(r"D:\gemini\tmp", "team_blobs")
    return os.path.join(tempfile.gettempdir(), "team_blobs")


# ==================== MODEL CONTEXT WINDOWS & TOKEN ESTIMATION ====================

MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "gpt-6.2-sol": 272_000,
    "gpt-6.2-astra": 272_000,
    "gpt-6.1": 200_000,
    "gpt-6": 200_000,
    "gpt-5.6": 200_000,
    "gpt-5.4": 200_000,
    "claude-opus-5-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "gemini-4-neon": 1_000_000,
    "gemini-4-carbon": 1_000_000,
    "gemini-4-hydrogen": 1_000_000,
    "gemini-3": 1_000_000,
    "gemini-2.5": 1_000_000,
}
CONSERVATIVE_FALLBACK_WINDOW = 64_000


def get_model_context_window(model_slug: Optional[str]) -> Tuple[int, str]:
    """Return (context_window_tokens, source_label) for a model slug."""
    low = str(model_slug or "").strip().lower()
    if not low:
        return CONSERVATIVE_FALLBACK_WINDOW, "conservative_fallback"
    if low in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[low], "known_model"
    for prefix, win in MODEL_CONTEXT_WINDOWS.items():
        if low.startswith(prefix):
            return win, "known_model_prefix"
    if "gemini" in low:
        return 1_000_000, "family_default"
    if "claude" in low:
        return 200_000, "family_default"
    if "gpt-" in low or low.startswith("o3") or low.startswith("o4"):
        return 200_000, "family_default"
    return CONSERVATIVE_FALLBACK_WINDOW, "conservative_fallback"


def estimate_tokens(text: Optional[str]) -> int:
    """Conservative CJK-aware pure-stdlib token estimator (no tiktoken dependency)."""
    s = str(text or "")
    if not s:
        return 0
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef")
    other = len(s) - cjk
    return int(math.ceil(cjk * 1.6 + other / 3.2)) + 16


def normalize_usage(
    raw_usage: Optional[Dict[str, Any]],
    prompt_chars: int = 0,
    response_chars: int = 0,
    internal_api_calls: Optional[int] = None,
    model_slug: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Normalize Codex CLI (`turn.completed.usage`) & Antigravity CLI (`result.usage`) usage dicts.
    Separates cumulative multi-tool `billing_input_tokens` from calibrated single-call `peak_context_tokens`
    so multi-tool turns do not prematurely trigger context-window rollover.
    """
    u = raw_usage if isinstance(raw_usage, dict) else {}

    def _pick_int(*keys: str, containers: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
        for src in (containers or [u]):
            if not isinstance(src, dict):
                continue
            for k in keys:
                val = src.get(k)
                if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
                    return int(val)
        return None

    detail_containers: List[Dict[str, Any]] = [u]
    for sub_k in ("prompt_tokens_details", "input_tokens_details", "usage_metadata", "cache", "details"):
        sub = u.get(sub_k)
        if isinstance(sub, dict):
            detail_containers.append(sub)

    inp = _pick_int("input_tokens", "prompt_tokens", "promptTokenCount", "inputTokens", containers=detail_containers)
    cached = _pick_int(
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cachedContentTokenCount",
        "cached_content_token_count",
        "cached_tokens",
        containers=detail_containers,
    )
    outp = _pick_int("output_tokens", "completion_tokens", "candidatesTokenCount", "outputTokens", containers=detail_containers)
    tot = _pick_int("total_tokens", "totalTokenCount", "totalTokens", containers=detail_containers)
    calls = (
        internal_api_calls
        if (isinstance(internal_api_calls, int) and internal_api_calls >= 1)
        else _pick_int("internal_api_calls", "api_calls", "num_turns", "tool_batches")
    )
    calls = max(1, int(calls or 1))
    if tot is None and inp is not None and outp is not None:
        tot = inp + outp

    est_in = int(math.ceil(prompt_chars / 2.2)) if prompt_chars > 0 else 0
    est_out = int(math.ceil(response_chars / 2.2)) if response_chars > 0 else 0
    cache_ratio = round((cached / inp), 3) if (cached is not None and inp and inp > 0) else None

    # Calibrate single-call peak context occupancy when CLI sums multiple internal tool-loop API calls
    explicit_peak = _pick_int("peak_context_tokens")
    peak_ctx: Optional[int] = explicit_peak if (explicit_peak is not None and explicit_peak > 0) else inp
    if explicit_peak is None and inp is not None and calls > 1:
        avg_call = int(math.ceil(inp / calls))
        cached_bound = int(math.ceil(cached / max(1, calls - 1))) if cached else 0
        # Slight 1.15x headroom over average to stay safely conservative without 5x over-counting
        peak_ctx = min(inp, max(int(math.ceil(avg_call * 1.15)), cached_bound))

    peak_clamped = False
    if model_slug and peak_ctx is not None:
        win_tok, _ = get_model_context_window(model_slug)
        if peak_ctx > win_tok:
            # A completed non-overflow turn cannot physically exceed the model's context window in a single call;
            # clamp miscounted cumulative billing sums while preserving full `billing_input_tokens`.
            peak_ctx = int(win_tok * 0.90)
            peak_clamped = True

    return {
        "input_tokens": inp,
        "billing_input_tokens": inp,
        "peak_context_tokens": peak_ctx,
        "peak_clamped": peak_clamped,
        "internal_api_calls": calls,
        "cached_input_tokens": cached,
        "output_tokens": outp,
        "total_tokens": tot,
        "estimated_input_tokens": est_in,
        "estimated_output_tokens": est_out,
        "cache_hit_ratio": cache_ratio,
        "has_provider_usage": inp is not None or outp is not None,
    }


# ==================== STRUCTURED-FIRST ERROR CLASSIFIER ====================

_CONTEXT_OVERFLOW_EXACT = (
    "context_length_exceeded",
    "maximum context length",
    "exceeds the context window",
    "context window exceeded",
    "context_window_exceeded",
    "prompt is too long",
    "input is too long",
    "input token count exceeds",
    "exceeds the maximum number of tokens",
    "token limit exceeded",
    "request payload size exceeds",
    "request too large",
    "content_too_large",
    "max_tokens_exceeded",
    "too many input tokens",
    "prompt_too_long",
)

_CONTEXT_OVERFLOW_COMBO_HINTS = (
    "context length",
    "context window",
    "token limit",
    "max token",
    "prompt length",
    "input tokens",
    "too many tokens",
    "payload size",
)

_QUOTA_PATTERNS = (
    "rate_limit",
    "rate limit",
    "too many requests",
    "http 429",
    "status 429",
    "error 429",
    "429 ",
    "quota exceeded",
    "quota_exceeded",
    "insufficient_quota",
    "usage_limit",
    "usage limit",
    "credit balance",
    "exhausted your capacity",
    "out of quota",
    "limit reached",
    "额度",
    "限流",
)

_SESSION_MISSING_PATTERNS = (
    "no rollout found",
    "session not found",
    "conversation not found",
    "could not find session",
    "no session",
)

_POLICY_PATTERNS = (
    "blocked by policy",
    "rejected(",
)

_TRANSIENT_NET_PATTERNS = (
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
    "watchdog_idle_timeout",
)


def classify_turn_error(
    error_text: Optional[str],
    stderr_lines: Optional[List[str]] = None,
    raw_events: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, Optional[str]]:
    """
    Structured-first error classifier.
    Priority: context_length_exceeded > policy_blocked > session_not_found > quota_rate_limit > transient_network > unknown.
    Critically prevents `RESOURCE_EXHAUSTED: input token count exceeds maximum` from being misrouted to quota swap!
    """
    # 1. Inspect structured event error codes first
    for ev in reversed(raw_events or []):
        if not isinstance(ev, dict):
            continue
        err_obj = ev.get("error") or (ev.get("result") or {}).get("error")
        if isinstance(err_obj, dict):
            code = str(err_obj.get("code") or err_obj.get("type") or "").lower()
            msg = str(err_obj.get("message") or "").lower()
            if code in ("context_length_exceeded", "context_window_exceeded", "prompt_too_long"):
                return "context_length_exceeded", f"{code}: {msg[:160]}"
            if any(p in msg for p in _CONTEXT_OVERFLOW_EXACT):
                return "context_length_exceeded", msg[:180]
            if code in ("rate_limit_exceeded", "insufficient_quota", "quota_exceeded"):
                return "quota_rate_limit", f"{code}: {msg[:160]}"

    combined = "\n".join([str(error_text or "")] + [str(x) for x in (stderr_lines or [])[-12:]])
    low = combined.lower().strip()
    if not low:
        return "unknown", None

    # 2. Context overflow ALWAYS checked BEFORE quota_rate_limit!
    for pat in _CONTEXT_OVERFLOW_EXACT:
        if pat in low:
            return "context_length_exceeded", pat
    if ("resource_exhausted" in low or "invalid_argument" in low or "400" in low or "413" in low) and any(
        h in low for h in _CONTEXT_OVERFLOW_COMBO_HINTS
    ):
        return "context_length_exceeded", "structured_overflow_hint"

    # 3. Policy blocked
    if "blocked by policy" in low or ("rejected(" in low and "policy" in low):
        return "policy_blocked", "blocked_by_policy"

    # 4. Session not found
    for pat in _SESSION_MISSING_PATTERNS:
        if pat in low:
            return "session_not_found", pat

    # 5. Quota / 429 rate limit
    for pat in _QUOTA_PATTERNS:
        if pat in low:
            return "quota_rate_limit", pat
    if "resource_exhausted" in low:
        return "quota_rate_limit", "resource_exhausted"

    # 6. Transient network
    for pat in _TRANSIENT_NET_PATTERNS:
        if pat in low:
            return "transient_network", pat

    return "unknown", None


def is_context_overflow_error(
    error_text: Optional[str],
    stderr_lines: Optional[List[str]] = None,
    raw_events: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    kind, _ = classify_turn_error(error_text, stderr_lines, raw_events)
    return kind == "context_length_exceeded"


# ==================== LOCK-FREE CONTENT-ADDRESSED BLOB STORE ====================

class BlobStore:
    """Lock-free content-addressed external store under D:\\gemini\\tmp\\team_blobs\\<task_id>\\."""

    def __init__(self, task_id: str, base_root: Optional[str] = None):
        root = base_root if base_root else _get_blob_base_root()
        safe_tid = re.sub(r"[^A-Za-z0-9_\-.]+", "_", str(task_id or "task"))[:64]
        self.task_dir = os.path.join(root, safe_tid)

    def put_text(self, text: str, subdir: str = "obs", ext: str = ".txt") -> Dict[str, Any]:
        raw = str(text or "")
        data = raw.encode("utf-8", errors="replace")
        digest = hashlib.sha256(data).hexdigest()
        bid = f"{subdir}_{digest[:16]}"
        target_dir = os.path.join(self.task_dir, subdir)
        os.makedirs(target_dir, exist_ok=True)
        fp = os.path.join(target_dir, f"{bid}{ext}")
        if not os.path.isfile(fp):
            tmp = f"{fp}.{os.getpid()}.{time.time_ns()}.tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, fp)
            except OSError:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                with open(fp, "wb") as f:
                    f.write(data)
        return {
            "blob_id": bid,
            "path": fp.replace("\\", "/"),
            "sha256": digest,
            "lines": raw.count("\n") + (1 if raw else 0),
            "chars": len(raw),
        }

    def put_json(self, obj: Dict[str, Any], subdir: str = "snapshots", name: Optional[str] = None) -> Dict[str, Any]:
        raw = json.dumps(obj, ensure_ascii=False, indent=2)
        if not name:
            return self.put_text(raw, subdir=subdir, ext=".json")
        target_dir = os.path.join(self.task_dir, subdir)
        os.makedirs(target_dir, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_\-.]+", "_", name)[:80]
        fp = os.path.join(target_dir, f"{safe_name}.json")
        data = raw.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        tmp = f"{fp}.{os.getpid()}.{time.time_ns()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, fp)
        return {
            "blob_id": safe_name,
            "path": fp.replace("\\", "/"),
            "sha256": digest,
            "lines": raw.count("\n") + 1,
            "chars": len(raw),
        }


# ==================== DETERMINISTIC ZERO-HALLUCINATION ANCHOR EXTRACTOR ====================

_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,120})\]\(([^()\n]{2,260})\)")
_BACKTICK_PATH_RE = re.compile(
    r"`([A-Za-z0-9_\-\u4e00-\u9fff./\\ ]{2,200}\.(?:py|js|ts|tsx|html|css|json|csv|tsv|md|r|R|svg|png|tiff|tif|pdf|drawio)(?:#L\d+(?:-L?\d+)?|:\d+)?)`"
)
_FILE_LINE_RE = re.compile(
    r"(?<![A-Za-z0-9_])((?:[A-Za-z]:[\\/])?(?:[A-Za-z0-9_\-\u4e00-\u9fff.]+[\\/])*[A-Za-z0-9_\-\u4e00-\u9fff]+\.(?:py|js|ts|tsx|html|css|json|csv|tsv|md|r|R|svg|png|tiff|tif|pdf|drawio)(?:#L\d+(?:-L?\d+)?|:\d+)?)"
)
_BLOCKING_LINE_RE = re.compile(
    r"(BLOCKING|BLK-\d+|CONTRADICTED|REFUTED|UNSUPPORTED|FAIL\b|REJECT\b|阻断|越界|漂移|死锁)",
    re.IGNORECASE,
)
_NUMERIC_CLAIM_RE = re.compile(
    r"(?:"
    r"\b(?:p_adj|p_raw|p_val|fdr|q_val|p|q)\s*[=<>≤≥:]\s*\d+(?:\.\d+)?(?:[eE][-+]?\d+|\s*[×xX]\s*10\^?[-−]?\d+)?|"
    r"\b(?:hr|or|rr|auc|auroc|auprc|f1|rmse|mae|mse|icc|log2?fc|fc|ci|dpi|n|exit_code|sha256|r2|aic|bic|chi2)\s*[=<>≤≥:]\s*[-+]?\d[\d.,eE\-+%–—]*|"
    r"(?:[Δδ]\s*(?:aic|bic|r2|r²|ll|logl)|R²|χ²|[tFzZ]\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))\s*[=<>≤≥:]\s*[-+]?\d[\d.,eE\-+]*|"
    r"\d+(?:\.\d+)?\s*(?:%|mm\b|pt\b|px\b|dpi\b|ms\b|例|个月|人|组)"
    r")",
    re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"(?:verdict|判定|裁决|签核)[^\n]{0,20}(CONFIRMED|REFUTED|PARTIAL|UNVERIFIABLE|ACCEPT|REJECT|PASS|FAIL|BLOCKED|REVISE)",
    re.IGNORECASE,
)


def extract_deterministic_anchors(text: str, source_event_id: str = "") -> Dict[str, Any]:
    """
    Pure-regex deterministic extraction of high-fidelity anchors (zero LLM hallucination!).
    Preserves exact raw literals for file paths (incl. spaced paths in links/backticks),
    scientific notation, biomedical/ML metrics, CJK statistics, and BLOCKING items.
    """
    src = str(text or "")
    file_refs: List[str] = []
    seen_files: set = set()
    blocking_lines: List[str] = []
    seen_blk: set = set()
    numeric_claims: List[str] = []
    seen_num: set = set()
    verdicts: List[str] = []
    seen_v: set = set()

    for _lbl, href in _MD_LINK_RE.findall(src):
        clean_h = href.strip().strip("<>")
        if clean_h and not clean_h.startswith("#") and clean_h not in seen_files:
            seen_files.add(clean_h)
            file_refs.append(clean_h)
            if len(file_refs) >= 24:
                break

    if len(file_refs) < 24:
        for bm in _BACKTICK_PATH_RE.findall(src):
            clean_b = bm.strip()
            if clean_b and clean_b not in seen_files:
                seen_files.add(clean_b)
                file_refs.append(clean_b)
                if len(file_refs) >= 24:
                    break

    if len(file_refs) < 24:
        for m in _FILE_LINE_RE.findall(src):
            clean_m = m.strip()
            if clean_m and clean_m not in seen_files:
                seen_files.add(clean_m)
                file_refs.append(clean_m)
                if len(file_refs) >= 24:
                    break

    for raw_ln in src.splitlines():
        ln = re.sub(r"\s+", " ", raw_ln.strip())
        if not ln or len(ln) < 6:
            continue
        clipped_ln = ln[:220]
        if _BLOCKING_LINE_RE.search(ln) and clipped_ln not in seen_blk and len(blocking_lines) < 16:
            seen_blk.add(clipped_ln)
            blocking_lines.append(clipped_ln)
        if _NUMERIC_CLAIM_RE.search(ln) and clipped_ln not in seen_num and len(numeric_claims) < 20:
            seen_num.add(clipped_ln)
            numeric_claims.append(clipped_ln)
        vm = _VERDICT_RE.search(ln)
        if vm:
            v_str = clipped_ln[:140]
            if v_str not in seen_v and len(verdicts) < 8:
                seen_v.add(v_str)
                verdicts.append(v_str)

    return {
        "source_event_id": source_event_id,
        "file_refs": file_refs,
        "blocking_lines": blocking_lines,
        "numeric_claims": numeric_claims,
        "verdicts": verdicts,
    }


# ==================== STRUCTURAL OBSERVATION MASKING ====================

_FENCED_BLOCK_RE = re.compile(r"```([A-Za-z0-9_\-]*)\n(.*?)```", flags=re.S)


def mask_observation_blocks(
    text: str,
    blob_store: BlobStore,
    *,
    min_code_lines: int = 32,
    min_table_lines: int = 25,
    min_block_chars: int = 1400,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Replace oversized fenced code blocks & wide Markdown tables with content-addressed pointer stubs
    that retain head/tail lines and deterministic anchors (file:line, numeric literals, BLOCKING).
    """
    src = str(text or "")
    if len(src) < min_block_chars:
        return src, []

    masked_refs: List[Dict[str, Any]] = []

    def _replace_fence(m: re.Match) -> str:
        lang = (m.group(1) or "code").strip().lower()
        body = m.group(2) or ""
        # Never mask structured control protocols!
        if lang in ("team_consult", "team_questions", "provenance_record", "experiment_run"):
            return m.group(0)
        line_cnt = body.count("\n") + (1 if body else 0)
        if line_cnt < min_code_lines and len(body) < min_block_chars:
            return m.group(0)
        meta = blob_store.put_text(m.group(0), subdir="obs", ext=".txt")
        anc = extract_deterministic_anchors(body)
        anc_bits = (anc["file_refs"][:3] + [x[:60] for x in anc["blocking_lines"][:2]])
        body_lines = [x.strip() for x in body.splitlines() if x.strip()]
        head = (body_lines[0][:90] if body_lines else "").replace('"', "'")
        tail = (body_lines[-1][:90] if len(body_lines) > 1 else "").replace('"', "'")
        stub = (
            f'[MASKED_OBS id={meta["blob_id"]} kind={lang or "code"} lines={line_cnt} chars={len(body)} '
            f'sha256={meta["sha256"][:8]} ref="{meta["path"]}" '
            + (f'anchors="{"; ".join(anc_bits)[:180]}" ' if anc_bits else "")
            + f'head="{head}" tail="{tail}"]'
        )
        masked_refs.append({**meta, "kind": lang or "code"})
        return stub

    out = _FENCED_BLOCK_RE.sub(_replace_fence, src)

    # Mask oversized contiguous Markdown tables (> min_table_lines)
    lines = out.splitlines()
    rebuilt: List[str] = []
    tbl_buf: List[str] = []

    def _flush_tbl() -> None:
        nonlocal tbl_buf
        if not tbl_buf:
            return
        joined = "\n".join(tbl_buf)
        if len(tbl_buf) >= min_table_lines or len(joined) >= min_block_chars * 2:
            meta = blob_store.put_text(joined, subdir="obs", ext=".md")
            anc = extract_deterministic_anchors(joined)
            hdr = tbl_buf[0][:120].replace('"', "'")
            anc_bits = [x[:60] for x in (anc["blocking_lines"][:2] + anc["numeric_claims"][:2])]
            rebuilt.append(
                f'[MASKED_OBS id={meta["blob_id"]} kind=table lines={len(tbl_buf)} chars={len(joined)} '
                f'sha256={meta["sha256"][:8]} ref="{meta["path"]}" header="{hdr}" '
                + (f'anchors="{"; ".join(anc_bits)[:180]}" ' if anc_bits else "")
                + 'hint="完整表格已落盘，需逐行核验请 read_file 读取 ref"]'
            )
            masked_refs.append({**meta, "kind": "table"})
        else:
            rebuilt.extend(tbl_buf)
        tbl_buf = []

    for ln in lines:
        if re.match(r"^\s*\|.+\|\s*$", ln):
            tbl_buf.append(ln)
        else:
            _flush_tbl()
            rebuilt.append(ln)
    _flush_tbl()

    return "\n".join(rebuilt), masked_refs


# ==================== IMMUTABLE ROUND STATE SNAPSHOT (RSS) ====================

def seal_round_snapshot(
    visible_events: List[Dict[str, Any]],
    *,
    visibility_scope: str,
    stage_index: int,
    cutoff_seq: int,
    task_meta: Dict[str, Any],
    blob_store: BlobStore,
    round_label: str = "",
) -> Dict[str, Any]:
    """
    Seal an IMMUTABLE deterministic Round State Snapshot strictly from pre-filtered `visible_events`.
    Because snapshots are immutable once sealed, Zone B remains append-only & KV-cache stable!
    """
    all_files: List[str] = []
    seen_files: set = set()
    all_blockers: List[str] = []
    seen_blk: set = set()
    all_numerics: List[str] = []
    seen_num: set = set()
    positions: List[str] = []
    consult_verdicts: List[str] = []
    masked_refs: List[Dict[str, Any]] = []

    for ev in visible_events:
        if ev.get("superseded_by_step_final") or ev.get("kind") == "pre_consult_draft":
            continue
        content = str(ev.get("content") or "")
        sender = str(ev.get("sender_name") or ev.get("sender_id") or "agent")
        kind = str(ev.get("kind") or "speech")
        ev_id = str(ev.get("id") or "")

        masked_txt, refs = mask_observation_blocks(content, blob_store)
        masked_refs.extend(refs)
        anc = extract_deterministic_anchors(content, source_event_id=ev_id)

        for fr in anc["file_refs"]:
            if fr not in seen_files and len(all_files) < 20:
                seen_files.add(fr)
                all_files.append(fr)
        for bl in anc["blocking_lines"]:
            tagged = f"[{sender}] {bl[:150]}"
            if tagged not in seen_blk and len(all_blockers) < 12:
                seen_blk.add(tagged)
                all_blockers.append(tagged)
        for nm in anc["numeric_claims"]:
            nm_clip = nm[:140]
            if nm_clip not in seen_num and len(all_numerics) < 12:
                seen_num.add(nm_clip)
                all_numerics.append(nm_clip)
        if kind == "mention_reply":
            v_summary = anc["verdicts"][0][:140] if anc["verdicts"] else re.sub(r"\s+", " ", masked_txt.strip())[:140]
            consult_verdicts.append(f"@{sender} 核验: {v_summary}")
        else:
            # Capture concise head + conclusion snippet per speaker
            non_empty = [re.sub(r"\s+", " ", x.strip()) for x in masked_txt.splitlines() if x.strip() and not x.strip().startswith("```")]
            head_s = non_empty[0][:130] if non_empty else ""
            tail_s = non_empty[-1][:90] if len(non_empty) > 1 else ""
            pos = f"- **{sender}** ({kind}): {head_s}" + (f" … 结论: {tail_s}" if tail_s and tail_s != head_s else "")
            positions.append(pos[:230])

    # Include authoritative rulings & signoff from task_meta
    rulings_lines: List[str] = []
    for rl in (task_meta.get("rulings") or [])[-4:]:
        if isinstance(rl, dict):
            rulings_lines.append(f"[{rl.get('decision', '')}] {str(rl.get('reason') or '')[:140]}")

    signoff = task_meta.get("research_signoff") if isinstance(task_meta.get("research_signoff"), dict) else None
    signoff_line = ""
    if signoff:
        signoff_line = (
            f"status={signoff.get('status')}, ok={signoff.get('ok')}, "
            f"blocking={len(signoff.get('blocking') or [])}, artifacts={signoff.get('checked_artifacts', 0)}"
        )

    lines_md: List[str] = [
        f"### [不可变轮次状态卡 · Stage {stage_index}" + (f" ({round_label})" if round_label else "") + f" · cutoff_seq={cutoff_seq}]",
    ]
    if positions:
        lines_md.append("**各席位核心立场**：\n" + "\n".join(positions[:8]))
    if all_blockers:
        lines_md.append("**未决 BLOCKING / 关键风险锚点（原样保留）**：\n" + "\n".join(f"- {b}" for b in all_blockers[:10]))
    if consult_verdicts:
        lines_md.append("**@协商核验判定**：\n" + "\n".join(f"- {c}" for c in consult_verdicts[:5]))
    if all_numerics:
        lines_md.append("**关键定量/统计字面值**：\n" + "\n".join(f"- {n}" for n in all_numerics[:10]))
    if all_files:
        lines_md.append("**核验文件与代码锚点**：" + " · ".join(f"`{f}`" for f in all_files[:14]))
    if rulings_lines:
        lines_md.append("**已生效裁决**：" + " | ".join(rulings_lines))
    if signoff_line:
        lines_md.append(f"**机械验收状态**：{signoff_line}")
    if masked_refs:
        lines_md.append(
            "**已外置大观测块指针**："
            + " · ".join(f"{r['blob_id']}({r['kind']},{r['lines']}L -> {r['path']})" for r in masked_refs[:6])
        )

    rendered_md = "\n".join(lines_md)
    snap_payload = {
        "stage_index": stage_index,
        "round_label": round_label,
        "visibility_scope": visibility_scope,
        "cutoff_seq": cutoff_seq,
        "event_ids": [str(ev.get("id") or "") for ev in visible_events],
        "immutable": True,
        "goal_and_rulings": rulings_lines,
        "unresolved_blockers": all_blockers,
        "locked_anchors": {"file_refs": all_files, "numeric_claims": all_numerics},
        "positions_and_verdicts": positions + consult_verdicts,
        "masked_obs_refs": masked_refs,
        "narrative_hint": None,  # M3 optional non-authoritative LLM digest slot (default None)
        "rendered_md": rendered_md,
        "sealed_at": time.time(),
    }
    scope_h = hashlib.sha256(visibility_scope.encode("utf-8")).hexdigest()[:8]
    saved = blob_store.put_json(snap_payload, subdir="snapshots", name=f"stage_{stage_index}_{scope_h}_c{cutoff_seq}")
    return {
        "stage_index": stage_index,
        "round_label": round_label,
        "visibility_scope": visibility_scope,
        "cutoff_seq": cutoff_seq,
        "event_ids": snap_payload["event_ids"],
        "event_count": len(visible_events),
        "snapshot_sha256": saved["sha256"],
        "blob_path": saved["path"],
        "rendered_md": rendered_md,
        "masked_count": len(masked_refs),
        "blocker_count": len(all_blockers),
        "anchor_count": len(all_files) + len(all_numerics),
        "sealed_at": snap_payload["sealed_at"],
    }


# ==================== CHECKPOINT HANDOFF PACK (SESSION ROLLOVER) ====================

def build_handoff_checkpoint(
    task: Dict[str, Any],
    step: Dict[str, Any],
    visible_msgs: List[Dict[str, Any]],
    blob_store: BlobStore,
    room_md_path: str,
    *,
    reason: str = "proactive_watermark",
    level: str = "L1",
) -> Dict[str, Any]:
    """
    Build a deterministic, anchor-preserving Checkpoint Handoff Pack for seamless Session Rollover (`session_id=None`).
    Persists full checkpoint to BlobStore and returns compact Markdown sized for the target compression level.
    """
    max_chars = 3200 if level == "L1" else (1800 if level == "L2" else 5200)
    all_text = "\n".join(str(m.get("content") or "") for m in visible_msgs[-8:])
    anc = extract_deterministic_anchors(all_text)

    completed_steps = [
        f"#{s['step_index']} {s.get('title', '')} ({s.get('member_id', '')})"
        for s in (task.get("steps") or [])
        if s.get("status") == "succeeded"
    ]
    changed_preview: List[str] = []
    for s in task.get("steps") or []:
        for att in s.get("attempts") or []:
            ls = att.get("ledger_summary") or {}
            for p in ls.get("changed_files_preview") or []:
                if p and p not in changed_preview:
                    changed_preview.append(p)

    signoff = task.get("research_signoff") if isinstance(task.get("research_signoff"), dict) else None
    signoff_blocks = list(signoff.get("blocking") or []) if signoff else []
    rulings = [f"{r.get('decision')}: {str(r.get('reason') or '')[:140]}" for r in (task.get("rulings") or [])[-3:]]

    recent_tail: List[str] = []
    for cm in visible_msgs[-3:]:
        txt = str(cm.get("content") or "").strip()
        if txt:
            masked_t, _ = mask_observation_blocks(txt, blob_store, min_code_lines=16, min_table_lines=14, min_block_chars=700)
            recent_tail.append(f"- [{cm.get('round_label') or cm.get('kind')}] {cm.get('sender_name')}: {masked_t[:260]}")

    parts = [
        f"【无缝会话换乘检查点 (Session Rollover Checkpoint · reason={reason} · level={level})】",
        f"- 原始完整群聊纪要文件（按需用 read_file 查阅）：{room_md_path}",
        f"- 任务总目标：{str(task.get('goal') or '')[:600]}",
    ]
    if completed_steps:
        parts.append("- 已完成前序节点：" + "；".join(completed_steps[-8:]))
    if changed_preview:
        parts.append("- 近期工作区已变更文件：" + ", ".join(changed_preview[:16]))
    if rulings:
        parts.append("- 负责人有效裁决：" + " | ".join(rulings))
    if signoff_blocks or anc["blocking_lines"]:
        blks = (signoff_blocks[:6] + anc["blocking_lines"][:6])[:8]
        parts.append("- 必须优先闭环的 BLOCKING / 验收阻断项：\n" + "\n".join(f"  * {b}" for b in blks))
    if anc["numeric_claims"]:
        parts.append("- 锁定关键统计/数值字面值：\n" + "\n".join(f"  * {n}" for n in anc["numeric_claims"][:8]))
    if anc["file_refs"]:
        parts.append("- 关键文件/行号锚点：" + " · ".join(f"`{f}`" for f in anc["file_refs"][:12]))
    if recent_tail and level != "L2":
        parts.append("- 最近发言要点：\n" + "\n".join(recent_tail))

    full_md = "\n".join(parts)
    saved = blob_store.put_text(full_md, subdir="checkpoints", ext=".md")
    if len(full_md) > max_chars:
        compact_md = full_md[: max_chars - 140] + f"\n…[完整检查点已落盘：{saved['path']}]"
    else:
        compact_md = full_md

    return {
        "checkpoint_md": compact_md,
        "blob_path": saved["path"],
        "sha256": saved["sha256"],
        "reason": reason,
        "level": level,
        "anchors_count": len(anc["file_refs"]) + len(anc["numeric_claims"]) + len(anc["blocking_lines"]),
    }


# ==================== BUDGET CONFIG & PROACTIVE ROLLOVER POLICY ====================

@dataclass
class ContextBudgetConfig:
    max_prompt_chars: int = 48_000          # L0 hard prompt budget (balanced default)
    l1_prompt_chars: int = 20_000           # L1 first reactive rollover budget
    l2_prompt_chars: int = 9_500            # L2 emergency anchor-only budget
    rollover_fill_ratio: float = 0.72       # Proactive rollover watermark against model context window
    max_turns_per_session: int = 8          # Turn watermark (keeps 3-round repair tests in same session!)
    session_mode: str = "bounded_native"    # "bounded_native" (M1) | "stateless_replay" (M2)
    auto_rollover: bool = True
    protect_recent_rounds: int = 1          # Never mask immediately preceding round under L0
    mask_min_code_lines: int = 32
    mask_min_table_lines: int = 25
    mask_min_block_chars: int = 1400
    explicit_budget: bool = False           # True when user/test explicitly pinned context_budget_chars

    @classmethod
    def from_settings(cls, settings: Optional[Dict[str, Any]]) -> "ContextBudgetConfig":
        s = settings if isinstance(settings, dict) else {}
        has_explicit = bool(s.get("context_budget_chars"))
        max_c = int(s.get("context_budget_chars") or 48_000)
        max_c = max(12_000, min(160_000, max_c))
        mode = str(s.get("context_session_mode") or "bounded_native").strip().lower()
        if mode not in ("bounded_native", "stateless_replay"):
            mode = "bounded_native"
        turns = int(s.get("max_turns_per_session") or 8)
        turns = max(3, min(30, turns))
        ratio = float(s.get("context_rollover_ratio") or 0.72)
        ratio = max(0.35, min(0.92, ratio))
        return cls(
            max_prompt_chars=max_c,
            l1_prompt_chars=max(10_000, min(22_000, max_c // 2)),
            l2_prompt_chars=max(6_500, min(11_000, max_c // 4)),
            rollover_fill_ratio=ratio,
            max_turns_per_session=turns,
            session_mode=mode,
            auto_rollover=bool(s.get("auto_context_rollover", True)),
            explicit_budget=has_explicit,
        )


def should_proactive_rollover(
    session_metric: Optional[Dict[str, Any]],
    model_slug: Optional[str],
    est_next_tokens: int,
    cfg: ContextBudgetConfig,
    *,
    current_prefix_sha256: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Decide BEFORE spawning CLI whether an existing native session should proactively roll over
    to a fresh session (`session_id=None`) with a Structured Checkpoint Handoff Pack.
    """
    if not cfg.auto_rollover:
        return False, None
    if cfg.session_mode == "stateless_replay":
        return True, "stateless_replay_mode"
    if not isinstance(session_metric, dict):
        return False, None

    window, _src = get_model_context_window(model_slug)
    turns = int(session_metric.get("turns") or 0)
    # Prefer calibrated single-call peak context occupancy over multi-tool cumulative billing sum!
    last_peak = int(session_metric.get("last_peak_context_tokens") or 0)
    last_in = int(session_metric.get("last_input_tokens") or 0)
    est_cum = int(session_metric.get("estimated_cumulative_tokens") or 0)
    observed_occ = max(last_peak if last_peak > 0 else last_in, est_cum)
    projected = observed_occ + max(0, est_next_tokens)
    watermark = int(window * cfg.rollover_fill_ratio)

    if observed_occ > 0 and projected >= watermark:
        return True, f"token_watermark ({projected}/{window} >= {int(cfg.rollover_fill_ratio * 100)}%)"
    if turns >= cfg.max_turns_per_session:
        return True, f"turn_watermark ({turns} >= {cfg.max_turns_per_session})"
    prev_p = session_metric.get("prefix_sha256")
    if prev_p and current_prefix_sha256 and prev_p != current_prefix_sha256 and turns >= 3:
        return True, "prefix_drift_reset"
    return False, None


# ==================== JIT PLAYBOOK FILTERING ====================

def select_jit_playbooks(
    playbooks: Optional[List[str]],
    *,
    step_type: str,
    sandbox: str,
    has_figure_contract: bool,
    is_review: bool,
) -> List[str]:
    """
    Filter OpenScience playbooks Just-In-Time per step role to avoid dumping irrelevant SOP walls.
    Preserves all playbooks on core execution/discussion/synthesis steps.
    """
    pbs = [str(x).strip() for x in (playbooks or []) if str(x).strip()]
    if not pbs:
        return []
    st = (step_type or "execution").strip().lower()
    if st in ("opening", "chief_plan"):
        # Lightweight planning steps only need high-level design/audit playbooks (max 2)
        pref = [p for p in pbs if p in ("experimental_design_anti_dup", "sources_citations_audit", "statistical_conventions")]
        return (pref or pbs)[:2]
    if is_review or st in ("review", "fact_check"):
        # Review/fact-check steps skip figure-export scripting boilerplate unless figure_contract is explicitly active
        if has_figure_contract:
            return pbs
        kept = [p for p in pbs if p != "publication_figures_schematics"]
        return kept or pbs
    return pbs


# ==================== THREE-ZONE KV-CACHE STABLE PROMPT COMPILER ====================

def _clip_preserve_ends(text: str, limit: int) -> str:
    s = str(text or "")
    if limit <= 0 or len(s) <= limit:
        return s
    head = int(limit * 0.65)
    tail = max(80, limit - head - 64)
    if head + tail >= len(s):
        return s[:limit]
    return s[:head] + f"\n…[已按硬预算省略中间 {len(s) - head - tail} 字]…\n" + s[-tail:]


def _clip_preserve_anchors(text: str, limit: int, raw_text: Optional[str] = None) -> str:
    """Clip an oversized message while deterministically preserving head, tail, and key BLOCKING/numeric anchors."""
    s = str(text or "")
    if limit <= 0 or len(s) <= limit:
        return s
    anc = extract_deterministic_anchors(raw_text if raw_text is not None else s)
    anc_lines: List[str] = []
    if anc["blocking_lines"]:
        anc_lines.append("BLOCKING/关键风险: " + " | ".join(x[:100] for x in anc["blocking_lines"][:3]))
    if anc["verdicts"]:
        anc_lines.append("判定: " + " | ".join(x[:80] for x in anc["verdicts"][:2]))
    if anc["numeric_claims"]:
        anc_lines.append("关键数值: " + " | ".join(x[:90] for x in anc["numeric_claims"][:3]))
    anc_block = ("\n[保留核心锚点] " + " ; ".join(anc_lines) + "\n") if anc_lines else ""
    if len(anc_block) > int(limit * 0.25):
        anc_block = anc_block[: int(limit * 0.25)] + "\n"
    avail = max(240, limit - len(anc_block) - 56)
    head = int(avail * 0.68)
    tail = max(80, avail - head)
    if head + tail >= len(s):
        return s[:limit]
    return (
        s[:head]
        + f"\n…[按公平配额压缩中间 {len(s) - head - tail} 字]…"
        + anc_block
        + s[-tail:]
    )


clip_preserve_ends = _clip_preserve_ends
clip_preserve_anchors = _clip_preserve_anchors


def _waterfill_budgets(lengths: List[int], total_budget: int, min_floor: int = 1200, max_cap: int = 14000) -> List[int]:
    """Fair water-fill budget allocator: short items take only what they need; surplus redistributes evenly to longer peers."""
    n = len(lengths)
    if n == 0:
        return []
    caps = [0] * n
    rem_indices = list(range(n))
    rem_budget = max(0, int(total_budget))
    while rem_indices and rem_budget > 0:
        fair = max(1, rem_budget // len(rem_indices))
        settled = []
        for i in rem_indices:
            need = min(lengths[i], max_cap)
            if need <= fair:
                caps[i] = need
                rem_budget -= need
                settled.append(i)
        if not settled:
            each = min(max_cap, max(min_floor, fair)) if rem_budget >= min_floor * len(rem_indices) else max(320, fair)
            for i in rem_indices:
                caps[i] = min(lengths[i], each)
            break
        rem_indices = [i for i in rem_indices if i not in settled]
    return caps


def compile_turn_prompt(
    *,
    zone_a_blocks: List[str],
    zone_b_static_blocks: List[str],
    sealed_snapshots: List[Dict[str, Any]],
    visible_msgs: List[Dict[str, Any]],
    is_blind_r1: bool,
    current_stage_index: int,
    step_prompt_block: str,
    handoff_block: str,
    protocol_blocks: List[str],
    room_md_path: str,
    blob_store: BlobStore,
    session_id: Optional[str],
    session_metric: Optional[Dict[str, Any]],
    model_slug: Optional[str],
    cfg: ContextBudgetConfig,
    compression_level: str = "L0",
    force_full_prompt: bool = False,
    dep_fallback_block: str = "",
    step_type: str = "execution",
    current_member_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compile a Three-Zone KV-Cache Prefix-Stable prompt with Four-Tier Hard Budget Governance (v5.7).

    Returns:
      {
        "prompt": str,
        "prefix_sha256": str,
        "is_delta": bool,
        "compression_level": str,
        "prompt_chars": int,
        "est_tokens": int,
        "budget_chars": int,
        "budget_decision": "send" | "proactive_rollover" | "budget_blocked",
        "rollover_reason": Optional[str],
        "chat_cursor_id": str,
        "delivered_msg_ids": List[str],
        "masked_blocks": int,
        "used_snapshots": int,
        "omitted_refs": List[str],
        "window_tokens": int,
        "window_source": str,
      }
    """
    zone_a_text = "\n\n".join(b for b in zone_a_blocks if b and b.strip())
    prefix_sha256 = hashlib.sha256(zone_a_text.encode("utf-8")).hexdigest()[:16]

    level = compression_level if compression_level in ("L0", "L1", "L2") else "L0"
    window_tokens, window_source = get_model_context_window(model_slug)
    if level == "L0":
        if cfg.explicit_budget:
            budget_chars = cfg.max_prompt_chars
        else:
            st_low = (step_type or "execution").strip().lower()
            if window_tokens >= 900_000:
                budget_chars = 96_000 if st_low in ("synthesis", "fact_check") else 72_000
            elif window_tokens >= 250_000:
                budget_chars = 80_000 if st_low in ("synthesis", "fact_check") else 72_000
            else:
                budget_chars = cfg.max_prompt_chars
    else:
        budget_chars = cfg.l1_prompt_chars if level == "L1" else cfg.l2_prompt_chars

    # Never let budget exceed 60% of model's context window in characters
    win_char_cap = int(window_tokens * 1.4)
    effective_budget = min(budget_chars, max(9_000, win_char_cap))

    # Determine whether we can use Resumed-Session Delta Mode (M1)
    has_seen_set = isinstance(session_metric, dict) and isinstance(session_metric.get("seen_msg_ids"), list) and len(session_metric.get("seen_msg_ids") or []) > 0
    has_cursor = isinstance(session_metric, dict) and bool(session_metric.get("last_synced_msg_id"))
    can_delta = (
        bool(session_id)
        and cfg.session_mode == "bounded_native"
        and not force_full_prompt
        and level == "L0"
        and isinstance(session_metric, dict)
        and session_metric.get("prefix_sha256") == prefix_sha256
        and (has_seen_set or has_cursor)
    )

    last_cursor_id = str(visible_msgs[-1].get("id") or "") if visible_msgs else ""
    omitted_refs: List[str] = []
    masked_blocks = 0

    # Filter sealed snapshots strictly (NEVER leak snapshots into Blind Round 1!)
    usable_snapshots: List[Dict[str, Any]] = []
    covered_cutoff_ids: set = set()
    protect_gap = max(0, int(cfg.protect_recent_rounds)) if level == "L0" else 0
    if not is_blind_r1 and sealed_snapshots:
        for sn in sorted(sealed_snapshots, key=lambda x: int(x.get("stage_index", 0))):
            sn_stage = int(sn.get("stage_index", 999))
            is_manual = str(sn.get("visibility_scope") or "") == "manual_compact"
            if (is_manual and sn_stage <= int(current_stage_index)) or (sn_stage < int(current_stage_index) - protect_gap):
                usable_snapshots.append(sn)
                for eid in sn.get("event_ids") or []:
                    covered_cutoff_ids.add(eid)

    # P0 mandatory floor blocks (NEVER silently dropped!)
    p0_tail_blocks: List[str] = []
    if handoff_block and handoff_block.strip():
        p0_tail_blocks.append(handoff_block.strip())
    if step_prompt_block and step_prompt_block.strip():
        p0_tail_blocks.append(step_prompt_block.strip())
    for pb in protocol_blocks:
        if pb and pb.strip():
            p0_tail_blocks.append(pb.strip())

    # Assemble Zone A + Zone B static prefix (or concise 1-line anchor if Delta Mode)
    if can_delta:
        head_blocks = [
            f"【同会话增量帧 Delta Frame · prefix={prefix_sha256} · 沿用已建立身份/规则与完整纪要 {room_md_path}】"
        ]
        if has_seen_set:
            seen_set = {str(x) for x in (session_metric.get("seen_msg_ids") or []) if x}
            delta_msgs = [cm for cm in visible_msgs if str(cm.get("id") or "") not in seen_set]
        else:
            prev_mid = str(session_metric.get("last_synced_msg_id") or "")
            delta_msgs = []
            found_cursor = False
            for cm in visible_msgs:
                if found_cursor:
                    delta_msgs.append(cm)
                elif str(cm.get("id") or "") == prev_mid:
                    found_cursor = True
            if not found_cursor:
                delta_msgs = visible_msgs[-4:]
        # Skip member's own prior speeches already present verbatim in the resumed session history!
        if current_member_id:
            delta_msgs = [
                cm for cm in delta_msgs
                if not (
                    cm.get("sender_id") == current_member_id
                    and cm.get("kind") in ("speech", "deliverable", "plan", "pre_consult_draft")
                )
            ]
        active_msgs = delta_msgs
        used_snaps: List[Dict[str, Any]] = []
    else:
        head_blocks = [zone_a_text] + [b for b in zone_b_static_blocks if b and b.strip()]
        used_snaps = usable_snapshots
        if used_snaps and level == "L0":
            uncovered = [cm for cm in visible_msgs if str(cm.get("id") or "") not in covered_cutoff_ids]
            active_msgs = uncovered[-12:] if uncovered else visible_msgs[-6:]
        elif level == "L1":
            active_msgs = visible_msgs[-6:]
        elif level == "L2":
            active_msgs = visible_msgs[-4:]
        else:
            active_msgs = visible_msgs[-16:]

    # Build Zone B immutable snapshots text (append-only, never rewritten!)
    snap_blocks: List[str] = []
    for sn in used_snaps:
        md = str(sn.get("rendered_md") or "").strip()
        if md:
            snap_blocks.append(md)

    # Calculate mandatory P0 + head size
    base_fixed_len = sum(len(x) + 2 for x in head_blocks) + sum(len(x) + 2 for x in p0_tail_blocks)
    if base_fixed_len > effective_budget and not can_delta:
        trimmed_head = [zone_a_text]
        for zb in zone_b_static_blocks:
            if zb and zb.strip():
                trimmed_head.append(_clip_preserve_ends(zb, max(800, (effective_budget // 6))))
        head_blocks = trimmed_head
        base_fixed_len = sum(len(x) + 2 for x in head_blocks) + sum(len(x) + 2 for x in p0_tail_blocks)

    if sum(len(x) for x in p0_tail_blocks) > effective_budget * 0.92:
        return {
            "prompt": "\n\n".join(head_blocks + p0_tail_blocks),
            "prefix_sha256": prefix_sha256,
            "is_delta": can_delta,
            "compression_level": level,
            "prompt_chars": base_fixed_len,
            "est_tokens": estimate_tokens("\n\n".join(head_blocks + p0_tail_blocks)),
            "budget_chars": effective_budget,
            "budget_decision": "budget_blocked",
            "rollover_reason": "p0_exceeds_hard_budget",
            "chat_cursor_id": last_cursor_id,
            "delivered_msg_ids": [],
            "masked_blocks": 0,
            "used_snapshots": len(snap_blocks),
            "omitted_refs": [f"P0 mandatory prompt ({sum(len(x) for x in p0_tail_blocks)} chars) > budget ({effective_budget} chars)"],
            "window_tokens": window_tokens,
            "window_source": window_source,
        }

    remaining_budget = max(1200, effective_budget - base_fixed_len)

    # Reserve up to 28% (40% under L1/L2) of remaining budget for sealed Zone B snapshots.
    kept_snap_rev: List[str] = []
    evicted_old_snaps: List[str] = []
    snap_budget = int(remaining_budget * (0.40 if level != "L0" else 0.28))
    used_snap_chars = 0
    for sb in reversed(snap_blocks):
        if used_snap_chars + len(sb) <= snap_budget:
            kept_snap_rev.append(sb)
            used_snap_chars += len(sb) + 2
        elif not kept_snap_rev and snap_budget >= 400:
            clip_sb = _clip_preserve_ends(sb, snap_budget)
            kept_snap_rev.append(clip_sb)
            used_snap_chars += len(clip_sb) + 2
        else:
            evicted_old_snaps.append(sb)
            omitted_refs.append("older_round_snapshot_folded")
    kept_snap_blocks: List[str] = list(reversed(kept_snap_rev))
    if evicted_old_snaps:
        evicted_old_snaps.reverse()
        anc_old = extract_deterministic_anchors("\n".join(evicted_old_snaps))
        fold_bits: List[str] = [f"### [早期轮次折叠锚点总账 ({len(evicted_old_snaps)} 轮已归档至 BlobStore/FTS5)]"]
        if anc_old["blocking_lines"]:
            fold_bits.append("- 遗留阻断：" + " | ".join(anc_old["blocking_lines"][:6]))
        if anc_old["numeric_claims"]:
            fold_bits.append("- 锁定数值：" + " | ".join(anc_old["numeric_claims"][:6]))
        if anc_old["file_refs"]:
            fold_bits.append("- 关键文件：" + " · ".join(f"`{x}`" for x in anc_old["file_refs"][:10]))
        fold_md = "\n".join(fold_bits)[:1200]
        kept_snap_blocks.insert(0, fold_md)
        used_snap_chars += len(fold_md) + 2
    remaining_budget = max(800, remaining_budget - used_snap_chars)

    # Render Zone C messages with Superseded Draft Folding + Fair Per-Speaker Water-Fill Budgeting!
    msg_blocks: List[str] = []
    delivered_msg_ids: List[str] = []
    has_non_goal_active = any(cm.get("kind") != "user_goal" for cm in active_msgs)
    show_active_msgs = (
        (can_delta and bool(active_msgs))
        or (bool(used_snaps) and has_non_goal_active)
        or (len(active_msgs) > 1)
    )
    if show_active_msgs:
        n_active = len(active_msgs)
        raw_total = sum(len(str(cm.get("content") or "")) for cm in active_msgs if not cm.get("superseded_by_step_final"))
        pressure_high = raw_total > remaining_budget

        # Pre-prepare every active message (folding superseded pre-consult drafts & masking oversized blocks)
        # Count reverse rank ONLY among non-superseded messages so folded pre-consult drafts NEVER demote real peers!
        live_indices = [
            i for i, cm in enumerate(active_msgs)
            if not (bool(cm.get("superseded_by_step_final")) or cm.get("kind") == "pre_consult_draft")
        ]
        live_rev_map = {orig_i: (len(live_indices) - 1 - pos) for pos, orig_i in enumerate(live_indices)}
        prio_window = 10 if level == "L0" else 6

        prepared: List[Dict[str, Any]] = []
        for idx_m, cm in enumerate(active_msgs):
            live_rev = live_rev_map.get(idx_m, 999)
            to_tag = f" @{cm['to_member_name']}" if cm.get("to_member_name") else ""
            raw_c = str(cm.get("content") or "")
            is_superseded = bool(cm.get("superseded_by_step_final")) or cm.get("kind") == "pre_consult_draft"
            is_user_or_ruling = cm.get("kind") in ("user_goal", "user_interjection", "user_choice", "ruling")
            is_protected_recent = (live_rev < prio_window) and (level == "L0")
            hdr_prefix = f"[{cm.get('round_label') or cm.get('kind')}] {cm.get('sender_name') or cm.get('sender_id')}{to_tag}：\n"

            if is_superseded:
                proc_c = f"(协商前初稿，已由同步骤协商后定稿取代；完整存档见 {room_md_path})"
            elif not is_user_or_ruling and (not is_protected_recent or level != "L0" or pressure_high):
                proc_c, refs = mask_observation_blocks(
                    raw_c,
                    blob_store,
                    min_code_lines=cfg.mask_min_code_lines if level == "L0" else 16,
                    min_table_lines=cfg.mask_min_table_lines if level == "L0" else 14,
                    min_block_chars=cfg.mask_min_block_chars if level == "L0" else 700,
                )
                masked_blocks += len(refs)
            else:
                proc_c = raw_c

            prepared.append({
                "idx": idx_m,
                "cm": cm,
                "hdr": hdr_prefix,
                "raw": raw_c,
                "proc": proc_c,
                "is_superseded": is_superseded,
                "is_priority": (not is_superseded) and (live_rev < prio_window or cm.get("kind") in ("user_interjection", "user_choice", "ruling")),
                "is_user_or_ruling": is_user_or_ruling,
            })

        # Allocate fair water-fill budgets to the priority cohort first (guarantees ZERO active Roundtable peers dropped
        # AND 100.0% verbatim zero-clip delivery whenever all priority messages fit inside remaining budget!)
        prio_items = [p for p in prepared if p["is_priority"]]
        other_items = [p for p in prepared if not p["is_priority"]]
        max_per_msg = (
            min(28_000, max(16_000, int(effective_budget * 0.40)))
            if level == "L0"
            else (4200 if level == "L1" else 1800)
        )
        prio_reserve = int(remaining_budget * (0.90 if other_items else 0.98))
        prio_lengths = [len(p["hdr"]) + len(p["proc"]) + 6 for p in prio_items]
        if sum(prio_lengths) <= prio_reserve and level == "L0":
            # Everything fits comfortably: deliver EVERY peer's complete opinion verbatim with ZERO clipping!
            prio_caps = list(prio_lengths)
        else:
            prio_caps = _waterfill_budgets(prio_lengths, prio_reserve, min_floor=1000, max_cap=max_per_msg)

        rendered_by_idx: Dict[int, str] = {}
        used_msg_chars = 0
        for p, cap in zip(prio_items, prio_caps):
            body = p["proc"]
            full_entry_len = len(p["hdr"]) + len(body) + 6
            if full_entry_len > cap:
                body_cap = max(320, cap - len(p["hdr"]) - 72)
                off = blob_store.put_text(p["raw"], subdir="msgs", ext=".md")
                body = _clip_preserve_anchors(body, body_cap, raw_text=p["raw"]) + f"\n[完整原文指针: {off['path']}]"
            entry = p["hdr"] + body
            rendered_by_idx[p["idx"]] = entry
            used_msg_chars += len(entry) + 6
            cid = str(p["cm"].get("id") or "")
            if cid:
                delivered_msg_ids.append(cid)

        # Spend remaining budget on non-priority / older messages (newest -> oldest)
        for p in reversed(other_items):
            avail = remaining_budget - used_msg_chars
            cm = p["cm"]
            cid = str(cm.get("id") or "")
            if p["is_superseded"]:
                entry = p["hdr"].rstrip("\n") + " " + p["proc"]
                if len(entry) + 6 <= avail:
                    rendered_by_idx[p["idx"]] = entry
                    used_msg_chars += len(entry) + 6
                    if cid:
                        delivered_msg_ids.append(cid)
                continue
            older_cap = (8000 if not pressure_high else 4000) if level == "L0" else (1800 if level == "L1" else 900)
            body = p["proc"]
            if len(body) > older_cap:
                off = blob_store.put_text(p["raw"], subdir="msgs", ext=".md")
                body = _clip_preserve_anchors(body, older_cap, raw_text=p["raw"]) + f"\n[完整原文指针: {off['path']}]"
            entry = p["hdr"] + body
            if len(entry) + 6 <= avail:
                rendered_by_idx[p["idx"]] = entry
                used_msg_chars += len(entry) + 6
                if cid:
                    delivered_msg_ids.append(cid)
            elif avail >= 280:
                anc = extract_deterministic_anchors(p["raw"], cid)
                brief = "; ".join(anc["blocking_lines"][:2] + anc["file_refs"][:3] + anc["numeric_claims"][:2])
                stub = (
                    f"[{cm.get('round_label') or cm.get('kind')}] {cm.get('sender_name') or cm.get('sender_id')} "
                    f"(已折叠，原文见 {room_md_path})：{brief[: max(120, avail - 120)]}"
                )
                rendered_by_idx[p["idx"]] = stub
                used_msg_chars += len(stub) + 6
                omitted_refs.append(f"msg:{cid or p['idx']}({cm.get('sender_id')})")
            else:
                omitted_refs.append(f"msg:{cid or p['idx']}({cm.get('sender_id')})")

        rendered_chron = [rendered_by_idx[i] for i in sorted(rendered_by_idx.keys())]
        if rendered_chron:
            hdr = (
                "【本轮可见上下文（Blind Round 1 隔离视图，不包含同轮其他席位草稿）】\n"
                if is_blind_r1
                else (
                    f"【团队共享群聊记录 · 新增增量帧（完整纪要：{room_md_path}）】\n"
                    if can_delta
                    else f"【团队共享群聊记录（完整纪要文件：{room_md_path}）】\n"
                )
            )
            msg_blocks.append(hdr + "\n---\n".join(rendered_chron))
    elif dep_fallback_block and not is_blind_r1 and not can_delta:
        dep_txt = dep_fallback_block
        if len(dep_txt) > remaining_budget:
            dep_txt, refs = mask_observation_blocks(dep_txt, blob_store)
            masked_blocks += len(refs)
            dep_txt = _clip_preserve_ends(dep_txt, remaining_budget)
        msg_blocks.append(dep_txt)

    all_blocks: List[str] = []
    all_blocks.extend(head_blocks)
    if kept_snap_blocks:
        all_blocks.append("【已封存不可变历史轮次状态卡（Zone B · 锚点零丢失）】\n" + "\n\n".join(kept_snap_blocks))
    all_blocks.extend(msg_blocks)
    all_blocks.extend(p0_tail_blocks)

    final_prompt = "\n\n".join(b for b in all_blocks if b and b.strip())
    est_tok = estimate_tokens(final_prompt)

    # Proactive watermark check when reusing an existing session
    budget_decision = "send"
    rollover_reason: Optional[str] = None
    if session_id and not force_full_prompt:
        need_ro, ro_reason = should_proactive_rollover(
            session_metric,
            model_slug,
            est_tok,
            cfg,
            current_prefix_sha256=prefix_sha256,
        )
        if need_ro:
            budget_decision = "proactive_rollover"
            rollover_reason = ro_reason

    return {
        "prompt": final_prompt,
        "prefix_sha256": prefix_sha256,
        "is_delta": can_delta,
        "compression_level": level,
        "prompt_chars": len(final_prompt),
        "est_tokens": est_tok,
        "budget_chars": effective_budget,
        "budget_decision": budget_decision,
        "rollover_reason": rollover_reason,
        "chat_cursor_id": last_cursor_id,
        "delivered_msg_ids": delivered_msg_ids,
        "masked_blocks": masked_blocks,
        "used_snapshots": len(kept_snap_blocks),
        "omitted_refs": omitted_refs,
        "window_tokens": window_tokens,
        "window_source": window_source,
    }
