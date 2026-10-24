# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Local Summary Builder MVP.

Purpose:
- Convert one retrieval step result into a graph-aware local semantic summary.
- Use graph_semantics_catalog.v1.json to avoid hallucinating graph semantics.
- Keep output deterministic for regression and paper deadline.

Important design:
- One executed query -> one Local Summary.
- If records contain a path variable (e.g., p), summarize path-like evidence.
- If records only contain endpoints (e.g., n, m), summarize endpoint reachability.
- Do NOT invent intermediate nodes/edges when the query did not return a path.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple


def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    if isinstance(d, dict):
        return d.get(key, default)
    return getattr(d, key, default)


def _as_dict(obj: Any) -> Dict[str, Any]:
    """
    Best-effort conversion for records/nodes/relationships returned by different Bolt clients.
    TuGraph/Neo4j clients may return dict-like objects, plain dicts, or custom objects.
    """
    if obj is None:
        return {}

    if isinstance(obj, dict):
        return dict(obj)

    # Neo4j Node/Relationship style: dict(obj) may work.
    try:
        return dict(obj)
    except Exception:
        pass

    out = {}
    for k in (
        "id",
        "kind",
        "labels",
        "label",
        "type",
        "source_loc",
        "fun_name",
        "val_name",
        "arg_no",
        "fun_obj_var_id",
        "icfg_node_id",
        "cg_node_id",
        "call_graph_node_id",
        "svf_type_id",
        "edge_id",
    ):
        try:
            v = getattr(obj, k)
            out[k] = v
        except Exception:
            pass

    if out:
        return out

    return {"repr": repr(obj)}


def _labels_of(obj: Any) -> List[str]:
    d = _as_dict(obj)

    labels = d.get("labels")
    if isinstance(labels, (list, tuple, set)):
        return [str(x) for x in labels]

    label = d.get("label")
    if label:
        return [str(label)]

    # Some graph clients expose labels as attribute.
    try:
        labels_attr = getattr(obj, "labels")
        if isinstance(labels_attr, (list, tuple, set)):
            return [str(x) for x in labels_attr]
    except Exception:
        pass

    # Edge type fallback.
    typ = d.get("type")
    if typ:
        return [str(typ)]

    return []


def _entity_name(obj: Any) -> str:
    d = _as_dict(obj)
    labels = _labels_of(obj)

    for k in ("fun_name", "val_name", "name", "source_loc"):
        v = d.get(k)
        if v not in (None, "", "null"):
            return str(v)

    node_id = d.get("id", d.get("edge_id", None))
    if labels and node_id is not None:
        return "%s#%s" % (labels[0], str(node_id))
    if node_id is not None:
        return "#%s" % str(node_id)

    return str(d.get("repr", repr(obj)))


def _short_node(obj: Any) -> Dict[str, Any]:
    d = _as_dict(obj)
    labels = _labels_of(obj)

    out = {
        "label": labels[0] if labels else d.get("label"),
        "id": d.get("id"),
        "name": _entity_name(obj),
    }

    for k in (
        "fun_name",
        "val_name",
        "source_loc",
        "arg_no",
        "fun_obj_var_id",
        "icfg_node_id",
        "cg_node_id",
        "call_graph_node_id",
        "svf_type_id",
    ):
        if k in d and d.get(k) not in (None, ""):
            out[k] = d.get(k)

    return {k: v for k, v in out.items() if v is not None}


def _short_edge(obj: Any) -> Dict[str, Any]:
    d = _as_dict(obj)
    labels = _labels_of(obj)

    out = {
        "label": labels[0] if labels else d.get("label") or d.get("type"),
        "edge_id": d.get("edge_id"),
        "kind": d.get("kind"),
    }

    for k in (
        "csid",
        "direct_call_set",
        "indirect_call_set",
        "icfg_node_id",
        "bb_id",
        "call_icfg_node_id",
        "fun_entry_icfg_node_id",
        "fun_exit_icfg_node_id",
        "condition_svf_var_node_id",
        "condition_var_id",
        "branch_cond_val",
        "call_pe_ids",
        "ret_pe_id",
    ):
        if k in d and d.get(k) not in (None, ""):
            out[k] = d.get(k)

    return {k: v for k, v in out.items() if v is not None}

