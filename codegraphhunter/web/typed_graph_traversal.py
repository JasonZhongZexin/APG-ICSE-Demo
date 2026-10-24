"""Distinct ICFG/PAG paths across concrete labels using cached indexed reads."""

from __future__ import annotations

from collections import Counter
import time

from codegraphhunter.query.guard import QueryGuard
from codegraphhunter.web.database_client import DatabaseQueryTimeout
from codegraphhunter.web.evidence_graph import EvidenceGraph
from codegraphhunter.web.path_search import iter_paths
from codegraphhunter.web.query_limits import MAX_QUERY_ENTITIES


BRANCH_BATCH_SIZE = 8
NEIGHBOR_PAGE_SIZE = MAX_QUERY_ENTITIES


def traversal_guard(backend):
    # Only deterministic one-hop batches use this policy. Model plans retain
    # their single-pattern, no-UNION policy and every actual read is guarded.
    return QueryGuard(backend.schema_guard, dict(
        backend.query_guard.policy, allow_union=True,
        max_rel_patterns=BRANCH_BATCH_SIZE, max_limit=MAX_QUERY_ENTITIES,
    ))


def traverse_typed_graph(
    plan, run, *, graph, node_labels, node_properties, properties,
    seed_conditions, seed_projection, seed_params, edge_properties=("kind",), timeout_s=30, on_progress=None,
):
    deadline = time.monotonic() + timeout_s
    records, reads = [], []
    evidence = EvidenceGraph()
    node_data, adjacency = {}, {}
    incoming = "predecessors" in plan.intent
    labels = (plan.result_label,) + tuple(label for label in node_labels if label != plan.result_label)
    stop_reason = None

    def check_deadline():
        if time.monotonic() >= deadline:
            raise DatabaseQueryTimeout(f"{graph} path search exceeded {timeout_s:g} seconds.")

    def read(cypher, params, hop):
        check_deadline()
        remaining = deadline - time.monotonic()
        if on_progress:
            on_progress({"stage": "database", "message": f"Finding {graph} paths: {len(records)} found, reading hop {hop}.",
                         "timeout_s": remaining, "execution_cypher": cypher, "execution_params": params})
        response = run(cypher, params, remaining)
        check_deadline()
        rows = response.get("records", [])
        if not isinstance(rows, list):
            raise ValueError(f"TuGraph returned invalid {graph} records.")
        reads.append({"cypher": cypher, "params": params, "record_count": len(rows), "guard": {"status": "ok"}})
        return rows

    def neighbor_batch(source_key, branches):
        queries, params = [], {}
        for index, (target_label, offset) in enumerate(branches):
            source, target, flow = f"frontier{index}", f"node{index}", f"flow{index}"
            frontier_key = f"frontier_id{index}"
            params[frontier_key] = source_key[1]
            source_pattern = f"({source}:{source_key[0]} {{id: ${frontier_key}}})"
            target_pattern = f"({target}:{target_label})"
            pattern = (f"{source_pattern}<-[{flow}:{plan.edge_label}]-{target_pattern}" if incoming
                       else f"{source_pattern}-[{flow}:{plan.edge_label}]->{target_pattern}")
            owned = node_properties(target_label)
            selected = [f"{target}.{name} AS node_{name}" if name in owned else f"null AS node_{name}" for name in properties]
            selected.extend(f"{flow}.{name} AS edge_{name}" for name in edge_properties)
            order = ", ".join([f"{target}.id"] + [f"{flow}.{name}" for name in edge_properties])
            queries.append(f"MATCH {pattern}\nRETURN DISTINCT {', '.join(selected)}, '{target_label}' AS node_label, {index} AS branch_id\n"
                           f"ORDER BY {order}\nSKIP {offset}\nLIMIT {NEIGHBOR_PAGE_SIZE}")
        return "\nUNION ALL\n".join(queries), params

    def neighbors(key, hop):
        if key in adjacency:
            yield from adjacency[key]
            return
        items = adjacency[key] = []
        for start in range(0, len(labels), BRANCH_BATCH_SIZE):
            pending = [(label, 0) for label in labels[start:start + BRANCH_BATCH_SIZE]]
            while pending:
                query, params = neighbor_batch(key, pending)
                rows = read(query, params, hop)
                counts = Counter()
                for row in rows:
                    branch = row["branch_id"]
                    label, node_id = row["node_label"], row["node_id"]
                    if not isinstance(branch, int) or not 0 <= branch < len(pending) or label != pending[branch][0]:
                        raise ValueError(f"TuGraph returned invalid {graph} branch metadata.")
                    counts[branch] += 1
                    neighbor = (label, node_id)
                    node_data[neighbor] = {name: row.get(f"node_{name}") for name in properties if name in node_properties(label)}
                    source, target = (neighbor, key) if incoming else (key, neighbor)
                    item = (neighbor, {"source": source, "target": target, "label": plan.edge_label,
                                       "properties": {name: row[f"edge_{name}"] for name in edge_properties if row.get(f"edge_{name}") is not None}})
                    items.append(item)
                    yield item
                pending = [(label, offset + counts[index]) for index, (label, offset) in enumerate(pending)
                           if counts[index] == NEIGHBOR_PAGE_SIZE]

    def matches(key):
        return key[0] == plan.result_label and (not getattr(plan, "returns_only", False) or node_data[key].get("is_return"))

    where = "\nWHERE " + " AND ".join(seed_conditions) if seed_conditions else ""
    try:
        offset = 0
        while True:
            seeds = read(f"MATCH (seed:{plan.seed_label}){where}\nRETURN {seed_projection}\n"
                         f"ORDER BY seed.id\nSKIP {offset}\nLIMIT {plan.limit}", dict(seed_params), 0)
            for seed in seeds:
                root = (plan.seed_label, seed["seed_id"])
                node_data[root] = {name[5:]: value for name, value in seed.items() if name.startswith("seed_")}
                for keys, edges in iter_paths(root, neighbors, matches, max_depth=plan.depth, check_deadline=check_deadline):
                    if len(records) == plan.limit:
                        stop_reason = "result_limit"
                        break
                    record = dict(seed)
                    record.update({f"result_{name}": value for name, value in node_data[keys[-1]].items()})
                    record["path_length"] = len(edges)
                    record["path_id"] = evidence.add_path(keys, edges, node_data, incoming=incoming, record_index=len(records))
                    records.append(record)
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
                "max_hops_reached": max((row["path_length"] for row in records), default=0),
                "query_count": len(reads), "complete": stop_reason is None, "stop_reason": stop_reason}
    return records, reads, strategy, evidence.as_dict(complete=stop_reason is None, stop_reason=stop_reason)
