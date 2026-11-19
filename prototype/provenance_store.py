"""
OpenScience Content-Addressed Provenance DAG & Experiment Run Tracker (Pure Stdlib, Lock-Free Sidecar).
Inspired directly by synthetic-sciences/openscience `tool/provenance.ts` & `tool/experiments.ts`:
- Content-addressed nodes (`source`, `run`, `artifact`, `claim`) + directed `derived-from` lineage edges
- Structured experiment runs (`baseline` vs candidate runs, primary metric + delta vs baseline + status)
- Stored strictly in sidecar files under `D:\\gemini\\tmp\\team_ledgers/<task_id>/` (zero team_state.json bloat!)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple


def _canonical_node_id(kind: str, label: str, path: str = "", sha256: str = "", extra_key: str = "") -> str:
    raw = json.dumps(
        {
            "kind": kind,
            "label": (label or "").strip(),
            "path": (path or "").replace("\\", "/").strip().lower(),
            "sha256": (sha256 or "").strip().lower(),
            "extra": (extra_key or "").strip(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    prefix = {"source": "src", "run": "run", "artifact": "art", "claim": "clm"}.get(kind, "nod")
    return f"{prefix}_{digest}"


class TaskProvenanceStore:
    """Lock-free per-task content-addressed provenance DAG & experiment ledger stored in sidecar JSON."""

    def __init__(self, ledger_root: str, task_id: str):
        self.task_dir = os.path.join(ledger_root, task_id)
        self.prov_path = os.path.join(self.task_dir, "provenance.json")
        self.exp_path = os.path.join(self.task_dir, "experiments.json")

    def _load_json(self, path: str, default_factory) -> Dict[str, Any]:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                    if isinstance(obj, dict):
                        return obj
            except Exception:
                pass
        return default_factory()

    def _write_atomic(self, path: str, payload: Dict[str, Any]) -> None:
        os.makedirs(self.task_dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)

    def load_graph(self) -> Dict[str, Any]:
        return self._load_json(self.prov_path, lambda: {"nodes": {}, "edges": [], "updated_at": 0.0})

    def load_experiments(self) -> Dict[str, Any]:
        return self._load_json(self.exp_path, lambda: {"runs": [], "updated_at": 0.0})

    def record_node(
        self,
        kind: str,
        label: str,
        artifact_type: str = "",
        path: str = "",
        sha256: str = "",
        tool: str = "",
        meta: Optional[Dict[str, Any]] = None,
        derived_from: Optional[List[str]] = None,
        extra_key: str = "",
    ) -> str:
        kind = kind if kind in ("source", "run", "artifact", "claim") else "artifact"
        g = self.load_graph()
        nid = _canonical_node_id(kind, label, path=path, sha256=sha256, extra_key=extra_key)
        now = time.time()
        existing = g["nodes"].get(nid)
        if not existing:
            g["nodes"][nid] = {
                "id": nid,
                "kind": kind,
                "label": label.strip(),
                "artifact_type": artifact_type,
                "path": path.replace("\\", "/") if path else "",
                "sha256": sha256,
                "tool": tool,
                "meta": dict(meta or {}),
                "created_at": now,
            }
        else:
            if meta:
                existing.setdefault("meta", {}).update(meta)
            if sha256 and not existing.get("sha256"):
                existing["sha256"] = sha256

        existing_edges = {(e["from"], e["to"], e.get("relation", "derived-from")) for e in g["edges"]}
        for parent_id in derived_from or []:
            if parent_id and parent_id != nid and parent_id in g["nodes"]:
                ekey = (nid, parent_id, "derived-from")
                if ekey not in existing_edges:
                    g["edges"].append({"from": nid, "to": parent_id, "relation": "derived-from", "ts": now})
                    existing_edges.add(ekey)

        g["updated_at"] = now
        self._write_atomic(self.prov_path, g)
        return nid

    def record_attempt_lineage(
        self,
        step_index: int,
        attempt_id: str,
        member_id: str,
        member_name: str,
        client: str,
        model: str,
        sandbox: str,
        final_status: str,
        readonly_snapshots: Optional[Dict[str, str]] = None,
        command_logs: Optional[List[Dict[str, Any]]] = None,
        changed_files: Optional[List[Dict[str, Any]]] = None,
        verif_report: Optional[Dict[str, Any]] = None,
        response_text: str = "",
        work_dir: str = "",
        sha_fn=None,
    ) -> Dict[str, Any]:
        """Auto-record OpenScience provenance DAG & any ```experiment_run``` / ```provenance_record``` blocks."""
        source_ids: List[str] = []
        for rel, digest in (readonly_snapshots or {}).items():
            sid = self.record_node(
                kind="source",
                label=os.path.basename(rel),
                artifact_type="dataset",
                path=rel,
                sha256=digest,
                meta={"readonly_input": True},
            )
            source_ids.append(sid)

        run_id = self.record_node(
            kind="run",
            label=f"Step #{step_index} · {member_name} ({client}/{model})",
            tool=f"{client}/{model}",
            meta={
                "step_index": step_index,
                "attempt_id": attempt_id,
                "member_id": member_id,
                "sandbox": sandbox,
                "status": final_status,
                "command_count": len(command_logs or []),
            },
            derived_from=source_ids,
            extra_key=attempt_id,
        )

        # Record changed files as artifact nodes derived from this run
        artifact_ids: List[str] = []
        for cf in changed_files or []:
            rel = str(cf.get("path") or "").strip()
            if not rel:
                continue
            ext = os.path.splitext(rel)[1].lower()
            atype = (
                "figure" if ext in (".svg", ".pdf", ".png", ".tif", ".tiff", ".jpg", ".jpeg")
                else ("dataset" if ext in (".csv", ".tsv", ".parquet", ".xlsx")
                      else ("manifest" if "manifest" in os.path.basename(rel).lower() else "code"))
            )
            digest = ""
            if sha_fn and work_dir:
                abs_p = rel if os.path.isabs(rel) else os.path.join(work_dir, rel)
                if os.path.isfile(abs_p):
                    try:
                        digest = sha_fn(abs_p)
                    except Exception:
                        digest = ""
            aid = self.record_node(
                kind="artifact",
                label=os.path.basename(rel),
                artifact_type=atype,
                path=rel,
                sha256=digest,
                meta={"change_kind": cf.get("kind", "edit"), "attempt_id": attempt_id},
                derived_from=[run_id],
            )
            artifact_ids.append(aid)

        # Record figure QA artifacts & verification verdict claims
        if isinstance(verif_report, dict):
            for fr in verif_report.get("figure_reports") or []:
                for fa in fr.get("artifacts") or []:
                    aid = self.record_node(
                        kind="artifact",
                        label=os.path.basename(fa.get("rel_path") or fa.get("path") or "figure"),
                        artifact_type="figure",
                        path=fa.get("rel_path") or "",
                        sha256=fa.get("sha256") or "",
                        meta={
                            "format": fa.get("format"),
                            "dpi_x": fa.get("dpi_x"),
                            "physical_width_mm": fa.get("physical_width_mm"),
                            "qa_ok": bool(fr.get("ok")),
                        },
                        derived_from=[run_id] + source_ids,
                    )
                    artifact_ids.append(aid)
            self.record_node(
                kind="claim",
                label=f"Mechanical Signoff ({verif_report.get('status', 'unknown')})",
                meta={
                    "ok": bool(verif_report.get("ok")),
                    "blocking_count": len(verif_report.get("blocking") or []),
                    "checks_count": len(verif_report.get("checks") or []),
                },
                derived_from=[run_id] + artifact_ids[:8],
                extra_key=f"signoff_{attempt_id}",
            )

        # Extract optional agent-authored ```provenance_record``` and ```experiment_run``` blocks
        for blk in re.findall(r"```provenance_record\s*(\{.*?\})\s*```", response_text or "", flags=re.S):
            try:
                obj = json.loads(blk)
                if isinstance(obj, dict) and obj.get("label"):
                    self.record_node(
                        kind=str(obj.get("kind") or "claim"),
                        label=str(obj["label"]),
                        artifact_type=str(obj.get("artifact_type") or ""),
                        path=str(obj.get("path") or ""),
                        meta=obj.get("meta") if isinstance(obj.get("meta"), dict) else {},
                        derived_from=[run_id],
                    )
            except Exception:
                pass

        recorded_runs = 0
        for blk in re.findall(r"```experiment_run\s*(\{.*?\})\s*```", response_text or "", flags=re.S):
            try:
                obj = json.loads(blk)
                if isinstance(obj, dict) and (obj.get("metric_name") or obj.get("metric") or obj.get("metrics")):
                    self.record_experiment_run(obj, attempt_id=attempt_id, member_id=member_id, model=model, run_node_id=run_id)
                    recorded_runs += 1
            except Exception:
                pass

        return {"run_node_id": run_id, "artifact_count": len(artifact_ids), "experiment_runs_added": recorded_runs}

    def record_experiment_run(
        self,
        raw: Dict[str, Any],
        attempt_id: str = "",
        member_id: str = "",
        model: str = "",
        run_node_id: str = "",
    ) -> Dict[str, Any]:
        """Record an OpenScience Autoresearch experiment run & auto-compute delta vs baseline."""
        exp_data = self.load_experiments()
        runs = exp_data.setdefault("runs", [])
        run_id = str(raw.get("run_id") or f"exp_{len(runs) + 1:03d}")
        is_baseline = bool(raw.get("is_baseline", False)) or (len(runs) == 0 and raw.get("priority") == 1000)
        metric_name = str(raw.get("metric_name") or "primary_metric")
        direction = str(raw.get("direction") or "maximize").lower()
        val_raw = raw.get("metric_value") if "metric_value" in raw else raw.get("metric")
        try:
            metric_val = float(val_raw) if val_raw is not None else None
        except Exception:
            metric_val = None

        # Compute delta vs baseline run on same metric_name
        base_run = next((r for r in runs if r.get("is_baseline") and r.get("metric_name") == metric_name), None)
        delta_vs_base: Optional[float] = None
        if not is_baseline and base_run is not None and metric_val is not None and base_run.get("metric_value") is not None:
            delta_vs_base = round(metric_val - float(base_run["metric_value"]), 6)

        rec = {
            "run_id": run_id,
            "is_baseline": is_baseline,
            "hypothesis": str(raw.get("hypothesis") or raw.get("label") or ""),
            "metric_name": metric_name,
            "metric_value": metric_val,
            "direction": direction,
            "delta_vs_baseline": delta_vs_base,
            "status": str(raw.get("status") or ("baseline" if is_baseline else "completed")),
            "script_path": str(raw.get("script_path") or ""),
            "notes": str(raw.get("notes") or ""),
            "attempt_id": attempt_id,
            "member_id": member_id,
            "model": model,
            "provenance_node_id": run_node_id,
            "ts": time.time(),
        }
        runs.append(rec)
        exp_data["updated_at"] = time.time()
        self._write_atomic(self.exp_path, exp_data)
        return rec

    def get_summary(self) -> Dict[str, Any]:
        g = self.load_graph()
        exp = self.load_experiments()
        nodes_list = sorted(g.get("nodes", {}).values(), key=lambda x: x.get("created_at", 0.0))
        return {
            "nodes": nodes_list,
            "edges": g.get("edges", []),
            "node_count": len(nodes_list),
            "edge_count": len(g.get("edges", [])),
            "experiments": exp.get("runs", []),
            "updated_at": max(g.get("updated_at", 0.0), exp.get("updated_at", 0.0)),
        }
