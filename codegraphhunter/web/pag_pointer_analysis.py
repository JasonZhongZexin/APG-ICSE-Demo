"""Read-only PAG scans and an inclusion-based, field-insensitive solver.

The solver's objects are exported allocation/base-object nodes. GEP fields
share a base object; unknown inputs/effects propagate an explicit top element.
This is a bounded analysis of the exported model, not an SVF result importer.
"""

from collections import defaultdict, deque
import re
import subprocess
import time

from codegraphhunter.web.database_client import DatabaseQueryTimeout

UNKNOWN = ("UnknownObject", -1)  # Internal lattice element, never a graph node.
COPY_EDGES = {"CopyStmt", "GepStmt", "PhiStmt", "SelectStmt", "CallPE", "RetPE", "RetPETDJoinPE", "TDForkPE"}
UNCERTAIN_EDGES = {"AssignStmt", "MultiOpndStmt", "UnaryOPStmt", "BinaryOPStmt", "CmpStmt"}
NODE_FIELDS = {"id", "svf_type_id", "arg_no", "cg_node_id", "call_graph_node_id",
               "base_obj_var_node_id", "base_val_id", "fun_obj_var_node_id", "is_decl", "intrinsic", "is_uncalled", "val_name"}
MODEL = "andersen_style_field_insensitive"
SCAN_PAGE_SIZE = 50000
NODE_PAGE_SIZE = 1000


class AnalysisLimit(RuntimeError):
    pass


