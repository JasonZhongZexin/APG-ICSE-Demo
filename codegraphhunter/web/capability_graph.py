"""Guarded graph primitives used by the named capability implementations."""

import json
import os
import re
import time
from collections import deque
from pathlib import Path

from codegraphhunter.catalog.schema_guard import SchemaGuard
from codegraphhunter.query.guard import QueryGuard
from codegraphhunter.web.database_client import DatabaseQueryTimeout, DeadlineBoltClient
from codegraphhunter.web.evidence_graph import EvidenceGraph
from codegraphhunter.web.path_search import iter_paths

ROOT = Path(__file__).resolve().parents[2]
GRAPH_ENV = {"CallGraph": "CALLGRAPH", "ICFG": "ICFG", "PAG": "PAG", "CHG": "CHG", "TypeGraph": "TYPE"}


class CapabilityGraph:
    def __init__(self, environ, limits, *, clients=None, on_progress=None):
        self.environ, self.limits = environ, limits
        schema_path = Path(environ.get("CODEGRAPHHUNTER_SCHEMA_PATH", ROOT / "codegraphhunter/resources/normalized_schema.v1.json"))
        caps_path = Path(environ.get("CODEGRAPHHUNTER_CYPHER_CAPS_PATH", ROOT / "codegraphhunter/datasource/tugraph_cypher_capabilities.v1.json"))
        self.schema = json.loads(schema_path.read_text())
        # A neighbor's concrete label is returned by label(node), validated
        # against the chosen graph, and used on its next indexed read. Edge
        # labels and seed labels are always explicit, schema-owned constants.
        guard = SchemaGuard(normalized_schema_path=str(schema_path), cypher_caps_path=str(caps_path),
                            strict_unknown_functions=True, allow_unlabeled_nodes=True, allow_wildcard_edge=False)
        self.guard = QueryGuard(guard, dict(read_only=True, require_limit=True, max_limit=1000,
                                           max_rel_patterns=1, default_var_length=1, max_var_length_upper=1, allow_union=False))
        # A private bulk reader for the fixed pointer-analysis scans; ordinary
        # capability queries keep their 1000-row per-read policy.
        self.scan_guard = QueryGuard(guard, dict(self.guard.policy, max_limit=50000))
        self.timeout = max(1, min(120, int(environ.get("CODEGRAPHHUNTER_CAPABILITY_TIMEOUT_S", "30"))))
        self.deadline = time.monotonic() + self.timeout
        self.clients = {} if clients is None else clients
        self.progress = on_progress
        self.reads, self.records, self.notes = [], [], []
        self.data, self.adjacency, self.adjacency_complete = {}, {}, set()
        self.evidence = EvidenceGraph()
        self.complete, self.stop_reason = True, None

    def partial(self, reason, note=""):
        self.complete, self.stop_reason = False, reason
        if note and note not in self.notes:
            self.notes.append(note)

    def check(self):
        if time.monotonic() >= self.deadline or len(self.reads) >= 4096:
            self.partial("timeout" if time.monotonic() >= self.deadline else "read_budget")
            raise DatabaseQueryTimeout("The capability query reached its total time or read budget.")

    def properties(self, label):
        item = self.schema["node_types"][label]
        return item.get("expanded_properties") or item.get("properties") or {}

    def primary(self, label):
        return self.schema["node_types"][label].get("primary_key") or ("id" if "id" in self.properties(label) else "st_info_id")

    def labels(self, graph):
        return tuple(self.schema["graphs"][graph]["node_labels"])

    def edges(self, graph):
        return tuple(self.schema["graphs"][graph]["edge_labels"])

    def client(self, graph):
        if graph not in self.clients:
            password = self.environ.get("TUGRAPH_BOLT_PASS", "")
            if not password:
                raise ValueError("Configure the Graph DB password in Settings.")
            database = self.environ.get("CODEGRAPHHUNTER_" + GRAPH_ENV[graph] + "_DB", "SVFType" if graph == "TypeGraph" else graph)
            self.clients[graph] = DeadlineBoltClient(self.environ.get("TUGRAPH_BOLT_URI", "bolt://localhost:7687"),
                                                   self.environ.get("TUGRAPH_BOLT_USER", "admin"), password, database)
        return self.clients[graph]

    def read(self, graph, cypher, params=None, *, edge_scan=False, bulk=False):
        self.check()
        labels = set(re.findall(r"\([^)]*:\s*([A-Za-z_]\w*)", cypher))
        edges = set(re.findall(r"\[[^]]*:\s*([A-Za-z_]\w*)", cypher))
        # Whole-PAG constraint scans bind a schema-owned edge label and
        # validate both returned endpoint labels. They never accept user Cypher.
        scoped_scan = edge_scan and graph == "PAG" and len(edges) == 1
        if (bulk and graph != "PAG") or (not labels and not scoped_scan) or not labels.issubset(self.labels(graph)) or not edges.issubset(self.edges(graph)):
            raise ValueError("The generated query escaped its graph scope.")
        checked = (self.scan_guard if bulk else self.guard).validate_for_execution(cypher, source="system")
        if checked.get("status") != "ok":
            raise ValueError("Capability query rejected: " + str(checked.get("reason")) + " " + str(checked.get("detail", "")))
        remaining = self.deadline - time.monotonic()
        if self.progress:
            self.progress(dict(stage="database", message=f"Reading {graph}: {len(self.records)} results, {len(self.reads)} queries.",
                               timeout_s=remaining, cypher=cypher, params=params or {}))
        result = self.client(graph).run(cypher, params=params or {}, timeout_s=remaining)
        self.check()
        rows = result.get("records", [])
        if not isinstance(rows, list):
            raise ValueError("The graph database returned invalid records.")
        self.reads.append(dict(graph=graph, cypher=cypher, params=params or {}, record_count=len(rows), guard={"status": "ok"}))
        return rows

    def remember(self, graph, label, values):
        if label not in self.labels(graph) or not isinstance(values, dict) or self.primary(label) not in values:
            raise ValueError("The graph database returned an unknown label or a node without its primary key.")
        key = (label, values[self.primary(label)])
        self.data[key] = dict(values, graph=graph)
        return key

    def select(self, graph, label, conditions=(), limit=1000):
        if label not in self.labels(graph):
            raise ValueError("Choose a node label belonging to the selected graph.")
        owned, primary = self.properties(label), self.primary(label)
        params, where, bound = {}, [], ""
        for index, (prop, op, value) in enumerate(conditions):
            if prop not in owned or op not in {"=", "CONTAINS", "IN"}:
                raise ValueError("Unknown node property or unsupported filter.")
            name = f"filter_{index}"
            params[name] = value
            if prop == primary and op == "=":
                bound = f" {{{prop}: ${name}}}"
            else:
                where.append(f"node.{prop} {op} ${name}")
        clause = "\nWHERE " + " AND ".join(where) if where else ""
        selected_props = [prop for prop in owned if (not prop.endswith("_map") or label == "StInfo") and prop != "reachable_bbs"]
        selected = ", ".join(f"node.{prop} AS node_{prop}" for prop in selected_props)
        query = f"MATCH (node:{label}{bound}){clause}\nRETURN {selected}\nORDER BY node_{primary}\nLIMIT {min(1000, limit)}"
        rows = self.read(graph, query, params)
        keys = [self.remember(graph, label, {prop: row.get(f"node_{prop}") for prop in selected_props}) for row in rows]
        if len(rows) == limit and not bound:
            self.partial("scope_limit", "A node selector reached its scan limit; conclusions cover the selected scope only.")
        return keys

    def node(self, graph, node_id, label=""):
        labels = [label] if label else self.labels(graph)
        found = []
        for item in labels:
            found.extend(self.select(graph, item, [(self.primary(item), "=", node_id)], limit=1))
        if not found:
            return []
        return found

    def neighbors(self, graph, key, edge_labels, direction="out", function_id=None):
        cache_key = (graph, key, tuple(edge_labels), direction, function_id)
        if cache_key in self.adjacency_complete:
            yield from self.adjacency[cache_key]
            return
        items = self.adjacency[cache_key] = []
        for edge in edge_labels:
            if edge not in self.edges(graph):
                raise ValueError("Choose an edge label belonging to the selected graph.")
            for incoming in ((False, True) if direction == "both" else (direction == "in",)):
                offset = 0
                while True:
                    seed = f"(seed:{key[0]} {{{self.primary(key[0])}: $seed_id}})"
                    pattern = f"{seed}<-[flow:{edge}]-(node)" if incoming else f"{seed}-[flow:{edge}]->(node)"
                    edge_info = self.schema["edge_types"][edge]
                    edge_props = {prop: value for prop, value in (edge_info.get("expanded_properties") or edge_info.get("properties") or {}).items()
                                  if not prop.endswith("_map")}
                    selected = ", ".join(f"flow.{prop} AS edge_{prop}" for prop in edge_props)
                    query = (f"MATCH {pattern}\nRETURN node.id AS node_id, label(node) AS node_label, "
                             f"{selected}, euid(flow) AS edge_uid\nORDER BY node_id, edge_uid\nSKIP {offset}\nLIMIT 1000")
                    rows = self.read(graph, query, {"seed_id": key[1]})
                    for row in rows:
                        if row["node_label"] not in self.labels(graph):
                            raise ValueError("The database returned a neighbor outside the selected graph.")
                        neighbor = (row["node_label"], row["node_id"])
                        if neighbor not in self.data:
                            self.node(graph, row["node_id"], row["node_label"])
                        if neighbor not in self.data:
                            raise ValueError("The database returned a neighbor without node data.")
                        if function_id is not None and self.data[neighbor].get("fun_obj_var_id") != function_id:
                            continue
                        source, target = (neighbor, key) if incoming else (key, neighbor)
                        properties = {prop: row.get(f"edge_{prop}") for prop in edge_props}
                        if row.get("edge_uid") is not None:
                            properties["exported_edge_uid"] = row["edge_uid"]
                        item = (neighbor, dict(source=source, target=target, label=edge, properties=properties))
                        items.append(item)
                        yield item
                    if len(rows) < 1000:
                        break
                    offset += len(rows)
        self.adjacency_complete.add(cache_key)

    def add_record(self, row, nodes=()):
        if len(self.records) >= self.limits.entity_limit:
            self.partial("result_limit", "The result limit was reached; additional evidence may exist.")
            return False
        index = len(self.records)
        self.records.append(row)
        for key in nodes:
            if key not in self.evidence.nodes:
                self.evidence.nodes[key] = dict(label=key[0], id=key[1], properties=self.data[key], records=set())
            self.evidence.nodes[key]["records"].add(index)
        return True

    def add_path(self, graph, keys, edges, *, incoming=False, extra=None):
        if len(self.records) >= self.limits.entity_limit:
            self.partial("result_limit", "The result limit was reached; additional paths may exist.")
            return False
        row = dict(graph=graph, seed_label=keys[0][0], seed_id=keys[0][1], result_label=keys[-1][0], result_id=keys[-1][1], path_length=len(edges))
        row.update(extra or {})
        row["path_id"] = self.evidence.add_path(keys, edges, self.data, incoming=incoming, record_index=len(self.records))
        self.records.append(row)
        return True

    def walk(self, graph, roots, edges, matches=lambda key: True, *, incoming=False, function_id=None, extra=None):
        for root in roots:
            neighbors = lambda key, hop: self.neighbors(graph, key, edges, "in" if incoming else "out", function_id)
            for keys, relationships in iter_paths(root, neighbors, matches, max_depth=self.limits.max_depth, check_deadline=self.check):
                details = extra(keys, relationships) if extra else None
                if not self.add_path(graph, keys, relationships, incoming=incoming, extra=details):
                    return

    def collect(self, graph, roots, edges, *, function_id=None, maximum=5000):
        visited, queue = set(roots), deque((key, 0) for key in roots)
        adjacency = {}
        while queue:
            self.check()
            key, depth = queue.popleft()
            if self.limits.max_depth is not None and depth >= self.limits.max_depth:
                self.partial("depth_limit", "Reachability analysis stops at the selected maximum depth.")
                continue
            adjacent = list(self.neighbors(graph, key, edges, function_id=function_id))
            adjacency[key] = adjacent
            for neighbor, _ in adjacent:
                if neighbor not in visited:
                    if len(visited) >= maximum:
                        self.partial("scan_limit", "The graph scan limit was reached.")
                        return visited, adjacency
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
        return visited, adjacency

    def close(self):
        for client in self.clients.values():
            if hasattr(client, "close"):
                client.close()
