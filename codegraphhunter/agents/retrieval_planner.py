# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict, List, Optional

from codegraphhunter.ir.graph_query_plan import (
    GraphQueryPlan,
    Seed,
    Constraints,
    TraverseOp,
    AttachICFGContextOp,
    LookupBySourceLocOp,
)


class RetrievalPlannerAgent(object):
    """
    Retrieval Planner (IR-only, paper-friendly).

    Responsibilities:
      - Translate high-level information needs into GraphQueryPlan (IR)
      - Ensure schema-valid labels / edge types
      - DO NOT:
          * decide budgets / iterations
          * decide when to stop
          * inject execution-time strategy
    """

    def __init__(self, normalized_schema: Dict[str, Any]):
        self.schema = normalized_schema or {}
        self.graphs = self.schema.get("graphs", {}) or {}
        self.node_types = self.schema.get("node_types", {}) or {}
        self.edge_types = self.schema.get("edge_types", {}) or {}

        # labels that support source_loc lookup
        self.source_loc_labels = self._labels_with_property("source_loc")

    # --------------------------------------------------
    # helpers
    # --------------------------------------------------

    def _labels_with_property(self, prop_name: str) -> List[str]:
        out: List[str] = []
        for lb, info in (self.node_types or {}).items():
            props = info.get("expanded_properties") or info.get("properties") or {}
            if prop_name in props:
                out.append(lb)
        return sorted(set(out))

    def _default_edge_types_for_graph(self, graph_type: str) -> List[str]:
        """
        Minimal, schema-driven fallback.
        Strategy:
          - ONLY use edge_labels declared in normalized_schema.graphs[graph_type]
          - Do NOT guess from global edge_types
        """
        g = str(graph_type or "").strip()
        info = self.graphs.get(g) or {}
        edge_labels = list(info.get("edge_labels") or [])

        if not edge_labels:
            raise ValueError(
                "No edge_labels declared in normalized_schema for graph_type=%r" % g
            )

        return [edge_labels[0]]

    # --------------------------------------------------
    # main entry
    # --------------------------------------------------

    def plan(
        self,
        high_level_needs: Dict[str, Any],
        retrieval_state: Any,
    ) -> List[GraphQueryPlan]:
        """
        Input (high_level_needs) is assumed to come from Repair Planner (or CLI simulation).

        This method MUST be:
          - deterministic
          - side-effect free
          - independent from budgets / iterations
        """
        needs = high_level_needs or {}

        task_id = str(needs.get("task_id", "T1"))
        graph_type = str(needs.get("graph_type", "PAG"))
        objective = str(needs.get("objective", "unknown"))

        # ------------------
        # seed
        # ------------------
        seed_d = needs.get("seed") or {}
        seed_label = str(seed_d.get("label"))
        seed_id = seed_d.get("id")

        if seed_label not in (self.node_types or {}):
            raise ValueError(
                "Unknown seed label=%r (not in normalized_schema.node_types)" % seed_label
            )

        seed = Seed(label=seed_label, id=seed_id)

        # ------------------
        # constraints (IR-level, NOT budgets)
        # ------------------
        c_d = needs.get("constraints") or {}
        constraints = Constraints(
            max_nodes=int(c_d.get("max_nodes", 500)),
            max_edges=int(c_d.get("max_edges", 2000)),
            require_limit=bool(c_d.get("require_limit", True)),
            limit=int(c_d.get("limit", 200)),
            timeout_ms=c_d.get("timeout_ms"),
        )

        plans: List[GraphQueryPlan] = []

        # ------------------
        # (A) optional lookup by source_loc
        # ------------------
        src = needs.get("source_loc")
        if isinstance(src, str) and src.strip():
            plans.append(
                GraphQueryPlan(
                    task_id=task_id,
                    plan_id="%s#lookup" % task_id,
                    stage="LOOKUP",
                    graph_type=graph_type,
                    objective="lookup_node_by_source_loc",
                    seed=seed,
                    operations=[
                        LookupBySourceLocOp(
                            source_loc=src,
                            label_candidates=list(self.source_loc_labels),
                            limit=min(50, int(constraints.limit)),
                        )
                    ],
                    constraints=constraints,
                    requested_depth=None,
                    priority=0,
                    explain="Lookup candidate nodes by source location.",
                )
            )

        # ------------------
        # (B) main traversal
        # ------------------
        edge_types = needs.get("edge_types") or []
        edge_types = [str(e) for e in edge_types if str(e).strip()]

        if not edge_types:
            edge_types = self._default_edge_types_for_graph(graph_type)

        requested_depth = needs.get("requested_depth")
        try:
            max_depth = int(requested_depth) if requested_depth is not None else None
        except Exception:
            max_depth = None

        ops = [
            TraverseOp(
                direction=str(needs.get("direction", "out")),
                edge_types=list(edge_types),
                max_depth=max_depth,
                dst_label=needs.get("dst_label"),
            )
        ]

        if bool(needs.get("need_icfg_context", False)):
            ops.append(
                AttachICFGContextOp(
                    icfg_hops=int(needs.get("icfg_hops", 2))
                )
            )

        plans.append(
            GraphQueryPlan(
                task_id=task_id,
                plan_id="%s#main" % task_id,
                stage="MAIN",
                graph_type=graph_type,
                objective=objective,
                seed=seed,
                operations=ops,
                constraints=constraints,
                requested_depth=max_depth,
                priority=int(needs.get("priority", 1)),
                explain=needs.get("explain"),
            )
        )

        plans.sort(key=lambda p: getattr(p, "priority", 1))
        return plans
