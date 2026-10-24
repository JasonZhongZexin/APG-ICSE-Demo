"""Implementations of source, graph and cross-graph evidence contracts."""

import json
import re
import subprocess
from collections import defaultdict, deque
from contextlib import contextmanager

from codegraphhunter.web.query_limits import QueryLimits

FLOW_EDGES = ("AddrStmt", "CopyStmt", "LoadStmt", "StoreStmt", "GepStmt", "PhiStmt", "SelectStmt", "CallPE", "RetPE",
              "BinaryOPStmt", "UnaryOPStmt", "CmpStmt")
ALIAS_EDGES = ("AddrStmt", "CopyStmt", "GepStmt", "PhiStmt", "SelectStmt")


def ids(value):
    return [int(item) for item in re.findall(r"(?<!\w)-?\d+", str(value or "")) if int(item) >= 0]


def short_name(value):
    if value.startswith("_Z"):
        try:
            value = subprocess.run(["c++filt", value], capture_output=True, text=True, timeout=2).stdout.strip()
        except OSError:
            pass
    return value.split("(", 1)[0].rsplit("::", 1)[-1]


class CapabilityHandlers:
    def __init__(self, graph, source, plan):
        self.g, self.s, self.p = graph, source, plan
        self.conclusion, self.enumeration = "evidence", "paths"
        self.limitations = []
        self.details = {}

    def note(self, message):
        if message not in self.limitations:
            self.limitations.append(message)

    @contextmanager
    def graph_budget(self, parts):
        original = self.g.limits
        self.g.limits = QueryLimits(original.max_depth, min(original.entity_limit,
            len(self.g.records) + max(1, original.entity_limit // parts)))
        try:
            yield
        finally:
            self.g.limits = original

    def execute(self):
        prefix = self.p["capability"].split(".")[0]
        return {"SRC": self.source, "CG": self.callgraph, "ICFG": self.icfg,
                "PAG": self.pag, "CHG": self.chg, "TYPE": self.types, "XG": self.cross_graph}[prefix]()

    def cg_functions(self, name=None, node_id=None, all_functions=False):
        name = self.p["function_name"] if name is None else name
        node_id = self.p["node_id"] if node_id is None else node_id
        if node_id >= 0:
            return self.g.node("CallGraph", node_id, "CallGraphNode")
        if name:
            return self.g.select("CallGraph", "CallGraphNode", [("fun_name", "CONTAINS", short_name(name) if "::" in name else name)], 1000)
        if self.p["function_id"] >= 0:
            return self.g.select("CallGraph", "CallGraphNode", [("fun_obj_var_id", "=", self.p["function_id"])])
        if all_functions:
            return self.g.select("CallGraph", "CallGraphNode")
        if self.p["file"] and self.p["line"] >= 1:
            found = []
            for symbol in self.function_names_at_location():
                found.extend(self.g.select("CallGraph", "CallGraphNode", [("fun_name", "CONTAINS", symbol)]))
            return list(dict.fromkeys(found))
        raise ValueError("Specify a function name or a CallGraph node ID.")

    def function_names_at_location(self):
        names = {row["function"] for row in self.s.functions()
                 if row["source_file"] == self.p["file"] and row["start_line"] <= self.p["line"] <= row["end_line"]}
        if names:
            return sorted(names)
        for key in self.source_nodes("ICFG"):
            ident = self.g.data[key].get("fun_obj_var_id", -1)
            if isinstance(ident, int) and ident >= 0:
                for fun in self.funobjs("", ident):
                    names.add(short_name(self.g.data[fun].get("val_name", "")))
        self.note("Generated-source locations are mapped through the exported function ID and then matched against original source symbols; overloaded symbols may have multiple candidates.")
        return sorted(names - {""})

    def funobjs(self, name=None, function_id=None):
        name = self.p["function_name"] if name is None else name
        function_id = self.p["function_id"] if function_id is None else function_id
        if function_id >= 0:
            return self.g.node("PAG", function_id, "FunObjVar")
        if not name:
            raise ValueError("Specify a function name or its FunObjVar ID.")
        return self.g.select("PAG", "FunObjVar", [("val_name", "CONTAINS", short_name(name) if "::" in name else name)])

    def arguments(self, name=None, function_id=None, arg_no=None):
        arg_no = self.p["arg_no"] if arg_no is None else arg_no
        found = []
        for function in self.funobjs(name, function_id):
            # The exporter stores FunObjVar IDs in ArgValVar.cg_node_id.
            conditions = [("cg_node_id", "=", function[1])]
            if arg_no >= 0:
                conditions.append(("arg_no", "=", arg_no))
            found.extend(self.g.select("PAG", "ArgValVar", conditions))
        return found

    def pag_seeds(self):
        if self.p["node_id"] >= 0:
            return self.g.node("PAG", self.p["node_id"], self.p["node_label"])
        if self.p["function_name"] or self.p["function_id"] >= 0:
            return self.arguments()
        if self.p["file"] or self.p["line"] >= 1:
            return self.source_nodes("PAG")
        raise ValueError("Specify a PAG node, a source location, or a function argument.")

    def source_nodes(self, graph):
        rows = []
        conditions = []
        if self.p["file"]:
            conditions.append(("source_loc", "CONTAINS", self.p["file"]))
        if self.p["line"] >= 1:
            conditions.append(("source_loc", "CONTAINS", '"ln": ' + str(self.p["line"])))
        if not conditions:
            return []
        for label in ([self.p["node_label"]] if self.p["node_label"] else self.g.labels(graph)):
            if "source_loc" in self.g.properties(label):
                rows.extend(self.g.select(graph, label, conditions))
        self.note("Source-line filters refer to the graph's exported source_loc; generated-source lines can differ from original source lines.")
        return rows

    @staticmethod
    def location_line(value):
        match = re.search(r'"ln"\s*:\s*(\d+)', str(value or ""))
        if not match:
            match = re.search(r':(\d+)(?::\d+)?$', str(value or ""))
        return int(match.group(1)) if match else None

    def entries(self, name=None, function_id=None):
        rows = []
        for function in self.funobjs(name, function_id):
            rows.extend(self.g.select("ICFG", "FunEntryICFGNode", [("fun_obj_var_id", "=", function[1])]))
        return rows

    def icfg_seeds(self, target=False):
        ident = self.p["target_node_id"] if target else self.p["node_id"]
        label = self.p["target_node_label"] if target else self.p["node_label"]
        if ident >= 0:
            return self.g.node("ICFG", ident, label)
        if not target and self.p["callsite_id"] >= 0:
            return self.g.node("ICFG", self.p["callsite_id"], "CallICFGNode")
        name = self.p["target_function"] if target else self.p["function_name"]
        if name or (not target and self.p["function_id"] >= 0):
            return self.entries(name, -1 if target else self.p["function_id"])
        if not target:
            return self.source_nodes("ICFG")
        raise ValueError("Specify the target ICFG node or target function.")

    def node_rows(self, keys, **extra):
        for key in keys:
            if not self.g.add_record(dict(label=key[0], node_id=key[1], **self.g.data[key], **extra), [key]):
                break

    def source(self):
        cap, p = self.p["capability"], self.p
        text = p["function_name"] or p["keyword"] or p["problem_statement"]
        if cap == "SRC.KEYWORD_USAGE":
            for row in self.s.usages(p["keyword"] or p["function_name"], p["limit"], p["file"]):
                self.g.add_record(row)
            return
        if cap == "SRC.SOURCE_WINDOW" and p["file"] and p["line"] >= 1:
            self.g.add_record(self.s.window(p["file"], p["line"]))
            return
        if cap == "SRC.PATCH_LOCATION" and p["node_id"] >= 0:
            functions = self.cg_functions(node_id=p["node_id"])
            text = short_name(self.g.data[functions[0]]["fun_name"]) if functions else text
        if cap == "SRC.PATCH_LOCATION" and not text and p["file"] and p["line"] >= 1:
            text = " ".join(self.function_names_at_location())
        if not text:
            raise ValueError("Specify a function, symbol, keyword, or bug description.")
        rows = self.s.search(text, p["limit"], "" if cap == "SRC.PATCH_LOCATION" else p["file"])
        if cap == "SRC.SOURCE_WINDOW":
            rows = [dict(row, **self.s.window(row["file"], row["start_line"], min(150, max(12, row["end_line"] - row["start_line"])))) for row in rows]
        for row in rows:
            self.g.add_record(row)
        self.note("Source search and localization use a bounded lexical C/C++ index; candidate ranking is heuristic and does not prove a root cause.")
        if cap == "SRC.PATCH_LOCATION":
            self.note("Patch locations are resolved in the original source index; generated graph line numbers are not copied into the original file.")

    def call_edges(self):
        """Stream exported CallGraph edges in pages, retaining parallel calls."""
        offset = 0
        while offset < 100000:
            query = ("MATCH (source:CallGraphNode)-[flow:CallGraphEdge]->(target:CallGraphNode)\n"
                     "RETURN source.id AS source_id, source.fun_name AS source_name, source.fun_obj_var_id AS source_fun_obj_var_id, "
                     "target.id AS target_id, target.fun_name AS target_name, target.fun_obj_var_id AS target_fun_obj_var_id, "
                     "flow.csid AS edge_csid, flow.kind AS edge_kind, euid(flow) AS edge_uid\n"
                     f"ORDER BY source_id, target_id, edge_uid\nSKIP {offset}\nLIMIT 1000")
            rows = self.g.read("CallGraph", query)
            for row in rows:
                self.g.check()
                left, right = ("CallGraphNode", row["source_id"]), ("CallGraphNode", row["target_id"])
                for key, role in ((left, "source"), (right, "target")):
                    self.g.data[key] = dict(id=key[1], fun_name=row[role + "_name"], fun_obj_var_id=row[role + "_fun_obj_var_id"], graph="CallGraph")
                yield left, right, dict(source=left, target=right, label="CallGraphEdge", properties={
                    "csid": row["edge_csid"], "kind": row["edge_kind"], "exported_edge_uid": row["edge_uid"]})
            if len(rows) < 1000:
                return
            offset += len(rows)
        self.g.partial("scan_limit", "The CallGraph edge scan reached 100000 exported edges.")

    def cycle_witnesses(self, graph, roots, edges, function_id=None):
        self.enumeration = "cycle_witnesses"
        _, adjacency = self.g.collect(graph, roots, edges, function_id=function_id)
        done, active, keys, path_edges = set(), {}, [], []
        seen = set()
        for root in adjacency:
            if root in done:
                continue
            active[root] = 0
            keys.append(root)
            stack = [iter(adjacency.get(root, []))]
            while stack:
                self.g.check()
                try:
                    target, edge = next(stack[-1])
                except StopIteration:
                    stack.pop()
                    done.add(keys[-1])
                    del active[keys.pop()]
                    if path_edges:
                        path_edges.pop()
                    continue
                if target in active:
                    start = active[target]
                    cycle, relationships = keys[start:] + [target], path_edges[start:] + [edge]
                    signature = tuple(sorted((e["source"], e["target"], str(e["properties"])) for e in relationships))
                    if signature in seen:
                        continue
                    seen.add(signature)
                    if self.p["depth"] is not None and len(relationships) > self.p["depth"]:
                        self.g.partial("depth_limit")
                        continue
                    if not self.g.add_path(graph, cycle, relationships, extra={"cycle_witness": True}):
                        return
                elif target not in done:
                    active[target] = len(keys)
                    keys.append(target)
                    path_edges.append(edge)
                    stack.append(iter(adjacency.get(target, [])))
        self.note("Cycle results are structural cycle witnesses, not an enumeration of every possible cycle or a proof of runtime recursion.")

    def callgraph(self):
        cap, p = self.p["capability"], self.p
        if cap == "CG.FANIN_FANOUT":
            incoming, outgoing, sites_in, sites_out = defaultdict(set), defaultdict(set), defaultdict(int), defaultdict(int)
            for left, right, edge in self.call_edges():
                outgoing[left].add(right)
                incoming[right].add(left)
                sites_out[left] += 1
                sites_in[right] += 1
            candidates = set(incoming) | set(outgoing)
            if p["function_name"] or p["node_id"] >= 0 or p["function_id"] >= 0:
                candidates = set(self.cg_functions())
            ranked = sorted(candidates, key=lambda k: (-len(incoming[k] if p["direction"] == "in" else outgoing[k]), k[1]))
            for key in ranked[:p["limit"]]:
                self.g.add_record(dict(function_id=key[1], function_name=self.g.data[key]["fun_name"], fan_in=len(incoming[key]),
                                       fan_out=len(outgoing[key]), incoming_call_sites=sites_in[key], outgoing_call_sites=sites_out[key]), [key])
            self.note("Fan-in/out count distinct neighboring functions; call-site counts are reported separately. Rankings cover the scanned graph.")
            return
        roots = self.cg_functions(all_functions=cap in {"CG.RECURSION_SELF", "CG.CYCLE_DETECTION"})
        if cap == "CG.FUNCTION_RESOLVE":
            return self.node_rows(roots)
        if cap == "CG.CALLER_CALLEE_NEIGHBORHOOD":
            for root in roots:
                for direction in ("in", "out"):
                    for target, edge in self.g.neighbors("CallGraph", root, ["CallGraphEdge"], direction):
                        if not self.g.add_path("CallGraph", [root, target], [edge], incoming=direction == "in", extra={"role": "caller" if direction == "in" else "callee"}):
                            return
            return
        if cap == "CG.RECURSION_SELF":
            for root in roots:
                for target, edge in self.g.neighbors("CallGraph", root, ["CallGraphEdge"]):
                    if target == root and not self.g.add_path("CallGraph", [root, target], [edge], extra={"direct_recursion": True}):
                        return
            return
        if cap == "CG.CYCLE_DETECTION":
            return self.cycle_witnesses("CallGraph", roots, ["CallGraphEdge"])
        if cap == "CG.ENTRY_TO_TARGET_CONTEXT":
            def entry(key):
                return not list(self.g.neighbors("CallGraph", key, ["CallGraphEdge"], "in"))
            for root in roots:
                if entry(root):
                    self.g.add_path("CallGraph", [root], [], extra={"entry_definition": "zero_in_degree"})
                else:
                    self.g.walk("CallGraph", [root], ["CallGraphEdge"], entry, incoming=True)
            self.note("An entry is a zero-in-degree node in the exported CallGraph; externally invoked callbacks may have other entry semantics.")
            return
        if not p["target_function"] and p["target_node_id"] < 0:
            raise ValueError("Specify the target function or target CallGraph node ID.")
        targets = set(self.cg_functions(p["target_function"], p["target_node_id"]))
        self.g.walk("CallGraph", roots, ["CallGraphEdge"], lambda key: key in targets)
        self.conclusion = "reachable_in_exported_callgraph" if self.g.records else "unknown" if not self.g.complete else "no_path_within_selected_depth" if p["depth"] is not None else "no_path_in_selected_exported_graph"
        self.note("CallGraph reachability is static structural evidence; a returned path is not proof that a particular input executes it.")

    def icfg_nodes(self, functions, labels=None):
        rows = []
        for function_id in functions:
            for label in labels or self.g.labels("ICFG"):
                if "fun_obj_var_id" in self.g.properties(label):
                    rows.extend(self.g.select("ICFG", label, [("fun_obj_var_id", "=", function_id)]))
        return rows

    def branch(self, key):
        return len({target for target, _ in self.g.neighbors("ICFG", key, ["IntraCFGEdge"])}) > 1

    def icfg(self):
        cap, p = self.p["capability"], self.p
        roots = self.icfg_seeds()
        if cap == "ICFG.SEED_RESOLVE":
            return self.node_rows(roots)
        function_ids = {self.g.data[key].get("fun_obj_var_id") for key in roots}
        function_ids.discard(None)
        if cap == "ICFG.INTERPROC_PATH":
            targets = set(self.icfg_seeds(target=True))
            self.g.walk("ICFG", roots, ["IntraCFGEdge", "CallCFGEdge", "RetCFGEdge"], lambda key: key in targets)
            self.note("Interprocedural paths follow exported ICFG edges; call/return contexts are not stack-balanced, so feasibility remains unproven.")
            return
        if cap == "ICFG.CALLSITE_CONTEXT":
            roots = [key for key in roots if key[0] == "CallICFGNode"]
            if not roots and (p["function_name"] or p["target_function"]):
                for function in self.funobjs(p["target_function"] or p["function_name"], -1 if p["target_function"] else None):
                    roots.extend(self.g.select("ICFG", "CallICFGNode", [("called_fun_obj_var_id", "=", function[1])]))
            self.g.walk("ICFG", roots, ["IntraCFGEdge"], incoming=True)
            self.g.walk("ICFG", roots, ["IntraCFGEdge"])
            return
        if cap == "ICFG.BRANCH_CONTEXT":
            if p["node_id"] < 0 and p["callsite_id"] < 0:
                scoped = self.icfg_nodes(function_ids)
                if p["line"] >= 1:
                    roots = [key for key in scoped if self.location_line(self.g.data[key].get("source_loc")) == p["line"]]
                else:
                    roots = [key for key in scoped if self.g.data[key].get("is_return")]
            self.g.walk("ICFG", roots, ["IntraCFGEdge"], self.branch, incoming=True,
                        extra=lambda keys, edges: {"branch_edges": [edge["properties"] for _, edge in self.g.neighbors("ICFG", keys[-1], ["IntraCFGEdge"])]})
            self.note("These branches can reach the selected node; reachability alone does not establish dominance or the predicate's truth value.")
            return
        if cap == "ICFG.EARLY_RETURN_GUARD":
            returns = [key for key in self.icfg_nodes(function_ids, ["IntraICFGNode"]) if self.g.data[key].get("is_return")]
            for root in returns:
                fn = self.g.data[root].get("fun_obj_var_id")
                self.g.walk("ICFG", [root], ["IntraCFGEdge"], self.branch, incoming=True, function_id=fn,
                            extra=lambda keys, edges: {"classification": "guard_like_return_path"})
            self.note("Early-return/guard labels are structural candidates; source semantics and dominance require additional proof.")
            return
        if cap == "ICFG.UNREACHABLE_APPROX":
            for fn in function_ids:
                scoped = set(self.icfg_nodes([fn]))
                entries = [key for key in scoped if key[0] == "FunEntryICFGNode"]
                if not entries:
                    self.note("No mapped function entry is available; unreachability cannot be decided.")
                    continue
                reachable, _ = self.g.collect("ICFG", entries, ["IntraCFGEdge"], function_id=fn)
                classification = "not_observed_from_entry_in_exported_intra_cfg" if self.g.complete else "not_observed_within_bounds"
                self.node_rows(sorted(scoped - reachable), reachability=classification)
            self.note("This is an approximation over IntraCFGEdge reachability. Call/return boundaries, missing or optimized statements and incomplete scans do not prove dead code; inspect INTERPROC_PATH for interprocedural evidence.")
            return
        if cap == "ICFG.LOOP_APPROX":
            for fn in function_ids:
                self.cycle_witnesses("ICFG", roots, ["IntraCFGEdge"], fn)
            return
        targets = {key for key in self.icfg_seeds(target=True)} if p["target_node_id"] >= 0 or p["target_function"] else None
        self.g.walk("ICFG", roots, ["IntraCFGEdge"], lambda key: key in targets if targets is not None else bool(self.g.data[key].get("is_return")))
        self.note("Entry-to-return paths are static control-flow paths; branch satisfiability is not evaluated.")
        self.note("Local traversal follows IntraCFGEdge only. It can stop at a call boundary; use INTERPROC_PATH for paths that enter a callee and return.")

    def object_witnesses(self, root):
        found, visited = {}, {root}
        queue = deque([(root, [root], [])])
        while queue:
            self.g.check()
            key, keys, edges = queue.popleft()
            if "ObjVar" in key[0] and key[0] != "FunObjVar":
                found.setdefault(key, (keys, edges))
            if self.p["depth"] is not None and len(edges) >= self.p["depth"]:
                continue
            for target, edge in self.g.neighbors("PAG", key, ALIAS_EDGES, "in"):
                if target not in visited:
                    visited.add(target)
                    if len(visited) >= 3000:
                        self.g.partial("scan_limit")
                        return found
                    queue.append((target, keys + [target], edges + [edge]))
        return found

    def pag(self):
        cap, p = self.p["capability"], self.p
        if cap in {"PAG.POINTS_TO", "PAG.PARAMETER_ALIAS"}:
            from codegraphhunter.web.pag_pointer_analysis import run_pointer_query
            return run_pointer_query(self)
        if cap == "PAG.FUNOBJ_RESOLVE":
            roots = self.g.node("PAG", p["node_id"], "FunObjVar") if p["node_id"] >= 0 else self.funobjs()
            return self.node_rows(roots)
        if cap == "PAG.ARGUMENT_RESOLVE":
            roots = self.g.node("PAG", p["node_id"], "ArgValVar") if p["node_id"] >= 0 else self.arguments()
            return self.node_rows(roots)
        if cap == "PAG.RETURN_FLOW":
            if p["node_id"] >= 0:
                roots = self.g.node("PAG", p["node_id"], "RetValPN")
            else:
                roots = []
                for function in self.funobjs():
                    # GraphDBClient exports RetValPN.call_graph_node_id from
                    # FunObjVar::getId(), rather than CallGraphNode::getId().
                    roots.extend(self.g.select("PAG", "RetValPN", [("call_graph_node_id", "=", function[1])]))
            return self.g.walk("PAG", roots, p["edge_labels"] or FLOW_EDGES)
        if cap == "PAG.CALL_PARAM_FLOW":
            roots = self.arguments(p["target_function"] or p["function_name"], -1 if p["target_function"] else None)
            caller_ids = None
            if p["target_function"] and (p["function_name"] or p["function_id"] >= 0):
                caller_ids = {key[1] for key in self.funobjs()}
            for root in roots:
                for target, edge in self.g.neighbors("PAG", root, ["CallPE"], "in"):
                    if p["callsite_id"] >= 0 and edge["properties"].get("call_icfg_node_id") != p["callsite_id"]:
                        continue
                    if caller_ids is not None:
                        site = edge["properties"].get("call_icfg_node_id")
                        sites = self.g.node("ICFG", site, "CallICFGNode") if isinstance(site, int) and site >= 0 else []
                        if not any(self.g.data[node].get("fun_obj_var_id") in caller_ids for node in sites):
                            continue
                    if not self.g.add_path("PAG", [root, target], [edge], incoming=True,
                                          extra={"formal_arg_no": self.g.data[root].get("arg_no"), "callsite_id": edge["properties"].get("call_icfg_node_id")}):
                        return
            return
        roots = self.pag_seeds()
        if cap == "PAG.ALIAS_APPROX":
            if p["target_node_id"] < 0:
                raise ValueError("Specify both PAG node IDs for shared-object alias evidence.")
            targets = self.g.node("PAG", p["target_node_id"], p["target_node_label"])
            for root in roots:
                left = self.object_witnesses(root)
                for target in targets:
                    right = self.object_witnesses(target)
                    for obj in left.keys() & right.keys():
                        for origin, witnesses in ((root, left), (target, right)):
                            keys, edges = witnesses[obj]
                            if not self.g.add_path("PAG", keys, edges, incoming=True,
                                                  extra={"alias_evidence": "shared_object", "shared_object_id": obj[1], "queried_value_id": origin[1]}):
                                return
            self.conclusion = "shared_object_evidence" if self.g.records else "unknown"
            self.note("Shared Addr/Copy/Gep/Phi/Select connectivity is may-alias evidence, not a points-to solution. No shared witness does not prove no-alias.")
            return
        if cap == "PAG.LOAD_STORE_USES":
            for root in roots:
                for direction in ("out", "in"):
                    for target, edge in self.g.neighbors("PAG", root, ["LoadStmt", "StoreStmt"], direction):
                        if not self.g.add_path("PAG", [root, target], [edge], incoming=direction == "in",
                                              extra={"use_icfg_node_id": edge["properties"].get("icfg_node_id")}):
                            return
            return
        matches = lambda key: True
        if cap == "PAG.SOURCE_SINK_FLOW":
            if p["target_node_id"] >= 0:
                targets = set(self.g.node("PAG", p["target_node_id"], p["target_node_label"]))
            elif p["target_function"]:
                targets = set(self.arguments(p["target_function"], -1, -1))
            else:
                raise ValueError("Specify a target PAG node or sink function.")
            matches = lambda key: key in targets
            self.note("Source-to-sink paths are exported PAG connectivity, not a complete taint or path-feasibility analysis.")
        edges = p["edge_labels"] or (("GepStmt", "LoadStmt", "StoreStmt", "CopyStmt") if cap == "PAG.FIELD_ACCESS_FLOW" else FLOW_EDGES)
        for direction in (("out", "in") if p["direction"] == "both" else (p["direction"],)):
            self.g.walk("PAG", roots, edges, matches, incoming=direction == "in")
        if cap == "PAG.FIELD_ACCESS_FLOW":
            self.note("Field evidence uses exported GEP offsets and type IDs. Field names are available only if the export contains them; field sensitivity is approximate.")
        if cap in {"PAG.DEF_USE_FLOW", "PAG.ARGUMENT_FLOW"}:
            self.note("Mixed PAG statement edges provide value-flow evidence; load/store memory effects and context sensitivity are approximate.")
        if cap == "PAG.SOURCE_SINK_FLOW":
            self.conclusion = "exported_flow_path_found" if self.g.records else "unknown" if not self.g.complete else "no_exported_path_within_selected_scope"

    @staticmethod
    def vectors(text):
        if not text:
            return []
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data if data and isinstance(data[0], list) else [data]
        except ValueError:
            pass
        # GraphDBClient::extractFuncVectors2String emits {id,id},{id,id}.
        return [ids(group) for group in re.findall(r"\{([^{}]*)\}", text)]

    def classes(self):
        p = self.p
        if p["node_id"] >= 0 and p["capability"] != "CHG.VIRTUAL_DISPATCH":
            return self.g.node("CHG", p["node_id"], "CHNode")
        name = p["class_name"] or (p["function_name"] if not p["method_name"] else "")
        return self.g.select("CHG", "CHNode", [("class_name", "CONTAINS", name)] if name else [])

    def chg(self):
        cap, p = self.p["capability"], self.p
        if cap == "CHG.VIRTUAL_DISPATCH":
            if p["callsite_id"] >= 0 or p["node_id"] >= 0:
                calls = self.g.node("ICFG", p["callsite_id"] if p["callsite_id"] >= 0 else p["node_id"], "CallICFGNode")
            else:
                conditions = [("is_vir_call_inst", "=", True)]
                if p["method_name"]:
                    conditions.append(("fun_name_of_v_call", "CONTAINS", p["method_name"]))
                calls = self.g.select("ICFG", "CallICFGNode", conditions)
            class_scope = set(self.classes()) if p["class_name"] else None
            for call in calls:
                data = self.g.data[call]
                if not data.get("is_vir_call_inst"):
                    self.note("The selected callsite is not exported as a virtual call.")
                    continue
                for target, edge in self.g.neighbors("ICFG", call, ["CallCFGEdge"]):
                    if not self.g.add_path("ICFG", [call, target], [edge], extra={"candidate_basis": "exported_callcfg_edge"}):
                        return
                slot = data.get("virtual_fun_idx", -1)
                for class_id in ids(data.get("chnodes_ids")):
                    for cls in self.g.node("CHG", class_id, "CHNode"):
                        if class_scope is not None and cls not in class_scope:
                            continue
                        for vector in self.vectors(self.g.data[cls].get("virtual_function_vectors")):
                            if not isinstance(slot, int) or not 0 <= slot < len(vector):
                                continue
                            for function in self.g.node("PAG", vector[slot], "FunObjVar"):
                                if not self.g.add_record(dict(callsite_id=call[1], class_name=self.g.data[cls]["class_name"],
                                                              virtual_slot=slot, candidate_fun_obj_var_id=function[1],
                                                              candidate_name=self.g.data[function].get("val_name"), candidate_basis="exported_vtable_slot"), [call, cls, function]):
                                    return
            self.note("Virtual dispatch returns exported CallCFG/CHA candidates. Dynamic receiver types and call-context feasibility are not proven.")
            return
        roots = self.classes()
        if cap == "CHG.CLASS_HIERARCHY":
            for root in roots:
                related = False
                for direction in ("out", "in"):
                    for target, edge in self.g.neighbors("CHG", root, ["CHEdge"], direction):
                        # CHEdge::INHERITANCE=1, INSTANCE=2 in this exporter.
                        if edge["properties"].get("edge_type") != 1:
                            continue
                        related = True
                        if not self.g.add_path("CHG", [root, target], [edge], incoming=direction == "in", extra={"relation": "exported_inheritance"}):
                            return
                if not related:
                    self.node_rows([root], relation="no_exported_inheritance_edge")
            return
        for root in roots:
            for target, edge in self.g.neighbors("CHG", root, ["CHEdge"], "both"):
                if edge["properties"].get("edge_type") != 1:
                    continue
                left = self.vectors(self.g.data[root].get("virtual_function_vectors"))
                right = self.vectors(self.g.data[target].get("virtual_function_vectors"))
                for vector_index, (a, b) in enumerate(zip(left, right)):
                    for slot, (first, second) in enumerate(zip(a, b)):
                        if first == second:
                            continue
                        names = []
                        functions = []
                        for ident in (first, second):
                            matches = self.g.node("PAG", ident, "FunObjVar")
                            functions.extend(matches)
                            names.append(self.g.data[matches[0]].get("val_name", "") if matches else "")
                        if p["method_name"] and not any(p["method_name"] in name for name in names):
                            continue
                        if not self.g.add_record(dict(class_a=self.g.data[root]["class_name"], class_b=self.g.data[target]["class_name"],
                                               vector_index=vector_index, slot=slot, function_a=first, function_b=second,
                                               names=names, relation="possible_override"), [root, target] + functions):
                            self.note("Override candidates compare exported vtable slots; multiple inheritance and ABI layout still require verification.")
                            return
        self.note("Override candidates share a vtable slot across an exported inheritance edge. Multiple inheritance and ABI slot layout require verification; this is not a C++ override proof.")

    def types(self):
        p, cap = self.p, self.p["capability"]
        nodes = self.pag_seeds() if p["node_id"] >= 0 or p["function_name"] or p["file"] else []
        type_ids = {p["type_id"]} if p["type_id"] >= 0 else set()
        for node in nodes:
            for prop in ("svf_type_id", "obj_type_info_type_id", "gep_val_svf_type_id", "ap_gep_pointee_type_id"):
                ident = self.g.data[node].get(prop)
                if isinstance(ident, int) and ident >= 0:
                    type_ids.add(ident)
        if not type_ids:
            raise ValueError("Specify a PAG node or an exported type ID.")
        for ident in sorted(type_ids):
            for typ in self.g.node("TypeGraph", ident):
                if typ[0] == "StInfo":
                    continue
                self.node_rows([typ], queried_type_id=ident)
                if cap == "TYPE.FIELD_INFO":
                    data = self.g.data[typ]
                    stinfo = data.get("stinfo_node_id", -1)
                    if isinstance(stinfo, int) and stinfo >= 0:
                        self.node_rows(self.g.node("TypeGraph", stinfo, "StInfo"), parent_type_id=ident)
                    field_types = ids(data.get("fields_id_vec"))
                    if p["field_index"] >= 0:
                        index = p["field_index"]
                        if index < len(field_types):
                            self.g.add_record(dict(struct_type_id=ident, field_index=index, field_type_id=field_types[index],
                                                   field_name=None, exported_type_vector=data.get("fields_id_vec")))
        if cap == "TYPE.FIELD_INFO":
            self.note("Struct/StInfo metadata reports exported indices, offsets and type IDs. This export does not contain source-level field names.")

    def mappings(self, name=None):
        rows = []
        functions = self.cg_functions(name=name)
        for cg in functions:
            data = self.g.data[cg]
            function_id = data.get("fun_obj_var_id", -1)
            funs = self.g.node("PAG", function_id, "FunObjVar") if isinstance(function_id, int) and function_id >= 0 else []
            entries = self.entries("", function_id) if funs else []
            args = self.arguments("", function_id, -1) if funs else []
            source = self.s.search(short_name(data.get("fun_name", "")), 10)
            rows.append(dict(function_name=data.get("fun_name"), callgraph_node_id=cg[1], fun_obj_var_id=function_id if funs else None,
                             argument_node_ids=[key[1] for key in args], icfg_entry_ids=[key[1] for key in entries],
                             source_candidates=source, graph_source_loc=data.get("source_loc"), _keys=[cg] + funs + entries + args))
        return rows

    def function_bundle(self):
        from codegraphhunter.web.query_limits import QueryLimits
        mappings = self.mappings()
        original = self.g.limits
        try:
            for mapping in mappings:
                cg = ("CallGraphNode", mapping["callgraph_node_id"])
                stages = [("CallGraph", [cg], ["CallGraphEdge"]),
                          ("PAG", [("ArgValVar", ident) for ident in mapping["argument_node_ids"]], FLOW_EDGES),
                          ("ICFG", [("FunEntryICFGNode", ident) for ident in mapping["icfg_entry_ids"]], ["IntraCFGEdge"])]
                for graph, roots, edges in stages:
                    budget = min(original.entity_limit, len(self.g.records) + max(1, original.entity_limit // 3))
                    self.g.limits = QueryLimits(original.max_depth, budget)
                    self.g.walk(graph, roots, edges)
        finally:
            self.g.limits = original
        self.note("The bundle combines three graph scopes; its result budget is shared across them. Cross-graph numeric IDs are joined only through exported mapping properties.")

    def check_context(self):
        for root in self.pag_seeds():
            for direction in ("out", "in"):
                for target, edge in self.g.neighbors("PAG", root, ["LoadStmt", "StoreStmt"], direction):
                    if not self.g.add_path("PAG", [root, target], [edge], incoming=direction == "in"):
                        return
                    ident = edge["properties"].get("icfg_node_id", -1)
                    if isinstance(ident, int) and ident >= 0:
                        nodes = self.g.node("ICFG", ident)
                        self.g.walk("ICFG", nodes, ["IntraCFGEdge"], self.branch, incoming=True,
                                    extra=lambda keys, edges: {"relation": "branch_can_reach_use", "value_node_id": root[1],
                                                              "check_dominates_use": "unknown"})
        self.note("A branch before a use is candidate check evidence. Dominance, checked-variable identity and successful validation are not established by reachability alone.")

    def cross_graph(self):
        cap, p = self.p["capability"], self.p
        if cap == "XG.SOURCE_TO_GRAPH_IDS":
            for row in self.mappings():
                keys = row.pop("_keys")
                if not self.g.add_record(row, keys):
                    break
            return
        if cap == "XG.FUNCTION_EVIDENCE_BUNDLE":
            return self.function_bundle()
        if cap in {"XG.BUG_LOCALIZATION", "XG.REPAIR_LOCALIZATION_CONTEXT"}:
            text = " ".join(value for value in (p["problem_statement"], p["keyword"]) if value)
            if not text:
                raise ValueError("Provide the bug description for source/graph localization.")
            candidates = self.s.search(text, min(p["limit"], 10), p["file"])
            ranked = []
            for candidate in candidates:
                mappings = self.mappings(candidate["function"])
                evidence = [{key: value for key, value in row.items() if key != "_keys"} for row in mappings]
                score = candidate["source_score"] + sum(bool(row.get("fun_obj_var_id")) + bool(row["icfg_entry_ids"]) + bool(row["argument_node_ids"]) for row in mappings)
                row = dict(candidate, graph_mappings=evidence, combined_score=score, classification="localization_candidate")
                if cap == "XG.REPAIR_LOCALIZATION_CONTEXT":
                    row.update(self.s.window(candidate["file"], candidate["start_line"]))
                ranked.append(row)
            for row in sorted(ranked, key=lambda item: -item["combined_score"]):
                self.g.add_record(row)
            self.note("Candidate ranking combines lexical source scores and exported mapping evidence. It does not establish a root cause or a correct repair.")
            return
        if cap == "XG.CHECK_BEFORE_USE":
            return self.check_context()
        if cap == "XG.SANITIZE_TO_SINK":
            if not p["target_function"] and p["target_node_id"] < 0:
                raise ValueError("Specify the sink function or sink PAG node.")
            previous = p["capability"]
            p["capability"] = "PAG.SOURCE_SINK_FLOW"
            try:
                with self.graph_budget(3):
                    self.pag()
            finally:
                p["capability"] = previous
            sanitizer_ids = {key[1] for key in self.funobjs(p["sanitizer"], -1)} if p["sanitizer"] else set()
            control_seeds = set()
            for path in list(self.g.evidence.paths):
                row = self.g.records[path["record_index"]]
                witnesses = []
                for node in path["nodes"]:
                    data = self.g.data[(node["label"], node["id"])]
                    ident = data.get("icfg_node_id", -1)
                    if isinstance(ident, int) and ident >= 0:
                        for icfg in self.g.node("ICFG", ident):
                            control_seeds.add(icfg)
                            if self.g.data[icfg].get("called_fun_obj_var_id") in sanitizer_ids:
                                witnesses.append(icfg[1])
                row.update(sanitizer_calls_on_exported_flow=witnesses, sanitized="unknown")
            if p["target_function"]:
                for sink in self.funobjs(p["target_function"], -1):
                    control_seeds.update(self.g.select("ICFG", "CallICFGNode", [("called_fun_obj_var_id", "=", sink[1])]))
            with self.graph_budget(3):
                for seed in sorted(control_seeds):
                    self.g.walk("ICFG", [seed], ["IntraCFGEdge"], self.branch, incoming=True,
                                extra=lambda keys, edges: {"sanitization_proven": False, "relation": "preceding_branch_context"})
            if p["function_name"]:
                self.g.walk("CallGraph", self.cg_functions(), ["CallGraphEdge"])
            self.conclusion = "sanitization_unknown"
            self.note("Sanitizer call presence or absence is evidence only. Whether its result constrains the sink, dominates the use, or sanitizes this value remains unknown.")
            return
        api = p["target_function"] or p["function_name"] or p["keyword"]
        if not api:
            raise ValueError("Specify the API whose caller and check context should be collected.")
        for function in self.funobjs(api, -1):
            calls = self.g.select("ICFG", "CallICFGNode", [("called_fun_obj_var_id", "=", function[1])])
            for call in calls:
                self.node_rows([call], api_fun_obj_var_id=function[1], missing_check="unknown")
                with self.graph_budget(3):
                    self.g.walk("ICFG", [call], ["IntraCFGEdge"], self.branch, incoming=True)
                with self.graph_budget(3):
                    for formal in self.arguments("", function[1], -1):
                        for actual, edge in self.g.neighbors("PAG", formal, ["CallPE"], "in"):
                            if edge["properties"].get("call_icfg_node_id") == call[1]:
                                if not self.g.add_path("PAG", [formal, actual], [edge], incoming=True,
                                                      extra={"api_callsite_id": call[1], "relation": "actual_to_formal_argument"}):
                                    break
                for caller in self.g.select("CallGraph", "CallGraphNode", [("fun_obj_var_id", "=", self.g.data[call].get("fun_obj_var_id", -1))]):
                    for target, edge in self.g.neighbors("CallGraph", caller, ["CallGraphEdge"]):
                        self.g.add_path("CallGraph", [caller, target], [edge])
        self.note("API misuse context exposes actual calls and preceding branches. Missing-check and API-contract violations are not inferred solely from absent evidence.")
