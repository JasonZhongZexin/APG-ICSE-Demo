# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict, List, Optional, Union, Literal

from pydantic import BaseModel, Field


class Seed(BaseModel):
    """
    A concrete starting point in the graph.
    Seed.id refers to the label's PRIMARY KEY value (property-based), NOT internal vid.
    """
    label: str = Field(..., description="Concrete label name (must exist in schema).")
    id: Union[int, str] = Field(..., description="Primary-key value for this label. int or str.")


class Focus(BaseModel):
    kind: str = Field(..., description="e.g., 'variable', 'function', 'type', 'node', 'edge'")
    value: Any = Field(..., description="Arbitrary payload, planner-defined.")


class Constraints(BaseModel):
    max_nodes: int = Field(500, ge=1)
    max_edges: int = Field(2000, ge=1)
    require_limit: bool = Field(True)
    limit: int = Field(200, ge=1)
    timeout_ms: Optional[int] = Field(None, ge=1)


class OperationBase(BaseModel):
    op: str = Field(..., description="Operation name (must be supported).")


class TraverseOp(OperationBase):
    op: Literal["traverse"] = "traverse"
    direction: str = Field("out", description="out | in | both")
    edge_types: List[str] = Field(default_factory=list)
    max_depth: int = Field(4, ge=1)
    dst_label: Optional[str] = Field(None)
    include_start_node: bool = Field(True)


class AttachICFGContextOp(OperationBase):
    op: Literal["attach_icfg_context"] = "attach_icfg_context"
    icfg_hops: int = Field(2, ge=0)
    edge_types: List[str] = Field(default_factory=list)


class LookupBySourceLocOp(OperationBase):
    op: Literal["lookup_by_source_loc"] = "lookup_by_source_loc"
    source_loc: str = Field(...)
    label_candidates: List[str] = Field(default_factory=list)
    limit: int = Field(20, ge=1)


Operation = Union[TraverseOp, AttachICFGContextOp, LookupBySourceLocOp]


class GraphQueryPlan(BaseModel):
    task_id: str = Field(...)
    graph_type: str = Field(...)
    objective: str = Field(...)
    seed: Seed = Field(...)

    focus: Optional[Focus] = None
    operations: List[Operation] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)

    requested_depth: Optional[int] = Field(None)

    priority: int = Field(1, ge=1)
    explain: Optional[str] = Field(None)


def plan_to_dict(plan: GraphQueryPlan) -> Dict[str, Any]:
    if hasattr(plan, "model_dump"):
        return plan.model_dump()
    return plan.dict()
