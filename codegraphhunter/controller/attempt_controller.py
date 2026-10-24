# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from codegraphhunter.state.retrieval_state import RetrievalState, is_unlimited


class AttemptStopReasons:
    # Retrieval-episode stop reasons (inside ONE patch iteration)
    OBJECTIVE_MET = "OBJECTIVE_MET"
    REPLAN_LIMIT_REACHED = "REPLAN_LIMIT_REACHED"
    DEPTH_INCREASE_LIMIT_REACHED = "DEPTH_INCREASE_LIMIT_REACHED"
    QUERY_BUDGET_EXHAUSTED = "QUERY_BUDGET_EXHAUSTED"


@dataclass
class AttemptController:
    """
    Controls one retrieval episode (NOT patch-iteration loop).

    Important:
    - Query budget is budgets.max_queries_total
    - Graph switch / skip does NOT consume query budget.
    """
    state: RetrievalState

    def remaining_queries(self) -> Optional[int]:
        cap = self.state.budgets.max_queries_total
        if is_unlimited(cap):
            return None
        return max(0, int(cap) - int(self.state.counters.queries_total))

    def should_force_patch(self) -> bool:
        # In new architecture, "force patch" == retrieval episode has to stop (budget exhausted),
        # then CLI proceeds to patch generation (even if objective not met).
        return self.state.stop_reason in (
            AttemptStopReasons.QUERY_BUDGET_EXHAUSTED,
            AttemptStopReasons.REPLAN_LIMIT_REACHED,
            AttemptStopReasons.DEPTH_INCREASE_LIMIT_REACHED,
        )

    # -----------------------------
    # Query budget (retrieval steps)
    # -----------------------------
    def can_query(self) -> bool:
        cap = self.state.budgets.max_queries_total
        if is_unlimited(cap):
            return True
        return int(self.state.counters.queries_total) < int(cap)

    def consume_query_budget(self, event: str, detail: Dict[str, Any]) -> bool:
        if not self.can_query():
            self.state.stop_reason = AttemptStopReasons.QUERY_BUDGET_EXHAUSTED
            self.state.stop_detail = {
                "queries_total": self.state.counters.queries_total,
                "max_queries_total": self.state.budgets.max_queries_total,
            }
            self.state.add_trace("QUERY_BUDGET_EXHAUSTED", dict(self.state.stop_detail))
            return False

        self.state.counters.queries_total += 1
        self.state.add_trace(event, detail)
        return True

    # ---------------------------------
    # Optional per-graph caps (disabled)
    # ---------------------------------
    def can_query_graph_type(self, graph_type: str) -> bool:
        if not self.state.budgets.enforce_per_graph_caps:
            return True

        gt = str(graph_type).upper().strip()
        cap = self.state.budgets.max_queries_per_graph_type.get(gt)
        if cap is None or is_unlimited(cap):
            return True

        cnt = self.state.counters.queries_by_graph_type.get(gt, 0)
        return int(cnt) < int(cap)

    def record_query(self, graph_type: str, task_id: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> bool:
        """
        Record a REAL query execution:
        - consumes query budget (one retrieval step)
        - increments queries_by_graph_type if enabled
        """
        if not self.can_query_graph_type(graph_type):
            # Graph skip/switch is NOT a query, no budget consumption here.
            self.state.add_trace("GRAPH_TYPE_SKIPPED", {
                "graph_type": graph_type,
                "task_id": task_id,
                "count": self.state.counters.queries_by_graph_type.get(str(graph_type).upper().strip(), 0),
                "cap": self.state.budgets.max_queries_per_graph_type.get(str(graph_type).upper().strip()),
            })
            return False

        ok = self.consume_query_budget("QUERY", {
            "graph_type": graph_type,
            "task_id": task_id,
            "payload": payload or {},
        })
        if not ok:
            return False

        gt = str(graph_type).upper().strip()
        self.state.counters.queries_by_graph_type[gt] = self.state.counters.queries_by_graph_type.get(gt, 0) + 1
        self.state.add_trace("QUERY_OK", {"graph_type": graph_type, "task_id": task_id})
        return True

    # ----------
    # Replanning
    # ----------
    def can_replan(self) -> bool:
        cap = self.state.budgets.max_replans
        if is_unlimited(cap):
            return True
        return int(self.state.counters.replans) < int(cap)

    def record_replan(self, payload: Dict[str, Any]) -> bool:
        if not self.can_replan():
            self.state.stop_reason = AttemptStopReasons.REPLAN_LIMIT_REACHED
            self.state.stop_detail = {
                "replans": self.state.counters.replans,
                "max_replans": self.state.budgets.max_replans,
            }
            self.state.add_trace("REPLAN_LIMIT_REACHED", dict(self.state.stop_detail))
            return False

        self.state.counters.replans += 1
        self.state.add_trace("REPLAN", payload)
        return True

    # ----------------
    # Depth increasing
    # ----------------
    def can_increase_depth(self, task_id: str) -> bool:
        cap = self.state.budgets.max_depth_increases_per_task
        if is_unlimited(cap):
            return True
        cnt = self.state.counters.depth_increases.get(task_id, 0)
        return int(cnt) < int(cap)

    def record_depth_increase(self, task_id: str, from_depth: int, to_depth: int, reason: str) -> bool:
        if not self.can_increase_depth(task_id):
            self.state.stop_reason = AttemptStopReasons.DEPTH_INCREASE_LIMIT_REACHED
            self.state.stop_detail = {
                "task_id": task_id,
                "depth_increases": self.state.counters.depth_increases.get(task_id, 0),
                "max_depth_increases_per_task": self.state.budgets.max_depth_increases_per_task,
            }
            self.state.add_trace("DEPTH_INCREASE_LIMIT_REACHED", dict(self.state.stop_detail))
            return False

        self.state.counters.depth_increases[task_id] = self.state.counters.depth_increases.get(task_id, 0) + 1
        self.state.add_trace("DEEPEN", {
            "task_id": task_id,
            "from_depth": from_depth,
            "to_depth": to_depth,
            "reason": reason,
        })
        return True