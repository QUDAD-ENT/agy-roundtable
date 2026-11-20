"""
AGY Roundtable v5.4 Project-Scoped SQLite FTS5 Cross-Task Memory & Pure-Stdlib AST RepoMap.

Surpasses mksglu/context-mode (adds bilingual CJK bi-gram + English unicode61 BM25 search)
and Aider RepoMap (pure Python stdlib `ast` + JS/TS/R structural symbol extraction +
cross-file reference centrality ranking with zero C-extension or third-party dependencies).

All database files strictly live under D:\\gemini\\tmp\\team_memory\\ (zero C: storage).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


_DB_LOCKS_GUARD = threading.Lock()
_DB_LOCKS: Dict[str, threading.RLock] = {}

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_TOK_RE = re.compile(r"[A-Za-z0-9_./#-]{2,64}")


def _get_db_lock(db_path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(db_path))
    with _DB_LOCKS_GUARD:
        lk = _DB_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _DB_LOCKS[key] = lk
        return lk


def _get_memory_base_root() -> str:
    env_tmp = os.environ.get("AGY_RUNTIME_TMP") or os.environ.get("TEAM_RUNTIME_TMP")
    if env_tmp:
        return os.path.join(env_tmp, "team_memory")
    if os.path.isdir(r"D:\gemini\tmp"):
        return os.path.join(r"D:\gemini\tmp", "team_memory")
    return os.path.join(tempfile.gettempdir(), "team_memory")


def _cjk_augment(text: str, max_terms: int = 1600) -> str:
    """Emit overlapping CJK bi-grams (+ single chars for 1-char runs) so SQLite FTS5 matches Chinese phrases accurately."""
    s = str(text or "")
    out: List[str] = []
    for run in _CJK_RUN_RE.findall(s):
        n = len(run)
        if n == 1:
            out.append(run)
        else:
            for i in range(n - 1):
                out.append(run[i : i + 2])
                if len(out) >= max_terms:
                    return " ".join(out)
        if len(out) >= max_terms:
            break
    return " ".join(out)


def _extract_query_terms(query: str, max_terms: int = 14) -> Tuple[List[str], List[str]]:
    """Return (ascii_terms, cjk_bigrams) safely sanitized for SQLite FTS5 MATCH."""
    raw = str(query or "").strip()
    ascii_terms: List[str] = []
    seen_a: set = set()
    for m in re.findall(r"[A-Za-z0-9_]{2,48}", raw):
        low = m.lower()
        if low in ("the", "and", "for", "with", "from", "this", "that", "into", "are", "was", "have"):
            continue
        if low not in seen_a:
            seen_a.add(low)
            ascii_terms.append(low)
            if len(ascii_terms) >= max_terms:
                break

    cjk_terms: List[str] = []
    seen_c: set = set()
    for run in _CJK_RUN_RE.findall(raw):
        if len(run) == 1:
            if run not in seen_c:
                seen_c.add(run)
                cjk_terms.append(run)
        else:
            for i in range(len(run) - 1):
                bg = run[i : i + 2]
                if bg not in seen_c:
                    seen_c.add(bg)
                    cjk_terms.append(bg)
                    if len(cjk_terms) >= max_terms:
                        break
        if len(cjk_terms) >= max_terms:
            break
    return ascii_terms, cjk_terms


def _build_fts5_match_expr(query: str) -> str:
    ascii_terms, cjk_terms = _extract_query_terms(query, max_terms=10)
    parts: List[str] = []
    for t in ascii_terms[:6]:
        safe = t.replace('"', "")
        if safe:
            parts.append(f'"{safe}"*')
    for c in cjk_terms[:8]:
        safe = c.replace('"', "")
        if safe:
            parts.append(f'"{safe}"')
    return " OR ".join(parts)


class ProjectMemoryIndex:
    """
    Thread-safe, WAL-enabled SQLite FTS5 full-text & BM25 index scoped per project workspace.
    Stores sealed Round State Snapshots, offloaded Observation Blobs, Deliverables, Rulings, and Visual QA reports.
    """

    def __init__(self, project_path: str, base_root: Optional[str] = None):
        norm_p = os.path.normcase(os.path.abspath(project_path or "."))
        proj_hash = hashlib.sha256(norm_p.encode("utf-8", errors="replace")).hexdigest()[:16]
        root = base_root if base_root else _get_memory_base_root()
        os.makedirs(root, exist_ok=True)
        self.project_path = norm_p
        self.db_path = os.path.join(root, f"mem_{proj_hash}.sqlite3")
        self._lock = _get_db_lock(self.db_path)
        self._fts5_ok = True
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=8.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.DatabaseError:
            pass
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS docs (
                        doc_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        blob_path TEXT DEFAULT '',
                        anchors_json TEXT DEFAULT '{}',
                        content TEXT NOT NULL,
                        stage_index INTEGER DEFAULT 0,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_task ON docs(task_id, kind);")
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                            doc_id UNINDEXED,
                            title,
                            content,
                            anchors_text,
                            cjk_text,
                            tokenize='unicode61 remove_diacritics 2'
                        )
                        """
                    )
                    self._fts5_ok = True
                except sqlite3.OperationalError:
                    self._fts5_ok = False
                conn.commit()
            finally:
                conn.close()

    def upsert_doc(
        self,
        *,
        doc_id: str,
        task_id: str,
        kind: str,
        title: str,
        content: str,
        blob_path: str = "",
        anchors: Optional[Dict[str, Any]] = None,
        stage_index: int = 0,
        created_at: Optional[float] = None,
    ) -> None:
        did = str(doc_id or "").strip()
        if not did:
            return
        body = str(content or "")
        # Cap indexed raw body per doc to 32k chars so DB stays compact and sub-10ms fast
        indexed_body = body[:32000]
        anc_dict = anchors if isinstance(anchors, dict) else {}
        anc_parts: List[str] = []
        for k in ("file_refs", "blocking_lines", "numeric_claims", "verdicts"):
            vals = anc_dict.get(k)
            if isinstance(vals, list):
                anc_parts.extend(str(v) for v in vals[:20])
        anc_text = " ".join(anc_parts)
        cjk_text = _cjk_augment(f"{title}\n{anc_text}\n{indexed_body[:8000]}")
        ts = float(created_at if created_at is not None else time.time())

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO docs (doc_id, task_id, kind, title, blob_path, anchors_json, content, stage_index, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(doc_id) DO UPDATE SET
                        task_id=excluded.task_id,
                        kind=excluded.kind,
                        title=excluded.title,
                        blob_path=excluded.blob_path,
                        anchors_json=excluded.anchors_json,
                        content=excluded.content,
                        stage_index=excluded.stage_index,
                        created_at=excluded.created_at
                    """,
                    (
                        did,
                        str(task_id or ""),
                        str(kind or "doc"),
                        str(title or "")[:240],
                        str(blob_path or ""),
                        json.dumps(anc_dict, ensure_ascii=False),
                        indexed_body,
                        int(stage_index or 0),
                        ts,
                    ),
                )
                if self._fts5_ok:
                    conn.execute("DELETE FROM docs_fts WHERE doc_id = ?", (did,))
                    conn.execute(
                        "INSERT INTO docs_fts (doc_id, title, content, anchors_text, cjk_text) VALUES (?, ?, ?, ?, ?)",
                        (did, str(title or "")[:240], indexed_body, anc_text, cjk_text),
                    )
                conn.commit()
            finally:
                conn.close()

    def search(
        self,
        query: str,
        *,
        limit: int = 6,
        task_id: Optional[str] = None,
        exclude_task_id: Optional[str] = None,
        kinds: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        q = str(query or "").strip()
        if not q:
            return []
        lim = max(1, min(30, int(limit or 6)))
        match_expr = _build_fts5_match_expr(q)

        with self._lock:
            conn = self._connect()
            try:
                where_clauses: List[str] = []
                params: List[Any] = []
                if task_id:
                    where_clauses.append("d.task_id = ?")
                    params.append(task_id)
                if exclude_task_id:
                    where_clauses.append("d.task_id != ?")
                    params.append(exclude_task_id)
                if kinds:
                    placeholders = ",".join("?" for _ in kinds)
                    where_clauses.append(f"d.kind IN ({placeholders})")
                    params.extend(kinds)

                rows: List[sqlite3.Row] = []
                if self._fts5_ok and match_expr:
                    sql = (
                        "SELECT d.doc_id, d.task_id, d.kind, d.title, d.blob_path, d.anchors_json, "
                        "d.content, d.stage_index, d.created_at, bm25(docs_fts, 4.0, 1.5, 3.0, 2.0) AS rank_score "
                        "FROM docs_fts JOIN docs d ON docs_fts.doc_id = d.doc_id "
                        "WHERE docs_fts MATCH ? "
                        + ("AND " + " AND ".join(where_clauses) + " " if where_clauses else "")
                        + "ORDER BY rank_score ASC, d.created_at DESC LIMIT ?"
                    )
                    try:
                        rows = conn.execute(sql, [match_expr] + params + [lim]).fetchall()
                    except sqlite3.OperationalError:
                        rows = []

                if not rows:
                    # Fallback substring match (covers symbols/punctuation FTS5 strips)
                    like_pat = f"%{q[:80]}%"
                    sql_fb = (
                        "SELECT d.doc_id, d.task_id, d.kind, d.title, d.blob_path, d.anchors_json, "
                        "d.content, d.stage_index, d.created_at, 0.0 AS rank_score "
                        "FROM docs d WHERE (d.content LIKE ? OR d.title LIKE ? OR d.anchors_json LIKE ?) "
                        + ("AND " + " AND ".join(where_clauses) + " " if where_clauses else "")
                        + "ORDER BY d.created_at DESC LIMIT ?"
                    )
                    rows = conn.execute(sql_fb, [like_pat, like_pat, like_pat] + params + [lim]).fetchall()

                hits: List[Dict[str, Any]] = []
                q_terms = _extract_query_terms(q, max_terms=6)
                flat_needles = q_terms[0] + q_terms[1]
                for r in rows:
                    txt = str(r["content"] or "")
                    snippet = _extract_best_snippet(txt, flat_needles, max_len=360)
                    try:
                        anc = json.loads(r["anchors_json"] or "{}")
                    except Exception:
                        anc = {}
                    hits.append(
                        {
                            "doc_id": r["doc_id"],
                            "task_id": r["task_id"],
                            "kind": r["kind"],
                            "title": r["title"],
                            "blob_path": r["blob_path"],
                            "stage_index": r["stage_index"],
                            "snippet": snippet,
                            "score": round(float(r["rank_score"]), 4),
                            "anchors": anc,
                            "created_at": r["created_at"],
                        }
                    )
                return hits
            finally:
                conn.close()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                tot = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                by_kind = {
                    str(r[0]): int(r[1])
                    for r in conn.execute("SELECT kind, COUNT(*) FROM docs GROUP BY kind").fetchall()
                }
                tasks_cnt = conn.execute("SELECT COUNT(DISTINCT task_id) FROM docs").fetchone()[0]
            finally:
                conn.close()
        size_b = os.path.getsize(self.db_path) if os.path.isfile(self.db_path) else 0
        return {
            "db_path": self.db_path.replace("\\", "/"),
            "fts5_enabled": self._fts5_ok,
            "total_docs": int(tot),
            "tasks_indexed": int(tasks_cnt),
            "by_kind": by_kind,
            "size_bytes": int(size_b),
        }

    def recall_cross_task_memory(
        self,
        goal_text: str,
        current_task_id: str,
        *,
        max_chars: int = 1800,
        limit: int = 3,
    ) -> str:
        """
        Retrieve high-BM25 historical snapshots/deliverables/rulings from PREVIOUS tasks in the same project.
        Strictly tagged as historical reference (never authoritative over live files) and hard-bounded.
        """
        hits = self.search(
            goal_text,
            limit=limit,
            exclude_task_id=current_task_id,
            kinds=["snapshot", "deliverable", "ruling", "fact_check", "visual_qa"],
        )
        if not hits:
            return ""
        lines: List[str] = [
            "【跨任务项目历史记忆召回 (SQLite FTS5 BM25 · 非本轮实证，引用前须读当前文件复核)】"
        ]
        used = len(lines[0])
        for h in hits:
            ptr = f" · 详情={h['blob_path']}" if h.get("blob_path") else ""
            entry = f"- [{h['task_id']} · {h['kind']}] {h['title']}{ptr}\n  {h['snippet']}"
            if used + len(entry) + 2 > max_chars:
                avail = max_chars - used - 24
                if avail > 120:
                    lines.append(entry[:avail] + "…")
                break
            lines.append(entry)
            used += len(entry) + 2
        return "\n".join(lines) if len(lines) > 1 else ""


def _extract_best_snippet(text: str, needles: List[str], max_len: int = 360) -> str:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if len(clean) <= max_len:
        return clean
    low = clean.lower()
    best_pos = -1
    for nd in needles:
        idx = low.find(nd.lower())
        if idx != -1:
            best_pos = idx
            break
    if best_pos == -1:
        return clean[: max_len - 1] + "…"
    start = max(0, best_pos - max_len // 3)
    end = min(len(clean), start + max_len)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return prefix + clean[start:end] + suffix


# ==================== PURE-STDLIB STRUCTURAL AST REPOMAP (AIDER-INSPIRED) ====================

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
}
_JS_SYM_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+([A-Za-z_$][A-Za-z0-9_$]*)|class\s+([A-Za-z_$][A-Za-z0-9_$]*)|(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)",
    re.MULTILINE,
)
_R_SYM_RE = re.compile(r"^\s*([A-Za-z.][A-Za-z0-9_.]*)\s*(?:<-|=)\s*function\s*\(", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z0-9_.]+)\s+import|import\s+([A-Za-z0-9_.]+))", re.MULTILINE)


def _extract_file_symbols(abs_path: str, ext: str) -> Tuple[List[str], List[str]]:
    """Return (symbol_signatures, imported_module_stems) using pure stdlib AST / regex."""
    try:
        if os.path.getsize(abs_path) > 450_000:
            return [], []
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return [], []

    syms: List[str] = []
    imports: List[str] = []

    if ext == ".py":
        try:
            tree = ast.parse(src, filename=abs_path)
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module.split(".")[0])
                elif isinstance(node, ast.ClassDef):
                    methods = [
                        n.name for n in node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("__")
                    ][:5]
                    m_tail = f"({','.join(methods)})" if methods else ""
                    syms.append(f"class {node.name}{m_tail}:L{node.lineno}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_") or len(syms) < 4:
                        args = [a.arg for a in node.args.args[:4] if a.arg != "self"]
                        syms.append(f"def {node.name}({','.join(args)}):L{node.lineno}")
            return syms[:10], imports
        except SyntaxError:
            pass
        for m in _PY_IMPORT_RE.findall(src):
            mod = (m[0] or m[1]).split(".")[0]
            if mod:
                imports.append(mod)
        return syms[:8], imports

    if ext in (".js", ".ts", ".tsx", ".jsx", ".html"):
        scan_txt = src
        if ext == ".html":
            sc_blocks = re.findall(r"<script[^>]*>(.*?)</script>", src, flags=re.S | re.I)
            scan_txt = "\n".join(sc_blocks)
        for m in _JS_SYM_RE.finditer(scan_txt):
            name = m.group(1) or m.group(2) or m.group(3)
            if name and len(syms) < 12:
                ln = scan_txt.count("\n", 0, m.start()) + 1
                syms.append(f"{name}:L{ln}")
        return syms, imports

    if ext in (".r", ".rmd"):
        for m in _R_SYM_RE.finditer(src):
            name = m.group(1)
            if name and len(syms) < 10:
                ln = src.count("\n", 0, m.start()) + 1
                syms.append(f"{name}():L{ln}")
        for lib in re.findall(r"library\s*\(\s*['\"]?([A-Za-z0-9_.]+)", src):
            imports.append(lib)
        return syms, imports

    return syms, imports


def build_structural_repomap(
    root_dir: str,
    *,
    max_chars: int = 2400,
    max_files: int = 160,
    focus_terms: Optional[List[str]] = None,
) -> str:
    """
    Build a compact, centrality-ranked structural AST RepoMap (pure stdlib, Aider-inspired).
    Ranks source files by cross-file inbound import centrality + task focus term overlap.
    """
    if not root_dir or not os.path.isdir(root_dir):
        return ""
    root_real = os.path.realpath(root_dir)
    code_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".r", ".rmd"}
    data_exts = {".csv", ".tsv", ".json", ".md"}

    code_records: List[Dict[str, Any]] = []
    other_files: List[str] = []
    scanned = 0

    for cur_r, subdirs, files in os.walk(root_real):
        rel_d = os.path.relpath(cur_r, root_real)
        depth = 0 if rel_d == "." else rel_d.count(os.sep) + 1
        subdirs[:] = [d for d in sorted(subdirs) if d not in _SKIP_DIRS and not d.startswith(".")] if depth < 4 else []
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            scanned += 1
            if scanned > max_files:
                break
            abs_p = os.path.join(cur_r, fn)
            rel_p = os.path.relpath(abs_p, root_real).replace("\\", "/")
            ext = os.path.splitext(fn)[1].lower()
            if ext in code_exts:
                syms, imps = _extract_file_symbols(abs_p, ext)
                stem = os.path.splitext(fn)[0].lower()
                code_records.append(
                    {
                        "rel": rel_p,
                        "stem": stem,
                        "syms": syms,
                        "imports": imps,
                        "score": 1.0 + min(3.0, len(syms) * 0.25),
                    }
                )
            elif ext in data_exts and len(other_files) < 24:
                other_files.append(rel_p)
        if scanned > max_files:
            break

    # Cross-file import centrality boost (lightweight PageRank intuition)
    inbound: Dict[str, int] = {}
    for rec in code_records:
        for imp in rec["imports"]:
            k = imp.lower()
            inbound[k] = inbound.get(k, 0) + 1

    f_terms = [t.lower() for t in (focus_terms or []) if len(t) >= 2]
    for rec in code_records:
        rec["score"] += inbound.get(rec["stem"], 0) * 2.5
        if f_terms:
            hay = (rec["rel"] + " " + " ".join(rec["syms"])).lower()
            rec["score"] += sum(2.0 for ft in f_terms if ft in hay)

    code_records.sort(key=lambda r: (-r["score"], r["rel"]))

    lines: List[str] = ["【项目结构化 AST RepoMap（按跨文件引用中心度排序，纯标准库提取）】"]
    used = len(lines[0])
    for rec in code_records:
        sym_str = " · ".join(rec["syms"][:8]) if rec["syms"] else "(no top-level symbols)"
        row = f"- {rec['rel']}: {sym_str}"
        if used + len(row) + 1 > max_chars - 240:
            break
        lines.append(row)
        used += len(row) + 1

    if other_files and used < max_chars - 120:
        tail_row = "- 数据/文档文件: " + ", ".join(other_files[:16])
        lines.append(tail_row[: max(80, max_chars - used)])

    return "\n".join(lines) if len(lines) > 1 else ""
