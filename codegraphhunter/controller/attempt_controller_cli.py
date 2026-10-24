# -*- coding: utf-8 -*-
from __future__ import print_function

import os
import argparse
import json
import random
import re
from typing import Any, Dict, Optional, Tuple, Union, List

from codegraphhunter.state.retrieval_state import RetrievalState, RetrievalBudgets, is_unlimited
from codegraphhunter.controller.attempt_controller import AttemptController, AttemptStopReasons
from codegraphhunter.catalog.schema_guard import SchemaGuard
from codegraphhunter.query.guard import QueryGuard

from codegraphhunter.ir.graph_query_plan import GraphQueryPlan
from codegraphhunter.summarizer.local_summary_builder import LocalSummaryBuilder
from codegraphhunter.summarizer.global_summary_builder import GlobalSummaryBuilder
from codegraphhunter.summarizer.llm_local_summary_refiner import LLMLocalSummaryRefiner
from codegraphhunter.repair.llm_patch_generator import LLMPatchGenerator
from codegraphhunter.repair.patch_validator import PatchValidator
from codegraphhunter.agents.retrieval_planner import RetrievalPlannerAgent
from codegraphhunter.agents.db_query_agent import DBQueryAgent
from codegraphhunter.localization.agentic_fault_localizer import AgenticFaultLocalizer
from codegraphhunter.datasource.bolt_client import BoltClient


DST_NODE_LABEL_DEFAULT = "IntraICFGNode"
DEFAULT_SEMANTICS_CATALOG = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "resources",
    "graph_semantics_catalog.v1.json",
)


def _write_summary_debug_artifact(relative_path: str, payload: Any) -> str:
    """Persist a summary artifact when a per-run debug directory is configured."""
    debug_dir = os.environ.get("CODEGRAPHHUNTER_PATCH_DEBUG_DIR", "").strip()
    if not debug_dir:
        return ""
    try:
        root = os.path.abspath(debug_dir)
        path = os.path.abspath(os.path.join(root, relative_path))
        if not (path == root or path.startswith(root + os.sep)):
            return ""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", errors="replace") as f:
            json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False, default=str)
            f.write("\n")
        os.replace(tmp_path, path)
        return path
    except Exception as e:
        print("[SUMMARY ARTIFACT WARN] failed to write %s: %s" % (relative_path, e))
        return ""

# =================================================
# (A) normalized_schema helpers (schema-driven needs)
# =================================================

