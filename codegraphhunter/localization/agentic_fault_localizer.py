# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple


_STOPWORDS = {
    "the", "and", "or", "for", "with", "without", "instead", "should", "return", "returns",
    "when", "where", "which", "this", "that", "from", "into", "than", "then", "else",
    "null", "bulk", "array", "reply", "expected", "actual", "error", "bug", "key", "value",
    "non", "existing", "nonexisting", "does", "not", "exist", "count",
}

_GENERIC_SOURCE_SIGNALS = [
    # Redis / C command-handler style signals.
    "lookup", "lookupKey", "lookupKeyRead", "lookupKeyWrite", "lookupKeyWriteOrReply",
    "reply", "addReply", "shared.null", "shared.empty", "Null", "Array", "Bulk",
    "argc", "argv", "count", "hascount", "return", "checkType", "type", "parse",
    # Generic validation / C++ library signals.
    "path", "valid", "invalid", "validate", "string", "find", "npos", "contains",
    "size", "compare", "operator", "assert", "request", "response",
]


@dataclass
class FunctionCandidate:
    function: str
    source_file: str
    source_file_basename: str
    start_line: int
    end_line: int
    signature: str
    source_score: float
    graph_score: float
    total_score: float
    arg_no: int
    evidence: Dict[str, Any]

    # v1: keep source-level and graph-level locations separate.
    # This is essential for C++ generated-source cases such as httplib, where
    # GraphDB sees out/httplib.cc but the patch must edit httplib.h.
    graph_function_name: str = ""
    callgraph_node_id: Optional[int] = None
    fun_obj_var_id: Optional[int] = None
    primary_arg_node_id: Optional[int] = None
    primary_icfg_node_id: Optional[int] = None
    graph_source_file: str = ""
    graph_start_line: int = 0
    patch_source_file: str = ""
    patch_start_line: int = 0
    source_mapping: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AgenticFaultLocalizer(object):
    """
    Information-need driven fault localization v0.

    This module does not accept a target function from the user. It treats the
    target function as a hypothesis to be discovered from:
      - problem statement intent,
      - source-level evidence,
      - PAG evidence,
      - CallGraph evidence,
      - ICFG/source-location evidence.

    It is intentionally conservative: graph queries may fail depending on the
    concrete SVF/TuGraph schema, but failures are recorded as evidence gaps
    rather than silently converted into a hardcoded answer.
    """

    def __init__(
        self,
        repo_root: str,
        problem_statement: str,
        patch_source_file: str = "",
        bolt_router: Any = None,
        default_db: str = "default",
        timeout_s: int = 30,
        top_k: int = 5,
        debug: bool = False,
        max_files: int = 200,
        max_file_bytes: int = 4 * 1024 * 1024,
        use_llm_planner: bool = False,
        info_planner_model: str = "gpt-4o-mini",
    ):
        self.repo_root = os.path.abspath(repo_root) if repo_root else ""
        self.problem_statement = problem_statement or ""
        self.patch_source_file = patch_source_file or ""
        self.bolt_router = bolt_router
        self.default_db = default_db or "default"
        self.timeout_s = int(timeout_s)
        self.top_k = int(top_k)
        self.debug = bool(debug)
        self.max_files = int(max_files)
        self.max_file_bytes = int(max_file_bytes)
        self.use_llm_planner = bool(use_llm_planner)
        self.info_planner_model = info_planner_model or "gpt-4o-mini"

    # -----------------------------
    # Problem intent / info needs
    # -----------------------------

    def _extract_problem_intent(self) -> Dict[str, Any]:
        text = self.problem_statement or ""
        upper_terms = []
        for m in re.finditer(r"\b[A-Z][A-Z0-9_]{1,}\b", text):
            t = m.group(0)
            if t not in upper_terms:
                upper_terms.append(t)

        lower_terms = []
        for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
            t = m.group(0).lower()
            if t in _STOPWORDS:
                continue
            if t not in lower_terms:
                lower_terms.append(t)

        behavior_flags = {
            "mentions_count": bool(re.search(r"\bcount\b", text, re.I)),
            "mentions_missing_key": bool(re.search(r"non[- ]?existing|does not exist|not exist|missing", text, re.I)),
            "mentions_null_array": bool(re.search(r"null array|array null|\*-1", text, re.I)),
            "mentions_null_bulk": bool(re.search(r"null bulk|bulk null|\$-1", text, re.I)),
            "mentions_path": bool(re.search(r"\bpath(s)?\b", text, re.I)),
            "mentions_invalid": bool(re.search(r"\binvalid|not valid|reject|fail", text, re.I)),
            "mentions_nul": bool(re.search(r"\bNUL\b|\bNULL\b|embedded\s+NUL|\\0", text, re.I)),
            "mentions_json": bool(re.search(r"\bJSON\b|jsonarg|jsonargs|parse", text, re.I)),
        }

        return {
            "command_or_api_terms": upper_terms[:20],
            "semantic_terms": lower_terms[:80],
            "behavior_flags": behavior_flags,
        }

    def _build_information_needs_with_llm(self, intent: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """
        Ask an LLM to plan information needs, not target functions.

        The LLM is explicitly prohibited from naming a final target function.
        It may only choose what information should be obtained from tools such
        as source_index, PAG, ICFG, and CallGraph. The tool router/evidence
        collector then executes concrete queries.
        """
        if not self.use_llm_planner:
            return None
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            prompt = (
                "You are the information-need planner for a graph-guided APR system.\n"
                "Given a bug/problem statement and extracted intent, decide what information must be collected.\n"
                "Do NOT guess or output a target function, target file, or patch.\n"
                "Output only JSON with key information_needs, a list of 3-7 objects.\n"
                "Each object must have: need_id, question, preferred_sources, query_intent, why.\n"
                "preferred_sources may include only: source_index, source_window, PAG, ICFG, CallGraph.\n\n"
                "Problem statement:\n%s\n\nExtracted intent JSON:\n%s\n"
            ) % (self.problem_statement, json.dumps(intent, ensure_ascii=False, sort_keys=True))
            resp = client.responses.create(
                model=self.info_planner_model,
                input=[
                    {"role": "system", "content": "Output valid JSON only. Plan information needs, not answers."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_output_tokens=1200,
            )
            text = getattr(resp, "output_text", "") or ""
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            obj = json.loads(text)
            needs = obj.get("information_needs") if isinstance(obj, dict) else None
            if not isinstance(needs, list) or not needs:
                return None
            clean = []
            allowed = {"source_index", "source_window", "PAG", "ICFG", "CallGraph"}
            for i, n in enumerate(needs[:7], start=1):
                if not isinstance(n, dict):
                    continue
                srcs = [s for s in (n.get("preferred_sources") or []) if s in allowed]
                if not srcs:
                    srcs = ["source_index"]
                clean.append({
                    "need_id": str(n.get("need_id") or "N%d" % i),
                    "question": str(n.get("question") or "Collect fault localization evidence."),
                    "preferred_sources": srcs,
                    "query_intent": str(n.get("query_intent") or "general_fault_localization"),
                    "why": str(n.get("why") or ""),
                    "planner": "llm",
                })
            return clean or None
        except Exception as e:
            return [{
                "need_id": "LLM_PLANNER_FAILED",
                "question": "LLM information-need planner failed; fall back to deterministic information needs.",
                "preferred_sources": ["source_index"],
                "query_intent": "planner_failure_fallback",
                "why": str(e),
                "planner": "llm_error",
            }]

    def _build_information_needs(self, intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "need_id": "N1",
                "question": "Which source locations implement or dispatch the failing behavior described by the problem statement?",
                "preferred_sources": ["source_index", "source_window"],
                "query_intent": "implementation_entry_discovery",
            },
            {
                "need_id": "N2",
                "question": "Which candidate functions are reachable from command/API entry points or have relevant caller/callee neighborhoods?",
                "preferred_sources": ["CallGraph"],
                "query_intent": "reachability_or_call_neighborhood",
            },
            {
                "need_id": "N3",
                "question": "Where do input arguments and relevant variables flow inside candidate functions?",
                "preferred_sources": ["PAG"],
                "query_intent": "argument_and_value_flow",
            },
            {
                "need_id": "N4",
                "question": "Does the failure-relevant path reach later branches, or does it return earlier?",
                "preferred_sources": ["ICFG", "source_window"],
                "query_intent": "control_flow_and_early_return",
            },
            {
                "need_id": "N5",
                "question": "Which localized source block best explains the observed-vs-expected behavior and can support a minimal repair?",
                "preferred_sources": ["source_window", "PAG", "ICFG"],
                "query_intent": "fault_location_ranking",
            },
        ]

    # -----------------------------
    # Source index
    # -----------------------------

    def _safe_read(self, rel_path: str) -> str:
        if not self.repo_root:
            return ""
        path = os.path.abspath(os.path.join(self.repo_root, rel_path))
        if not (path == self.repo_root or path.startswith(self.repo_root + os.sep)):
            return ""
        if not os.path.isfile(path):
            return ""
        try:
            if os.path.getsize(path) > self.max_file_bytes:
                return ""
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""

    def _iter_source_files(self) -> List[str]:
        if self.patch_source_file:
            return [self.patch_source_file]

        out = []
        if not self.repo_root or not os.path.isdir(self.repo_root):
            return out
        for root, dirs, files in os.walk(self.repo_root):
            dirs[:] = [d for d in dirs if d not in (".git", "deps", "build", ".codegraphhunter")]
            for name in files:
                if not name.endswith((".c", ".cc", ".cpp", ".h", ".hpp")):
                    continue
                rel = os.path.relpath(os.path.join(root, name), self.repo_root)
                out.append(rel)
                if len(out) >= self.max_files:
                    return out
        return out

    def _extract_c_functions(self, rel_file: str, text: str) -> List[Dict[str, Any]]:
        """A lightweight C function extractor for APR candidate discovery."""
        lines = text.splitlines()
        funcs = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # Common one-line function header: return-type name(args) {
            m = re.match(
                r"^\s*(?:static\s+)?(?:inline\s+)?(?:[A-Za-z_][\w\s\*]*?\s+)+(?P<name>[A-Za-z_]\w*)\s*\([^;]*\)\s*\{\s*$",
                line,
            )
            if not m:
                i += 1
                continue
            name = m.group("name")
            if name in ("if", "for", "while", "switch"):
                i += 1
                continue
            start = i
            depth = line.count("{") - line.count("}")
            j = i + 1
            while j < len(lines) and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            end = max(start, j - 1)
            body = "\n".join(lines[start:end + 1])
            funcs.append({
                "function": name,
                "source_file": rel_file,
                "source_file_basename": os.path.basename(rel_file),
                "start_line": start + 1,
                "end_line": end + 1,
                "signature": line.strip(),
                "body": body,
            })
            i = max(j, i + 1)
        return funcs

    def _score_source_candidate(self, fn: Dict[str, Any], intent: Dict[str, Any]) -> Tuple[float, List[str]]:
        name = str(fn.get("function", ""))
        body = str(fn.get("body", ""))
        text_l = (name + "\n" + body).lower()
        score = 0.0
        evidence = []

        # Problem-derived command/API terms are used only as one evidence source,
        # not as the final decision.
        for term in intent.get("command_or_api_terms", [])[:10]:
            t = str(term).lower()
            if not t:
                continue
            if t in name.lower():
                score += 14.0
                evidence.append("function name contains problem command/API term `%s`" % term)
            # Split command-ish terms into subterms (e.g. LPOP -> pop as a weak signal).
            if len(t) >= 3:
                if t in text_l:
                    score += 8.0
                    evidence.append("function body/signature contains problem command/API term `%s`" % term)
                suffix = re.sub(r"^[a-z]", "", t)
                if suffix and len(suffix) >= 3 and suffix in text_l:
                    score += 3.0
                    evidence.append("function contains weak subterm `%s` derived from `%s`" % (suffix, term))

        flags = intent.get("behavior_flags", {}) or {}
        if flags.get("mentions_count") and re.search(r"\b(hascount|count)\b", body, re.I):
            score += 15.0
            evidence.append("function body contains count/hascount logic required by the bug condition")
        if flags.get("mentions_missing_key") and re.search(r"lookupKey|key", body):
            score += 12.0
            evidence.append("function body contains key lookup logic relevant to missing-key behavior")
        if (flags.get("mentions_null_array") or flags.get("mentions_null_bulk")) and re.search(r"addReply|shared\.(null|empty)|reply", body, re.I):
            score += 14.0
            evidence.append("function body contains reply-shape logic relevant to observed/expected output")

        # Generic semantic-term matching. This makes the localizer useful beyond
        # Redis command names: e.g., `detail::is_valid_path` from a problem
        # statement should rank the source function `is_valid_path` highly even
        # before graph evidence is collected.
        for term in intent.get("semantic_terms", [])[:40]:
            t = str(term or "").lower()
            if len(t) < 3 or t in _STOPWORDS:
                continue
            if t in name.lower():
                score += 10.0
                evidence.append("function name contains semantic problem term `%s`" % term)
            elif t in text_l:
                score += 2.0

        if flags.get("mentions_path") and re.search(r"\bpath\b|sub_path|filepath|file_path", body, re.I):
            score += 10.0
            evidence.append("function body contains path-validation relevant logic")
        if flags.get("mentions_invalid") and re.search(r"valid|invalid|return\s+false|return\s+true", body, re.I):
            score += 8.0
            evidence.append("function body contains validity/boolean decision logic")
        if flags.get("mentions_nul") and re.search(r"std::string|string|char|size\(|\[", body, re.I):
            score += 4.0
            evidence.append("function body manipulates string/character content relevant to embedded NUL checks")

        for sig in _GENERIC_SOURCE_SIGNALS:
            if sig.lower() in text_l:
                score += 1.5
        if "lookupKeyWriteOrReply" in body:
            score += 12.0
            evidence.append("function contains lookupKeyWriteOrReply, a missing-key reply-selection helper")
        if "checkType" in body:
            score += 4.0
            evidence.append("function contains type check control flow")
        if "return" in body and ("lookupKey" in body or "addReply" in body):
            score += 4.0
            evidence.append("function contains return paths around lookup/reply logic")

        # Very small wrapper functions are useful as entries but usually need
        # callgraph/source expansion before becoming final repair locations.
        nlines = int(fn.get("end_line", 0)) - int(fn.get("start_line", 0)) + 1
        if nlines <= 4:
            score -= 4.0
            evidence.append("small wrapper function; may be an entry but less likely the final repair block")
        elif nlines >= 20:
            score += 3.0

        return max(0.0, score), evidence[:40]

    # -----------------------------
    # Graph queries
    # -----------------------------

    def _db_for_graph(self, graph_type: str) -> str:
        m = {
            "PAG": "PAG",
            "ICFG": "ICFG",
            "CALLGRAPH": "CallGraph",
            "CallGraph": "CallGraph",
        }
        return m.get(graph_type, self.default_db)

    def _run_graph_query(self, graph_type: str, cypher: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.bolt_router is None:
            return {"ok": False, "error": "bolt_router unavailable", "records": []}
        dbname = self._db_for_graph(graph_type)
        try:
            client = self.bolt_router.get(dbname)
            out = client.run(cypher, params=params or {}, timeout_s=self.timeout_s)
            return {"ok": True, "db": dbname, "records": out.get("records") or []}
        except Exception as e:
            return {"ok": False, "db": dbname, "error": str(e), "records": []}

    def _to_int_or_none(self, v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            if isinstance(v, bool):
                return None
            return int(v)
        except Exception:
            return None

    def _first_non_negative_int(self, values: List[Any]) -> Optional[int]:
        for v in values:
            iv = self._to_int_or_none(v)
            if iv is not None and iv >= 0:
                return iv
        return None

    def _parse_source_loc(self, source_loc: Any) -> Dict[str, Any]:
        """Extract file/line from SVF source_loc strings.

        SVF dumps are not uniform. Examples seen in the same DB include:
          { "ln": 127, "file": "out/httplib.cc" }
          { "ln": 127, "fl": "out/httplib.cc" }
          { "basic block": if.end, "location": { "ln": 1127, "cl": 1, "fl": "out/httplib.cc" } }
          { 0th arg _Z... "ln": 127, "file": "out/httplib.cc" }
        """
        text = str(source_loc or "")
        line = None
        file_name = ""
        m = re.search(r'"ln"\s*:\s*(\d+)', text)
        if m:
            try:
                line = int(m.group(1))
            except Exception:
                line = None
        m = re.search(r'"(?:file|fl)"\s*:\s*"([^"]+)"', text)
        if m:
            file_name = m.group(1)
        return {"line": line, "file": file_name, "raw": text}

    def _line_filter_fragments(self, line: Optional[int]) -> List[str]:
        if line is None:
            return []
        # Use fragments that avoid matching 1127/2127 when the target is 127.
        return [
            '"ln": %d,' % int(line),
            '"ln": %d ' % int(line),
            '"ln": %d}' % int(line),
            '"ln": %d, "' % int(line),
        ]

    def _collect_graph_evidence_for_candidate(self, fn: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """Collect v1 graph evidence for one source-level function candidate.

        v1 treats graph identifiers as first-class evidence. It does not depend
        on exact source-level function names being equal to GraphDB function
        names, which is important for C++ mangled symbols and generated sources.
        """
        function = str(fn.get("function", ""))
        basename = str(fn.get("source_file_basename", ""))
        graph_score = 0.0
        evidence: Dict[str, Any] = {
            "PAG": {},
            "CallGraph": {},
            "ICFG": {},
            "_graph_location": {},
        }

        callgraph_node_id: Optional[int] = None
        fun_obj_var_id: Optional[int] = None
        graph_function_name = ""

        # 1) CallGraph: resolve by `fun_name CONTAINS`, not by exact source-level name.
        # This supports C++ mangled symbols such as _ZN7httplib6detail13is_valid_path...
        q_cg_node = """
MATCH (n)
WHERE n.fun_name CONTAINS $function_name
RETURN n.id AS callgraph_node_id,
       n.fun_name AS graph_function_name,
       n.fun_obj_var_id AS fun_obj_var_id,
       n.source_loc AS source_loc
LIMIT 20
"""
        out_cg_node = self._run_graph_query("CallGraph", q_cg_node, {"function_name": function})
        evidence["CallGraph"]["function_lookup"] = self._brief_records(out_cg_node)
        cg_rows = out_cg_node.get("records") or []
        if out_cg_node.get("ok") and cg_rows:
            # Prefer the shortest matching symbol: usually the real function over wrappers/templates.
            def _cg_rank(row: Dict[str, Any]) -> Tuple[int, int]:
                name = str(row.get("graph_function_name") or "")
                exactish = 0 if function in name else 1
                return (exactish, len(name))
            best = sorted(cg_rows, key=_cg_rank)[0]
            callgraph_node_id = self._to_int_or_none(best.get("callgraph_node_id"))
            fun_obj_var_id = self._to_int_or_none(best.get("fun_obj_var_id"))
            graph_function_name = str(best.get("graph_function_name") or "")
            graph_score += 18.0
            evidence["CallGraph"]["claim"] = "candidate resolved in CallGraph by fun_name substring"

        if callgraph_node_id is not None:
            q_cg_nb = """
MATCH (n)-[e]->(m)
WHERE n.id = $callgraph_node_id OR m.id = $callgraph_node_id
RETURN n,e,m
LIMIT 50
"""
            out_cg_nb = self._run_graph_query("CallGraph", q_cg_nb, {"callgraph_node_id": int(callgraph_node_id)})
            evidence["CallGraph"]["neighborhood"] = self._brief_records(out_cg_nb, max_rows=8)
            rows = out_cg_nb.get("records") or []
            if out_cg_nb.get("ok") and rows:
                graph_score += min(18.0, 6.0 + len(rows) * 1.0)
                evidence["CallGraph"]["claim_neighborhood"] = "candidate has caller/callee CallGraph evidence"

        # 2) PAG: resolve FunObjVar using CallGraph.fun_obj_var_id first.
        pag_fun_rows: List[Dict[str, Any]] = []
        if fun_obj_var_id is not None:
            q_fun_id = """
MATCH (f:FunObjVar)
WHERE f.id = $fun_obj_var_id
RETURN f.id AS function_seed_id,
       f.val_name AS function_name,
       f.source_loc AS source_loc,
       f.all_args_node_ids AS all_args_node_ids,
       f.icfg_node_id AS icfg_node_id
LIMIT 5
"""
            out_fun_id = self._run_graph_query("PAG", q_fun_id, {"fun_obj_var_id": int(fun_obj_var_id)})
            evidence["PAG"]["function_lookup_by_fun_obj_var_id"] = self._brief_records(out_fun_id)
            pag_fun_rows = out_fun_id.get("records") or []
            if out_fun_id.get("ok") and pag_fun_rows:
                graph_score += 22.0
                evidence["PAG"]["claim"] = "CallGraph.fun_obj_var_id maps to PAG.FunObjVar"

        if not pag_fun_rows:
            q_fun_name = """
MATCH (f:FunObjVar)
WHERE f.val_name CONTAINS $function_name
RETURN f.id AS function_seed_id,
       f.val_name AS function_name,
       f.source_loc AS source_loc,
       f.all_args_node_ids AS all_args_node_ids,
       f.icfg_node_id AS icfg_node_id
LIMIT 20
"""
            out_fun_name = self._run_graph_query("PAG", q_fun_name, {"function_name": function})
            evidence["PAG"]["function_lookup_by_name_contains"] = self._brief_records(out_fun_name)
            pag_fun_rows = out_fun_name.get("records") or []
            if out_fun_name.get("ok") and pag_fun_rows:
                graph_score += 16.0
                evidence["PAG"]["claim"] = "candidate function exists as FunObjVar in PAG by name substring"

        function_seed_id: Optional[int] = None
        graph_source_file = ""
        graph_start_line = 0
        all_args_node_ids = ""
        if pag_fun_rows:
            fun = pag_fun_rows[0]
            function_seed_id = self._to_int_or_none(fun.get("function_seed_id"))
            if fun_obj_var_id is None:
                fun_obj_var_id = function_seed_id
            if not graph_function_name:
                graph_function_name = str(fun.get("function_name") or "")
            all_args_node_ids = str(fun.get("all_args_node_ids") or "")
            parsed = self._parse_source_loc(fun.get("source_loc"))
            graph_source_file = str(parsed.get("file") or "")
            graph_start_line = int(parsed.get("line") or 0)

        # 3) PAG arguments and value-flow from primary argument.
        primary_arg_node_id: Optional[int] = None
        arg_rows: List[Dict[str, Any]] = []
        if function_seed_id is not None:
            q_arg = """
MATCH (a:ArgValVar)
WHERE a.cg_node_id = $function_seed_id
RETURN a.id AS arg_id,
       a.arg_no AS arg_no,
       a.source_loc AS source_loc,
       a.icfg_node_id AS icfg_node_id
LIMIT 12
"""
            out_arg = self._run_graph_query("PAG", q_arg, {"function_seed_id": int(function_seed_id)})
            evidence["PAG"]["arg_lookup"] = self._brief_records(out_arg, max_rows=8)
            arg_rows = out_arg.get("records") or []
            if out_arg.get("ok") and arg_rows:
                graph_score += 12.0
                primary_arg_node_id = self._to_int_or_none(arg_rows[0].get("arg_id"))

        if primary_arg_node_id is None and all_args_node_ids:
            m = re.search(r"\d+", all_args_node_ids)
            if m:
                primary_arg_node_id = self._to_int_or_none(m.group(0))

        if primary_arg_node_id is not None:
            graph_source_hint = graph_source_file or basename
            if graph_source_hint:
                q_flow = """
MATCH p=(n:ArgValVar {id: $seed_id})-[*1..4]->(m)
WHERE m.source_loc CONTAINS $source_hint
RETURN p, n, m
LIMIT 30
"""
                out_flow = self._run_graph_query("PAG", q_flow, {
                    "seed_id": int(primary_arg_node_id),
                    "source_hint": str(graph_source_hint),
                })
            else:
                q_flow = """
MATCH p=(n:ArgValVar {id: $seed_id})-[*1..4]->(m)
RETURN p, n, m
LIMIT 30
"""
                out_flow = self._run_graph_query("PAG", q_flow, {"seed_id": int(primary_arg_node_id)})
            evidence["PAG"]["primary_arg_flow"] = self._brief_records(out_flow, max_rows=6)
            rows = out_flow.get("records") or []
            if out_flow.get("ok") and rows:
                graph_score += min(20.0, 5.0 + len(rows) * 0.5)

        # 4) PAG -> ICFG bridge. Prefer exact graph source line from PAG.FunObjVar.
        primary_icfg_node_id: Optional[int] = None
        bridge_rows: List[Dict[str, Any]] = []
        if graph_source_file and graph_start_line:
            line_frags = self._line_filter_fragments(graph_start_line)
            conditions = " OR ".join(["v.source_loc CONTAINS $line_frag_%d" % i for i in range(len(line_frags))])
            q_bridge = """
MATCH (v)
WHERE v.source_loc CONTAINS $graph_source_file
  AND (%s)
RETURN v.id AS pag_id,
       v.icfg_node_id AS icfg_node_id,
       v.source_loc AS source_loc
LIMIT 80
""" % conditions
            params = {"graph_source_file": graph_source_file}
            for i, frag in enumerate(line_frags):
                params["line_frag_%d" % i] = frag
            out_bridge = self._run_graph_query("PAG", q_bridge, params)
            evidence["PAG"]["source_line_icfg_bridge"] = self._brief_records(out_bridge, max_rows=10)
            bridge_rows = out_bridge.get("records") or []
            primary_icfg_node_id = self._first_non_negative_int([r.get("icfg_node_id") for r in bridge_rows])
            if primary_icfg_node_id is not None:
                graph_score += 15.0
                evidence["PAG"]["claim_icfg_bridge"] = "PAG source-line nodes expose icfg_node_id"

        if primary_icfg_node_id is None and arg_rows:
            primary_icfg_node_id = self._first_non_negative_int([r.get("icfg_node_id") for r in arg_rows])

        # As a final bridge fallback, query ICFG by fun_obj_var_id. This is often
        # available even when individual PAG argument nodes do not carry icfg IDs.
        if primary_icfg_node_id is None and fun_obj_var_id is not None:
            q_icfg_by_fun = """
MATCH (n)
WHERE n.fun_obj_var_id = $fun_obj_var_id
RETURN n.id AS icfg_node_id,
       n.source_loc AS source_loc,
       n.fun_obj_var_id AS fun_obj_var_id
LIMIT 20
"""
            out_icfg_by_fun = self._run_graph_query("ICFG", q_icfg_by_fun, {"fun_obj_var_id": int(fun_obj_var_id)})
            evidence["ICFG"]["nodes_by_fun_obj_var_id"] = self._brief_records(out_icfg_by_fun, max_rows=8)
            primary_icfg_node_id = self._first_non_negative_int([r.get("icfg_node_id") for r in (out_icfg_by_fun.get("records") or [])])
            if primary_icfg_node_id is not None:
                graph_score += 8.0

        # 5) ICFG local control-flow around the bridged node.
        if primary_icfg_node_id is not None:
            q_icfg_node = """
MATCH (n)
WHERE n.id = $icfg_node_id
RETURN n
LIMIT 5
"""
            out_icfg_node = self._run_graph_query("ICFG", q_icfg_node, {"icfg_node_id": int(primary_icfg_node_id)})
            evidence["ICFG"]["seed_node"] = self._brief_records(out_icfg_node)

            q_icfg_path = """
MATCH p=(n)-[*1..3]->(m)
WHERE n.id = $icfg_node_id
RETURN p,n,m
LIMIT 30
"""
            out_icfg_path = self._run_graph_query("ICFG", q_icfg_path, {"icfg_node_id": int(primary_icfg_node_id)})
            evidence["ICFG"]["local_paths"] = self._brief_records(out_icfg_path, max_rows=6)
            if out_icfg_node.get("ok") and out_icfg_node.get("records"):
                graph_score += 8.0
            if out_icfg_path.get("ok") and out_icfg_path.get("records"):
                graph_score += min(14.0, 4.0 + len(out_icfg_path.get("records") or []) * 1.0)
                evidence["ICFG"]["claim"] = "PAG.icfg_node_id maps to ICFG local control-flow"

        evidence["_graph_location"] = {
            "graph_function_name": graph_function_name,
            "callgraph_node_id": callgraph_node_id,
            "fun_obj_var_id": fun_obj_var_id,
            "primary_arg_node_id": primary_arg_node_id,
            "primary_icfg_node_id": primary_icfg_node_id,
            "graph_source_file": graph_source_file,
            "graph_start_line": graph_start_line,
            "patch_source_file": str(fn.get("source_file", "")),
            "patch_start_line": int(fn.get("start_line", 0) or 0),
            "source_mapping": {
                "patch_source": "%s:%s" % (str(fn.get("source_file", "")), str(fn.get("start_line", 0))),
                "graph_source": "%s:%s" % (graph_source_file, str(graph_start_line or "")),
                "mapping_reason": "source-level candidate mapped to graph-level FunObjVar/source_loc; generated source may differ from patch source",
            },
        }

        return graph_score, evidence

    def _brief_records(self, out: Dict[str, Any], max_rows: int = 5) -> Dict[str, Any]:
        rows = out.get("records") or []
        return {
            "ok": bool(out.get("ok")),
            "db": out.get("db"),
            "error": out.get("error"),
            "rows": len(rows),
            "preview": rows[:max_rows],
        }

    # -----------------------------
    # Localization
    # -----------------------------

    def localize(self) -> Dict[str, Any]:
        if not self.repo_root:
            raise RuntimeError("repo_root is required for AgenticFaultLocalizer")

        intent = self._extract_problem_intent()
        llm_needs = self._build_information_needs_with_llm(intent)
        if llm_needs and not (len(llm_needs) == 1 and llm_needs[0].get("need_id") == "LLM_PLANNER_FAILED"):
            info_needs = llm_needs
            planner_status = "llm"
        else:
            info_needs = self._build_information_needs(intent)
            planner_status = "deterministic_fallback"
            if llm_needs:
                info_needs = llm_needs + info_needs

        raw_functions: List[Dict[str, Any]] = []
        for rel in self._iter_source_files():
            text = self._safe_read(rel)
            if not text:
                continue
            raw_functions.extend(self._extract_c_functions(rel, text))

        # First rank by source evidence, then collect graph evidence only for
        # the most plausible candidates. This avoids hundreds of expensive
        # graph probes in large C++ header-only files.
        source_ranked: List[Tuple[float, List[str], Dict[str, Any]]] = []
        for fn in raw_functions:
            source_score, source_evidence = self._score_source_candidate(fn, intent)
            if source_score <= 0:
                continue
            source_ranked.append((float(source_score), source_evidence, fn))

        source_ranked.sort(key=lambda x: (-x[0], int(x[2].get("start_line", 0))))
        graph_probe_limit = min(len(source_ranked), max(25, int(self.top_k) * 12))

        scored: List[FunctionCandidate] = []
        for idx, (source_score, source_evidence, fn) in enumerate(source_ranked):
            graph_score = 0.0
            graph_evidence: Dict[str, Any] = {}
            if idx < graph_probe_limit and source_score >= 8.0:
                graph_score, graph_evidence = self._collect_graph_evidence_for_candidate(fn)
            total = source_score + graph_score
            arg_no = self._infer_primary_arg_no(fn)
            loc = graph_evidence.get("_graph_location", {}) if isinstance(graph_evidence, dict) else {}
            scored.append(FunctionCandidate(
                function=str(fn.get("function")),
                source_file=str(fn.get("source_file")),
                source_file_basename=str(fn.get("source_file_basename")),
                start_line=int(fn.get("start_line", 0)),
                end_line=int(fn.get("end_line", 0)),
                signature=str(fn.get("signature", "")),
                source_score=float(source_score),
                graph_score=float(graph_score),
                total_score=float(total),
                arg_no=int(arg_no),
                evidence={
                    "information_needs_addressed": [n["need_id"] for n in info_needs],
                    "source": source_evidence,
                    "graph": graph_evidence,
                },
                graph_function_name=str(loc.get("graph_function_name") or ""),
                callgraph_node_id=loc.get("callgraph_node_id"),
                fun_obj_var_id=loc.get("fun_obj_var_id"),
                primary_arg_node_id=loc.get("primary_arg_node_id"),
                primary_icfg_node_id=loc.get("primary_icfg_node_id"),
                graph_source_file=str(loc.get("graph_source_file") or ""),
                graph_start_line=int(loc.get("graph_start_line") or 0),
                patch_source_file=str(loc.get("patch_source_file") or fn.get("source_file") or ""),
                patch_start_line=int(loc.get("patch_start_line") or fn.get("start_line") or 0),
                source_mapping=dict(loc.get("source_mapping") or {}),
            ))

        scored.sort(key=lambda c: (-c.total_score, -c.graph_score, c.start_line))
        top = scored[:max(1, self.top_k)]
        selected = top[0].to_dict() if top else None

        return {
            "status": "ok" if selected else "no_candidate",
            "problem_intent": intent,
            "information_needs": info_needs,
            "information_need_planner_status": planner_status,
            "selection_policy": "rank candidates by source evidence plus PAG/CallGraph/ICFG evidence; target function is not supplied by CLI",
            "selected_location": selected,
            "ranked_candidates": [c.to_dict() for c in top],
            "candidates_considered": len(scored),
            "source_files_scanned": self._iter_source_files(),
        }

    def _infer_primary_arg_no(self, fn: Dict[str, Any]) -> int:
        sig = str(fn.get("signature", ""))
        # Most command handlers in C repos pass request/client/context as the first
        # formal parameter. Use arg0 as primary seed unless future evidence says otherwise.
        if re.search(r"\(\s*(client|struct\s+client|request|context|ctx)\b", sig):
            return 0
        return 0
