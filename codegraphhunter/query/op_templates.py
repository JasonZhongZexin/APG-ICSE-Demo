# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict, Tuple, Optional

from codegraphhunter.ir.graph_query_plan import (
    GraphQueryPlan,
    TraverseOp,
    LookupBySourceLocOp,
)


def _limit_from_plan(plan: GraphQueryPlan, default: int = 200) -> int:
    lim = getattr(plan.constraints, "limit", None)
    try:
        return int(lim) if lim is not None else int(default)
    except Exception:
        return int(default)


def build_lookup_by_source_loc(plan: GraphQueryPlan, op: LookupBySourceLocOp) -> Tuple[str, Dict[str, Any]]:
    cands = list(op.label_candidates or [])
    if not cands:
        raise ValueError("LookupBySourceLocOp.label_candidates is empty (refuse to guess labels).")

    label = str(cands[0])
    lim = int(op.limit or _limit_from_plan(plan, default=50))

    cypher = (
        "MATCH (n:%s) "
        "WHERE n.source_loc = $source_loc "
        "RETURN n "
        "LIMIT $limit"
    ) % label

    params = {"source_loc": str(op.source_loc), "limit": lim}
    return cypher, params


def build_traverse(
    plan: GraphQueryPlan,
    op: TraverseOp,
    depth: int,
    dst_node_label: Optional[str],
    seed_key_field: str,
) -> Tuple[str, Dict[str, Any]]:
    seed_label = str(plan.seed.label)
    seed_id = plan.seed.id  # may be int or str
    lim = _limit_from_plan(plan, default=200)

    if depth < 1:
        depth = 1

    edge_types = list(op.edge_types or [])
    if not edge_types:
        raise ValueError("TraverseOp.edge_types is empty. Planner must specify concrete edge_types.")

    edge_label = str(edge_types[0]).strip()
    any_edge = (edge_label == "*" or edge_label == "")

    dst = dst_node_label or getattr(op, "dst_label", None)
    direction = str(getattr(op, "direction", "out") or "out").lower()

    seed_match = "{%s: $seed_id}" % seed_key_field

    # Build relationship pattern.
    # edge_types=["*"] or empty edge label means wildcard traversal.
    if any_edge:
        rel = "[*1..%d]" % depth
    else:
        rel = "[:%s*1..%d]" % (edge_label, depth)

    # Important:
    # Return path variable p together with endpoints n and m.
    # LocalSummaryBuilder uses p to build path-level semantic summaries.
    if direction == "both":
        # undirected traversal
        if dst:
            cypher = (
                "MATCH p=(n:%s %s)-%s-(m:%s) "
                "RETURN p, n, m "
                "LIMIT $limit"
            ) % (seed_label, seed_match, rel, str(dst))
        else:
            cypher = (
                "MATCH p=(n:%s %s)-%s-(m) "
                "RETURN p, n, m "
                "LIMIT $limit"
            ) % (seed_label, seed_match, rel)

    elif direction == "in":
        if dst:
            cypher = (
                "MATCH p=(m:%s)-%s->(n:%s %s) "
                "RETURN p, n, m "
                "LIMIT $limit"
            ) % (str(dst), rel, seed_label, seed_match)
        else:
            cypher = (
                "MATCH p=(m)-%s->(n:%s %s) "
                "RETURN p, n, m "
                "LIMIT $limit"
            ) % (rel, seed_label, seed_match)

    else:
        # default: out
        if dst:
            cypher = (
                "MATCH p=(n:%s %s)-%s->(m:%s) "
                "RETURN p, n, m "
                "LIMIT $limit"
            ) % (seed_label, seed_match, rel, str(dst))
        else:
            cypher = (
                "MATCH p=(n:%s %s)-%s->(m) "
                "RETURN p, n, m "
                "LIMIT $limit"
            ) % (seed_label, seed_match, rel)

    params = {"seed_id": seed_id, "limit": int(lim)}
    return cypher, params