# -*- coding: utf-8 -*-
from __future__ import annotations

"""
LLM Local Summary Refiner.

This module implements the constrained-B design:

- Deterministic LocalSummaryBuilder extracts factual evidence:
  paths, nodes, edges, signals, query context.
- The LLM only verbalizes this evidence into a narrative.
- The LLM is not allowed to create new graph facts.
- LLM output is attached under summary["llm_narrative"].
- If the LLM call fails, we return a safe fallback and never break retrieval.
"""

import json
import os
from typing import Any, Dict, List, Optional


LLM_NARRATIVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "brief",
        "graph_semantic_interpretation",
        "patch_relevance",
        "limitations",
        "evidence_used",
        "confidence",
        "unsupported_inferences_avoided",
    ],
    "properties": {
        "brief": {
            "type": "string",
            "description": "One or two sentences summarizing only the supplied graph evidence.",
        },
        "graph_semantic_interpretation": {
            "type": "string",
            "description": "Explain what the returned graph path may indicate, using only supplied paths, nodes, edges, and graph semantics.",
        },
        "patch_relevance": {
            "type": "string",
            "description": "Explain how this evidence may help patch generation, without claiming a proven root cause.",
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Caveats about what cannot be concluded from the returned evidence.",
        },
        "evidence_used": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path_indices", "edge_labels", "node_names"],
            "properties": {
                "path_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "edge_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "node_names": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Confidence in the narrative based only on evidence richness, not bug correctness.",
        },
        "unsupported_inferences_avoided": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List inferences the model intentionally did not make because they were unsupported.",
        },
    },
}


