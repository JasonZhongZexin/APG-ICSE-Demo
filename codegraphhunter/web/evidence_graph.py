"""Shared graph elements and an ordered, independently expandable path list."""

from __future__ import annotations

import json


class EvidenceGraph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}
        self.paths = []

    def add_path(self, keys, relationships, node_data, *, incoming, record_index):
        if incoming:
            keys, relationships = list(reversed(keys)), list(reversed(relationships))
        for key in keys:
            if key not in self.nodes:
                self.nodes[key] = {"label": key[0], "id": key[1], "properties": dict(node_data[key]), "records": set()}
            self.nodes[key]["records"].add(record_index)
        indices = []
        for edge in relationships:
            source, target, label = edge["source"], edge["target"], edge["label"]
            properties = edge.get("properties") or {}
            edge_key = (source, target, label, json.dumps(properties, sort_keys=True, default=str))
            if edge_key not in self.edges:
                self.edges[edge_key] = {
                    "source": {"label": source[0], "id": source[1]},
                    "target": {"label": target[0], "id": target[1]},
                    "label": label, "properties": properties, "records": set(), "index": len(self.edges),
                }
            self.edges[edge_key]["records"].add(record_index)
            indices.append(self.edges[edge_key]["index"])
        path_id = f"path-{record_index + 1}"
        self.paths.append({"id": path_id, "record_index": record_index, "hop_count": len(relationships),
                           "nodes": [{"label": key[0], "id": key[1]} for key in keys], "edge_indices": indices})
        return path_id

    def as_dict(self, *, complete=True, stop_reason=None):
        return {
            "nodes": [dict(item, records=sorted(item["records"])) for item in self.nodes.values()],
            "edges": [dict(item, records=sorted(item["records"])) for item in self.edges.values()],
            "paths": self.paths, "path_policy": "node_simple_paths", "complete": complete, "stop_reason": stop_reason,
        }