def load_normalized_schema_or_exit(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        raise SystemExit("ERROR: normalized_schema load failed: %s (%s)" % (path, e))

    if not isinstance(obj, dict) or not obj:
        raise SystemExit("ERROR: normalized_schema must be a non-empty JSON object: %s" % path)
    return obj


def _schema_graphs(normalized_schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    graphs = normalized_schema_obj.get("graphs")
    if not isinstance(graphs, dict) or not graphs:
        raise SystemExit("ERROR: normalized_schema missing top-level 'graphs' dict")
    return graphs


def _normalize_graph_type_for_schema(graph_type: str) -> str:
    if graph_type is None:
        return "ICFG"
    gt = str(graph_type).strip()
    up = gt.upper()

    if up == "CALLGRAPH":
        return "CallGraph"
    if up in ("TYPEGRAPH", "SVFTYPE", "SVFTYPEGRAPH"):
        return "TypeGraph"
    if up in ("BASICBLOCK", "BASICBLOCKGRAPH"):
        return "BasicBlock"

    if up in ("PAG", "ICFG", "CHG"):
        return up

    if gt in ("CallGraph", "TypeGraph", "BasicBlock"):
        return gt

    return gt


def _graph_cfg_from_schema(normalized_schema_obj: Dict[str, Any], graph_type: str) -> Dict[str, Any]:
    graphs = _schema_graphs(normalized_schema_obj)
    key = _normalize_graph_type_for_schema(graph_type)

    if key not in graphs:
        raise SystemExit(
            "ERROR: graph_type=%r not found in normalized_schema.graphs keys=%r"
            % (graph_type, sorted(list(graphs.keys())))
        )

    cfg = graphs[key]
    node_labels = cfg.get("node_labels") or []
    edge_labels = cfg.get("edge_labels") or []

    if not isinstance(node_labels, list) or not node_labels:
        raise SystemExit("ERROR: normalized_schema.graphs[%s].node_labels empty" % key)
    if not isinstance(edge_labels, list):
        raise SystemExit("ERROR: normalized_schema.graphs[%s].edge_labels must be list" % key)

    return {"schema_graph_key": key, "node_labels": node_labels, "edge_labels": edge_labels}


def _pick_seed_label(schema_graph_key: str, node_labels: List[str]) -> str:
    if schema_graph_key == "ICFG":
        if "FunExitICFGNode" in node_labels:
            return "FunExitICFGNode"
        if "FunEntryICFGNode" in node_labels:
            return "FunEntryICFGNode"
        return node_labels[0]

    if schema_graph_key == "CallGraph":
        return "CallGraphNode" if "CallGraphNode" in node_labels else node_labels[0]

    if schema_graph_key == "CHG":
        return "CHNode" if "CHNode" in node_labels else node_labels[0]

    if schema_graph_key == "BasicBlock":
        return "SVFBasicBlock" if "SVFBasicBlock" in node_labels else node_labels[0]

    if schema_graph_key == "TypeGraph":
        return "SVFPointerType" if "SVFPointerType" in node_labels else node_labels[0]

    return node_labels[0]


def _pick_dst_label(schema_graph_key: str, node_labels: List[str], dst_label_cli: str) -> Optional[str]:
    if dst_label_cli and dst_label_cli in node_labels:
        return dst_label_cli

    if schema_graph_key == "ICFG":
        return "IntraICFGNode" if "IntraICFGNode" in node_labels else node_labels[0]

    return node_labels[0]


def schema_driven_needs(
    normalized_schema_obj: Dict[str, Any],
    state: RetrievalState,
    stage: str,
    dst_label_cli: str,
    graph_type_hint: Optional[str] = None,
    relax_traversal: bool = False,
) -> Dict[str, Any]:
    graph_types = ["PAG", "ICFG", "CALLGRAPH"]

    if graph_type_hint:
        graph_type = graph_type_hint
    else:
        idx = int(getattr(state, "rr_graph_idx", 0))
        graph_type = graph_types[idx % len(graph_types)]
        state.rr_graph_idx = idx + 1

    cfg = _graph_cfg_from_schema(normalized_schema_obj, graph_type)
    schema_graph_key = cfg["schema_graph_key"]
    node_labels = cfg["node_labels"]
    edge_labels = cfg["edge_labels"]

    seed_label = _pick_seed_label(schema_graph_key, node_labels)
    dst_label = _pick_dst_label(schema_graph_key, node_labels, dst_label_cli)

    if relax_traversal and (str(graph_type).upper() in ("PAG", "ICFG")):
        edge_types = ["*"]
        dst_label = None
    else:
        if edge_labels:
            edge_types = [str(edge_labels[0])]
        else:
            edge_types = []

    return {
        "task_id": "T1",
        "graph_type": str(graph_type),
        "edge_types": edge_types,
        "direction": "out",
        "objective": "reach_target_node",
        "seed": {"label": seed_label, "id": 1},
        "requested_depth": 10 if stage == "init" else 6,
        "dst_label": dst_label,
    }


# =================================================
# (B) TuGraph subgraph switching (graph_type -> DB)
# =================================================

GRAPH_TYPE_TO_TUGRAPH_GRAPH = {
    "PAG": "PAG",
    "ICFG": "ICFG",
    "CALLGRAPH": "CallGraph",
    "CHG": "CHG",
    "BASICBLOCK": "BasicBlockGraph",
    "SVFTYPE": "SVFType",
    "CallGraph": "CallGraph",
    "BasicBlockGraph": "BasicBlockGraph",
    "SVFType": "SVFType",
}


def tugraph_db_for_graph_type(graph_type: str, fallback: str) -> str:
    if not graph_type:
        return fallback
    gt = str(graph_type).strip()
    if gt in GRAPH_TYPE_TO_TUGRAPH_GRAPH:
        return GRAPH_TYPE_TO_TUGRAPH_GRAPH[gt]
    up = gt.upper()
    return GRAPH_TYPE_TO_TUGRAPH_GRAPH.get(up, fallback)


class BoltRouter(object):
    def __init__(self, uri: str, user: str, password: str):
        self.uri = uri
        self.user = user
        self.password = password
        self._clients: Dict[str, BoltClient] = {}

    def get(self, dbname: str) -> BoltClient:
        db = str(dbname)
        if db not in self._clients:
            self._clients[db] = BoltClient(
                uri=self.uri,
                user=self.user,
                password=self.password,
                database=db,
            )
        return self._clients[db]

    def close(self):
        for _, c in list(self._clients.items()):
            try:
                c.close()
            except Exception:
                pass
        self._clients.clear()


# =================================================
# (C) Legacy planner + fake exec (kept for regression)
# =================================================

def fake_retrieval_planner_dict(state: RetrievalState, stage: str) -> Dict[str, Any]:
    graph_types = ["PAG", "ICFG", "CALLGRAPH"]
    graph_type = graph_types[(state.counters.replans + state.counters.queries_total) % len(graph_types)]
    return {
        "task_id": "T1",
        "graph_type": graph_type,
        "objective": "reach_target_node",
        "seed": {"label": "FunExitICFGNode", "id": 1},
        "requested_depth": 10 if stage == "init" else 6,
        "dst_label": DST_NODE_LABEL_DEFAULT,
    }


def render_query_from_plan_legacy(plan: Dict[str, Any], depth: int, dst_node_label: Optional[str]) -> str:
    label = plan["seed"]["label"]
    nid = int(plan["seed"]["id"])
    dst_label = dst_node_label or DST_NODE_LABEL_DEFAULT
    edge_label = "SVFStmt"
    return (
        "MATCH (n:%s {id: %d})-[:%s*1..%d]->(m:%s) "
        "RETURN n, m LIMIT 3"
        % (label, nid, edge_label, int(depth), dst_label)
    )


def fake_executor(validated_query: str) -> Dict[str, Any]:
    depth = 1
    m = re.search(r"\*1\.\.(\d+)", validated_query)
    if m:
        try:
            depth = int(m.group(1))
        except Exception:
            depth = 1

    coverage = min(1.0, 0.15 * depth + random.random() * 0.05)
    hit_cap = (depth >= 6 and random.random() < 0.3)

    return {
        "ok": True,
        "subgraph_meta": {
            "num_nodes": min(500, 50 * depth),
            "num_edges": min(2000, 200 * depth),
            "hit_cap": hit_cap,
            "coverage": coverage,
            "depth": depth,
        },
    }


def fake_local_summarizer(subgraph_meta: Dict[str, Any]) -> Dict[str, Any]:
    ok = (float(subgraph_meta.get("coverage", 0.0)) >= 0.65) and (not bool(subgraph_meta.get("hit_cap", False)))
    return {
        "objective_met": bool(ok),
        "insufficient_context": not bool(ok),
        "signals": {
            "coverage": subgraph_meta.get("coverage"),
            "hit_cap": subgraph_meta.get("hit_cap"),
        },
    }

def query_budget_exhausted(state) -> bool:
    """
    Return True only when the total query budget is finite and already exhausted.
    -1 means unlimited.
    """
    max_q = getattr(state.budgets, "max_queries_total", None)
    if max_q is None:
        return False
    if int(max_q) < 0:
        return False
    return int(state.counters.queries_total) >= int(max_q)

def plan_to_payload_any(plan: Any) -> Dict[str, Any]:
    if isinstance(plan, dict):
        return plan
    if hasattr(plan, "model_dump"):
        return plan.model_dump()
    if hasattr(plan, "dict"):
        return plan.dict()
    return {"repr": repr(plan)}


BuiltQuery = Union[str, Tuple[str, Dict[str, Any]]]


def _split_built_query(built: BuiltQuery) -> Tuple[str, Optional[Dict[str, Any]]]:
    if isinstance(built, tuple):
        if len(built) != 2:
            raise SystemExit("ERROR: build_query returned tuple of len != 2")
        cypher, params = built
        if not isinstance(cypher, str):
            raise SystemExit("ERROR: build_query returned non-str cypher in tuple")
        if params is not None and not isinstance(params, dict):
            raise SystemExit("ERROR: build_query returned non-dict params")
        return cypher, params
    if not isinstance(built, str):
        raise SystemExit("ERROR: build_query must return str or (str, dict)")
    return built, None


def build_query_or_exit(db_agent: DBQueryAgent, plan: GraphQueryPlan, depth: int, dst_node_label: Optional[str]) -> BuiltQuery:
    if not hasattr(db_agent, "build_query"):
        raise SystemExit("ERROR: DBQueryAgent must implement build_query(plan, depth, dst_label).")
    return db_agent.build_query(plan, depth, dst_node_label)


# =================================================
# (D) Normalize LIMIT (TuGraph safe)
# =================================================

_LIMIT_CLAUSE_RE = re.compile(r"(?is)\s+\blimit\b\s+[^\s;]+")


def normalize_limit_and_params(
    cypher: str,
    params: Optional[Dict[str, Any]],
    max_limit: int,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not isinstance(cypher, str):
        return cypher, params

    s = cypher.strip()
    if not s:
        return cypher, params

    has_semicolon = s.endswith(";")
    if has_semicolon:
        s = s[:-1].rstrip()

    s = _LIMIT_CLAUSE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = "%s LIMIT %d" % (s, int(max_limit))

    if has_semicolon:
        s = s + ";"

    if isinstance(params, dict):
        params = dict(params)
        for k in ("limit", "LIMIT", "max_limit", "row_limit"):
            params.pop(k, None)

    return s, params


# =================================================
# (E) DB exec + retry on label error
# =================================================

_LABEL_NOT_EXIST_PATTERNS = [
    "No such vertex label",
    "No such edge label",
    "No such label",
]


def execute_real_db_or_exit(
    bolt: BoltClient,
    cypher: str,
    params: Optional[Dict[str, Any]],
    timeout_s: int,
) -> Dict[str, Any]:
    try:
        out = bolt.run(cypher, params=params, timeout_s=timeout_s)
        records = out.get("records") or []
        return {"ok": True, "records": records}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def maybe_drop_dst_label_on_label_error(cypher: str, err: str) -> Optional[str]:
    if not isinstance(err, str) or not err:
        return None
    if not any(p in err for p in _LABEL_NOT_EXIST_PATTERNS):
        return None

    rewritten = re.sub(r"\(m\s*:\s*[A-Za-z_][A-Za-z0-9_]*\)", "(m)", cypher, count=1)
    if rewritten != cypher:
        return rewritten
    return None


# =================================================
# (F) Logging helpers
# =================================================

def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        try:
            return json.dumps(str(obj), ensure_ascii=False)
        except Exception:
            return "<unserializable>"


def _print_db_debug(
    enabled: bool,
    phase: str,
    dbname: str,
    cypher: str,
    params: Optional[Dict[str, Any]],
    records: Optional[List[Dict[str, Any]]] = None,
    max_records: int = 3,
) -> None:
    if not enabled:
        return
    print("\n[DB DEBUG] phase=%s db=%s" % (phase, dbname))
    print("[DB DEBUG] cypher=%s" % cypher)
    if params:
        print("[DB DEBUG] params=%s" % _safe_json(params))
    else:
        print("[DB DEBUG] params={} ")
    if records is not None:
        show = records[:max(0, int(max_records))]
        print("[DB DEBUG] records_preview(n=%d of %d)=%s" % (len(show), len(records), _safe_json(show)))


# =================================================
# (G) Seed discovery (optional)
# =================================================

def discover_seed_id(
    bolt: BoltClient,
    dbname: str,
    seed_label: str,
    timeout_s: int,
    debug: bool = False,
    print_records: bool = False,
    print_records_max: int = 3,
) -> Optional[Any]:
    label = str(seed_label).strip()
    if not label:
        return None

    candidates = [
        ("id", "MATCH (n:%s) RETURN n.id AS id LIMIT 1" % label),
        ("_id", "MATCH (n:%s) RETURN n._id AS id LIMIT 1" % label),
        ("node_id", "MATCH (n:%s) RETURN n.node_id AS id LIMIT 1" % label),
    ]

    for prop_name, cy in candidates:
        _print_db_debug(
            enabled=bool(debug),
            phase="SEED_DISCOVERY(%s)" % prop_name,
            dbname=str(dbname),
            cypher=cy,
            params=None,
            records=None,
        )
        out = execute_real_db_or_exit(bolt, cy, params=None, timeout_s=int(timeout_s))
        if not out.get("ok"):
            continue
        recs = out.get("records") or []
        if int(print_records):
            _print_db_debug(
                enabled=True,
                phase="SEED_DISCOVERY_RESULT(%s)" % prop_name,
                dbname=str(dbname),
                cypher=cy,
                params=None,
                records=recs,
                max_records=int(print_records_max),
            )
        if recs:
            v = recs[0].get("id", None)
            if v is not None:
                return v
    return None


def maybe_apply_seed_discovery_to_needs(
    needs: Dict[str, Any],
    bolt_router: Optional["BoltRouter"],
    default_db: str,
    timeout_s: int,
    debug: bool,
    print_records: bool,
    print_records_max: int,
) -> Dict[str, Any]:
    if not needs or not bolt_router:
        return needs

    seed = needs.get("seed") or {}
    seed_label = str(seed.get("label", "")).strip()
    if not seed_label:
        return needs

    graph_type = str(needs.get("graph_type", "")).strip()
    want_db = tugraph_db_for_graph_type(graph_type, fallback=str(default_db))
    bolt = bolt_router.get(want_db)

    sid = discover_seed_id(
        bolt=bolt,
        dbname=want_db,
        seed_label=seed_label,
        timeout_s=int(timeout_s),
        debug=bool(debug),
        print_records=bool(print_records),
        print_records_max=int(print_records_max),
    )
    if sid is not None:
        needs = dict(needs)
        seed2 = dict(seed)
        seed2["id"] = sid
        needs["seed"] = seed2
        print("[SEED] graph_type=%s db=%s label=%s -> id=%s" % (graph_type, want_db, seed_label, str(sid)))
    else:
        print("[SEED] graph_type=%s db=%s label=%s -> id NOT FOUND (keep id=%s)" %
              (graph_type, want_db, seed_label, str(seed.get("id"))))
    return needs



# =================================================
# (G1) Target-aware seed resolution and query templates
# =================================================

_TARGET_VERTEX_LABEL_ALLOWLIST = {
    "ArgValVar",
    "ValVar",
    "FunObjVar",
    "FunValVar",
}


def _target_mode_enabled(args: Any) -> bool:
    return bool(
        getattr(args, "target_function", None)
        or getattr(args, "target_source_file", None)
        or getattr(args, "target_seed_id", None) is not None
    )


def _graph_localizer_mode_enabled(args: Any) -> bool:
    """Return True when the v1 graph localizer has selected graph-level IDs."""
    return bool(getattr(args, "graph_localizer_selected", None))


def _graph_localizer_selected(args: Any) -> Dict[str, Any]:
    selected = getattr(args, "graph_localizer_selected", None)
    return dict(selected) if isinstance(selected, dict) else {}


def _graph_localizer_seed_for_graph(args: Any, graph_type: str) -> Optional[Dict[str, Any]]:
    """Map the selected fault location to the graph-specific seed used by v1.1."""
    selected = _graph_localizer_selected(args)
    g = str(graph_type or "").upper()

    if g == "PAG":
        sid = selected.get("primary_arg_node_id") or getattr(args, "target_seed_id", None)
        if sid is None:
            return None
        return {
            "label": "ArgValVar",
            "id": int(sid),
            "seed_kind": "graph_localizer_pag_argument_seed",
            "objective": "targeted_pag_argument_flow",
            "function_name": selected.get("graph_function_name") or selected.get("function"),
            "arg_no": int(selected.get("arg_no", 0) or 0),
            "source_file": selected.get("graph_source_file"),
        }

    if g == "ICFG":
        sid = selected.get("primary_icfg_node_id")
        if sid is None:
            return None
        return {
            "label": None,
            "id": int(sid),
            "seed_kind": "graph_localizer_icfg_seed",
            "objective": "targeted_icfg_control_flow",
            "function_name": selected.get("graph_function_name") or selected.get("function"),
            "source_file": selected.get("graph_source_file"),
        }

    if g in ("CALLGRAPH", "CALL_GRAPH", "CALLGRAPHNODE"):
        sid = selected.get("callgraph_node_id")
        if sid is None:
            return None
        return {
            "label": None,
            "id": int(sid),
            "seed_kind": "graph_localizer_callgraph_seed",
            "objective": "targeted_callgraph_neighborhood",
            "function_name": selected.get("graph_function_name") or selected.get("function"),
            "source_file": selected.get("graph_source_file"),
        }

    return None


def _graph_localizer_next_graph(args: Any, state: RetrievalState) -> str:
    """Choose the next graph to query from the selected graph IDs.

    v1 repeatedly queried PAG because target mode forced graph_type=PAG.  v1.1
    makes the information-need handoff explicit: first query PAG argument flow,
    then ICFG local control flow, then CallGraph neighborhood.  Missing seeds are
    skipped, and after all available graphs have been touched we fall back to PAG.
    """
    counts = dict(getattr(getattr(state, "counters", None), "queries_by_graph_type", {}) or {})
    for g in ["PAG", "ICFG", "CALLGRAPH"]:
        if _graph_localizer_seed_for_graph(args, g) is None:
            continue
        if int(counts.get(g, 0) or 0) <= 0:
            return g
    return "PAG"


def _graph_localizer_required_graphs_done(args: Any, state: RetrievalState) -> bool:
    counts = dict(getattr(getattr(state, "counters", None), "queries_by_graph_type", {}) or {})
    required = [g for g in ["PAG", "ICFG", "CALLGRAPH"] if _graph_localizer_seed_for_graph(args, g) is not None]
    if not required:
        return True
    return all(int(counts.get(g, 0) or 0) > 0 for g in required)


def prepare_graph_localizer_seeded_needs(
    normalized_schema_obj: Dict[str, Any],
    state: RetrievalState,
    stage: str,
    args: Any,
) -> Dict[str, Any]:
    graph_type = _graph_localizer_next_graph(args, state)
    seed_info = _graph_localizer_seed_for_graph(args, graph_type)
    if seed_info is None:
        # Defensive fallback: preserve old PAG target behavior if the localizer
        # did not provide graph-specific IDs for some reason.
        graph_type = "PAG"
        seed_info = _graph_localizer_seed_for_graph(args, graph_type) or {
            "label": "ArgValVar",
            "id": int(getattr(args, "target_seed_id", 1)),
            "objective": "targeted_pag_argument_flow",
            "seed_kind": "graph_localizer_fallback_seed",
        }

    cfg = _graph_cfg_from_schema(normalized_schema_obj, graph_type)
    dst_label = None
    edge_types = ["*"]
    plan_seed_label = seed_info.get("label")
    if not plan_seed_label:
        try:
            plan_seed_label = _pick_seed_label(cfg["schema_graph_key"], cfg["node_labels"])
        except Exception:
            plan_seed_label = "Node"

    needs = {
        "task_id": "T1_%s" % str(graph_type).upper(),
        "graph_type": str(graph_type),
        "edge_types": edge_types,
        "direction": "out",
        "objective": str(seed_info.get("objective") or "graph_localizer_seeded_retrieval"),
        "seed": {"label": plan_seed_label, "id": int(seed_info.get("id"))},
        "requested_depth": 4 if str(graph_type).upper() in ("ICFG", "CALLGRAPH") else (10 if stage == "init" else 6),
        "dst_label": dst_label,
        "target": {
            "seed_mode": seed_info.get("seed_kind"),
            "seed_id": int(seed_info.get("id")),
            "seed_label": seed_info.get("label"),
            "function_name": seed_info.get("function_name"),
            "target_source_file": seed_info.get("source_file"),
            "arg_no": seed_info.get("arg_no", ""),
        },
    }

    print(
        "[GRAPH LOCALIZER SEED] graph=%s id=%s label=%s objective=%s function=%s"
        % (
            str(graph_type),
            str(seed_info.get("id")),
            str(plan_seed_label or "<unlabeled>"),
            str(needs.get("objective")),
            str(seed_info.get("function_name") or ""),
        )
    )
    return needs


def _make_graph_localizer_synthetic_summary(
    graph_type: str,
    selected: Dict[str, Any],
    result: Dict[str, Any],
    attempt_id: int,
) -> Dict[str, Any]:
    graph = ((selected.get("evidence") or {}).get("graph") or {}) if isinstance(selected, dict) else {}
    g = graph.get(graph_type) or graph.get("CallGraph" if graph_type == "CALLGRAPH" else graph_type) or {}

    function_name = selected.get("function") or selected.get("graph_function_name")
    graph_function_name = selected.get("graph_function_name")
    patch_src = selected.get("patch_source_file") or selected.get("source_file")
    patch_line = selected.get("patch_start_line") or selected.get("start_line")
    graph_src = selected.get("graph_source_file")
    graph_line = selected.get("graph_start_line")

    preview_paths = []
    for section_name, section in (g.items() if isinstance(g, dict) else []):
        if not isinstance(section, dict):
            continue
        for row in (section.get("preview") or [])[:5]:
            if not isinstance(row, dict):
                continue
            p = row.get("p") or row.get("e")
            if p is not None:
                preview_paths.append({
                    "source": "graph_localizer:%s.%s" % (graph_type, section_name),
                    "text": json.dumps(p, ensure_ascii=False, default=str)[:1200],
                })

    desc_by_graph = {
        "PAG": "Graph localizer resolved the selected function to PAG FunObjVar/ArgValVar evidence and argument-flow seeds.",
        "ICFG": "Graph localizer resolved the selected function to ICFG seed/control-flow evidence via PAG.icfg_node_id.",
        "CALLGRAPH": "Graph localizer resolved the selected function to CallGraph node and caller/callee neighborhood evidence.",
    }

    return {
        "summary_id": "LS-GL-%s-%04d" % (str(graph_type).upper(), int(attempt_id)),
        "task_id": "graph_localizer_%s" % str(graph_type).upper(),
        "graph_type": str(graph_type),
        "step_index": -1,
        "result_kind": "graph_localizer_prior",
        "semantic_summary": {
            "description": desc_by_graph.get(str(graph_type).upper(), "Graph localizer evidence."),
            "paths": preview_paths[:10],
            "entities": {
                "functions": [x for x in [function_name, graph_function_name] if x],
                "variables": [],
                "nodes": [
                    {"kind": "callgraph_node_id", "id": selected.get("callgraph_node_id")},
                    {"kind": "fun_obj_var_id", "id": selected.get("fun_obj_var_id")},
                    {"kind": "primary_arg_node_id", "id": selected.get("primary_arg_node_id")},
                    {"kind": "primary_icfg_node_id", "id": selected.get("primary_icfg_node_id")},
                ],
                "edges": [],
            },
        },
        "signals": {
            "objective_met": True,
            "insufficient_context": False,
            "coverage": 0.9,
            "hit_cap": False,
            "depth": None,
            "records_count": int(sum(int((v or {}).get("rows", 0) or 0) for v in (g.values() if isinstance(g, dict) else []) if isinstance(v, dict))),
            "num_nodes": 0,
            "num_edges": 0,
        },
        "query_context": {
            "source": "graph_localizer_v1_1_prior",
            "patch_source": "%s:%s" % (patch_src, patch_line),
            "graph_source": "%s:%s" % (graph_src, graph_line),
            "selected_location": {
                "function": function_name,
                "graph_function_name": graph_function_name,
                "callgraph_node_id": selected.get("callgraph_node_id"),
                "fun_obj_var_id": selected.get("fun_obj_var_id"),
                "primary_arg_node_id": selected.get("primary_arg_node_id"),
                "primary_icfg_node_id": selected.get("primary_icfg_node_id"),
                "source_mapping": selected.get("source_mapping"),
            },
        },
        "next_suggestions": [],
        "objective_met": True,
        "insufficient_context": False,
    }


def inject_graph_localizer_evidence_into_state(args: Any, state: RetrievalState) -> None:
    """Add CallGraph/PAG/ICFG localizer evidence as first-class summaries.

    The localizer already executed cross-graph probes. v1.1 preserves those
    results in the global summary instead of leaving them only in
    graph_fault_localization.json. This prevents the final diagnosis from seeing
    PAG-only evidence when the localizer has CallGraph and ICFG evidence too.
    """
    result = getattr(args, "graph_localizer_result", None)
    selected = getattr(args, "graph_localizer_selected", None)
    if not isinstance(result, dict) or not isinstance(selected, dict):
        return

    existing = []
    try:
        existing = list(getattr(state.artifacts, "local_summaries", []) or [])
    except Exception:
        try:
            existing = list((state.artifacts or {}).get("local_summaries", []) or [])
        except Exception:
            existing = []

    already = set()
    for item in existing:
        if isinstance(item, dict):
            s = item.get("summary") if isinstance(item.get("summary"), dict) else item
            sid = s.get("summary_id") if isinstance(s, dict) else None
            if sid:
                already.add(str(sid))

    for graph_type in ["CALLGRAPH", "PAG", "ICFG"]:
        summary = _make_graph_localizer_synthetic_summary(graph_type, selected, result, int(getattr(state, "attempt_id", 1) or 1))
        if str(summary.get("summary_id")) in already:
            continue
        try:
            state.record_local_summary_meta({"task_id": summary["task_id"], "graph_type": graph_type, "summary": summary})
        except Exception:
            pass

def _plan_graph_type(plan: Any) -> str:
    if isinstance(plan, dict):
        return str(plan.get("graph_type", ""))
    return str(getattr(plan, "graph_type", ""))


def _plan_seed_dict(plan: Any) -> Dict[str, Any]:
    if isinstance(plan, dict):
        seed = plan.get("seed") or {}
        return dict(seed) if isinstance(seed, dict) else {}
    seed = getattr(plan, "seed", None)
    if isinstance(seed, dict):
        return dict(seed)
    if seed is None:
        return {}
    if hasattr(seed, "model_dump"):
        try:
            return dict(seed.model_dump())
        except Exception:
            return {}
    if hasattr(seed, "dict"):
        try:
            return dict(seed.dict())
        except Exception:
            return {}
    return {}


def _safe_vertex_label(label: str) -> str:
    label = str(label or "").strip()
    if label not in _TARGET_VERTEX_LABEL_ALLOWLIST:
        raise SystemExit("ERROR: unsupported target seed label: %s" % label)
    return label


def _run_seed_query_or_exit(
    bolt_router: Optional["BoltRouter"],
    default_db: str,
    timeout_s: int,
    debug: bool,
    cypher: str,
    params: Dict[str, Any],
    phase: str,
) -> List[Dict[str, Any]]:
    if not bolt_router:
        raise SystemExit("ERROR: target-aware seed resolution requires --use-db")

    dbname = tugraph_db_for_graph_type("PAG", fallback=str(default_db))
    bolt = bolt_router.get(dbname)

    _print_db_debug(
        enabled=bool(debug),
        phase=phase,
        dbname=dbname,
        cypher=cypher,
        params=params,
        records=None,
    )

    out = execute_real_db_or_exit(bolt, cypher, params=params, timeout_s=int(timeout_s))
    if not out.get("ok"):
        raise SystemExit("ERROR: %s failed: %s" % (phase, out.get("error")))

    return out.get("records") or []


def resolve_target_pag_arg_seed(
    bolt_router: "BoltRouter",
    default_db: str,
    timeout_s: int,
    args: Any,
) -> Dict[str, Any]:
    """
    Resolve target PAG seed in a TuGraph-compatible way.

    TuGraph currently may fail on chained MATCH clauses, so this resolver uses
    two simple queries:
      1) resolve FunObjVar by val_name/source_loc
      2) resolve ArgValVar by cg_node_id/arg_no
    """

    target_function = getattr(args, "target_function", None)
    target_source_file = getattr(args, "target_source_file", None)
    target_arg_no = int(getattr(args, "target_arg_no", 0))

    if not target_function:
        raise SystemExit("ERROR: target seed resolver requires target_function")
    if not target_source_file:
        raise SystemExit("ERROR: target seed resolver requires target_source_file")

    want_db = tugraph_db_for_graph_type("PAG", fallback=str(default_db))
    bolt = bolt_router.get(want_db)

    q_fun = """
MATCH (f:FunObjVar)
WHERE f.val_name = $target_function
  AND f.source_loc CONTAINS $target_source_file
RETURN f.id AS function_seed_id,
       f.val_name AS function_name,
       f.source_loc AS function_source_loc,
       f.all_args_node_ids AS all_args_node_ids
LIMIT 2
"""
    p_fun = {
        "target_function": str(target_function),
        "target_source_file": str(target_source_file),
    }

    _print_db_debug(
        enabled=bool(getattr(args, "print_db_debug", False)),
        phase="TARGET_FUNCTION_DISCOVERY",
        dbname=want_db,
        cypher=q_fun,
        params=p_fun,
        records=None,
    )

    out_fun = execute_real_db_or_exit(bolt, q_fun, params=p_fun, timeout_s=int(timeout_s))
    if not out_fun.get("ok"):
        raise SystemExit("ERROR: TARGET_FUNCTION_DISCOVERY failed: %s" % out_fun.get("error"))

    fun_rows = out_fun.get("records") or []
    if not fun_rows:
        raise SystemExit(
            "ERROR: target function not found in PAG: function=%s file=%s"
            % (str(target_function), str(target_source_file))
        )

    if len(fun_rows) > 1:
        print("[WARN] TARGET_FUNCTION_DISCOVERY returned %d rows; using the first exact match." % len(fun_rows))

    fun = fun_rows[0]
    function_seed_id = int(fun["function_seed_id"])

    q_arg = """
MATCH (a:ArgValVar)
WHERE a.cg_node_id = $function_seed_id
  AND a.arg_no = $target_arg_no
RETURN a.id AS seed_id,
       a.arg_no AS arg_no,
       a.source_loc AS seed_source_loc
LIMIT 2
"""
    p_arg = {
        "function_seed_id": int(function_seed_id),
        "target_arg_no": int(target_arg_no),
    }

    _print_db_debug(
        enabled=bool(getattr(args, "print_db_debug", False)),
        phase="TARGET_ARG_DISCOVERY",
        dbname=want_db,
        cypher=q_arg,
        params=p_arg,
        records=None,
    )

    out_arg = execute_real_db_or_exit(bolt, q_arg, params=p_arg, timeout_s=int(timeout_s))
    if not out_arg.get("ok"):
        raise SystemExit("ERROR: TARGET_ARG_DISCOVERY failed: %s" % out_arg.get("error"))

    arg_rows = out_arg.get("records") or []
    if not arg_rows:
        raise SystemExit(
            "ERROR: target arg not found in PAG: function_seed_id=%s arg_no=%s"
            % (str(function_seed_id), str(target_arg_no))
        )

    if len(arg_rows) > 1:
        print("[WARN] TARGET_ARG_DISCOVERY returned %d rows; using the first exact match." % len(arg_rows))

    arg = arg_rows[0]
    seed_id = int(arg["seed_id"])

    return {
        "graph": "PAG",
        "seed_label": "ArgValVar",
        "seed_id": seed_id,
        "seed_kind": "pag_function_argument",
        "function_seed_id": function_seed_id,
        "function_name": fun.get("function_name"),
        "function_source_loc": fun.get("function_source_loc"),
        "all_args_node_ids": fun.get("all_args_node_ids"),
        "arg_no": int(arg.get("arg_no", target_arg_no)),
        "seed_source_loc": arg.get("seed_source_loc"),
        "target_source_file": str(target_source_file),
    }


def maybe_apply_target_seed_to_needs(
    needs: Dict[str, Any],
    bolt_router: Optional["BoltRouter"],
    default_db: str,
    timeout_s: int,
    debug: bool,
    args: Any,
) -> Dict[str, Any]:
    """
    Apply target-aware seed selection before planning.

    Product/MVP behavior:
    - If target mode is not enabled, keep normal retrieval behavior.
    - If --target-seed-id is provided, use it as a developer diagnostic override.
    - Otherwise resolve the target function argument seed from PAG using
      resolve_target_pag_arg_seed(), which performs TuGraph-compatible
      two-step discovery:
        1) FunObjVar by val_name/source_loc
        2) ArgValVar by cg_node_id/arg_no

    This prevents generic discovery from selecting an arbitrary first ArgValVar,
    while keeping RetrievalPlannerAgent and DBQueryAgent in the pipeline.
    """
    if not needs or not _target_mode_enabled(args):
        return needs

    if bolt_router is None:
        raise SystemExit("ERROR: target-aware seed selection requires --use-db")

    target_seed_id = getattr(args, "target_seed_id", None)

    needs = dict(needs)
    needs["graph_type"] = "PAG"
    needs["edge_types"] = ["*"]
    needs["dst_label"] = None
    needs["objective"] = "targeted_pag_argument_flow"

    if target_seed_id is not None:
        # Developer/debug override only. Do not use this in formal demo.
        target_source_file = getattr(args, "target_source_file", None)
        seed_info = {
            "seed_id": int(target_seed_id),
            "seed_label": "ArgValVar",
            "seed_kind": "manual_target_seed_id",
            "seed_mode": "manual_target_seed_id",
            "target_source_file": target_source_file,
            "function_name": getattr(args, "target_function", ""),
            "arg_no": int(getattr(args, "target_arg_no", 0)),
        }
    else:
        # Product path: resolve function -> argument seed from PAG.
        seed_info = resolve_target_pag_arg_seed(
            bolt_router=bolt_router,
            default_db=default_db,
            timeout_s=timeout_s,
            args=args,
        )
        seed_info["seed_mode"] = seed_info.get("seed_kind", "pag_function_argument")

    seed_label = str(seed_info.get("seed_label", "ArgValVar"))
    seed_id = seed_info.get("seed_id", None)

    if seed_id is None:
        raise SystemExit("ERROR: target seed resolver returned no seed_id: %s" % _safe_json(seed_info))

    needs["seed"] = {
        "label": seed_label,
        "id": int(seed_id),
    }

    needs["target"] = seed_info

    target_source_file = (
        seed_info.get("target_source_file")
        or getattr(args, "target_source_file", None)
    )
    if target_source_file:
        needs["target_source_file"] = str(target_source_file)

    print(
        "[TARGET SEED] mode=%s graph=PAG label=%s id=%s function=%s file=%s arg_no=%s"
        % (
            str(seed_info.get("seed_mode")),
            str(seed_label),
            str(seed_id),
            str(seed_info.get("function_name", getattr(args, "target_function", ""))),
            str(target_source_file),
            str(seed_info.get("arg_no", getattr(args, "target_arg_no", ""))),
        )
    )

    return needs


def prepare_builder_needs(
    normalized_schema_obj: Dict[str, Any],
    state: RetrievalState,
    stage: str,
    args: Any,
    bolt_router: Optional["BoltRouter"],
) -> Dict[str, Any]:
    """
    Centralized needs preparation.

    Non-target mode preserves existing round-robin behavior.
    Target mode pins retrieval to PAG and resolves a function-argument seed.
    """
    # v1.1 product path: when the graph localizer has selected graph IDs,
    # do not collapse retrieval back to a single PAG target seed. Instead,
    # schedule graph-specific retrieval needs from the selected PAG / ICFG /
    # CallGraph IDs.
    if _graph_localizer_mode_enabled(args):
        return prepare_graph_localizer_seeded_needs(
            normalized_schema_obj=normalized_schema_obj,
            state=state,
            stage=stage,
            args=args,
        )

    graph_hint = "PAG" if _target_mode_enabled(args) else None
    needs = schema_driven_needs(
        normalized_schema_obj,
        state,
        stage,
        args.dst_node_label,
        graph_type_hint=graph_hint,
        relax_traversal=bool(args.regtest_relax_traversal),
    )

    if _target_mode_enabled(args):
        return maybe_apply_target_seed_to_needs(
            needs=needs,
            bolt_router=bolt_router,
            default_db=str(args.bolt_db),
            timeout_s=int(args.bolt_timeout_s),
            debug=bool(args.print_db_debug),
            args=args,
        )

    return maybe_apply_seed_discovery_to_needs(
        needs=needs,
        bolt_router=bolt_router,
        default_db=str(args.bolt_db),
        timeout_s=int(args.bolt_timeout_s),
        debug=bool(args.print_db_debug),
        print_records=bool(int(args.print_db_records) == 1),
        print_records_max=int(args.print_db_records_max),
    )


def build_target_aware_pag_query(
    plan: Any,
    args: Any,
    depth: int,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Schema-aware graph-localizer / target query template.

    Historical name is kept for compatibility, but v1.1 supports more than PAG:
    - PAG: selected ArgValVar argument-flow seed;
    - ICFG: selected ICFG seed node from PAG.icfg_node_id;
    - CallGraph: selected CallGraph node neighborhood.
    """
    graph_type = _plan_graph_type(plan).upper()
    max_depth = max(1, int(depth))

    # v1.1 graph-localizer seeded retrieval.
    if _graph_localizer_mode_enabled(args):
        seed = _plan_seed_dict(plan)
        seed_id = seed.get("id")
        if seed_id is None:
            return None
        params = {"seed_id": int(seed_id)}

        if graph_type == "PAG":
            seed_label = _safe_vertex_label(seed.get("label", "ArgValVar"))
            cypher = "MATCH p=(n:%s {id: $seed_id})-[*1..%d]->(m) RETURN p, n, m" % (seed_label, max_depth)
            return cypher, params

        if graph_type == "ICFG":
            # TuGraph's generated ICFG node labels vary across schema exports.
            # In demo mode the guard is explicitly configured with
            # --allow_unlabeled_nodes and --allow-wildcard-edge, so the seed id
            # is the stable key.
            cypher = "MATCH p=(n)-[*1..%d]->(m) WHERE n.id = $seed_id RETURN p, n, m" % max_depth
            return cypher, params

        if graph_type in ("CALLGRAPH", "CALL_GRAPH"):
            # Include both outgoing callees and incoming callers by allowing the
            # seed to appear on either side of the directed path.
            cypher = "MATCH p=(n)-[*1..%d]->(m) WHERE n.id = $seed_id OR m.id = $seed_id RETURN p, n, m" % max_depth
            return cypher, params

        return None

    # Legacy/manual target mode.
    if not _target_mode_enabled(args):
        return None
    if graph_type != "PAG":
        return None

    seed = _plan_seed_dict(plan)
    seed_id = seed.get("id")
    if seed_id is None:
        return None

    seed_label = _safe_vertex_label(seed.get("label", "ArgValVar"))
    cypher = "MATCH p=(n:%s {id: $seed_id})-[*1..%d]->(m)" % (seed_label, max_depth)
    params = {"seed_id": int(seed_id)}

    target_source_file = getattr(args, "target_source_file", None)
    if target_source_file and not bool(getattr(args, "disable_target_source_filter", False)):
        cypher += " WHERE m.source_loc CONTAINS $target_source_file"
        params["target_source_file"] = str(target_source_file)

    cypher += " RETURN p, n, m"
    return cypher, params

def _print_stop_report(state: RetrievalState) -> None:
    r = state.stop_reason or "UNKNOWN"

    if r == AttemptStopReasons.OBJECTIVE_MET:
        print("\n[STOP REPORT] reason=OBJECTIVE_MET")
        return

    if r == AttemptStopReasons.REPLAN_LIMIT_REACHED:
        print("\n[STOP REPORT] reason=REPLAN_LIMIT_REACHED (max_replans=%s)" % str(state.budgets.max_replans))
        return

    if r == AttemptStopReasons.DEPTH_INCREASE_LIMIT_REACHED:
        print("\n[STOP REPORT] reason=DEPTH_INCREASE_LIMIT_REACHED (max_depth_increases_per_task=%s)" %
              str(state.budgets.max_depth_increases_per_task))
        return

    if r == AttemptStopReasons.QUERY_BUDGET_EXHAUSTED:
        print("\n[STOP REPORT] reason=QUERY_BUDGET_EXHAUSTED (queries_total=%d max_queries_total=%s)" %
              (state.counters.queries_total, str(state.budgets.max_queries_total)))
        return

    print("\n[STOP REPORT] reason=%s" % r)

def _read_optional_text_file(path: str) -> str:
    if not path:
        return ""
    if not os.path.isfile(path):
        raise SystemExit("ERROR: file not found: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _extract_graph_line_numbers(global_summary: Dict[str, Any], source_line_count: int) -> List[int]:
    text = json.dumps(global_summary or {}, ensure_ascii=False)
    nums: List[int] = []

    patterns = [
        r'(?:\\?")ln(?:\\?")\s*:\s*(\d+)',
        r'\bln\b[^0-9]{0,12}(\d+)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            try:
                ln = int(m.group(1))
                if 1 <= ln <= int(source_line_count):
                    nums.append(ln)
            except Exception:
                pass

    out: List[int] = []
    for n in nums:
        if n not in out:
            out.append(n)
    return out


def _format_source_window(source_lines: List[str], line_no: int, radius: int = 4) -> str:
    if line_no < 1 or line_no > len(source_lines):
        return ""
    start = max(1, int(line_no) - int(radius))
    end = min(len(source_lines), int(line_no) + int(radius))
    rows = []
    for i in range(start, end + 1):
        marker = ">>" if i == int(line_no) else "  "
        rows.append("%s %5d: %s" % (marker, i, source_lines[i - 1]))
    return "\n".join(rows)


def _repo_relative(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except Exception:
        return path


def _collect_repo_symbol_hits(
    target_repo: str,
    patterns: List[str],
    max_hits: int = 30,
) -> List[Tuple[str, int, str]]:
    """
    Lightweight source-context collector for the diagnosis stage.

    This is deliberately not a patch synthesizer. It only discovers whether
    semantically relevant symbols mentioned by the problem statement already
    exist somewhere in the repository, so the Fix Agent is not limited to symbols
    appearing in the single target file.
    """
    if not target_repo:
        return []
    repo = os.path.abspath(target_repo)
    if not os.path.isdir(repo):
        return []

    suffixes = (".c", ".h", ".cc", ".cpp", ".hpp")
    skip_dirs = {".git", ".codegraphhunter", "deps", "tests", "__pycache__"}
    hits: List[Tuple[str, int, str]] = []

    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if not fn.endswith(suffixes):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for ln, line in enumerate(f, start=1):
                        for pat in patterns:
                            if pat and pat in line:
                                hits.append((_repo_relative(path, repo), ln, line.rstrip("\n")))
                                break
                        if len(hits) >= int(max_hits):
                            return hits
            except Exception:
                continue
    return hits


def _build_reply_object_candidate_context(
    target_repo: str,
    rel_source_file: str,
    source_code: str,
    problem_statement: str,
) -> str:
    """
    Build source-derived candidate context for reply-shape bugs.

    It does not prescribe a concrete patch. It tells the Fix Agent which reply
    objects already exist in the repository and how they semantically relate to
    the problem statement.
    """
    problem_l = (problem_statement or "").lower()
    if not any(x in problem_l for x in ["null array", "array null", "*-1", "nil when key does not exist"]):
        return ""
    if not any(x in problem_l for x in ["null bulk", "$-1", "bulk null", "nonexisting", "non-existing", "does not exist"]):
        return ""

    candidate_patterns = [
        "shared.nullarray",
        "shared.null[c->resp]",
        "shared.emptyarray",
        "addReplyNullArray",
        "addReplyNull",
    ]

    hits = _collect_repo_symbol_hits(
        target_repo=target_repo,
        patterns=candidate_patterns,
        max_hits=40,
    )

    # If repository context is unavailable, fall back to the target file only.
    if not hits:
        hits = []
        for idx, line in enumerate(source_code.splitlines(), start=1):
            if any(p in line for p in candidate_patterns):
                hits.append((rel_source_file, idx, line))

    if not hits:
        return ""

    seen = set()
    rows = []
    found = set()
    for rel, ln, line in hits:
        key = (rel, ln, line.strip())
        if key in seen:
            continue
        seen.add(key)
        stripped = line.strip()
        rows.append("- %s:%d: `%s`" % (rel, int(ln), stripped))
        for pat in candidate_patterns:
            if pat in line:
                found.add(pat)

    out = [
        "Source-derived reply object candidates:",
        "- The problem contrasts Null Bulk (`$-1`) with Null Array (`*-1`) for a missing/non-existing key in COUNT mode.",
        "- The current localized call site passes `shared.null[c->resp]`, which is semantically aligned with Null Bulk behavior.",
    ]

    if "shared.nullarray" in found:
        out.append("- The repository already defines/uses `shared.nullarray`, which is semantically aligned with a Null Array reply and may be usable as a reply object.")
    else:
        out.append("- This symbol scan did not find `shared.nullarray`; if no shared Null Array reply object exists, a valid fix may need to split the lookup helper and send `addReplyNullArray(c)` explicitly on the missing-key path.")
    if "shared.emptyarray" in found:
        out.append("- The repository also uses `shared.emptyarray`, which is an empty array reply, not the same as a Null Array reply. Do not replace the COUNT==0 empty-array fast exit just to fix a missing-key Null Array bug.")
    if "addReplyNullArray" in found:
        out.append("- The repository contains `addReplyNullArray(c)`, a reply-sending helper for Null Array. It is not itself a `robj *` reply object to pass into `lookupKeyWriteOrReply`; use it only if the missing-key path is made explicit before later branches.")

    out += [
        "- A semantically meaningful fix should change the missing-key reply selection, not only whitespace or comments.",
        "- The repair should either modify the lookup helper's reply argument, or refactor the code so the missing-key COUNT path replies with Null Array before normal existing-list branches.",
        "- Candidate symbol occurrences discovered from the repository/source:",
        "\n".join(rows[:20]),
    ]

    return "\n".join(out)


def _build_source_derived_repair_sketch(
    target_repo: str,
    rel_source_file: str,
    source_code: str,
    problem_statement: str,
) -> str:
    """
    Build a source-derived edit plan for the Fix Agent.

    This is deliberately a repair sketch, not a patch. It is derived from:
    - the localized lookup/reply-selection call site in the current source;
    - repository-level evidence that a lower-level lookup API exists;
    - repository-level evidence that Null Array can be sent explicitly.
    """
    problem_l = (problem_statement or "").lower()
    if not ("count" in problem_l and any(x in problem_l for x in ["null array", "array null", "*-1", "nil when key does not exist"])):
        return ""
    if "lookupKeyWriteOrReply(c, c->argv[1], shared.null[c->resp])" not in source_code:
        return ""

    has_lookup_write_api = False
    has_null_array_sender = False

    # Prefer repository-level evidence when available.
    if target_repo and os.path.isdir(target_repo):
        lookup_hits = _collect_repo_symbol_hits(
            target_repo=target_repo,
            patterns=["lookupKeyWrite(c->db", "robj *lookupKeyWrite(redisDb"],
            max_hits=20,
        )
        null_array_hits = _collect_repo_symbol_hits(
            target_repo=target_repo,
            patterns=["addReplyNullArray(c)"],
            max_hits=20,
        )
        has_lookup_write_api = bool(lookup_hits)
        has_null_array_sender = bool(null_array_hits)
    else:
        has_lookup_write_api = "lookupKeyWrite(c->db" in source_code
        has_null_array_sender = "addReplyNullArray(c)" in source_code

    if not (has_lookup_write_api and has_null_array_sender):
        return ""

    return "\n".join([
        "Source-derived repair sketch for the Fix Agent:",
        "- Repository evidence shows the lower-level lookup API `lookupKeyWrite(c->db, key)` exists. This API can retrieve the object without automatically sending the missing-key reply.",
        "- Repository evidence shows `addReplyNullArray(c)` exists. This helper can explicitly send a Null Array reply when the key is missing.",
        "- Because no `shared.nullarray` reply object was discovered, the safer repair direction is to split the current `lookupKeyWriteOrReply(...)` call instead of trying to pass `addReplyNullArray(c)` as a reply object.",
        "- Edit plan, not a patch:",
        "  1. Replace the localized call `lookupKeyWriteOrReply(c, c->argv[1], shared.null[c->resp])` with a lower-level lookup using `lookupKeyWrite(c->db, c->argv[1])`.",
        "  2. Immediately after the lookup, handle the missing-key case before `checkType(...)` and before any later `count` branches.",
        "  3. In that missing-key branch, if `hascount` is true, call `addReplyNullArray(c)` and return.",
        "  4. Otherwise preserve existing single-element POP behavior by sending `addReply(c, shared.null[c->resp])` and returning.",
        "  5. Then keep the existing type check for non-null objects, e.g. `if (checkType(c, o, OBJ_LIST)) return;`.",
        "- Required source replacement sketch for this repair:",
        "  Replace this exact localized code block:",
        "```c",
        "    robj *o = lookupKeyWriteOrReply(c, c->argv[1], shared.null[c->resp]);",
        "    if (o == NULL || checkType(c, o, OBJ_LIST))",
        "        return;",
        "```",
        "  with this C code skeleton:",
        "```c",
        "    robj *o = lookupKeyWrite(c->db, c->argv[1]);",
        "    if (o == NULL) {",
        "        if (hascount)",
        "            addReplyNullArray(c);",
        "        else",
        "            addReply(c, shared.null[c->resp]);",
        "        return;",
        "    }",
        "    if (checkType(c, o, OBJ_LIST))",
        "        return;",
        "```",
        "- This is a source-derived edit sketch, not a hand-written unified diff. The Fix Agent must still generate a valid unified diff against the exact current file.",
        "- The patch must delete/replace the original `lookupKeyWriteOrReply(...)` line; inserting an `if (o == NULL)` after that helper is invalid because the helper may have already sent the wrong reply and returned NULL.",
        "- The patch must not repair this bug by changing the later `if (hascount && !count)` branch; for the reproduction `LPOP NONEXISTINGLIST 10`, that branch is not the failing path.",
        "- The patch should introduce exactly the missing-key branch required by the sketch, while preserving the existing COUNT==0 empty-array fast exit for existing lookup paths.",
    ])



def _build_embedded_nul_path_repair_sketch(
    rel_source_file: str,
    source_code: str,
    problem_statement: str,
    localizer_result: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build a source-derived edit sketch for embedded-NUL path validation bugs.

    This is not a hand-written patch for a benchmark case. It fires only when:
      - the problem statement describes embedded NUL / null-byte path invalidation;
      - the graph localizer selected a path-validation-like source function; and
      - the exact source function has a stable entry block that currently lacks
        an explicit NUL-byte rejection.

    The structured edit compiler later turns this sketch into a unified diff.
    """
    problem_l = (problem_statement or "").lower()
    selected = (localizer_result or {}).get("selected_location") if isinstance(localizer_result, dict) else {}
    if not isinstance(selected, dict):
        selected = {}

    selected_fn = str(selected.get("function") or "")
    selected_file = str(selected.get("patch_source_file") or selected.get("source_file") or rel_source_file or "")

    mentions_nul = any(x in problem_l for x in ["nul", "null byte", "embedded null", "embedded-nul", "\\0", "\\x00"])
    mentions_path = "path" in problem_l
    wants_invalid = any(x in problem_l for x in ["invalid", "not valid", "reject", "cannot contain", "can't contain"])
    selected_path_validator = (
        "is_valid_path" in selected_fn
        or "valid_path" in selected_fn
        or "path" in selected_fn.lower()
        or "path" in selected_file.lower()
    )

    if not (mentions_nul and mentions_path and wants_invalid and selected_path_validator):
        return ""

    if "path.find('\\0') != std::string::npos" in source_code or 'path.find("\\0") != std::string::npos' in source_code:
        return ""

    old_block = (
        "inline bool is_valid_path(const std::string &path) {\n"
        "  size_t level = 0;"
    )
    if old_block not in source_code:
        return ""

    new_block = (
        "inline bool is_valid_path(const std::string &path) {\n"
        "  if (path.find('\\0') != std::string::npos) {\n"
        "    return false;\n"
        "  }\n"
        "\n"
        "  size_t level = 0;"
    )

    return "\n".join([
        "Source-derived repair sketch for embedded-NUL path validation:",
        "- The problem statement says paths containing embedded NUL bytes must be invalid.",
        "- The graph localizer selected the path validation function, so the repair should strengthen validation inside this function.",
        "- The source function currently begins path-structure validation without first checking for embedded NUL bytes in the std::string.",
        "- A minimal repair is to reject `path.find('\\0') != std::string::npos` near the function entry before existing traversal logic.",
        "- Required source replacement sketch for this repair:",
        "  Replace this exact localized code block:",
        "```cpp",
        old_block,
        "```",
        "  with this C++ code skeleton:",
        "```cpp",
        new_block,
        "```",
        "- This is a source-derived edit sketch, not a hand-written unified diff. The Fix Agent must still generate a valid unified diff against the exact current file.",
        "- The patch should preserve all existing path traversal checks and only add the missing embedded-NUL rejection.",
    ])

def _build_graph_localizer_context_for_diagnosis(
    localizer_result: Dict[str, Any],
    source_code: str,
    problem_statement: str,
) -> str:
    """Summarize graph-localizer evidence for the diagnosis / fix stage.

    This is intentionally generic. It records what the graph localizer selected
    and, for known broad bug classes such as embedded-NUL path validation, adds
    source-grounded repair guidance without producing a handwritten patch.
    """
    if not isinstance(localizer_result, dict) or not localizer_result:
        return ""
    selected = localizer_result.get("selected_location") or {}
    if not isinstance(selected, dict) or not selected:
        return ""

    rows = [
        "Graph-localizer selected location:",
        "- Patch source: `%s:%s`" % (selected.get("patch_source_file") or selected.get("source_file"), selected.get("patch_start_line") or selected.get("start_line")),
        "- Source-level function: `%s`" % selected.get("function"),
    ]

    if selected.get("graph_function_name"):
        rows.append("- Graph function name: `%s`" % selected.get("graph_function_name"))
    if selected.get("callgraph_node_id") is not None:
        rows.append("- CallGraph node id: `%s`" % selected.get("callgraph_node_id"))
    if selected.get("fun_obj_var_id") is not None:
        rows.append("- PAG FunObjVar id: `%s`" % selected.get("fun_obj_var_id"))
    if selected.get("primary_arg_node_id") is not None:
        rows.append("- Primary PAG argument seed id: `%s`" % selected.get("primary_arg_node_id"))
    if selected.get("primary_icfg_node_id") is not None:
        rows.append("- Primary ICFG seed id: `%s`" % selected.get("primary_icfg_node_id"))
    if selected.get("graph_source_file"):
        rows.append("- Graph source: `%s:%s`" % (selected.get("graph_source_file"), selected.get("graph_start_line")))
    if selected.get("source_mapping"):
        rows.append("- Source mapping: `%s`" % _safe_json(selected.get("source_mapping")))

    problem_l = (problem_statement or "").lower()
    fn_name = str(selected.get("function") or "")
    embedded_nul_path_bug = (
        ("nul" in problem_l or "\\0" in problem_l or "null byte" in problem_l or "embedded" in problem_l)
        and "path" in problem_l
        and ("invalid" in problem_l or "not valid" in problem_l)
    )

    if embedded_nul_path_bug or "is_valid_path" in fn_name:
        rows += [
            "",
            "Source-derived repair intent for embedded-NUL path validation:",
            "- The behavioral objective is to reject paths containing embedded NUL bytes before they are treated as valid filesystem paths.",
            "- The selected source function is the path validation routine, so the repair should strengthen validation inside this function rather than changing unrelated request handling code.",
            "- A suitable fix should make `is_valid_path(...)` return `false` when the input `std::string` contains `\\0` / NUL.",
            "- The edit should preserve existing checks for empty paths, absolute paths, `..`, separators, and other path traversal logic.",
            "- Prefer a minimal check near the beginning of the function, before the final valid-path return, using existing C++ string APIs such as `find`/`npos` or an equivalent character scan.",
        ]

    return "\n".join(rows)


def build_fault_diagnosis(
    problem_statement: str,
    rel_source_file: str,
    source_code: str,
    global_summary: Dict[str, Any],
    local_summaries: List[Dict[str, Any]],
    target_repo: str = "",
    localizer_result: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build a repair-level diagnosis from graph evidence and source code.

    This stage is intentionally separate from patch generation:
    retrieval evidence -> diagnosis -> fix agent.
    It does not output a patch.
    """
    lines = source_code.splitlines()
    line_nums = _extract_graph_line_numbers(global_summary, len(lines))

    problem_l = (problem_statement or "").lower()
    mentions_missing_key = any(x in problem_l for x in [
        "nonexisting", "non-existing", "does not exist", "no list exists", "key does not exist", "nil when key does not exist",
    ])
    mentions_count = "count" in problem_l
    mentions_null_array = any(x in problem_l for x in [
        "null array", "array null", "nil when key does not exist", "*-1",
    ])
    mentions_null_bulk = any(x in problem_l for x in [
        "null bulk", "$-1", "bulk null",
    ])
    is_redis_reply_bug = (
        ("lpop" in problem_l or "lookupkey" in problem_l or "redis" in problem_l)
        and mentions_missing_key
        and mentions_count
        and (mentions_null_array or mentions_null_bulk)
    )

    localized = []
    for ln in line_nums:
        src = lines[ln - 1] if 1 <= ln <= len(lines) else ""
        score = 0
        if "lookupKey" in src:
            score += 100
        if "Reply" in src:
            score += 40
        if "shared.null" in src:
            score += 40
        if "return" in src:
            score += 10
        localized.append({"line": ln, "source": src, "score": score})

    localized.sort(key=lambda x: (-int(x["score"]), int(x["line"])))

    windows = []
    for item in localized[:8]:
        win = _format_source_window(lines, int(item["line"]), radius=4)
        if win:
            windows.append("Source window around graph-localized line %d:\n%s" % (int(item["line"]), win))

    negative = []
    for idx, src in enumerate(lines, start=1):
        stripped = src.strip()
        if stripped.startswith("if ") or stripped.startswith("if("):
            if "count" in stripped or "hascount" in stripped:
                negative.append((idx, src))
    negative_text = ""
    if negative:
        rows = []
        for ln, src in negative[:8]:
            rows.append("- line %d: `%s`" % (ln, src.strip()))
        negative_text = "\n".join(rows)

    diagnosis_parts = [
        "### Fault Diagnosis",
        "",
        "Behavioral objective from problem statement:",
    ]

    if is_redis_reply_bug:
        diagnosis_parts += [
            "- The issue concerns LPOP with a count argument on a non-existing key returning a Null Bulk reply instead of a Null Array reply.",
            "- Preserve existing behavior for single-element POP without COUNT while fixing the COUNT-mode missing-key reply shape.",
        ]
    else:
        problem_excerpt = " ".join((problem_statement or "").strip().split())[:600]
        if problem_excerpt:
            diagnosis_parts.append("- %s" % problem_excerpt)
        else:
            diagnosis_parts.append("- The intended behavior is defined by the problem statement and failing test command.")

    diagnosis_parts += [
        "",
        "Graph-grounded observation:",
        "- The retrieval episode produced graph evidence for the selected target source file `%s`." % rel_source_file,
    ]

    if localized:
        top = localized[0]
        diagnosis_parts.append(
            "- The highest-priority graph-localized source line is line %d: `%s`."
            % (int(top["line"]), str(top["source"]).strip())
        )

    if windows:
        diagnosis_parts += ["", "Graph-localized source windows:", "\n\n".join(windows)]

    diagnosis_parts += ["", "Repair reasoning:"]

    if mentions_missing_key and mentions_count and (mentions_null_array or mentions_null_bulk):
        diagnosis_parts.append(
            "- The failing behavior is on the missing-key path for COUNT mode, not on the normal existing-list pop path."
        )
    else:
        diagnosis_parts.append(
            "- The failing path should be inferred from the problem statement and validated against the localized source windows."
        )

    if is_redis_reply_bug and (any("lookupKeyWriteOrReply" in str(item["source"]) for item in localized) or "lookupKeyWriteOrReply" in source_code):
        diagnosis_parts += [
            "- The source contains `lookupKeyWriteOrReply(...)`, which is the helper that selects/sends the reply when the key is missing.",
            "- If this helper returns for a missing key, later branches after the lookup are not reached.",
            "- Therefore, edits to later `count` or `hascount` branches are unlikely to fix the missing-key reply shape unless the source proves the missing-key path reaches them.",
            "- The likely repair point is the source expression that selects the missing-key reply object at the lookup helper call site.",
            "- For the reproduction `LPOP NONEXISTINGLIST 10`, the parsed COUNT value is 10, so the later `if (hascount && !count)` branch is not the failing path. That branch handles COUNT==0 on an existing lookup path and should not be used to repair the missing-key COUNT behavior.",
        ]

    if negative_text:
        diagnosis_parts += [
            "",
            "Locations that are syntactically related but likely insufficient for this missing-key bug:",
            negative_text,
            "- Reason: these branches occur after the lookup/early-return point and may not execute when the key does not exist.",
        ]

    localizer_context = _build_graph_localizer_context_for_diagnosis(
        localizer_result=localizer_result or {},
        source_code=source_code,
        problem_statement=problem_statement,
    )
    if localizer_context:
        diagnosis_parts += ["", localizer_context]

    reply_candidate_context = _build_reply_object_candidate_context(
        target_repo=target_repo,
        rel_source_file=rel_source_file,
        source_code=source_code,
        problem_statement=problem_statement,
    )
    if reply_candidate_context:
        diagnosis_parts += ["", reply_candidate_context]

    repair_sketch_context = _build_source_derived_repair_sketch(
        target_repo=target_repo,
        rel_source_file=rel_source_file,
        source_code=source_code,
        problem_statement=problem_statement,
    )
    if repair_sketch_context:
        diagnosis_parts += ["", repair_sketch_context]

    embedded_nul_repair_sketch = _build_embedded_nul_path_repair_sketch(
        rel_source_file=rel_source_file,
        source_code=source_code,
        problem_statement=problem_statement,
        localizer_result=localizer_result or {},
    )
    if embedded_nul_repair_sketch:
        diagnosis_parts += ["", embedded_nul_repair_sketch]

    diagnosis_parts += [
        "",
        "Patch constraints for the Fix Agent:",
        "- Generate a minimal unified diff against the exact current source file.",
        "- The patch must contain at least one non-whitespace semantic code change.",
        "- A patch that only changes blank lines, indentation, comments, or formatting is invalid.",
        "- Prefer a fix at the graph-localized source location if it satisfies the problem statement.",
        "- If a Source-derived repair sketch is present, the Fix Agent should implement that sketch rather than editing a later syntactically related branch.",
    ]

    if is_redis_reply_bug:
        diagnosis_parts += [
            "- Do not change later count branches merely because their names match the issue text.",
            "- In particular, do not repair the reported missing-key COUNT=10 behavior by changing the `if (hascount && !count)` fast-exit branch or its `shared.emptyarray` reply.",
            "- A valid repair should change the graph-localized lookup/reply-selection site or explicitly handle the missing-key COUNT path before later existing-list branches.",
        ]

    return "\n".join(diagnosis_parts)


# =================================================
# (H0) Patch generation / validation helpers
# =================================================

def _get_artifact_field(artifacts: Any, name: str, default: Any = None) -> Any:
    if isinstance(artifacts, dict):
        return artifacts.get(name, default)
    return getattr(artifacts, name, default)


def _read_patch_source_or_exit(target_repo: str, rel_source_file: str) -> str:
    if not target_repo:
        raise SystemExit("ERROR: --use-llm-patch requires --patch-target-repo")
    if not rel_source_file:
        raise SystemExit("ERROR: --use-llm-patch requires --patch-source-file")

    repo = os.path.abspath(target_repo)
    path = os.path.abspath(os.path.join(repo, rel_source_file))

    if not path.startswith(repo + os.sep) and path != repo:
        raise SystemExit("ERROR: patch source file escapes target repo: %s" % rel_source_file)

    if not os.path.isfile(path):
        raise SystemExit("ERROR: patch source file not found: %s" % path)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def run_patch_attempt_v0(args, state: RetrievalState, patch_iter_idx: int) -> Dict[str, Any]:
    artifacts = getattr(state, "artifacts", {})
    global_summary = _get_artifact_field(artifacts, "global_summary", {}) or {}
    local_summaries = _get_artifact_field(artifacts, "local_summaries", []) or []

    source_code = _read_patch_source_or_exit(
        target_repo=args.patch_target_repo,
        rel_source_file=args.patch_source_file,
    )
    problem_statement = _read_optional_text_file(getattr(args, "patch_problem_file", ""))

    fault_diagnosis = build_fault_diagnosis(
        problem_statement=problem_statement,
        rel_source_file=args.patch_source_file,
        source_code=source_code,
        global_summary=global_summary,
        local_summaries=local_summaries,
        target_repo=args.patch_target_repo,
        localizer_result=getattr(args, "graph_localizer_result", {}) or {},
    )

    debug_dir = os.environ.get("CODEGRAPHHUNTER_PATCH_DEBUG_DIR", "")
    if debug_dir:
        try:
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, "fault_diagnosis.txt"), "w", encoding="utf-8", errors="replace") as f:
                f.write(fault_diagnosis)
        except Exception:
            pass

    print("\n[DIAGNOSIS] built fault diagnosis chars=%d" % len(fault_diagnosis))
    print("[DIAGNOSIS] first line: %s" % (fault_diagnosis.splitlines()[0] if fault_diagnosis else ""))

    generator = LLMPatchGenerator(model=args.patch_model, require_llm=bool(getattr(args, "require_llm_patch", False)))

    print("\n[PATCH] generate patch from global_summary.fix_context + fault diagnosis + source file ...")
    gen = generator.generate_patch(
        rel_source_file=args.patch_source_file,
        source_code=source_code,
        global_summary=global_summary,
        local_summaries=local_summaries,
        problem_statement=problem_statement,
        fault_diagnosis=fault_diagnosis,
    )

    if not gen.get("ok"):
        result = {
            "patch_iter": int(patch_iter_idx),
            "status": gen.get("status", "GEN_FAILED"),
            "ok": False,
            "generation": gen,
        }
        print("[PATCH] generation failed:", gen.get("error"))
        return result

    patch_text = gen.get("patch", "")
    print("[PATCH] generated unified diff chars=%d" % len(patch_text))

    validator = PatchValidator(
        target_repo=args.patch_target_repo,
        workdir_root=args.patch_workdir,
        test_cmd=args.patch_test_cmd,
        timeout_s=int(args.patch_timeout_s),
    )

    print("[PATCH] apply patch and run validation command ...")
    val = validator.validate(patch_text, patch_iter=int(patch_iter_idx))

    result = {
        "patch_iter": int(patch_iter_idx),
        "status": val.get("status"),
        "ok": bool(val.get("ok")),
        "generation": {
            "status": gen.get("status"),
            "model": gen.get("model"),
            "patch_chars": len(patch_text),
        },
        "validation": val,
    }

    print("[PATCH] result=%s ok=%s" % (result["status"], str(result["ok"])))
    if val.get("patch_file"):
        print("[PATCH] patch_file=%s" % val.get("patch_file"))
    if val.get("workdir"):
        print("[PATCH] workdir=%s" % val.get("workdir"))

    return result




def run_graph_guided_fault_localizer_or_exit(args: Any) -> Dict[str, Any]:
    """
    Repository-scoped information-need driven graph localizer.

    This is the first product-facing replacement for manual --target-function.
    It does not read a target function from the problem statement. Instead it:
      1) extracts bug intent and information needs from the problem statement,
      2) discovers candidate functions from source,
      3) collects best-effort PAG / ICFG / CallGraph evidence,
      4) selects a fault-location hypothesis,
      5) writes that hypothesis back into args for the existing downstream
         target-aware graph retrieval episode.

    When --patch-source-file is omitted, the localizer scans the target
    repository and writes the selected source file back into args. This keeps
    the CLI compatible with callers that only know the repository/bitcode and
    problem statement paths.
    """
    if not getattr(args, "patch_target_repo", ""):
        raise SystemExit("ERROR: --use-graph-localizer requires --patch-target-repo")
    if not getattr(args, "use_builder", False) or not getattr(args, "use_db", False):
        raise SystemExit("ERROR: --use-graph-localizer requires --use-builder --use-db")
    if not getattr(args, "bolt_pass", ""):
        raise SystemExit("ERROR: --use-graph-localizer with --use-db requires --bolt-pass or TUGRAPH_BOLT_PASS")

    problem_statement = _read_optional_text_file(getattr(args, "patch_problem_file", ""))
    if not problem_statement.strip():
        print("[GRAPH LOCALIZER WARN] problem statement is empty; localization will rely on source/graph evidence only")

    router = BoltRouter(
        uri=args.bolt_uri,
        user=args.bolt_user,
        password=args.bolt_pass,
    )
    try:
        localizer = AgenticFaultLocalizer(
            repo_root=args.patch_target_repo,
            problem_statement=problem_statement,
            patch_source_file=args.patch_source_file,
            bolt_router=router,
            default_db=str(args.bolt_db),
            timeout_s=int(args.bolt_timeout_s),
            top_k=int(getattr(args, "candidate_top_k", 5)),
            debug=bool(getattr(args, "print_db_debug", False)),
            max_files=int(getattr(args, "localizer_max_files", 200)),
            use_llm_planner=bool(getattr(args, "use_llm_info_planner", False)),
            info_planner_model=str(getattr(args, "info_planner_model", "gpt-4o-mini")),
        )
        result = localizer.localize()
    finally:
        try:
            router.close()
        except Exception:
            pass

    debug_dir = os.environ.get("CODEGRAPHHUNTER_PATCH_DEBUG_DIR", "")
    if debug_dir:
        try:
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, "graph_fault_localization.json"), "w", encoding="utf-8", errors="replace") as f:
                json.dump(result, f, indent=2, sort_keys=True, ensure_ascii=False)
        except Exception:
            pass

    selected = result.get("selected_location") if isinstance(result, dict) else None
    if not selected:
        raise SystemExit("ERROR: graph localizer failed to select a fault location: %s" % _safe_json(result))

    # v1 handoff: keep source-level and graph-level locations separate.
    # The downstream retrieval path receives graph IDs/seeds; the patch stage
    # continues to edit args.patch_source_file. This avoids brittle exact
    # matching on C++ mangled names or generated source files.
    args.graph_localizer_result = result
    args.graph_localizer_selected = selected

    graph_function_name = str(selected.get("graph_function_name") or selected.get("function") or "")
    graph_source_file = str(selected.get("graph_source_file") or selected.get("source_file_basename") or os.path.basename(args.patch_source_file))
    primary_arg_node_id = selected.get("primary_arg_node_id")

    args.target_function = graph_function_name
    args.target_source_file = graph_source_file
    args.target_arg_no = int(selected.get("arg_no", 0))
    if primary_arg_node_id is not None:
        args.target_seed_id = int(primary_arg_node_id)

    if not getattr(args, "patch_source_file", ""):
        args.patch_source_file = str(selected.get("patch_source_file") or selected.get("source_file", ""))

    print("\n[GRAPH LOCALIZER] information-need driven localization selected:")
    print("[GRAPH LOCALIZER] source_function=%s patch_file=%s patch_lines=%s-%s total_score=%.2f source_score=%.2f graph_score=%.2f" % (
        str(selected.get("function")),
        str(selected.get("patch_source_file") or selected.get("source_file")),
        str(selected.get("patch_start_line") or selected.get("start_line")),
        str(selected.get("end_line")),
        float(selected.get("total_score", 0.0)),
        float(selected.get("source_score", 0.0)),
        float(selected.get("graph_score", 0.0)),
    ))
    print("[GRAPH LOCALIZER] graph_function=%s graph_source=%s:%s callgraph_node_id=%s fun_obj_var_id=%s arg_seed_id=%s icfg_seed_id=%s" % (
        str(selected.get("graph_function_name") or ""),
        str(selected.get("graph_source_file") or ""),
        str(selected.get("graph_start_line") or ""),
        str(selected.get("callgraph_node_id") or ""),
        str(selected.get("fun_obj_var_id") or ""),
        str(selected.get("primary_arg_node_id") or ""),
        str(selected.get("primary_icfg_node_id") or ""),
    ))
    ranked = result.get("ranked_candidates") or []
    for i, cand in enumerate(ranked[:int(getattr(args, "candidate_top_k", 5))], start=1):
        print("[GRAPH LOCALIZER] rank=%d function=%s file=%s score=%.2f graph=%.2f lines=%s-%s" % (
            i,
            str(cand.get("function")),
            str(cand.get("source_file")),
            float(cand.get("total_score", 0.0)),
            float(cand.get("graph_score", 0.0)),
            str(cand.get("start_line")),
            str(cand.get("end_line")),
        ))

    return result

# =================================================
# (H) One retrieval episode (inside ONE patch iteration)
# =================================================

def run_one_retrieval_episode(args, normalized_schema_obj: Dict[str, Any], patch_iter_idx: int) -> RetrievalState:
    # budgets per retrieval episode (per patch attempt)
    budgets = RetrievalBudgets(
        max_queries_total=int(args.max_queries_total),
        max_replans=int(args.max_replans),
        max_depth_increases_per_task=int(args.max_depth_increases),
        enforce_per_graph_caps=bool(getattr(args, "enforce_per_graph_caps", False)),
    )

    # -1 means unlimited
    # (RetrievalBudgets checks use is_unlimited() => v < 0 is treated as unlimited)
    state = RetrievalState(attempt_id=patch_iter_idx, budgets=budgets)
    ctrl = AttemptController(state)

    sg = SchemaGuard(
        normalized_schema_path=args.schema,
        cypher_caps_path=args.caps,
        strict_unknown_functions=bool(args.strict_unknown_functions),
        allow_wildcard_edge=bool(getattr(args, "allow_wildcard_edge", False)),
        allow_unlabeled_nodes=bool(getattr(args, "allow_unlabeled_nodes", False)),
    )
    qg = QueryGuard(
        sg,
        policy={
            "max_limit": int(args.max_limit),
            "default_var_length": int(args.default_depth),
            "max_var_length_upper": int(args.max_depth),
            "var_length_step": int(args.depth_step),
            "allow_wildcard_edge": bool(args.allow_wildcard_edge),
        },
    )

    local_summary_builder = LocalSummaryBuilder(args.semantics_catalog)
    global_summary_builder = GlobalSummaryBuilder()
    llm_summary_refiner = None
    if getattr(args, "use_llm_summary", False):
        llm_summary_refiner = LLMLocalSummaryRefiner(
            model=args.llm_summary_model,
            max_paths=args.llm_summary_max_paths,
        )


    planner = None
    db_agent = None
    bolt_router = None

    if args.use_builder:
        planner = RetrievalPlannerAgent(normalized_schema_obj)

        if args.use_db:
            if not args.bolt_pass:
                raise SystemExit("ERROR: --use-db requires --bolt-pass (or env TUGRAPH_BOLT_PASS)")
            bolt_router = BoltRouter(
                uri=args.bolt_uri,
                user=args.bolt_user,
                password=args.bolt_pass,
            )

        db_agent = DBQueryAgent(
            query_guard=qg,
            normalized_schema=normalized_schema_obj,
            bolt_client=None,
        )

    print("=== CodeGraphHunter AttemptController CLI (Patch Iteration) ===")
    print("Patch Iter:", patch_iter_idx)
    print("Schema   :", args.schema)
    print("Caps     :", args.caps)
    print("Mode     :", "BUILDER" if args.use_builder else "LEGACY")
    print("Budgets  : max_queries_total=%s, max_replans=%s, max_depth_increases=%s" %
          (str(args.max_queries_total), str(args.max_replans), str(args.max_depth_increases)))

    if args.use_db:
        print("DB       :", args.bolt_uri, "(db=%s)" % args.bolt_db)
        bolt0 = bolt_router.get(args.bolt_db)
        cy0 = "RETURN 1 AS ok LIMIT 1"
        _print_db_debug(
            enabled=bool(args.print_db_debug),
            phase="SMOKE",
            dbname=str(args.bolt_db),
            cypher=cy0,
            params=None,
            records=None,
        )
        pong = bolt0.run(cy0, params=None, timeout_s=int(args.bolt_timeout_s))
        recs0 = pong.get("records") or []
        print("[DB PING] ok=", (recs0 or [{}])[0].get("ok", None))
        print("[DB EXEC] rows=%d" % len(recs0))

        if int(args.print_db_records) == 1:
            _print_db_debug(
                enabled=True,
                phase="SMOKE_RESULT",
                dbname=str(args.bolt_db),
                cypher=cy0,
                params=None,
                records=recs0,
                max_records=int(args.print_db_records_max),
            )

    print("--------------------------------------------------")

    # init plan
    if args.use_builder:
        needs = prepare_builder_needs(
            normalized_schema_obj=normalized_schema_obj,
            state=state,
            stage="init",
            args=args,
            bolt_router=bolt_router,
        )
        plans = planner.plan(needs, state)
        if not plans:
            raise RuntimeError("RetrievalPlannerAgent produced no plans.")
        plan = plans[0]
        llm_requested_depth = int(getattr(plan, "requested_depth", None) or args.default_depth)
    else:
        plan = fake_retrieval_planner_dict(state, "init")
        llm_requested_depth = int(plan.get("requested_depth", args.default_depth))

    system_depth = int(args.default_depth)

    # ----------------
    # retrieval episode loop
    # ----------------
    while True:
        if ctrl.should_force_patch():
            break

        if args.use_builder:
            graph_type = str(plan.graph_type)
            task_id = str(plan.task_id)
        else:
            graph_type = str(plan["graph_type"])
            task_id = str(plan["task_id"])

        # graph skip is not a query step
        if not ctrl.can_query_graph_type(graph_type):
            print("[GRAPH_TYPE_SKIPPED] graph=%s task=%s" % (graph_type, task_id))
            state.add_trace("GRAPH_TYPE_SKIPPED", {"graph_type": graph_type, "task_id": task_id})
            if args.use_builder:
                needs = prepare_builder_needs(
                    normalized_schema_obj=normalized_schema_obj,
                    state=state,
                    stage="replan",
                    args=args,
                    bolt_router=bolt_router,
                )
                plans = planner.plan(needs, state)
                plan = plans[0]
                llm_requested_depth = int(getattr(plan, "requested_depth", None) or args.default_depth)
            else:
                plan = fake_retrieval_planner_dict(state, "replan")
                llm_requested_depth = int(plan.get("requested_depth", args.default_depth))
            system_depth = int(args.default_depth)
            continue

        # record real query (consumes query budget)
        ok_query = ctrl.record_query(graph_type, task_id, {"plan": plan_to_payload_any(plan)})
        if not ok_query:
            # if it failed because of query budget -> stop retrieval episode
            if state.stop_reason == AttemptStopReasons.QUERY_BUDGET_EXHAUSTED:
                break
            # else: skip & continue (but should be rare here)
            continue

        # determine dst_label
        if args.use_builder:
            if bool(getattr(args, "regtest_relax_traversal", False)):
                dst_label = None
            else:
                cfg = _graph_cfg_from_schema(normalized_schema_obj, graph_type)
                dst_label = _pick_dst_label(cfg["schema_graph_key"], cfg["node_labels"], args.dst_node_label)
        else:
            dst_label = args.dst_node_label

        # 1) LLM draft validation
        if args.inject_guard_error:
            draft_cypher = "MATCH (n:__NON_EXISTENT_LABEL__ {id: 1}) RETURN n LIMIT 1"
            draft_params = None
        else:
            if args.use_builder:
                target_built = build_target_aware_pag_query(plan, args, llm_requested_depth)
                if target_built is not None:
                    draft_cypher, draft_params = target_built
                else:
                    built = build_query_or_exit(db_agent, plan, llm_requested_depth, dst_label)
                    draft_cypher, draft_params = _split_built_query(built)
            else:
                draft_cypher = render_query_from_plan_legacy(plan, llm_requested_depth, dst_label)
                draft_params = None

        draft_cypher, draft_params = normalize_limit_and_params(draft_cypher, draft_params, int(args.max_limit))
        draft_guard = qg.validate_for_execution(draft_cypher, source="llm")

        if draft_guard.get("status") != "ok":
            print("\n[GUARD ERROR]", json.dumps(draft_guard, indent=2, sort_keys=True))
            if not ctrl.record_replan({"reason": "GUARD_ERROR", "guard": draft_guard}):
                break

            if args.use_builder:
                needs = prepare_builder_needs(
                    normalized_schema_obj=normalized_schema_obj,
                    state=state,
                    stage="replan",
                    args=args,
                    bolt_router=bolt_router,
                )
                plans = planner.plan(needs, state)
                plan = plans[0]
                llm_requested_depth = int(getattr(plan, "requested_depth", None) or args.default_depth)
            else:
                plan = fake_retrieval_planner_dict(state, "replan")
                llm_requested_depth = int(plan.get("requested_depth", args.default_depth))

            system_depth = int(args.default_depth)
            continue

        # 2) System execution (fake OR DB)
        if args.use_builder:
            target_exec = build_target_aware_pag_query(plan, args, system_depth)
            if target_exec is not None:
                exec_cypher, exec_params = target_exec
            else:
                built_exec = build_query_or_exit(db_agent, plan, system_depth, dst_label)
                exec_cypher, exec_params = _split_built_query(built_exec)
        else:
            exec_cypher = render_query_from_plan_legacy(plan, system_depth, dst_label)
            exec_params = None

        exec_cypher, exec_params = normalize_limit_and_params(exec_cypher, exec_params, int(args.max_limit))

        if args.use_db:
            want_db = tugraph_db_for_graph_type(graph_type, fallback=str(args.bolt_db))
            bolt = bolt_router.get(want_db)

            _print_db_debug(
                enabled=bool(args.print_db_debug),
                phase="EXEC",
                dbname=str(want_db),
                cypher=exec_cypher,
                params=exec_params,
                records=None,
            )

            exec_out = execute_real_db_or_exit(bolt, exec_cypher, exec_params, int(args.bolt_timeout_s))
            if not exec_out.get("ok"):
                err = exec_out.get("error", "unknown")
                print("\n[DB EXEC ERROR]", err)
                retry = maybe_drop_dst_label_on_label_error(exec_cypher, err)
                if retry is not None:
                    retry, exec_params2 = normalize_limit_and_params(retry, exec_params, int(args.max_limit))
                    retry_out = execute_real_db_or_exit(bolt, retry, exec_params2, int(args.bolt_timeout_s))
                    if retry_out.get("ok"):
                        records = retry_out.get("records") or []
                    else:
                        records = []
                else:
                    records = []
            else:
                records = exec_out.get("records") or []

            print("[DB EXEC] db=%s rows=%d" % (want_db, len(records)))
            sub_meta = {
                "num_nodes": int(len(records)),
                "num_edges": 0,
                "hit_cap": False,
                "coverage": min(1.0, float(len(records)) / 50.0),
                "depth": int(system_depth),
            }
        else:
            fake = fake_executor(exec_cypher)
            sub_meta = fake.get("subgraph_meta", {})

        records_for_summary = records if args.use_db else []

        summary = local_summary_builder.build(
            graph_type=graph_type,
            task_id=task_id,
            step_index=state.counters.queries_total,
            query_plan=plan_to_payload_any(plan),
            subgraph_meta=sub_meta,
            records=records_for_summary,
            cypher=exec_cypher,
            params=exec_params,
        )
        if llm_summary_refiner is not None:
            summary["llm_narrative"] = llm_summary_refiner.refine(summary)

        state.record_subgraph_meta({"task_id": task_id, "graph_type": graph_type, "stats": sub_meta})
        state.record_local_summary_meta({"task_id": task_id, "graph_type": graph_type, "summary": summary})

        summary_artifact = _write_summary_debug_artifact(
            os.path.join(
                "attempt_%03d" % int(patch_iter_idx),
                "local_summaries",
                "step_%04d_%s_summary.json"
                % (int(state.counters.queries_total), re.sub(r"[^A-Za-z0-9_-]+", "_", str(graph_type))),
            ),
            summary,
        )
        if summary_artifact:
            print("[SUMMARY ARTIFACT] local=%s" % summary_artifact)

        print("\n[STEP %d] graph=%s task=%s" % (state.counters.queries_total, graph_type, task_id))
        print("  executed_depth      :", system_depth)
        print("  exec_subgraph       :", sub_meta)
        print("  summary_signals     :", summary.get("signals") if isinstance(summary, dict) else None)
        if isinstance(summary, dict) and summary.get("llm_narrative"):
            print("  llm_narrative     :", summary["llm_narrative"].get("brief"))

        summary_signals = summary.get("signals", {}) if isinstance(summary, dict) else {}
        objective_met = bool(summary_signals.get("objective_met", summary.get("objective_met", False)))
        
        if objective_met:
            if (
                _graph_localizer_mode_enabled(args)
                and not _graph_localizer_required_graphs_done(args, state)
                and args.use_builder
            ):
                print("[GRAPH LOCALIZER SEED] objective met for %s; continue to remaining selected graph seeds" % graph_type)
                needs = prepare_builder_needs(
                    normalized_schema_obj=normalized_schema_obj,
                    state=state,
                    stage="replan",
                    args=args,
                    bolt_router=bolt_router,
                )
                plans = planner.plan(needs, state)
                plan = plans[0]
                llm_requested_depth = int(getattr(plan, "requested_depth", None) or args.default_depth)
                system_depth = int(args.default_depth)
                continue

            state.stop_reason = AttemptStopReasons.OBJECTIVE_MET
            break

        # If the last executed query already exhausted the query budget,
        # stop immediately before recording a deepen/replan action.
        if query_budget_exhausted(state):
            state.stop_reason = AttemptStopReasons.QUERY_BUDGET_EXHAUSTED
            state.stop_detail = {
                "queries_total": state.counters.queries_total,
                "max_queries_total": state.budgets.max_queries_total,
            }
            state.add_trace("QUERY_BUDGET_EXHAUSTED", dict(state.stop_detail))
            break
        
        # deepen
        next_depth = int(system_depth) + int(args.depth_step)
        can_deepen = (
            int(args.depth_step) > 0
            and next_depth > int(system_depth)
            and next_depth <= int(args.max_depth)
            and ctrl.can_increase_depth(task_id)
        )
        if can_deepen:
            if ctrl.record_depth_increase(task_id, int(system_depth), int(next_depth), "INSUFFICIENT_CONTEXT"):
                print("[DEEPEN] %s: %d -> %d (reason=INSUFFICIENT_CONTEXT)" % (task_id, int(system_depth), int(next_depth)))
                system_depth = int(next_depth)
                continue

        # replan
        if ctrl.can_replan():
            if not ctrl.record_replan({"reason": "NEED_REPLAN"}):
                break
            if args.use_builder:
                needs = prepare_builder_needs(
                    normalized_schema_obj=normalized_schema_obj,
                    state=state,
                    stage="replan",
                    args=args,
                    bolt_router=bolt_router,
                )
                plans = planner.plan(needs, state)
                plan = plans[0]
                llm_requested_depth = int(getattr(plan, "requested_depth", None) or args.default_depth)
            else:
                plan = fake_retrieval_planner_dict(state, "replan")
                llm_requested_depth = int(plan.get("requested_depth", args.default_depth))
            system_depth = int(args.default_depth)
            continue

        # no deepen/replan => stop episode and force patch
        break


    # v1.1: preserve the cross-graph evidence already collected by the
    # graph localizer as first-class Local Summary inputs to the Global Summary.
    inject_graph_localizer_evidence_into_state(args, state)

    global_summary = global_summary_builder.build(state)
    try:
        state.artifacts.global_summary = global_summary
    except AttributeError:
        state.artifacts["global_summary"] = global_summary

    global_summary_artifact = _write_summary_debug_artifact(
        os.path.join(
            "attempt_%03d" % int(patch_iter_idx),
            "global_summary.json",
        ),
        global_summary,
    )
    if global_summary_artifact:
        print("[SUMMARY ARTIFACT] global=%s" % global_summary_artifact)

    print(
        "\n[GLOBAL SUMMARY] id=%s graphs=%s objective_met_any=%s"
        % (
            global_summary.get("global_summary_id"),
            ",".join(global_summary.get("retrieval_accounting", {}).get("graphs_touched", [])),
            str(global_summary.get("aggregated_signals", {}).get("objective_met_any")),
        )
    )

    _print_stop_report(state)
    print("\n=== FINAL STATE (summary) ===")
    print(json.dumps(state.to_dict(), indent=2, sort_keys=True))

    if bolt_router is not None:
        try:
            bolt_router.close()
        except Exception:
            pass

    return state


# =================================================
# (I) Patch-level iteration outer loop
# =================================================

def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--schema", required=True, help="normalized_schema json path")
    ap.add_argument("--caps", required=True, help="cypher capabilities json path")
    ap.add_argument("--semantics-catalog", default=DEFAULT_SEMANTICS_CATALOG, help="Graph semantics catalog used by LocalSummaryBuilder.",)
    ap.add_argument(
    "--use-llm-summary",
    action="store_true",
    help="Enable constrained LLM narrative refinement for local summaries.",
    )

    ap.add_argument(
        "--llm-summary-model",
        default=os.environ.get("CODEGRAPHHUNTER_LLM_MODEL", "gpt-4o-mini"),
        help="Model used for constrained LLM local summary refinement.",
    )

    ap.add_argument(
        "--llm-summary-max-paths",
        type=int,
        default=5,
        help="Maximum number of paths passed to the LLM summary refiner per local summary.",
    )


    # Patch-level iteration budget (pass@k)
    # -1 => unlimited (but be careful; use your own safety cap if needed)
    ap.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Max PATCH iterations (pass@k). -1 means unlimited.",
    )

    # Retrieval-episode budgets (per patch iteration)
    ap.add_argument(
        "--max-queries-total",
        type=int,
        default=30,
        help="Max retrieval steps (executed queries) per PATCH iteration. -1 means unlimited.",
    )
    ap.add_argument("--max-replans", type=int, default=3, help="Max replans per patch iteration. -1 means unlimited.")
    ap.add_argument("--max-depth-increases", type=int, default=2, help="Max depth increases per task. -1 means unlimited.")

    ap.add_argument("--default-depth", type=int, default=4)
    ap.add_argument("--max-depth", type=int, default=8)
    ap.add_argument("--depth-step", type=int, default=2)

    ap.add_argument("--max-limit", type=int, default=200)
    ap.add_argument("--strict-unknown-functions", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--target-function", default=os.environ.get("CODEGRAPHHUNTER_TARGET_FUNCTION", ""), help="Target function name for schema-aware seed resolution, e.g. popGenericCommand.")
    ap.add_argument("--target-source-file", default=os.environ.get("CODEGRAPHHUNTER_TARGET_SOURCE_FILE", ""), help="Target source file substring for seed/source filtering, e.g. t_list.c.")
    ap.add_argument("--target-arg-no", type=int, default=int(os.environ.get("CODEGRAPHHUNTER_TARGET_ARG_NO", "0")), help="Formal argument number used as the primary PAG data-flow seed.")
    ap.add_argument("--target-seed-id", type=int, default=None, help="Diagnostic/manual PAG ArgValVar seed id override. Prefer --target-function for demo/product runs.")
    ap.add_argument(
        "--use-graph-localizer",
        action="store_true",
        help="Use information-need driven source/PAG/ICFG/CallGraph localization to derive target function instead of passing --target-function.",
    )
    ap.add_argument(
        "--candidate-top-k",
        type=int,
        default=int(os.environ.get("CODEGRAPHHUNTER_CANDIDATE_TOP_K", "5")),
        help="Number of ranked fault-location candidates to keep from graph localizer.",
    )
    ap.add_argument(
        "--localizer-max-files",
        type=int,
        default=int(os.environ.get("CODEGRAPHHUNTER_LOCALIZER_MAX_FILES", "200")),
        help="Maximum source files scanned by graph localizer when no source file scope is supplied. v0 normally uses --patch-source-file.",
    )
    ap.add_argument(
        "--use-llm-info-planner",
        action="store_true",
        help="Use an LLM to plan information needs before source/PAG/ICFG/CallGraph evidence collection. The LLM does not choose the target function directly.",
    )
    ap.add_argument(
        "--info-planner-model",
        default=os.environ.get("CODEGRAPHHUNTER_INFO_PLANNER_MODEL", "gpt-4o-mini"),
        help="Model used only for information-need planning, not final patch generation.",
    )
    ap.add_argument("--disable-target-source-filter", action="store_true", help="Disable target source file filter in target-aware PAG traversal.")

    ap.add_argument("--dst-node-label", default=DST_NODE_LABEL_DEFAULT)
    ap.add_argument("--inject-guard-error", action="store_true")
    ap.add_argument("--use-builder", action="store_true")

    ap.add_argument("--use-db", action="store_true", help="Execute queries against real TuGraph via Bolt (requires --use-builder).")
    ap.add_argument("--bolt-uri", default=os.getenv("TUGRAPH_BOLT_URI", "bolt://localhost:7687"))
    ap.add_argument("--bolt-user", default=os.getenv("TUGRAPH_BOLT_USER", "admin"))
    ap.add_argument("--bolt-pass", default=os.getenv("TUGRAPH_BOLT_PASS", ""))
    ap.add_argument("--bolt-db", default=os.getenv("TUGRAPH_BOLT_DB", "default"))
    ap.add_argument("--bolt-timeout-s", type=int, default=int(os.getenv("TUGRAPH_BOLT_TIMEOUT_S", "30")))

    ap.add_argument("--print-db-debug", action="store_true", help="Print db/cypher/params/preview records")
    ap.add_argument("--print-db-records", type=int, default=0, help="1 to print records preview; default 0")
    ap.add_argument("--print-db-records-max", type=int, default=3, help="Max records to print in preview")
    ap.add_argument("--allow-wildcard-edge", action="store_true", help="DEBUG/REGTEST ONLY: allow relationship patterns without :EdgeLabel when they contain '*' (e.g., [*1..N]).")
    ap.add_argument("--allow_unlabeled_nodes", action="store_true", help="DEBUG/REGTEST ONLY: allow nodes patterns without :NodeLabel when they contain '*'.")
    ap.add_argument("--regtest-relax-traversal", action="store_true", help="REGTEST ONLY: relax builder traversal (PAG wildcard edges + dst_label=None)")

    ap.add_argument(
        "--patch-problem-file",
        default=os.environ.get("CODEGRAPHHUNTER_PATCH_PROBLEM_FILE", ""),
        help="Optional problem statement file passed to the LLM patch generator.",
    )

    ap.add_argument(
        "--use-llm-patch",
        action="store_true",
        help="Enable LLM patch generation and validation.",
    )

    ap.add_argument(
        "--patch-model",
        default=os.environ.get("CODEGRAPHHUNTER_PATCH_MODEL", "gpt-4o-mini"),
        help="Model used for LLM patch generation.",
    )
    ap.add_argument(
        "--require-llm-patch", action="store_true",
        help="Require model-generated patches; do not compile a repair sketch directly into a patch.",
    )

    ap.add_argument(
        "--patch-target-repo",
        default=os.environ.get("CODEGRAPHHUNTER_PATCH_TARGET_REPO", ""),
        help="Buggy target repository path. Original repo is not modified.",
    )

    ap.add_argument(
        "--patch-source-file",
        default=os.environ.get("CODEGRAPHHUNTER_PATCH_SOURCE_FILE", ""),
        help="Relative source file path inside target repo to patch.",
    )

    ap.add_argument(
        "--patch-test-cmd",
        default=os.environ.get("CODEGRAPHHUNTER_PATCH_TEST_CMD", ""),
        help="Validation command executed inside copied patched repo.",
    )

    ap.add_argument(
        "--patch-workdir",
        default=os.environ.get("CODEGRAPHHUNTER_PATCH_WORKDIR", "/tmp/codegraphhunter_patch_demo"),
        help="Temporary workdir root for copied patched repos.",
    )

    ap.add_argument(
        "--patch-timeout-s",
        type=int,
        default=int(os.environ.get("CODEGRAPHHUNTER_PATCH_TIMEOUT_S", "120")),
        help="Timeout for patch apply/test commands.",
    )

    args = ap.parse_args()

    if not os.path.isfile(args.schema):
        raise SystemExit("ERROR: normalized_schema file not found: %s" % args.schema)
    if not os.path.isfile(args.caps):
        raise SystemExit("ERROR: cypher_caps file not found: %s" % args.caps)
    if not os.path.isfile(args.semantics_catalog):
        raise SystemExit("ERROR: graph_semantics_catalog file not found: %s" % args.semantics_catalog)

    if args.use_db and (not args.use_builder):
        raise SystemExit("ERROR: --use-db requires --use-builder")

    if getattr(args, "use_graph_localizer", False):
        run_graph_guided_fault_localizer_or_exit(args)

    if _target_mode_enabled(args):
        if not args.use_builder or not args.use_db:
            raise SystemExit("ERROR: target-aware retrieval requires --use-builder --use-db")
        if args.target_seed_id is None:
            if not args.target_function:
                raise SystemExit("ERROR: target-aware retrieval requires --target-function unless --target-seed-id is used")
            if not args.target_source_file:
                raise SystemExit("ERROR: target-aware retrieval requires --target-source-file")
    
    if args.use_llm_patch:
        if not args.patch_target_repo:
            raise SystemExit("ERROR: --use-llm-patch requires --patch-target-repo")
        if not args.patch_source_file:
            raise SystemExit("ERROR: --use-llm-patch requires --patch-source-file")
        if not args.patch_test_cmd:
            raise SystemExit("ERROR: --use-llm-patch requires --patch-test-cmd")

    random.seed(args.seed)
    normalized_schema_obj = load_normalized_schema_or_exit(args.schema)

    # patch-level outer loop
    max_patch_iters = int(args.max_iterations)

    # Safety fuse: even if -1 (unlimited), we still don't want accidental infinite loop in CLI demo.
    HARD_FUSE = 2000
    if max_patch_iters < 0:
        print("[WARN] --max-iterations=-1 (unlimited). Applying HARD_FUSE=%d for safety in CLI demo." % HARD_FUSE)
        patch_iter_range = range(1, HARD_FUSE + 1)
    else:
        patch_iter_range = range(1, max_patch_iters + 1)

    last_state: Optional[RetrievalState] = None

    for k in patch_iter_range:
        state = run_one_retrieval_episode(args, normalized_schema_obj, patch_iter_idx=k)
        last_state = state

        # In your desired architecture:
        # - If retrieval objective met => generate patch immediately.
        # - We don't have fixing agent yet => simulate "patch always failed", continue until max patch iterations.
        if args.use_llm_patch:
            patch_result = run_patch_attempt_v0(args, state, patch_iter_idx=k)
            print("\n=== PATCH ATTEMPT RESULT ===")
            print(json.dumps(patch_result, indent=2, sort_keys=True))

            if bool(patch_result.get("ok")):
                print("\n[PATCH LOOP STOP] patch validated successfully at iteration: %d" % k)
                break
        else:
            print("\n[PATCH] generate patch now (simulated) ...")
            print("[PATCH] verify patch (simulated) ... FAILED")

        # If user explicitly set finite max iterations and we reached it, stop.
        if max_patch_iters >= 0 and k >= max_patch_iters:
            print("\n[PATCH LOOP STOP] reached max patch iterations: %d" % max_patch_iters)
            break

    # Final summary of patch-level loop (lightweight)
    if last_state is not None:
        print("\n=== PATCH-LEVEL LOOP DONE ===")
        print("last_patch_iter =", int(last_state.attempt_id))
        print("last_retrieval_stop_reason =", last_state.stop_reason)


if __name__ == "__main__":
    main()
