# -*- coding: utf-8 -*-
"""
Policy Guard for queries (read-only + complexity constraints) for CodeGraphHunter.

This sits ABOVE SchemaGuard:
- SchemaGuard: schema correctness + allowed functions/procedures (capabilities) + type checks
- PolicyGuard (QueryGuard): read-only safety + resource bounds (LIMIT, path expansion depth, etc.)

Depth policy (per your Pass@1 requirement):
- LLM draft query may request a depth via the query (e.g., [:E*1..10])
- System arbitrates:
    * source="llm": if requested > default_depth -> clamp to default_depth
    * source="system": allow controller deepening up to hard max_depth
- Hard max_depth is always enforced.

Python: 3.6 compatible.
"""

import re

from codegraphhunter.catalog.schema_guard import SchemaGuard, SchemaGuardErrorCodes

from typing import Any, Dict, Optional, Tuple, Union

BuiltQuery = Union[str, Tuple[str, Dict[str, Any]]]

def _normalize_query_input(query: BuiltQuery) -> str:
    if isinstance(query, tuple):
        if len(query) != 2:
            raise TypeError("QueryGuard expected (cypher, params) tuple of len=2")
        cypher, _params = query
        if not isinstance(cypher, str):
            raise TypeError("QueryGuard expected cypher to be str in (cypher, params)")
        return cypher
    if not isinstance(query, str):
        raise TypeError("QueryGuard expected query to be str or (str, dict)")
    return query

class QueryPolicyErrorCodes(object):
    WRITE_OPERATION_FORBIDDEN = "WRITE_OPERATION_FORBIDDEN"
    DANGEROUS_PROCEDURE_FORBIDDEN = "DANGEROUS_PROCEDURE_FORBIDDEN"
    LIMIT_REQUIRED = "LIMIT_REQUIRED"
    LIMIT_TOO_LARGE = "LIMIT_TOO_LARGE"
    VARIABLE_LENGTH_TOO_DEEP = "VARIABLE_LENGTH_TOO_DEEP"
    TOO_MANY_REL_PATTERNS = "TOO_MANY_REL_PATTERNS"
    UNSUPPORTED_UNION = "UNSUPPORTED_UNION"


DEFAULT_POLICY = {
    # Enforce read-only
    "read_only": True,

    # Require LIMIT for MATCH queries (recommended for safety)
    "require_limit": True,
    "max_limit": 200,

    # Relationship patterns (count of [...] patterns)
    "max_rel_patterns": 8,

    # Variable-length expansion depth control
    "default_var_length": 4,     # default execution depth
    "max_var_length_upper": 8,   # hard max (safety ceiling)
    "var_length_step": 2,        # for controller deepening

    # Disallow UNION in MVP
    "allow_union": False,
}


# Write clauses / keywords we never allow in query stage
WRITE_KEYWORDS = [
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE",
    "DROP", "ALTER", "RENAME",
    "LOAD CSV",
]

# Dangerous procedures (defense-in-depth; capabilities should also exclude)
DANGEROUS_PROCEDURE_PREFIXES = [
    "dbms.security.",
    "dbms.config.",
    "dbms.graph.",
    "db.plugin.",
    "db.importor.",
    "db.dropDB",
    "db.flushDB",
    "db.backup",
]


def _upper_no_strings(q):
    """
    Uppercase query with string literals stripped (to avoid false positives):
    - remove '...' and "..."
    """
    q2 = re.sub(r"'([^'\\]|\\.)*'", "''", q)
    q3 = re.sub(r'"([^"\\]|\\.)*"', '""', q2)
    return q3.upper()


def _find_limit(q):
    m = re.search(r"(?is)\bLIMIT\s+(\d+)\b", q)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _count_rel_patterns(q):
    return len(re.findall(r"\[[^\]]*\]", q))


def _max_var_length_upper_bound(q):
    """
    Extract variable-length relationship patterns and return the maximum upper bound.
    Handles forms:
      - [:E*1..4]
      - [:E*..4]
      - [:E*4] (treat as upper=4)
    Returns:
      - int upper bound if any
      - None if no variable-length expansion is present or its upper bound is omitted
    """
    max_k = None

    for inside in re.findall(r"\[([^\]]*)\]", q):
        if re.search(r"\*\s*(?:\d+\s*)?\.\.\s*$|\*\s*$", inside):
            return None

    # *a..b or *..b (capture b)
    for m in re.finditer(r"\*\s*(?:\d+\s*\.\.\s*)?(\d+)", q):
        try:
            k = int(m.group(1))
            if max_k is None or k > max_k:
                max_k = k
        except Exception:
            continue

    # *b (no dots) - keep it too
    for m in re.finditer(r"\*\s*(\d+)\b", q):
        try:
            k = int(m.group(1))
            if max_k is None or k > max_k:
                max_k = k
        except Exception:
            continue

    return max_k


def _arbitrate_var_length(requested, policy, source):
    """
    Depth arbitration rule.

    source="llm":
      - if requested is None: use default
      - if requested <= default: honor requested
      - if requested > default: clamp to default (NOT max)

    source="system":
      - system deepening is allowed up to hard max
      - if requested is None: use default
      - else effective = min(requested, max)
    """
    default_value = policy.get("default_var_length", 4)
    default_d = int(default_value) if default_value is not None else None
    max_value = policy.get("max_var_length_upper", default_d)
    max_d = int(max_value) if max_value is not None else None

    if requested is None:
        return default_d, False

    try:
        req = int(requested)
    except Exception:
        return default_d, False

    if source == "system":
        if max_d is None or req <= max_d:
            return req, False
        return max_d, True

    # default: "llm"
    if default_d is None or req <= default_d:
        return req, False
    return default_d, True