def _clean_edge_label_token(x: Any) -> Optional[str]:
    """
    TuGraph path may return edge labels as string tokens, e.g. "'StoreStmt'".
    Normalize them into edge labels.
    """
    if x is None:
        return None

    if not isinstance(x, str):
        return None

    s = x.strip()
    if not s:
        return None

    # TuGraph preview may show edge labels with quotes inside the string.
    s = s.strip("'").strip('"').strip()
    if not s:
        return None

    return s


def _looks_like_edge_label_token(x: Any) -> bool:
    """
    Detect path items that are actually edge labels rather than graph nodes.
    This is intentionally conservative.
    """
    label = _clean_edge_label_token(x)
    if not label:
        return False

    known_suffixes = (
        "Stmt",
        "CFGEdge",
        "CallGraphEdge",
        "Edge",
        "PE",
    )

    if label.endswith(known_suffixes):
        return True

    known_labels = {
        "AddrStmt",
        "CopyStmt",
        "StoreStmt",
        "LoadStmt",
        "GepStmt",
        "CallPE",
        "RetPE",
        "PhiStmt",
        "SelectStmt",
        "CmpStmt",
        "BinaryOPStmt",
        "UnaryOPStmt",
        "BranchStmt",
        "IntraCFGEdge",
        "CallCFGEdge",
        "RetCFGEdge",
        "CallGraphEdge",
    }

    return label in known_labels

def _truncate_list(xs: List[Any], limit: int = 30) -> List[Any]:
    if len(xs) <= limit:
        return xs
    return xs[:limit] + [{"truncated": True, "remaining": len(xs) - limit}]


