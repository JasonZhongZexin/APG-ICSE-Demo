# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Global Summary Builder MVP.

Purpose:
- Aggregate all Local Summaries from one retrieval episode.
- Produce a patch-ready global context.
- Keep deterministic behavior for regression tests and paper deadline.

Scope:
- One RetrievalState -> one Global Summary.
- Does not call LLM.
- Does not execute new graph queries.
- Does not generate a patch.
"""

import json
from typing import Any, Dict, List, Optional


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    try:
        return dict(obj)
    except Exception:
        pass
    return {"repr": repr(obj)}


def _safe_json_key(x: Any) -> str:
    try:
        return json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _dedup(xs: List[Any], limit: int = 50) -> List[Any]:
    seen = set()
    out = []
    for x in xs:
        k = _safe_json_key(x)
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
        if len(out) >= limit:
            break
    return out


def _truncate(xs: List[Any], limit: int = 50) -> List[Any]:
    if len(xs) <= limit:
        return xs
    return xs[:limit] + [{"truncated": True, "remaining": len(xs) - limit}]


def _extract_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    state.artifacts.local_summaries currently stores:
      {"task_id": ..., "graph_type": ..., "summary": {...}}

    This helper also supports direct summary objects for future compatibility.
    """
    if not isinstance(item, dict):
        return {}
    if isinstance(item.get("summary"), dict):
        return item["summary"]
    return item