def _truncate_str(s: Any, limit: int = 1200) -> str:
    text = "" if s is None else str(s)
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _dedup_str(xs: List[Any], limit: int = 50) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in xs or []:
        s = str(x)
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _extract_output_text(response: Any) -> str:
    """
    Extract text from OpenAI Responses API result in a version-tolerant way.
    """
    txt = getattr(response, "output_text", None)
    if txt:
        return str(txt)

    try:
        output = getattr(response, "output", None) or []
        parts: List[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content")
            for c in content or []:
                if isinstance(c, dict):
                    if c.get("type") in ("output_text", "text"):
                        parts.append(str(c.get("text", "")))
                else:
                    c_type = getattr(c, "type", None)
                    c_text = getattr(c, "text", None)
                    if c_type in ("output_text", "text") and c_text:
                        parts.append(str(c_text))
        if parts:
            return "\n".join(parts)
    except Exception:
        pass

    return ""


class LLMLocalSummaryRefiner(object):
    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key_env: str = "OPENAI_API_KEY",
        max_paths: int = 5,
        max_nodes: int = 20,
        max_edges: int = 20,
        fail_open: bool = True,
    ):
        self.model = model
        self.api_key_env = api_key_env
        self.max_paths = int(max_paths)
        self.max_nodes = int(max_nodes)
        self.max_edges = int(max_edges)
        self.fail_open = bool(fail_open)

    def _build_evidence_payload(self, local_summary: Dict[str, Any]) -> Dict[str, Any]:
        sem = local_summary.get("semantic_summary", {}) or {}
        entities = sem.get("entities", {}) or {}
        signals = local_summary.get("signals", {}) or {}

        paths = sem.get("paths", []) or []
        nodes = entities.get("nodes", []) or []
        edges = entities.get("edges", []) or []

        compact_paths = []
        for i, p in enumerate(paths[: self.max_paths]):
            if isinstance(p, dict):
                compact_paths.append({
                    "index": i,
                    "text": _truncate_str(p.get("text"), 600),
                    "edge_labels": [
                        str(e.get("label") or e.get("kind") or e.get("name"))
                        for e in (p.get("edges", []) or [])
                        if isinstance(e, dict)
                    ],
                    "node_names": [
                        str(n.get("name") or n.get("id"))
                        for n in (p.get("nodes", []) or [])
                        if isinstance(n, dict)
                    ],
                })
            else:
                compact_paths.append({
                    "index": i,
                    "text": _truncate_str(p, 600),
                    "edge_labels": [],
                    "node_names": [],
                })

        compact_nodes = []
        for n in nodes[: self.max_nodes]:
            if isinstance(n, dict):
                compact_nodes.append({
                    "id": n.get("id"),
                    "name": n.get("name"),
                    "label": n.get("label"),
                    "source_loc": n.get("source_loc"),
                    "icfg_node_id": n.get("icfg_node_id"),
                    "cg_node_id": n.get("cg_node_id"),
                    "svf_type_id": n.get("svf_type_id"),
                })
            else:
                compact_nodes.append({"name": str(n)})

        compact_edges = []
        for e in edges[: self.max_edges]:
            if isinstance(e, dict):
                compact_edges.append({
                    "label": e.get("label"),
                    "kind": e.get("kind"),
                    "name": e.get("name"),
                })
            else:
                compact_edges.append({"label": str(e)})

        return {
            "summary_id": local_summary.get("summary_id"),
            "task_id": local_summary.get("task_id"),
            "graph_type": local_summary.get("graph_type"),
            "result_kind": local_summary.get("result_kind"),
            "deterministic_description": sem.get("description"),
            "signals": {
                "objective_met": signals.get("objective_met"),
                "insufficient_context": signals.get("insufficient_context"),
                "coverage": signals.get("coverage"),
                "hit_cap": signals.get("hit_cap"),
                "depth": signals.get("depth"),
                "records_count": signals.get("records_count"),
            },
            "paths": compact_paths,
            "nodes": compact_nodes,
            "edges": compact_edges,
            "catalog_context": local_summary.get("catalog_context", {}),
            "query_context": {
                "cypher": local_summary.get("query_context", {}).get("cypher"),
                "params": local_summary.get("query_context", {}).get("params"),
            },
        }

    def _system_prompt(self) -> str:
        return (
            "You are a constrained graph-evidence summarizer for a repository-level C/C++ "
            "program repair system. Your job is to verbalize only the supplied graph evidence.\n\n"
            "Hard rules:\n"
            "1. Do not invent functions, variables, source code statements, files, line numbers, paths, or edge labels.\n"
            "2. Do not claim a definitive bug root cause unless explicitly present in the evidence.\n"
            "3. Use cautious language such as 'suggests', 'may indicate', or 'provides evidence for'.\n"
            "4. If evidence is weak, say so in limitations.\n"
            "5. You may explain graph semantics using the supplied graph_type, edge labels, and catalog context.\n"
            "6. Output must be valid JSON that matches the requested schema.\n"
        )

    def _user_prompt(self, evidence_payload: Dict[str, Any]) -> str:
        return (
            "Generate a constrained semantic interpretation for this local graph summary.\n"
            "Only use the following JSON evidence. Do not add facts that are not present.\n\n"
            + json.dumps(evidence_payload, indent=2, sort_keys=True, ensure_ascii=False)
        )

    def _fallback(self, local_summary: Dict[str, Any], reason: str) -> Dict[str, Any]:
        sem = local_summary.get("semantic_summary", {}) or {}
        entities = sem.get("entities", {}) or {}
        edges = entities.get("edges", []) or {}
        nodes = entities.get("nodes", []) or {}
        paths = sem.get("paths", []) or []

        edge_labels = []
        for e in edges if isinstance(edges, list) else []:
            if isinstance(e, dict):
                edge_labels.append(e.get("label") or e.get("kind") or e.get("name"))

        node_names = []
        for n in nodes if isinstance(nodes, list) else []:
            if isinstance(n, dict):
                node_names.append(n.get("name") or n.get("id"))

        return {
            "enabled": True,
            "provider": "openai",
            "model": self.model,
            "status": "fallback",
            "error": _truncate_str(reason, 400),
            "brief": _truncate_str(sem.get("description", "No LLM narrative was generated."), 500),
            "graph_semantic_interpretation": _truncate_str(sem.get("description", ""), 800),
            "patch_relevance": (
                "Patch relevance was not refined by the LLM. Use deterministic paths, nodes, "
                "edges, and signals as the grounded repair context."
            ),
            "limitations": [
                "LLM refinement failed or was unavailable.",
                "This fallback does not add any semantic claim beyond deterministic evidence.",
            ],
            "evidence_used": {
                "path_indices": list(range(min(len(paths), self.max_paths))),
                "edge_labels": _dedup_str([x for x in edge_labels if x], self.max_edges),
                "node_names": _dedup_str([x for x in node_names if x], self.max_nodes),
            },
            "confidence": "low",
            "unsupported_inferences_avoided": [
                "No root cause was inferred.",
                "No source-level statements were invented.",
            ],
        }

    def _sanitize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        out = {
            "enabled": True,
            "provider": "openai",
            "model": self.model,
            "status": "ok",
            "brief": _truncate_str(data.get("brief"), 800),
            "graph_semantic_interpretation": _truncate_str(data.get("graph_semantic_interpretation"), 1600),
            "patch_relevance": _truncate_str(data.get("patch_relevance"), 1200),
            "limitations": [str(x) for x in (data.get("limitations") or [])][:8],
            "evidence_used": data.get("evidence_used") or {
                "path_indices": [],
                "edge_labels": [],
                "node_names": [],
            },
            "confidence": data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else "low",
            "unsupported_inferences_avoided": [
                str(x) for x in (data.get("unsupported_inferences_avoided") or [])
            ][:8],
        }

        if not out["limitations"]:
            out["limitations"] = [
                "The narrative is constrained to returned graph evidence and does not prove the root cause."
            ]

        return out

    def refine(self, local_summary: Dict[str, Any]) -> Dict[str, Any]:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            return self._fallback(local_summary, "Missing environment variable: %s" % self.api_key_env)

        try:
            from openai import OpenAI
        except Exception as e:
            return self._fallback(local_summary, "Failed to import openai package: %s" % str(e))

        evidence_payload = self._build_evidence_payload(local_summary)

        try:
            client = OpenAI(api_key=api_key)

            response = client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": self._system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": self._user_prompt(evidence_payload),
                    },
                ],
                temperature=0,
                max_output_tokens=700,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "local_graph_summary_narrative",
                        "strict": True,
                        "schema": LLM_NARRATIVE_SCHEMA,
                    }
                },
            )

            txt = _extract_output_text(response)
            if not txt:
                return self._fallback(local_summary, "OpenAI response did not contain output_text.")

            data = json.loads(txt)
            return self._sanitize(data)

        except Exception as e:
            if self.fail_open:
                return self._fallback(local_summary, str(e))
            raise