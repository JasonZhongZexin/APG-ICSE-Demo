"""Distinct CallGraph paths from guarded, indexed, cached one-hop reads."""

from __future__ import annotations

import time

from codegraphhunter.web.database_client import DatabaseQueryTimeout
from codegraphhunter.web.evidence_graph import EvidenceGraph
from codegraphhunter.web.path_search import iter_paths
from codegraphhunter.web.query_limits import MAX_QUERY_ENTITIES


def traverse_callgraph(plan, run, *, timeout_s=30, on_progress=None):
    deadline = time.monotonic() + timeout_s
    records, reads = [], []
    evidence = EvidenceGraph()
    node_data, adjacency = {}, {}
    incoming = plan.intent == "find_reachable_callers"
    edge_properties = ("csid", "kind", "direct_call_set", "indirect_call_set")
    stop_reason = None

    def check_deadline():
        if time.monotonic() >= deadline:
            raise DatabaseQueryTimeout(f"CallGraph path search exceeded {timeout_s:g} seconds.")

    def read(cypher, params, hop):
        check_deadline()
        remaining = deadline - time.monotonic()
        if on_progress:
            on_progress({"stage": "database", "message": f"Finding CallGraph paths: {len(records)} found, reading hop {hop}.",
                         "timeout_s": remaining, "execution_cypher": cypher, "execution_params": params})
        response = run(cypher, params, remaining)
        check_deadline()
        rows = response.get("records", [])
        if not isinstance(rows, list):
            raise ValueError("TuGraph returned invalid CallGraph records.")
        reads.append({"cypher": cypher, "params": params, "record_count": len(rows), "guard": {"status": "ok"}})
        return rows

    projection = "node.id AS node_id, node.fun_name AS node_name"

    def neighbors(key, hop):
        if key in adjacency:
            yield from adjacency[key]
            return
        items = adjacency[key] = []
        offset = 0
        while True:
            source = "(frontier:CallGraphNode {id: $frontier_id})"
            pattern = f"{source}<-[flow:CallGraphEdge]-(node:CallGraphNode)" if incoming else f"{source}-[flow:CallGraphEdge]->(node:CallGraphNode)"
            edge_projection = ", ".join(f"flow.{name} AS edge_{name}" for name in edge_properties)
            order = ", ".join(["node.id"] + [f"flow.{name}" for name in edge_properties])
            rows = read(f"MATCH {pattern}\nRETURN DISTINCT {projection}, {edge_projection}\n"
                        f"ORDER BY {order}\nSKIP {offset}\nLIMIT {MAX_QUERY_ENTITIES}", {"frontier_id": key[1]}, hop)
            for row in rows:
                neighbor = ("CallGraphNode", row["node_id"])
                node_data[neighbor] = {"id": row["node_id"], "name": row.get("node_name", "")}
                left, right = (neighbor, key) if incoming else (key, neighbor)
                item = (neighbor, {"source": left, "target": right, "label": "CallGraphEdge",
                                   "properties": {name: row[f"edge_{name}"] for name in edge_properties if row.get(f"edge_{name}") is not None}})
                items.append(item)
                yield item
            if len(rows) < MAX_QUERY_ENTITIES:
                break
            offset += len(rows)

    try:
        offset = 0
        while True:
            seeds = read("MATCH (node:CallGraphNode)\nWHERE node.fun_name CONTAINS $function_name\n"
                         f"RETURN {projection}\nORDER BY node.id\nSKIP {offset}\nLIMIT {plan.limit}",
                         {"function_name": plan.function_name}, 0)
            for seed in seeds:
                root = ("CallGraphNode", seed["node_id"])
                node_data[root] = {"id": seed["node_id"], "name": seed.get("node_name", "")}
                for keys, edges in iter_paths(root, neighbors, lambda key: True, max_depth=plan.depth, check_deadline=check_deadline):
                    if len(records) == plan.limit:
                        stop_reason = "result_limit"
                        break
                    endpoint = node_data[keys[-1]]
                    first, second = (endpoint, node_data[root]) if incoming else (node_data[root], endpoint)
                    left, right = ("caller", "target") if incoming else ("source", "callee")
                    path_id = evidence.add_path(keys, edges, node_data, incoming=incoming, record_index=len(records))
                    records.append({f"{left}_id": first["id"], f"{left}_name": first.get("name", ""),
                                    f"{right}_id": second["id"], f"{right}_name": second.get("name", ""),
                                    "call_distance": len(edges), "path_id": path_id})
                if stop_reason:
                    break
            if stop_reason or len(seeds) < plan.limit:
                break
            offset += len(seeds)
    except DatabaseQueryTimeout:
        if not records:
            raise
        stop_reason = "timeout"
    strategy = {"name": "cached_adjacency_path_search", "max_depth": plan.depth,
                "max_hops_reached": max((row["call_distance"] for row in records), default=0),
                "query_count": len(reads), "complete": stop_reason is None, "stop_reason": stop_reason}
    return records, reads, strategy, evidence.as_dict(complete=stop_reason is None, stop_reason=stop_reason)