class GlobalSummaryBuilder(object):
    def __init__(self, max_items: int = 50):
        self.max_items = int(max_items)

    def _collect_local_summaries(self, state: Any) -> List[Dict[str, Any]]:
        artifacts = _get(state, "artifacts", {})
        local_summaries = _get(artifacts, "local_summaries", [])
        if local_summaries is None:
            return []
        return list(local_summaries)

    def _collect_subgraphs(self, state: Any) -> List[Dict[str, Any]]:
        artifacts = _get(state, "artifacts", {})
        subgraphs = _get(artifacts, "subgraphs", [])
        if subgraphs is None:
            return []
        return list(subgraphs)

    def _retrieval_accounting(self, state: Any, local_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        counters = _get(state, "counters", {})
        budgets = _get(state, "budgets", {})

        graphs_touched = []
        for item in local_items:
            s = _extract_summary(item)
            gt = s.get("graph_type") or item.get("graph_type")
            if gt:
                graphs_touched.append(str(gt))

        return {
            "attempt_id": _get(state, "attempt_id"),
            "steps_executed": len(local_items),
            "queries_total": _get(counters, "queries_total", 0),
            "queries_by_graph_type": dict(_get(counters, "queries_by_graph_type", {}) or {}),
            "replans": _get(counters, "replans", 0),
            "depth_increases": dict(_get(counters, "depth_increases", {}) or {}),
            "graphs_touched": _dedup(graphs_touched, self.max_items),
            "budgets": {
                "max_queries_total": _get(budgets, "max_queries_total"),
                "max_replans": _get(budgets, "max_replans"),
                "max_depth_increases_per_task": _get(budgets, "max_depth_increases_per_task"),
            },
        }

    def _aggregate_signals(self, local_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        coverages = []
        objective_met_any = False
        insufficient_context_any = False
        hit_cap_any = False
        records_count_total = 0
        num_nodes_total = 0
        num_edges_total = 0

        for item in local_items:
            s = _extract_summary(item)
            signals = s.get("signals", {}) if isinstance(s, dict) else {}

            if signals.get("objective_met") is True or s.get("objective_met") is True:
                objective_met_any = True

            if signals.get("insufficient_context") is True or s.get("insufficient_context") is True:
                insufficient_context_any = True

            if signals.get("hit_cap") is True:
                hit_cap_any = True

            cov = signals.get("coverage")
            try:
                coverages.append(float(cov))
            except Exception:
                pass

            try:
                records_count_total += int(signals.get("records_count") or 0)
            except Exception:
                pass

            try:
                num_nodes_total += int(signals.get("num_nodes") or 0)
            except Exception:
                pass

            try:
                num_edges_total += int(signals.get("num_edges") or 0)
            except Exception:
                pass

        best_coverage = max(coverages) if coverages else None
        avg_coverage = (sum(coverages) / len(coverages)) if coverages else None

        return {
            "objective_met_any": bool(objective_met_any),
            "insufficient_context_any": bool(insufficient_context_any),
            "hit_cap_any": bool(hit_cap_any),
            "best_coverage": best_coverage,
            "avg_coverage": avg_coverage,
            "records_count_total": records_count_total,
            "num_nodes_total": num_nodes_total,
            "num_edges_total": num_edges_total,
        }

    def _merge_evidence(self, local_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        functions: List[Any] = []
        variables: List[Any] = []
        nodes: List[Any] = []
        edges: List[Any] = []
        paths: List[Any] = []

        for item in local_items:
            s = _extract_summary(item)
            sem = s.get("semantic_summary", {}) if isinstance(s, dict) else {}
            entities = sem.get("entities", {}) if isinstance(sem, dict) else {}

            functions.extend(entities.get("functions", []) or [])
            variables.extend(entities.get("variables", []) or [])
            nodes.extend(entities.get("nodes", []) or [])
            edges.extend(entities.get("edges", []) or [])
            paths.extend(sem.get("paths", []) or [])

        return {
            "functions": _dedup(functions, self.max_items),
            "variables": _dedup(variables, self.max_items),
            "nodes": _dedup(nodes, self.max_items),
            "edges": _dedup(edges, self.max_items),
            "paths": _dedup(paths, 20),
        }

    def _derive_gaps(
        self,
        state: Any,
        local_items: List[Dict[str, Any]],
        aggregated_signals: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        gaps: List[Dict[str, Any]] = []

        stop_reason = _get(state, "stop_reason")

        if stop_reason == "QUERY_BUDGET_EXHAUSTED":
            gaps.append({
                "type": "query_budget_exhausted",
                "detail": "Retrieval stopped because the per-patch query budget was exhausted before the objective was met.",
            })

        if stop_reason == "REPLAN_LIMIT_REACHED":
            gaps.append({
                "type": "replan_limit_reached",
                "detail": "Retrieval stopped because the replan budget was exhausted.",
            })

        if stop_reason == "DEPTH_INCREASE_LIMIT_REACHED":
            gaps.append({
                "type": "depth_increase_limit_reached",
                "detail": "Retrieval stopped because the depth-increase budget for the current task was exhausted.",
            })

        if not aggregated_signals.get("objective_met_any"):
            gaps.append({
                "type": "objective_not_met",
                "detail": "No local summary reported that the retrieval objective was fully satisfied.",
            })

        if aggregated_signals.get("hit_cap_any"):
            gaps.append({
                "type": "result_cap_hit",
                "detail": "At least one query result hit a cap; the returned graph fragment may be incomplete.",
            })

        # Merge local next suggestions as global gaps.
        for item in local_items:
            s = _extract_summary(item)
            sid = s.get("summary_id")
            for sug in s.get("next_suggestions", []) or []:
                if not isinstance(sug, dict):
                    continue
                gaps.append({
                    "type": "local_suggestion",
                    "from": sid,
                    "action": sug.get("action"),
                    "detail": sug.get("reason"),
                })

        return _dedup(gaps, 30)

    def _build_fix_context_markdown(
        self,
        state: Any,
        accounting: Dict[str, Any],
        aggregated_signals: Dict[str, Any],
        evidence_pool: Dict[str, Any],
        gaps: List[Dict[str, Any]],
        local_items: List[Dict[str, Any]],
    ) -> str:
        stop_reason = _get(state, "stop_reason") or "UNKNOWN"

        lines: List[str] = []
        lines.append("### CodeGraphHunter Global Retrieval Summary")
        lines.append("")
        lines.append("**Attempt ID:** %s" % str(_get(state, "attempt_id")))
        lines.append("**Retrieval stop reason:** %s" % stop_reason)
        lines.append("**Queries executed:** %s" % str(accounting.get("queries_total")))
        lines.append("**Graphs touched:** %s" % (", ".join(accounting.get("graphs_touched", [])) or "None"))
        lines.append("")

        lines.append("### Aggregated Signals")
        lines.append("- Objective met by any local summary: `%s`" % str(aggregated_signals.get("objective_met_any")))
        lines.append("- Best coverage: `%s`" % str(aggregated_signals.get("best_coverage")))
        lines.append("- Hit result cap: `%s`" % str(aggregated_signals.get("hit_cap_any")))
        lines.append("- Total summarized records: `%s`" % str(aggregated_signals.get("records_count_total")))
        lines.append("")

        lines.append("### Evidence Pool")

        functions = evidence_pool.get("functions", []) or []
        variables = evidence_pool.get("variables", []) or []
        paths = evidence_pool.get("paths", []) or []

        if functions:
            lines.append("**Functions:**")
            for x in functions[:20]:
                lines.append("- `%s`" % str(x))
        else:
            lines.append("**Functions:** None found in returned records.")

        if variables:
            lines.append("")
            lines.append("**Variables:**")
            for x in variables[:20]:
                lines.append("- `%s`" % str(x))
        else:
            lines.append("")
            lines.append("**Variables:** None found in returned records.")

        if paths:
            lines.append("")
            lines.append("**Paths:**")
            for p in paths[:10]:
                if isinstance(p, dict):
                    txt = p.get("text") or json.dumps(p, ensure_ascii=False, default=str)
                else:
                    txt = str(p)
                lines.append("- %s" % txt)
        else:
            lines.append("")
            lines.append("**Paths:** No full path was returned. Endpoint-only or metadata-only summaries cannot reconstruct intermediate graph steps.")

        lines.append("")
        lines.append("### Local Summaries")
        for item in local_items[:20]:
            s = _extract_summary(item)
            sid = s.get("summary_id", "LS-UNKNOWN")
            gt = s.get("graph_type", item.get("graph_type", "UNKNOWN"))
            result_kind = s.get("result_kind", "unknown")
            sem = s.get("semantic_summary", {}) if isinstance(s, dict) else {}
            desc = sem.get("description", "")
            lines.append("- `%s` [%s/%s]: %s" % (sid, gt, result_kind, desc))

        lines.append("")
        lines.append("### Gaps / Limitations")
        if gaps:
            for g in gaps[:20]:
                lines.append("- `%s`: %s" % (g.get("type"), g.get("detail")))
        else:
            lines.append("- No major retrieval gap was recorded.")

        return "\n".join(lines)

    def build(self, state: Any) -> Dict[str, Any]:
        local_items = self._collect_local_summaries(state)
        subgraphs = self._collect_subgraphs(state)

        accounting = self._retrieval_accounting(state, local_items)
        aggregated = self._aggregate_signals(local_items)
        evidence = self._merge_evidence(local_items)
        gaps = self._derive_gaps(state, local_items, aggregated)

        markdown = self._build_fix_context_markdown(
            state=state,
            accounting=accounting,
            aggregated_signals=aggregated,
            evidence_pool=evidence,
            gaps=gaps,
            local_items=local_items,
        )

        summary_ids = []
        for item in local_items:
            s = _extract_summary(item)
            if s.get("summary_id"):
                summary_ids.append(s.get("summary_id"))

        gs = {
            "global_summary_id": "GS-%04d" % int(_get(state, "attempt_id", 1) or 1),
            "attempt_id": _get(state, "attempt_id"),
            "stop_reason": _get(state, "stop_reason"),
            "stop_detail": _as_dict(_get(state, "stop_detail", {})),

            "retrieval_accounting": accounting,
            "aggregated_signals": aggregated,
            "evidence_pool": evidence,
            "gaps": gaps,

            "fix_context": {
                "markdown": markdown,
                "compact_json": {
                    "stop_reason": _get(state, "stop_reason"),
                    "objective_met_any": aggregated.get("objective_met_any"),
                    "graphs_touched": accounting.get("graphs_touched"),
                    "best_coverage": aggregated.get("best_coverage"),
                    "functions": evidence.get("functions", []),
                    "variables": evidence.get("variables", []),
                    "path_count": len(evidence.get("paths", []) or []),
                    "gap_types": [g.get("type") for g in gaps],
                },
            },

            "local_summary_refs": _dedup(summary_ids, self.max_items),
            "subgraph_count": len(subgraphs),
        }

        return gs