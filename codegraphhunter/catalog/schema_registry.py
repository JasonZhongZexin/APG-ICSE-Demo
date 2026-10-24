# coding: utf-8
import json


class InMemorySchemaRegistry(object):
    """
    Runtime Schema Registry backed by normalized_schema.v1.json.

    Key design:
    - Trust normalized_schema's computed fields:
        extends / expanded_properties / extra_properties
    - Provide uniform APIs for:
        nodes, edges, abstract types
    """

    def __init__(self, schema_path):
        self.schema_path = schema_path
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.graphs = self.schema.get("graphs", {})

        self.node_types = self.schema.get("node_types", {})      # label -> record
        self.edge_types = self.schema.get("edge_types", {})      # label -> record
        self.abstract_types = self.schema.get("abstract_types", {})  # abstract label -> {"properties":[...]}

        self.node_labels = set(self.node_types.keys())
        self.edge_labels = set(self.edge_types.keys())
        self.abstract_labels = set(self.abstract_types.keys())

    # ---------- catalog ----------

    def get_graph_catalog(self):
        """
        For Retrieval Planner:
          graph -> {node_labels:[...], edge_labels:[...]}
        """
        return self.graphs

    # ---------- label resolution ----------

    def is_node_label(self, label):
        return label in self.node_labels

    def is_edge_label(self, label):
        return label in self.edge_labels

    def is_abstract_label(self, label):
        return label in self.abstract_labels

    def is_queryable_label(self, label):
        """
        Abstract labels must NOT appear in MATCH (n:Label).
        """
        return (label in self.node_labels) or (label in self.edge_labels)

    def kind_of(self, label):
        if label in self.node_labels:
            return "VERTEX"
        if label in self.edge_labels:
            return "EDGE"
        if label in self.abstract_labels:
            return "ABSTRACT"
        return None

    # ---------- node APIs ----------

    def node_info(self, label):
        return self.node_types.get(label, {})

    def node_primary(self, label):
        return self.node_types.get(label, {}).get("primary")

    def node_base(self, label):
        return self.node_types.get(label, {}).get("extends")

    def node_properties(self, label, expanded=True):
        info = self.node_types.get(label, {})
        if expanded:
            # prefer precomputed expanded_properties
            ep = info.get("expanded_properties")
            if isinstance(ep, dict):
                return ep
        return info.get("properties", {}) or {}

    def node_extra_properties(self, label):
        info = self.node_types.get(label, {})
        ex = info.get("extra_properties")
        if isinstance(ex, dict):
            return ex
        # fallback
        base = self.node_base(label)
        if not base:
            return self.node_properties(label, expanded=False)
        base_props = self.node_properties(base, expanded=True)
        own = self.node_properties(label, expanded=False)
        return {k: v for k, v in own.items() if k not in base_props}

    # ---------- edge APIs ----------

    def edge_info(self, label):
        return self.edge_types.get(label, {})

    def edge_base(self, label):
        return self.edge_types.get(label, {}).get("extends")

    def edge_properties(self, label, expanded=True):
        info = self.edge_types.get(label, {})
        if expanded:
            ep = info.get("expanded_properties")
            if isinstance(ep, dict):
                return ep
        return info.get("properties", {}) or {}

    def edge_extra_properties(self, label):
        info = self.edge_types.get(label, {})
        ex = info.get("extra_properties")
        if isinstance(ex, dict):
            return ex
        base = self.edge_base(label)
        if not base:
            return self.edge_properties(label, expanded=False)
        base_props = self.edge_properties(base, expanded=True)
        own = self.edge_properties(label, expanded=False)
        return {k: v for k, v in own.items() if k not in base_props}

    # ---------- abstract APIs ----------

    def abstract_properties(self, label):
        """
        Returns list of property names for abstract types
        (no type info, just names).
        """
        return (self.abstract_types.get(label, {}) or {}).get("properties", []) or []