def setting(environ, name, default, ceiling=2000000):
    try:
        return max(1, min(ceiling, int(environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def is_object(key):
    return "ObjVar" in key[0]


class PAGSnapshot:
    def __init__(self, graph):
        self.g = graph
        self.nodes, self.edges = {}, []
        self.complete = False

    def load(self):
        maximum_nodes = setting(self.g.environ, "CODEGRAPHHUNTER_PTA_MAX_NODES", 250000)
        maximum_edges = setting(self.g.environ, "CODEGRAPHHUNTER_PTA_MAX_EDGES", 1000000)
        try:
            # Keyset pages do not depend on the form's result limit or depth.
            for label in self.g.labels("PAG"):
                # Plain value identities are obtained from constraint endpoints.
                # Only function/return and field-object mappings require a
                # property scan; unused isolated scalar values are inert.
                if label not in {"FunObjVar", "GepObjVar", "RetValPN"}:
                    continue
                fields = sorted(NODE_FIELDS & self.g.properties(label).keys())
                selected = ", ".join(f"node.{prop} AS node_{prop}" for prop in fields)
                after = -1
                while True:
                    rows = self.g.read("PAG", f"MATCH (node:{label})\nWHERE node.id > $after_id\nRETURN {selected}\nORDER BY node_id\nLIMIT {NODE_PAGE_SIZE}", {"after_id": after}, bulk=True)
                    for row in rows:
                        values = {prop: row.get("node_" + prop) for prop in fields}
                        key = (label, values["id"])
                        if key not in self.nodes and len(self.nodes) >= maximum_nodes:
                            raise AnalysisLimit("node_budget")
                        key = self.g.remember("PAG", label, dict(self.g.data.get(key, {}), **values))
                        self.nodes[key] = self.g.data[key]
                    if len(rows) < NODE_PAGE_SIZE:
                        break
                    following = rows[-1]["node_id"]
                    if following <= after:
                        raise ValueError("The PAG node scan did not advance.")
                    after = following
            for label in self.g.edges("PAG"):
                owned = self.g.schema["edge_types"][label]
                fields = owned.get("expanded_properties") or owned.get("properties") or {}
                # Loading unrelated statement metadata multiplies transfer and
                # sort costs on large PAGs. These are the solver's only inputs.
                fields = [prop for prop in ("edge_id", "copy_kind", "op_var_node_ids") if prop in fields]
                selected = ", ".join(f"flow.{prop} AS edge_{prop}" for prop in fields)
                offset = 0
                while True:
                    query = (f"MATCH (source)-[flow:{label}]->(target)\n"
                             f"RETURN source.id AS source_id, label(source) AS source_label, target.id AS target_id, label(target) AS target_label, "
                             f"{selected}\nORDER BY edge_edge_id, source_id, target_id, source_label, target_label\nSKIP {offset}\nLIMIT {SCAN_PAGE_SIZE}")
                    rows = self.g.read("PAG", query, edge_scan=True, bulk=True)
                    for row in rows:
                        if len(self.edges) >= maximum_edges:
                            raise AnalysisLimit("edge_budget")
                        if not isinstance(row.get("edge_edge_id"), int):
                            self.g.partial("missing_edge_ids", "Some exported constraint IDs are missing; complete scan ordering cannot be certified and negative alias conclusions are unavailable.")
                        source, target = (row["source_label"], row["source_id"]), (row["target_label"], row["target_id"])
                        for key in (source, target):
                            if key[0] not in self.g.labels("PAG"):
                                raise ValueError("A PAG constraint has an endpoint outside its graph scope.")
                            if key not in self.nodes:
                                if len(self.nodes) >= maximum_nodes:
                                    raise AnalysisLimit("node_budget")
                                self.g.remember("PAG", key[0], dict(self.g.data.get(key, {}), id=key[1]))
                                self.nodes[key] = self.g.data[key]
                        self.edges.append(dict(source=source, target=target, label=label,
                                               properties={prop: row.get("edge_" + prop) for prop in fields}))
                    if len(rows) < SCAN_PAGE_SIZE:
                        break
                    offset += len(rows)
            # A base object without its own statement can still be required by
            # a GEP address. Resolve those exported mappings by ID, in batches.
            present = {key[1] for key in self.nodes}
            missing_bases = {data.get("base_obj_var_node_id") for key, data in self.nodes.items() if key[0] == "GepObjVar"} - present - {None, -1}
            for label in self.g.labels("PAG"):
                if not is_object((label, 0)) or not missing_bases:
                    continue
                identifiers = sorted(missing_bases)
                fields = sorted(NODE_FIELDS & self.g.properties(label).keys())
                selected = ", ".join(f"node.{prop} AS node_{prop}" for prop in fields)
                for offset in range(0, len(identifiers), NODE_PAGE_SIZE):
                    rows = self.g.read("PAG", f"MATCH (node:{label})\nWHERE node.id IN $filter_0\nRETURN {selected}\nLIMIT {NODE_PAGE_SIZE}", {"filter_0": identifiers[offset:offset + NODE_PAGE_SIZE]})
                    for row in rows:
                        if len(self.nodes) >= maximum_nodes:
                            raise AnalysisLimit("node_budget")
                        key = self.g.remember("PAG", label, {prop: row.get("node_" + prop) for prop in fields})
                        self.nodes[key] = self.g.data[key]
                        missing_bases.discard(key[1])
            self.complete = self.g.complete
        except AnalysisLimit as error:
            self.g.partial(str(error), "Pointer analysis could not load every PAG constraint within its resource budget; negative alias conclusions are unavailable.")
        except DatabaseQueryTimeout:
            self.g.partial("timeout", "Pointer analysis stopped before every PAG constraint was loaded; negative alias conclusions are unavailable.")
        # Already resolved operands remain inspectable after an interrupted scan.
        self.nodes.update({key: value for key, value in self.g.data.items() if value.get("graph") == "PAG"})
        return self


class PointsToView:
    """Expose readable sets without expanding every solver bitset at once."""
    def __init__(self, solver):
        self.solver = solver

    def __getitem__(self, node):
        return self.solver.decode(self.solver.bits.get(node, 0))


class AndersenSolver:
    def __init__(self, nodes, edges, *, check=lambda: None, max_facts=10000000, max_copies=1000000,
                 max_bitset_bytes=256 * 1024 * 1024, progress=None):
        self.nodes, self.edges = nodes, edges
        self.check, self.max_facts, self.max_copies, self.progress = check, max_facts, max_copies, progress
        self.bits, self.copies = defaultdict(int), defaultdict(set)
        self.object_indices, self.object_keys = {UNKNOWN: 0}, [UNKNOWN]
        self.pts = PointsToView(self)
        self.bitset_bytes, self.max_bitset_bytes = 0, max_bitset_bytes
        self.loads, self.stores = defaultdict(set), defaultdict(set)
        self.queue, self.queued = deque(), set()
        self.fact_count, self.copy_count, self.steps = 0, 0, 0
        self.converged, self.unknown_memory = False, False
        self.reasons, self.stop_reason = set(), None
        self.by_id = defaultdict(list)
        for key in nodes:
            self.by_id[key[1]].append(key)

    def object(self, key):
        visited = set()
        while key[0] == "GepObjVar":
            if key in visited:
                self.reasons.add("A field object has a cyclic or missing base-object mapping.")
                return UNKNOWN
            visited.add(key)
            candidates = self.by_id.get(self.nodes[key].get("base_obj_var_node_id"), [])
            if len(candidates) != 1 or not is_object(candidates[0]):
                self.reasons.add("A field object has a cyclic or missing base-object mapping.")
                return UNKNOWN
            key = candidates[0]
        if key[0] == "ConstNullPtrObjVar":
            return None
        if key[0] == "DummyObjVar" or not is_object(key):
            self.reasons.add("Unknown or unmodeled object identities are present.")
            return UNKNOWN
        return key

    def decode(self, bits):
        objects = set()
        while bits:
            lowest = bits & -bits
            objects.add(self.object_keys[lowest.bit_length() - 1])
            bits ^= lowest
        return objects

    def add(self, node, objects):
        bits = 0
        for obj in objects:
            if obj not in self.object_indices:
                self.object_indices[obj] = len(self.object_keys)
                self.object_keys.append(obj)
            bits |= 1 << self.object_indices[obj]
        self.add_bits(node, bits)

    def add_bits(self, node, bits):
        old = self.bits[node]
        difference = bits & ~old
        if not difference:
            return
        count = difference.bit_count()
        if self.fact_count + count > self.max_facts:
            raise AnalysisLimit("fact_budget")
        merged = old | difference
        growth = (merged.bit_length() + 7) // 8 - (old.bit_length() + 7) // 8
        if self.bitset_bytes + growth > self.max_bitset_bytes:
            raise AnalysisLimit("bitset_budget")
        self.bits[node] = merged
        self.bitset_bytes += growth
        self.fact_count += count
        if node not in self.queued:
            self.queued.add(node)
            self.queue.append(node)

    def copy(self, source, target):
        if target in self.copies[source]:
            return
        if self.copy_count >= self.max_copies:
            raise AnalysisLimit("derived_edge_budget")
        self.copies[source].add(target)
        self.copy_count += 1
        self.add_bits(target, self.bits[source])

    def unknown_writes(self, reason):
        self.reasons.add(reason)
        if not self.unknown_memory:
            self.unknown_memory = True
            for destinations in self.loads.values():
                for target in destinations:
                    self.add(target, {UNKNOWN})

    def initialize(self):
        incoming, bound_arguments = set(), set()
        declarations = {key[1] for key, data in self.nodes.items() if key[0] == "FunObjVar" and data.get("is_decl") in (True, 1)}
        called_declaration = False
        addresses = []
        for edge in self.edges:
            self.check()
            source, target, label, properties = edge["source"], edge["target"], edge["label"], edge.get("properties") or {}
            if label != "StoreStmt":
                incoming.add(target)
            if label == "AddrStmt":
                addresses.append((source, target))
            elif label == "LoadStmt":
                self.loads[source].add(target)
            elif label == "StoreStmt":
                self.stores[target].add(source)
            elif label in COPY_EDGES:
                self.copy(source, target)
                if label in {"CallPE", "TDForkPE"}:
                    bound_arguments.add(target)
                    called_declaration |= self.nodes[target].get("cg_node_id") in declarations
                if label in {"PhiStmt", "SelectStmt"}:
                    operands = properties.get("op_var_node_ids")
                    operand_ids = [int(value) for value in re.findall(r"(?<![\w-])\d+", str(operands or ""))]
                    if not operand_ids:
                        self.add(target, {UNKNOWN})
                        self.reasons.add("Some Phi/Select operand lists are missing from the export.")
                    for ident in operand_ids:
                        candidates = self.by_id.get(ident, [])
                        if len(candidates) == 1:
                            self.copy(candidates[0], target)
                        else:
                            self.add(target, {UNKNOWN})
                            self.reasons.add("Some Phi/Select operands could not be resolved.")
                if label == "CopyStmt" and properties.get("copy_kind") not in (None, 0):
                    self.add(target, {UNKNOWN})
                    self.reasons.add("Pointer conversions without a decoded copy subtype are modeled as unknown.")
            elif label in UNCERTAIN_EDGES:
                self.add(target, {UNKNOWN})
                self.reasons.add("Unmodeled operation results are conservatively treated as unknown pointers.")
            elif label != "BranchStmt":
                self.add(target, {UNKNOWN})
                self.reasons.add("An unsupported statement kind was encountered.")
        for key, data in self.nodes.items():
            self.check()
            if key[0] == "FunValVar":
                candidates = self.by_id.get(data.get("fun_obj_var_node_id"), [])
                if len(candidates) == 1 and candidates[0][0] == "FunObjVar":
                    self.add(key, {candidates[0]})
                elif key not in incoming:
                    self.add(key, {UNKNOWN})
                    self.reasons.add("A function value has no unambiguous exported function-object mapping.")
            if key[0] in {"BlackHoleValVar", "DummyValVar", "VarArgValPN"}:
                self.add(key, {UNKNOWN})
                self.reasons.add("Unknown, dummy or variadic input values are present.")
            if key[0] == "ArgValVar" and key not in bound_arguments and key not in incoming:
                self.add(key, {UNKNOWN})
                self.reasons.add("Formal arguments without exported actual-to-formal bindings are unknown inputs.")
            if key[0] == "RetValPN" and (data.get("call_graph_node_id") in declarations or key not in incoming):
                self.add(key, {UNKNOWN})
                called_declaration |= data.get("call_graph_node_id") in declarations
            if not is_object(key) and key[0] not in {"ConstNullPtrValVar", "FunValVar"} and key not in incoming:
                self.add(key, {UNKNOWN})
        for source, target in addresses:
            obj = self.object(source)
            if obj is not None:
                self.add(target, {obj})
        called_declaration |= any(key[0] == "FunObjVar" and key[1] in declarations and data.get("is_uncalled") in (False, 0) for key, data in self.nodes.items())
        if called_declaration:
            self.unknown_writes("External function bodies/effects are not available; loaded values may include unknown objects.")

    def solve(self):
        propagated = defaultdict(int)
        try:
            self.initialize()
            while self.queue:
                self.check()
                node = self.queue.popleft()
                self.queued.remove(node)
                difference = self.bits[node] & ~propagated[node]
                propagated[node] |= difference
                self.steps += 1
                for target in tuple(self.copies[node]):
                    self.add_bits(target, difference)
                # Most variables have only copies. They propagate whole
                # bitsets, without visiting each individual abstract object.
                for obj in self.decode(difference) if self.loads[node] or self.stores[node] else ():
                    self.check()
                    for target in self.loads[node]:
                        if obj == UNKNOWN:
                            self.add(target, {UNKNOWN})
                        else:
                            self.copy(obj, target)
                    for source in self.stores[node]:
                        if obj == UNKNOWN:
                            self.unknown_writes("A store through an unknown pointer may affect loaded memory values.")
                        else:
                            self.copy(source, obj)
                if self.progress and self.steps % 1000 == 0:
                    self.progress(self.steps, self.fact_count)
            self.converged = True
        except AnalysisLimit as error:
            self.stop_reason = str(error)
        except DatabaseQueryTimeout:
            self.stop_reason = "timeout"
        return self

    def objects(self, node):
        return self.decode(self.bits[node] & ~1)

    def alias(self, left, right, *, constraints_complete):
        first, second = self.bits[left], self.bits[right]
        common = first & second & ~1
        if common:
            return "MayAlias", self.decode(common)
        if constraints_complete and self.converged and first & ~1 and second & ~1 and not (first | second) & 1:
            return "NoAlias", set()
        return "Unknown", set()


def resolve_operand(handlers, *, target=False):
    p, g = handlers.p, handlers.g
    node_id = p["target_node_id"] if target else p["node_id"]
    label = p["target_node_label"] if target else p["node_label"]
    if node_id >= 0:
        roots = g.node("PAG", node_id, label or "ArgValVar")
        if not roots and not label:
            roots = g.node("PAG", node_id)
    else:
        name = (p["target_function"] or p["function_name"]) if target else p["function_name"]
        function_id = p["target_function_id"] if target else p["function_id"]
        if target and not p["target_function"] and function_id < 0:
            function_id = p["function_id"]
        number = p["target_arg_no"] if target else p["arg_no"]
        if number < 0:
            raise ValueError("Specify a zero-based argument number for each parameter, or its explicit PAG node ID.")
        if "::" in name and function_id < 0:
            functions = handlers.funobjs(name, function_id)
            names = [g.data[key].get("val_name") or "" for key in functions]
            if any("\n" in stored or "\r" in stored for stored in names):
                raise ValueError("Use an exported FunObjVar ID for this function name.")
            try:
                decoded = subprocess.run(["c++filt"], input="\n".join(names), text=True, capture_output=True, timeout=2, check=True).stdout.splitlines()
            except (OSError, subprocess.SubprocessError):
                decoded = names
            requested = name.split("(", 1)[0].strip()
            functions = [key for key, stored in zip(functions, decoded) if
                         stored.split("(", 1)[0].strip() == requested or
                         stored.split("(", 1)[0].strip().endswith(("::" + requested, " " + requested))]
            roots = []
            for function in functions:
                roots.extend(g.select("PAG", "ArgValVar", [("cg_node_id", "=", function[1]), ("arg_no", "=", number)]))
        else:
            roots = handlers.arguments(name, function_id, number)
    if not roots:
        raise ValueError("No matching " + ("second" if target else "first") + " parameter was found. Check the function name and exported argument index.")
    if any(is_object(key) for key in roots):
        raise ValueError("Select a parameter/value node, rather than a memory-object node.")
    return roots


def hydrate_objects(graph, objects, hydrated):
    """Read source text only for displayed objects, never for the whole PAG."""
    groups = defaultdict(list)
    for key in objects:
        if key not in hydrated:
            groups[key[0]].append(key[1])
    try:
        for label, identifiers in groups.items():
            fields = sorted({"id", "source_loc", "val_name", "svf_type_id"} & graph.properties(label).keys())
            selected = ", ".join(f"node.{prop} AS node_{prop}" for prop in fields)
            for offset in range(0, len(identifiers), 1000):
                rows = graph.read("PAG", f"MATCH (node:{label})\nWHERE node.id IN $filter_0\nRETURN {selected}\nLIMIT 1000", {"filter_0": identifiers[offset:offset + 1000]})
                for row in rows:
                    key = (label, row["node_id"])
                    graph.remember("PAG", label, dict(graph.data[key], **{prop: row.get("node_" + prop) for prop in fields}))
                    hydrated.add(key)
    except DatabaseQueryTimeout:
        graph.partial("metadata_timeout", "Some displayed object metadata could not be loaded before the deadline; inferred object identities and alias decisions remain available.")


def run_pointer_query(handlers):
    g, p = handlers.g, handlers.p
    left = resolve_operand(handlers)
    right = resolve_operand(handlers, target=True) if p["capability"] == "PAG.PARAMETER_ALIAS" else []
    handlers.note("This is a flow-insensitive, context-insensitive, field-insensitive inclusion analysis of the exported PAG. GEP fields share their base object; MayAlias does not imply MustAlias or runtime aliasing.")
    handlers.note("Call and return propagation uses the exported parameter bindings; new indirect-call targets and external function bodies are not reconstructed.")
    handlers.note("NoAlias is scoped to the fully loaded, converged exported model. Missing bindings, unknown objects or incomplete solving yield Unknown when there is no concrete common object.")
    handlers.note("Result limits bound display only. The form's hop limit does not truncate pointer constraint solving. Parameter indices are exported zero-based LLVM indices and may include hidden C++ parameters.")
    if len(left) > 1 or len(right) > 1:
        handlers.note("Multiple function/parameter nodes matched; results are reported separately by their typed PAG IDs.")
    snapshot = PAGSnapshot(g).load()
    def progress(steps, facts):
        if g.progress:
            g.progress(dict(stage="applying", message=f"Solving pointer constraints: {steps} worklist steps, {facts} points-to facts…",
                            timeout_s=max(0.01, g.deadline - time.monotonic())))
    if snapshot.complete:
        progress(0, 0)
    solver = AndersenSolver(snapshot.nodes, snapshot.edges, check=g.check,
        max_facts=setting(g.environ, "CODEGRAPHHUNTER_PTA_MAX_FACTS", 10000000, 50000000),
        max_bitset_bytes=setting(g.environ, "CODEGRAPHHUNTER_PTA_MAX_BITSET_BYTES", 256 * 1024 * 1024, 1024 * 1024 * 1024),
        max_copies=setting(g.environ, "CODEGRAPHHUNTER_PTA_MAX_DERIVED_EDGES", 1000000), progress=progress).solve()
    complete = snapshot.complete and solver.converged
    if not solver.converged:
        g.partial(solver.stop_reason, "Pointer constraint solving stopped before convergence; negative alias conclusions are unavailable.")
    for note in sorted(solver.reasons):
        handlers.note(note)
    operands = list(dict.fromkeys(left + right))
    functions = {key[1]: data.get("val_name") for key, data in snapshot.nodes.items() if key[0] == "FunObjVar"}
    sets = [dict(node_label=key[0], node_id=key[1], arg_no=g.data[key].get("arg_no"), function_id=g.data[key].get("cg_node_id"),
                 function_name=functions.get(g.data[key].get("cg_node_id")),
                 object_count=len(solver.objects(key)), contains_unknown=UNKNOWN in solver.pts[key], complete=complete and UNKNOWN not in solver.pts[key]) for key in operands]
    handlers.details["pointer_analysis"] = dict(model=MODEL, complete=complete, fixed_point_reached=solver.converged,
        constraints_complete=snapshot.complete, field_sensitive=False, context_sensitive=False, flow_sensitive=False,
        call_graph_scope="exported_parameter_bindings",
        node_count=len(snapshot.nodes), constraint_count=len(snapshot.edges), fact_count=solver.fact_count,
        worklist_steps=solver.steps, derived_copy_count=solver.copy_count, bitset_bytes=solver.bitset_bytes,
        stop_reason=solver.stop_reason or (None if snapshot.complete else g.stop_reason),
        unknown_memory_effects=solver.unknown_memory, points_to_sets=sets, result_limit_scope="display_only", hop_limit_applied=False)
    hydrated = set()
    if not right:
        handlers.conclusion = ("points_to_set_with_unknown_objects" if any(solver.bits[key] & 1 for key in left) else "points_to_set_computed") if complete else "partial_points_to_set"
        for key in left:
            objects = sorted(solver.objects(key), key=lambda obj: (obj[0] == "FunObjVar", obj))
            hydrate_objects(g, objects[:max(0, g.limits.entity_limit - len(g.records))], hydrated)
            common = dict(graph="PAG", pointer_label=key[0], pointer_node_id=key[1], arg_no=g.data[key].get("arg_no"),
                          function_id=g.data[key].get("cg_node_id"), points_to_count=len(objects), contains_unknown=UNKNOWN in solver.pts[key],
                          function_name=functions.get(g.data[key].get("cg_node_id")),
                          set_complete=complete and UNKNOWN not in solver.pts[key], analysis_model=MODEL)
            if not objects:
                if not g.add_record(dict(common, relation="points_to_set", result="Unknown", reason="No concrete object was inferred; absence does not prove an empty runtime points-to set."), [key]):
                    return
            for obj in objects:
                if not g.add_record(dict(common, relation="points_to", object_label=obj[0], object_node_id=obj[1],
                        object_source_loc=g.data[obj].get("source_loc"), object_name=g.data[obj].get("val_name")), [key, obj]):
                    return
    else:
        results, object_budget = set(), g.limits.entity_limit
        for first in left:
            for second in right:
                result, common = solver.alias(first, second, constraints_complete=snapshot.complete)
                results.add(result)
                shown = sorted(common)[:object_budget]
                object_budget -= len(shown)
                hydrate_objects(g, shown, hydrated)
                if len(shown) < len(common):
                    g.partial("result_limit", "Some common points-to objects are omitted from display; the alias decision uses the full inferred sets.")
                row = dict(graph="PAG", relation="parameter_alias", left_label=first[0], left_node_id=first[1],
                    left_arg_no=g.data[first].get("arg_no"), left_function_id=g.data[first].get("cg_node_id"),
                    left_function_name=functions.get(g.data[first].get("cg_node_id")),
                    right_label=second[0], right_node_id=second[1], right_arg_no=g.data[second].get("arg_no"), right_function_id=g.data[second].get("cg_node_id"),
                    right_function_name=functions.get(g.data[second].get("cg_node_id")),
                    alias_result=result, left_points_to_count=len(solver.objects(first)), right_points_to_count=len(solver.objects(second)),
                    left_contains_unknown=UNKNOWN in solver.pts[first], right_contains_unknown=UNKNOWN in solver.pts[second],
                    common_object_count=len(common), common_objects=[dict(label=obj[0], id=obj[1], source_loc=g.data[obj].get("source_loc")) for obj in shown],
                    common_objects_truncated=len(shown) < len(common), analysis_complete=complete, analysis_model=MODEL)
                if not g.add_record(row, [first, second] + shown):
                    handlers.conclusion = "mixed_alias_results" if len(results) > 1 else result
                    return
        handlers.conclusion = "mixed_alias_results" if len(results) > 1 else next(iter(results), "Unknown")
