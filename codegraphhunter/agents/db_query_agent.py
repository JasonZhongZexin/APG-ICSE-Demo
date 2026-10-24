# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict, Optional, Tuple, Union, List

from codegraphhunter.query.builder import QueryBuilder
from codegraphhunter.query.guard import QueryGuard
from codegraphhunter.ir.graph_query_plan import GraphQueryPlan

from codegraphhunter.datasource.bolt_client import BoltClient


class DBQueryAgent(object):
    """
    Convert GraphQueryPlan -> (cypher, params) using QueryBuilder, validate via QueryGuard,
    optionally execute via BoltClient.
    """

    def __init__(
        self,
        query_guard: QueryGuard,
        normalized_schema: Dict[str, Any],
        bolt_client: Optional[BoltClient] = None,
    ):
        if normalized_schema is None or not isinstance(normalized_schema, dict) or not normalized_schema:
            raise ValueError("DBQueryAgent requires a non-empty normalized_schema dict.")

        self.builder = QueryBuilder(normalized_schema=normalized_schema)
        self.guard = query_guard
        self.bolt = bolt_client

    def build_query(
        self,
        plan: GraphQueryPlan,
        depth: Optional[int] = None,
        dst_node_label: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Returns (cypher, params). Params may be {}.
        """
        built = self.builder.build(plan, depth=depth, dst_node_label=dst_node_label)

        if isinstance(built, tuple) and len(built) == 2:
            cypher, params = built
            params = params or {}
        else:
            cypher = str(built)
            params = {}

        return cypher, params

    def generate_query(
        self,
        plan: GraphQueryPlan,
        depth: Optional[int] = None,
        dst_node_label: Optional[str] = None,
        source: str = "system",
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """
        Returns (cypher, params, guard_res)
        """
        cypher, params = self.build_query(plan, depth=depth, dst_node_label=dst_node_label)
        guard_res = self.guard.validate_for_execution(cypher, source=source)
        return cypher, params, guard_res

    def execute(
        self,
        plan: GraphQueryPlan,
        depth: Optional[int] = None,
        dst_node_label: Optional[str] = None,
        timeout_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Execute against DB (Bolt). Requires bolt_client.
        Returns:
          {
            "ok": bool,
            "records": [..] (possibly truncated),
            "meta": {"num_records":..., ...},
          }
        """
        if self.bolt is None:
            raise RuntimeError("DBQueryAgent.execute() requires bolt_client, but it is None.")

        cypher, params, guard_res = self.generate_query(
            plan, depth=depth, dst_node_label=dst_node_label, source="system"
        )
        if guard_res.get("status") != "ok":
            return {"ok": False, "error": "GUARD_REJECTED", "guard": guard_res, "cypher": cypher, "params": params}

        rows = self.bolt.run(cypher, params=params, timeout_s=timeout_s)

        # Keep payload small: only keep first N rows
        max_keep = 20
        kept = rows[:max_keep]

        return {
            "ok": True,
            "records": kept,
            "meta": {
                "num_records": len(rows),
                "kept": len(kept),
            },
        }
