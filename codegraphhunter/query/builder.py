# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict, Optional, Set, Tuple

from codegraphhunter.ir.graph_query_plan import (
    GraphQueryPlan,
    TraverseOp,
    AttachICFGContextOp,
    LookupBySourceLocOp,
)

from codegraphhunter.query.op_templates import (
    build_lookup_by_source_loc,
    build_traverse,
)


class QueryBuilder(object):
    """
    Schema-aware template builder (Scheme-A, strict).
    Returns: (cypher, params)
    """

    def __init__(self, normalized_schema: Dict[str, Any]):
        if normalized_schema is None or not isinstance(normalized_schema, dict) or not normalized_schema:
            raise ValueError("QueryBuilder requires a non-empty normalized_schema dict.")

        self.schema = normalized_schema
        self.node_types = self.schema.get("node_types", {}) or {}
        self.edge_types = self.schema.get("edge_types", {}) or {}

        self.node_labels: Set[str] = set(self.node_types.keys())
        self.edge_labels: Set[str] = set(self.edge_types.keys())

    def _ensure_node_label(self, label: str) -> None:
        if label not in self.node_labels:
            raise ValueError("Unknown node label: %r" % label)

    def _ensure_edge_label(self, label: str) -> None:
        # Allow wildcard edge label for regtest relaxed traversal
        if label == "*" or label == "":
            return
        if label not in self.edge_labels:
            raise ValueError("Unknown edge label: %r" % label)
    
    def _get_node_info(self, label: str) -> Dict[str, Any]:
        return (self.node_types.get(label, {}) or {})

    def _get_node_props(self, label: str) -> Dict[str, Any]:
        info = self._get_node_info(label)
        return (info.get("expanded_properties") or info.get("properties") or {}) or {}

    def _seed_pk_field_and_type(self, label: str) -> Tuple[str, Optional[str]]:
        """
        Strict: require schema to explicitly provide primary_key.
        """
        info = self._get_node_info(label)
        pk = info.get("primary_key")
        pk_type = info.get("primary_key_type")  # optional but recommended

        if not isinstance(pk, str) or not pk.strip():
            raise ValueError(
                "normalized_schema missing node_types[%r].primary_key. "
                "Do NOT infer from index=true; please propagate from native schema.primary."
                % label
            )
        pk = pk.strip()

        # validate pk exists in properties
        props = self._get_node_props(label)
        if pk not in props:
            raise ValueError("Schema primary_key=%r not found in properties for label=%r" % (pk, label))

        # Optional: if pk_type missing, attempt derive from property spec
        if not pk_type:
            pinfo = props.get(pk, {}) or {}
            # tolerate different schema shapes
            pk_type = pinfo.get("type") or pinfo.get("dtype") or None

        return pk, (str(pk_type) if pk_type else None)

    def _validate_op(self, plan: GraphQueryPlan, op: Any, dst_node_label: Optional[str]) -> None:
        self._ensure_node_label(str(plan.seed.label))

        if dst_node_label:
            self._ensure_node_label(str(dst_node_label))

        if isinstance(op, LookupBySourceLocOp):
            for lb in list(op.label_candidates or []):
                self._ensure_node_label(str(lb))
            return

        if isinstance(op, TraverseOp):
            ets = list(op.edge_types or [])
            if not ets:
                raise ValueError("TraverseOp.edge_types is empty (planner must specify).")

            # NEW: allow wildcard edge type "*"
            first_et = str(ets[0]).strip()
            if first_et != "*":
                self._ensure_edge_label(first_et)

            # dst label may be None in relaxed regtest mode
            if getattr(op, "dst_label", None):
                self._ensure_node_label(str(op.dst_label))
            return

        if isinstance(op, AttachICFGContextOp):
            ets = list(getattr(op, "edge_types", None) or [])
            if ets:
                self._ensure_edge_label(str(ets[0]))
            return

        raise ValueError("Unsupported op type: %r" % type(op))

    def build(
        self,
        plan: GraphQueryPlan,
        depth: Optional[int] = None,
        dst_node_label: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        if plan is None:
            raise ValueError("GraphQueryPlan is required.")

        ops = list(plan.operations or [])
        if not ops:
            raise ValueError("GraphQueryPlan.operations is empty. Builder mode requires explicit ops.")

        first = None
        for op in ops:
            if isinstance(op, (LookupBySourceLocOp, TraverseOp)):
                first = op
                break
        if first is None:
            raise ValueError("No query-producing op found. Provide LookupBySourceLocOp or TraverseOp.")

        self._validate_op(plan, first, dst_node_label)

        if isinstance(first, LookupBySourceLocOp):
            return build_lookup_by_source_loc(plan, first)

        if isinstance(first, TraverseOp):
            if depth is not None:
                eff_depth = int(depth)
            elif getattr(first, "max_depth", None) is not None:
                eff_depth = int(first.max_depth)
            elif getattr(plan, "requested_depth", None) is not None:
                eff_depth = int(plan.requested_depth)
            else:
                eff_depth = 4

            pk_field, pk_type = self._seed_pk_field_and_type(str(plan.seed.label))

            # Optional strict type check (recommended): if pk_type says STRING, seed.id must be str
            if pk_type is not None:
                t = pk_type.upper()
                if t in ("STRING", "STR") and not isinstance(plan.seed.id, str):
                    raise ValueError("Seed.id must be str for label=%r primary_key_type=%r" % (plan.seed.label, pk_type))
                if t.startswith("INT") and not isinstance(plan.seed.id, int):
                    raise ValueError("Seed.id must be int for label=%r primary_key_type=%r" % (plan.seed.label, pk_type))

            return build_traverse(
                plan,
                first,
                depth=int(eff_depth),
                dst_node_label=dst_node_label,
                seed_key_field=pk_field,
            )

        raise ValueError("Unsupported op in build(): %r" % type(first))
