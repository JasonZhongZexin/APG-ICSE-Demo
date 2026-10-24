# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Retrieval episode state (NOT patch-iteration state).

Patch-level iteration (pass@k) orchestration lives in CLI outer loop.

Key definitions:
- Patch Iteration: one attempt to produce a patch (pass@k). Controlled by CLI --max-iterations.
- Retrieval Episode: within ONE patch iteration, run multiple retrieval steps (queries), replans,
  depth increases until objective met OR budgets exhausted.
- Retrieval Step: one executed DB query (or fake executor query). Controlled by budgets.max_queries_total.
- Graph switch / skip: NOT a retrieval step, must NOT consume query budget.

Budget semantics:
- Use -1 to mean "unlimited" for:
  --max-queries-total, --max-replans, --max-depth-increases
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def is_unlimited(v: Optional[int]) -> bool:
    return v is None or (isinstance(v, int) and v < 0)


@dataclass
class RetrievalBudgets:
    # Per-retrieval-episode budgets
    max_queries_total: int = 30        # -1 => unlimited
    max_replans: int = 3               # -1 => unlimited
    max_depth_increases_per_task: int = 2  # -1 => unlimited

    # Optional per-graph caps (disabled by default in new architecture)
    enforce_per_graph_caps: bool = False
    max_queries_per_graph_type: Dict[str, Optional[int]] = field(default_factory=lambda: {
        "PAG": None,
        "ICFG": None,
        "CALLGRAPH": None,
        "CHG": None,
        "BASICBLOCK": None,
    })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_queries_total": self.max_queries_total,
            "max_replans": self.max_replans,
            "max_depth_increases_per_task": self.max_depth_increases_per_task,
            "enforce_per_graph_caps": self.enforce_per_graph_caps,
            "max_queries_per_graph_type": dict(self.max_queries_per_graph_type),
        }


@dataclass
class RetrievalCounters:
    # Retrieval-step counters (queries)
    queries_total: int = 0
    queries_by_graph_type: Dict[str, int] = field(default_factory=dict)

    # Replan counters
    replans: int = 0

    # Depth increases per task
    depth_increases: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queries_total": self.queries_total,
            "queries_by_graph_type": dict(self.queries_by_graph_type),
            "replans": self.replans,
            "depth_increases": dict(self.depth_increases),
        }


@dataclass
class RetrievalArtifacts:
    subgraphs: List[Dict[str, Any]] = field(default_factory=list)
    local_summaries: List[Dict[str, Any]] = field(default_factory=list)
    global_summary: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subgraphs": list(self.subgraphs),
            "local_summaries": list(self.local_summaries),
            "global_summary": self.global_summary,
        }


@dataclass
class RetrievalState:
    attempt_id: int
    budgets: RetrievalBudgets

    counters: RetrievalCounters = field(default_factory=RetrievalCounters)
    artifacts: RetrievalArtifacts = field(default_factory=RetrievalArtifacts)

    rr_graph_idx: int = 0  # planner round-robin cursor (builder mode)

    stop_reason: Optional[str] = None
    stop_detail: Dict[str, Any] = field(default_factory=dict)

    covered: Dict[str, List[Any]] = field(
        default_factory=lambda: {"functions": [], "variables": [], "types": [], "labels": []}
    )

    trace: List[Dict[str, Any]] = field(default_factory=list)

    def add_trace(self, event: str, data: Dict[str, Any]) -> None:
        self.trace.append({"event": str(event), "data": data})

    @property
    def trace_len(self) -> int:
        return len(self.trace)

    def record_subgraph_meta(self, item: Dict[str, Any]) -> None:
        self.artifacts.subgraphs.append(item)

    def record_local_summary_meta(self, item: Dict[str, Any]) -> None:
        self.artifacts.local_summaries.append(item)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "budgets": self.budgets.to_dict(),
            "counters": self.counters.to_dict(),
            "artifacts": self.artifacts.to_dict(),
            "covered": dict(self.covered),
            "rr_graph_idx": self.rr_graph_idx,
            "stop_reason": self.stop_reason,
            "stop_detail": dict(self.stop_detail),
            "trace_len": self.trace_len,
        }