class LocalSummaryBuilder(object):
    def __init__(self, catalog_path: str):
        self.catalog_path = catalog_path
        self.catalog = self._load_catalog(catalog_path)

    def _load_catalog(self, catalog_path: str) -> Dict[str, Any]:
        if not catalog_path:
            return {}

        if not os.path.isfile(catalog_path):
            raise FileNotFoundError("Graph semantics catalog not found: %s" % catalog_path)

        with open(catalog_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _graph_key(self, graph_type: str) -> str:
        gt = str(graph_type or "").strip()
        up = gt.upper()

        if up == "CALLGRAPH":
            return "CALLGRAPH"
        if up in ("PAG", "ICFG", "CHG", "BASICBLOCK"):
            return up
        if gt in self.catalog.get("graphs", {}):
            return gt
        return up

    def _graph_catalog(self, graph_type: str) -> Dict[str, Any]:
        key = self._graph_key(graph_type)
        return self.catalog.get("graphs", {}).get(key, {})

    def _detect_result_kind(self, records: List[Dict[str, Any]], explicit: Optional[str]) -> str:
        if explicit:
            return explicit

        if not records:
            return "metadata_only"

        for rec in records:
            if not isinstance(rec, dict):
                continue
            for key in ("p", "path", "paths"):
                if key in rec and rec.get(key) not in (None, [], ""):
                    return "path"

        # Common endpoint form: RETURN n, m
        has_endpoint = False
        for rec in records:
            if isinstance(rec, dict) and ("n" in rec or "m" in rec):
                has_endpoint = True
                break

        if has_endpoint:
            return "endpoint_reachability"

        return "records"

    def _extract_path_like(self, value: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
        """
        Best-effort extraction from path-like objects.

        Supported shapes:
        - {"nodes": [...], "edges": [...]}
        - {"relationships": [...], "nodes": [...]}
        - list of node/edge objects
        - TuGraph mixed path list:
            [node, "'StoreStmt'", node, "'LoadStmt'", node]
        """
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        path_parts: List[str] = []

        if value is None:
            return nodes, edges, ""

        if isinstance(value, dict):
            raw_nodes = value.get("nodes") or value.get("vertices") or []
            raw_edges = value.get("edges") or value.get("relationships") or value.get("rels") or []

            if isinstance(raw_nodes, list):
                nodes = [_short_node(x) for x in raw_nodes]
            if isinstance(raw_edges, list):
                edges = [_short_edge(x) for x in raw_edges]

            # Build readable path text from nodes/edges if possible.
            if raw_nodes or raw_edges:
                node_names = [_safe_get(n, "name", None) for n in nodes]
                node_names = [str(x) for x in node_names if x not in (None, "")]
                edge_labels = [_safe_get(e, "label", None) for e in edges]
                edge_labels = [str(x) for x in edge_labels if x not in (None, "")]

                if edge_labels and len(edge_labels) >= len(node_names) - 1:
                    for i, node_name in enumerate(node_names):
                        path_parts.append(node_name)
                        if i < len(edge_labels):
                            path_parts.append("--%s-->" % edge_labels[i])
                    return nodes, edges, " ".join(path_parts)

                return nodes, edges, " -> ".join(node_names) if node_names else ""

        elif isinstance(value, list):
            # TuGraph often returns path as an alternating mixed list:
            #   node, edge_label_string, node, edge_label_string, node
            for item in value:
                if _looks_like_edge_label_token(item):
                    label = _clean_edge_label_token(item)
                    edge = {
                        "label": label,
                        "name": label,
                        "kind": label,
                    }
                    edges.append(edge)
                    path_parts.append("--%s-->" % label)
                    continue

                d = _as_dict(item)

                # Relationship-like dict/object.
                if (
                    "edge_id" in d
                    or d.get("type")
                    or d.get("label") in {
                        "AddrStmt",
                        "CopyStmt",
                        "StoreStmt",
                        "LoadStmt",
                        "GepStmt",
                        "CallPE",
                        "RetPE",
                        "PhiStmt",
                        "SelectStmt",
                        "CmpStmt",
                        "BinaryOPStmt",
                        "UnaryOPStmt",
                        "BranchStmt",
                        "IntraCFGEdge",
                        "CallCFGEdge",
                        "RetCFGEdge",
                        "CallGraphEdge",
                    }
                    or ("kind" in d and "id" not in d)
                ):
                    edge = _short_edge(item)
                    edges.append(edge)
                    label = edge.get("label") or edge.get("kind") or edge.get("name") or "edge"
                    path_parts.append("--%s-->" % str(label))
                else:
                    node = _short_node(item)
                    nodes.append(node)
                    name = node.get("name")
                    if name not in (None, ""):
                        path_parts.append(str(name))

            # Clean spacing around arrows.
            path_text = " ".join(path_parts).replace(" -->", "-->").replace("--> ", "-->")
            return nodes, edges, path_text

        node_names = [_safe_get(n, "name", None) for n in nodes]
        node_names = [str(x) for x in node_names if x not in (None, "")]
        path_text = " -> ".join(node_names) if node_names else ""

        return nodes, edges, path_text

    def _extract_evidence(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        paths: List[Dict[str, Any]] = []
        functions: List[str] = []
        variables: List[str] = []

        for rec in records or []:
            if not isinstance(rec, dict):
                continue

            # path evidence
            for key in ("p", "path", "paths"):
                if key in rec and rec.get(key) is not None:
                    raw = rec.get(key)
                    if isinstance(raw, list) and key == "paths":
                        for one in raw:
                            ns, es, txt = self._extract_path_like(one)
                            paths.append({
                                "text": txt,
                                "nodes": ns,
                                "edges": es,
                            })
                            nodes.extend(ns)
                            edges.extend(es)
                    else:
                        ns, es, txt = self._extract_path_like(raw)
                        paths.append({
                            "text": txt,
                            "nodes": ns,
                            "edges": es,
                        })
                        nodes.extend(ns)
                        edges.extend(es)

            # endpoint evidence
            for key in ("n", "m", "src", "dst", "caller", "callee"):
                if key in rec and rec.get(key) is not None:
                    node = _short_node(rec.get(key))
                    nodes.append(node)

            # direct scalar properties in records
            for key, value in rec.items():
                if key in ("n", "m", "src", "dst", "caller", "callee", "p", "path", "paths"):
                    continue

                if isinstance(value, dict):
                    d = _as_dict(value)
                    if "fun_name" in d or "val_name" in d or "id" in d:
                        nodes.append(_short_node(value))

                if key.endswith("fun_name") or key == "fun_name":
                    if value not in (None, ""):
                        functions.append(str(value))

                if key.endswith("val_name") or key == "val_name":
                    if value not in (None, ""):
                        variables.append(str(value))

        for n in nodes:
            fn = n.get("fun_name")
            vn = n.get("val_name")
            label = n.get("label")
            name = n.get("name")

            if fn:
                functions.append(str(fn))
            elif label in ("CallGraphNode", "FunObjVar", "FunValVar") and name:
                functions.append(str(name))

            if vn:
                variables.append(str(vn))
            elif label and ("Var" in str(label)) and name:
                variables.append(str(name))

        # de-duplicate while preserving order
        def dedup(xs: List[Any]) -> List[Any]:
            seen = set()
            out = []
            for x in xs:
                key = json.dumps(x, sort_keys=True, ensure_ascii=False) if isinstance(x, dict) else str(x)
                if key not in seen:
                    seen.add(key)
                    out.append(x)
            return out

        return {
            "nodes": _truncate_list(dedup(nodes), 30),
            "edges": _truncate_list(dedup(edges), 30),
            "paths": _truncate_list(dedup(paths), 10),
            "functions": _truncate_list(dedup(functions), 30),
            "variables": _truncate_list(dedup(variables), 30),
        }

    def _description(
        self,
        graph_type: str,
        result_kind: str,
        subgraph_meta: Dict[str, Any],
        evidence: Dict[str, Any],
    ) -> str:
        graph_key = self._graph_key(graph_type)
        coverage = subgraph_meta.get("coverage")
        depth = subgraph_meta.get("depth")
        records_count = subgraph_meta.get("records_count", None)

        if graph_key == "CALLGRAPH":
            if result_kind == "path" and evidence.get("paths"):
                return (
                    "The query returned CallGraph path evidence. Interpret each path as a caller-to-callee "
                    "chain. Function names from CallGraphNode.fun_name should be preferred when available."
                )
            if result_kind == "endpoint_reachability":
                return (
                    "The query returned CallGraph endpoint reachability evidence. It supports that the seed "
                    "function node can reach returned callee/caller endpoint nodes within the queried depth, "
                    "but intermediate call chain nodes were not returned."
                )
            return (
                "The query returned CallGraph-level evidence about function call relationships. "
                "No complete call path was reconstructed unless a path variable was returned."
            )

        if graph_key == "PAG":
            if result_kind == "path" and evidence.get("paths"):
                return (
                    "The query returned PAG path evidence. Interpret it as value/object propagation through "
                    "SVF assignment, address, load, store, field-access, call-parameter, or return-parameter edges."
                )
            if result_kind == "endpoint_reachability":
                return (
                    "The query returned PAG endpoint reachability evidence. It indicates that returned endpoint "
                    "nodes are reachable from the seed in the Program Assignment Graph within the queried depth. "
                    "Because no path variable was returned, intermediate data-flow steps are not reconstructed."
                )
            return (
                "The query returned PAG metadata or endpoint records. Interpret them as pointer/value/object-flow "
                "evidence only when supported by concrete returned nodes or edges."
            )

        if graph_key == "ICFG":
            if result_kind == "path" and evidence.get("paths"):
                return (
                    "The query returned ICFG path evidence. Interpret it as an interprocedural control-flow path "
                    "through intra-procedural, call, and return control-flow edges."
                )
            if result_kind == "endpoint_reachability":
                return (
                    "The query returned ICFG endpoint reachability evidence. It supports control-flow reachability "
                    "within the queried depth, but intermediate execution steps were not returned."
                )
            return (
                "The query returned ICFG-level evidence about execution/control-flow context."
            )

        return (
            "The query returned graph evidence for %s. The summary is restricted to returned records and metadata."
            % str(graph_type)
        )

    def _next_suggestions(
        self,
        graph_type: str,
        result_kind: str,
        signals: Dict[str, Any],
        subgraph_meta: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if signals.get("objective_met"):
            return []

        out: List[Dict[str, Any]] = []

        if result_kind == "endpoint_reachability":
            out.append({
                "action": "return_path",
                "reason": "Only endpoints were returned. Return a path variable such as p to enable path-level semantic summarization.",
            })

        if subgraph_meta.get("hit_cap"):
            out.append({
                "action": "narrow_query",
                "reason": "The query appears to hit a result cap. Add labels, edge types, or smaller depth.",
            })

        depth = subgraph_meta.get("depth")
        try:
            depth_int = int(depth)
            out.append({
                "action": "deepen_or_replan",
                "reason": "Objective was not met. Deeper exploration or a different graph may be needed.",
                "current_depth": depth_int,
            })
        except Exception:
            out.append({
                "action": "replan",
                "reason": "Objective was not met and depth information is unavailable.",
            })

        return out

    def build(
        self,
        graph_type: str,
        task_id: str,
        step_index: int,
        query_plan: Dict[str, Any],
        subgraph_meta: Dict[str, Any],
        records: Optional[List[Dict[str, Any]]] = None,
        cypher: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        result_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        records = records or []
        subgraph_meta = dict(subgraph_meta or {})
        subgraph_meta.setdefault("records_count", len(records))

        graph_key = self._graph_key(graph_type)
        graph_catalog = self._graph_catalog(graph_type)

        detected_kind = self._detect_result_kind(records, result_kind)
        evidence = self._extract_evidence(records)

        coverage = float(subgraph_meta.get("coverage", 0.0) or 0.0)
        hit_cap = bool(subgraph_meta.get("hit_cap", False))

        objective = ""
        if isinstance(query_plan, dict):
            objective = str(query_plan.get("objective") or "")

        # v1.1: graph-localizer seeded retrieval should be considered sufficient
        # when it returns concrete path evidence from the selected graph seed.
        # The previous coverage-only rule marked real, targeted TuGraph paths as
        # insufficient merely because the result set was intentionally small.
        targeted_objectives = {
            "targeted_pag_argument_flow",
            "targeted_icfg_control_flow",
            "targeted_callgraph_neighborhood",
        }
        seeded_path_hit = (
            objective in targeted_objectives
            and detected_kind == "path"
            and bool(evidence.get("paths"))
            and not hit_cap
        )

        # Keep the old fake_local_summarizer semantics for generic controller
        # compatibility, but let targeted graph-localizer evidence satisfy the
        # objective even when coverage is low.
        objective_met = ((coverage >= 0.65) and (not hit_cap)) or seeded_path_hit
        if seeded_path_hit:
            coverage = max(coverage, 0.75)
        insufficient_context = not objective_met

        signals = {
            "objective_met": bool(objective_met),
            "insufficient_context": bool(insufficient_context),
            "coverage": coverage,
            "hit_cap": hit_cap,
            "depth": subgraph_meta.get("depth"),
            "records_count": len(records),
            "num_nodes": subgraph_meta.get("num_nodes"),
            "num_edges": subgraph_meta.get("num_edges"),
        }

        desc = self._description(graph_key, detected_kind, subgraph_meta, evidence)

        local_summary = {
            "summary_id": "LS-%04d" % int(step_index),
            "task_id": str(task_id),
            "graph_type": str(graph_key),
            "step_index": int(step_index),
            "result_kind": detected_kind,

            "semantic_summary": {
                "description": desc,
                "paths": evidence.get("paths", []),
                "entities": {
                    "functions": evidence.get("functions", []),
                    "variables": evidence.get("variables", []),
                    "nodes": evidence.get("nodes", []),
                    "edges": evidence.get("edges", []),
                },
            },

            "signals": signals,

            "catalog_context": {
                "graph_description": graph_catalog.get("description"),
                "summary_policy_available": bool(self.catalog.get("summary_policy")),
                "property_semantics_available": bool(graph_catalog.get("property_semantics")),
            },

            "query_context": {
                "plan": query_plan or {},
                "cypher": cypher,
                "params": params or {},
            },

            "next_suggestions": self._next_suggestions(graph_key, detected_kind, signals, subgraph_meta),

            # Backward-compatible top-level aliases.
            # Existing controller code can still call summary.get("objective_met").
            "objective_met": bool(objective_met),
            "insufficient_context": bool(insufficient_context),
        }

        return local_summary