class QueryGuard(object):
    """
    Combine policy checks + SchemaGuard.
    """

    def __init__(self, schema_guard, policy=None):
        if not isinstance(schema_guard, SchemaGuard):
            raise TypeError("schema_guard must be an instance of SchemaGuard")
        self.schema_guard = schema_guard
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)

    def validate_for_execution(self, query, source="llm"):
        """
        source:
          - "llm": draft query produced by LLM (apply clamp-to-default)
          - "system": query produced by controller deepening (allow up to hard max)

        Returns:
          - ok: {"status":"ok","validated_query":..., "warnings":[...], "metadata":{...}}
          - error: {"status":"error","reason":..., "detail":...}
        """
        if query is None:
            return {"status": "error", "reason": SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX, "detail": "Empty query"}

        query = _normalize_query_input(query)
        q = query.strip()
        if not q:
            return {"status": "error", "reason": SchemaGuardErrorCodes.UNSUPPORTED_SYNTAX, "detail": "Empty query"}

        # 0) forbid multi-statement early
        if ";" in q:
            return {
                "status": "error",
                "reason": SchemaGuardErrorCodes.MULTI_STATEMENT_FORBIDDEN,
                "detail": "Semicolon is not allowed",
            }

        uq = _upper_no_strings(q)

        # 1) read-only enforcement (ignore strings)
        if self.policy.get("read_only", True):
            for kw in WRITE_KEYWORDS:
                if re.search(r"\b" + re.escape(kw) + r"\b", uq):
                    return {
                        "status": "error",
                        "reason": QueryPolicyErrorCodes.WRITE_OPERATION_FORBIDDEN,
                        "detail": "Write operation is forbidden in query stage: %s" % kw,
                    }

        # 2) UNION policy
        if (not self.policy.get("allow_union", False)) and re.search(r"(?is)\bUNION\b", q):
            return {
                "status": "error",
                "reason": QueryPolicyErrorCodes.UNSUPPORTED_UNION,
                "detail": "UNION is not allowed in this MVP policy",
            }

        # 3) LIMIT required + cap (for non-CALL)
        if not uq.startswith("CALL "):
            lim = _find_limit(q)
            if self.policy.get("require_limit", True) and lim is None:
                return {
                    "status": "error",
                    "reason": QueryPolicyErrorCodes.LIMIT_REQUIRED,
                    "detail": "LIMIT is required by policy (add LIMIT N)",
                }
            if lim is not None and lim > int(self.policy.get("max_limit", 200)):
                return {
                    "status": "error",
                    "reason": QueryPolicyErrorCodes.LIMIT_TOO_LARGE,
                    "detail": "LIMIT %d exceeds policy max_limit=%d"
                              % (lim, int(self.policy.get("max_limit", 200))),
                }

        # 4) relationship pattern count cap
        rel_cnt = _count_rel_patterns(q)
        if rel_cnt > int(self.policy.get("max_rel_patterns", 8)):
            return {
                "status": "error",
                "reason": QueryPolicyErrorCodes.TOO_MANY_REL_PATTERNS,
                "detail": "Too many relationship patterns: %d > %d"
                          % (rel_cnt, int(self.policy.get("max_rel_patterns", 8))),
            }

        # 5) variable-length depth arbitration + hard ceiling
        requested_k = _max_var_length_upper_bound(q)  # may be None if no var-length in query
        effective_k, clamped = _arbitrate_var_length(requested_k, self.policy, source)

        max_value = self.policy.get("max_var_length_upper", effective_k)
        hard_max = int(max_value) if max_value is not None else None
        if hard_max is not None and effective_k is not None and effective_k > hard_max:
            return {
                "status": "error",
                "reason": QueryPolicyErrorCodes.VARIABLE_LENGTH_TOO_DEEP,
                "detail": "Effective depth %d exceeds hard max %d" % (effective_k, hard_max),
            }

        # 6) dangerous procedures defense-in-depth (CALL only)
        if uq.startswith("CALL "):
            for pref in DANGEROUS_PROCEDURE_PREFIXES:
                if pref.upper() in uq:
                    return {
                        "status": "error",
                        "reason": QueryPolicyErrorCodes.DANGEROUS_PROCEDURE_FORBIDDEN,
                        "detail": "Dangerous procedure forbidden by policy: %s" % pref,
                    }

        # 7) SchemaGuard correctness validation (authoritative)
        res = self.schema_guard.validate(q)
        if res.get("status") != "ok":
            return res

        # 8) attach depth policy metadata for controller
        res.setdefault("metadata", {})
        res["metadata"]["depth_policy"] = {
            "source": source,
            "requested_depth": requested_k,
            "effective_depth": effective_k,
            "default_depth": self.policy.get("default_var_length", 4),
            "max_depth": hard_max,
            "step": int(self.policy.get("var_length_step", 2)),
            # for llm: clamped_to_default means requested>default
            "clamped_to_default": bool(clamped) if source == "llm" else False,
            # for system: clamped_to_max means requested>max
            "clamped_to_max": bool(clamped) if source == "system" else False,
            "note": "No traversal depth limit is configured." if hard_max is None else
                    "LLM drafts are clamped to default depth; controller deepening is allowed up to hard max.",
        }
        return res
