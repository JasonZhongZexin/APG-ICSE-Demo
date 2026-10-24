/* Run with: node tests/test_graph_view.js */
"use strict";
const assert = require("assert");
const { buildEvidenceGraph, evidenceForPath } = require("../codegraphhunter/web/static/graph-view.js");
const checks = [];
function check(name, run) { run(); checks.push(name); }
const icfgPlan = { intent: "find_successors", seed_label: "IntraICFGNode", result_label: "RetICFGNode", edge_label: "IntraCFGEdge", depth: 4 };

check("ICFG predecessor arrows point from result to seed, retaining node properties", () => {
  const data = buildEvidenceGraph([{ seed_id: 0, seed_source_loc: "file.cc:12", result_id: 8, result_kind: 3, flow_kind: 1 }], "icfg", Object.assign({}, icfgPlan, { intent: "find_predecessors" }));
  const byKey = new Map(data.nodes.map((node) => [node.key, node]));
  assert.strictEqual(data.nodes.length, 2);
  assert.strictEqual(byKey.get(data.edges[0].source).id, 8);
  assert.strictEqual(byKey.get(data.edges[0].target).id, 0);
  assert.strictEqual(byKey.get(data.edges[0].target).properties.source_loc, "file.cc:12");
  assert.strictEqual(data.edges[0].label, "IntraCFGEdge");
  assert.strictEqual(data.edges[0].path, false);
});

check("Reachability stays a path summary even when the query omits hop count", () => {
  const data = buildEvidenceGraph([{ seed_id: 1, result_id: 9 }], "icfg", Object.assign({}, icfgPlan, { intent: "find_reachable_successors" }));
  assert.strictEqual(data.edges[0].path, true);
  assert.strictEqual(data.edges[0].properties.max_hops, 4);
  assert.strictEqual(data.edges[0].caption, "1–4 hops");
  assert.strictEqual(data.nodes.length, 2, "No intermediate nodes should be invented");
});

check("Identities include the concrete ICFG label and preserve zero IDs", () => {
  const data = buildEvidenceGraph([{ seed_id: 0, result_id: 0 }], "icfg", icfgPlan);
  assert.strictEqual(data.nodes.length, 2);
  assert.notStrictEqual(data.edges[0].source, data.edges[0].target);
});

check("Unbounded traversal summaries show no invented maximum hop count", () => {
  const data = buildEvidenceGraph([{ seed_id: 1, result_id: 9 }], "icfg", Object.assign({}, icfgPlan, { intent: "find_reachable_successors", depth: null }));
  assert.strictEqual(data.edges[0].path, true);
  assert.strictEqual(data.edges[0].caption, "1+ hops");
  assert.strictEqual(data.edges[0].properties.hop_limit, "Unlimited");
  assert.strictEqual(data.edges[0].properties.max_hops, undefined);
});

check("CallGraph projections support each compiled intent and edge direction", () => {
  [
    [{ caller_id: 1, caller_name: "a", target_id: 2, target_name: "b" }, "find_callers"],
    [{ source_id: 1, source_name: "a", callee_id: 2, callee_name: "b" }, "find_callees"],
    [{ caller_id: 1, caller_name: "a", callee_id: 2, callee_name: "b" }, "find_neighborhood"],
    [{ caller_id: 1, caller_name: "a", target_id: 2, target_name: "b" }, "find_reachable_callers"],
    [{ source_id: 1, source_name: "a", callee_id: 2, callee_name: "b" }, "find_reachable_callees"],
  ].forEach(([record, intent]) => {
    const data = buildEvidenceGraph([record], "callgraph", { intent, depth: 3 });
    assert.strictEqual(data.nodes.length, 2);
    assert.strictEqual(data.edges.length, 1);
    assert.strictEqual(data.nodes.find((node) => node.key === data.edges[0].source).id, 1);
    assert.strictEqual(data.edges[0].path, intent.includes("reachable"));
  });
});

check("Plain function and ICFG listings do not invent relationships", () => {
  const functions = buildEvidenceGraph([{ function_id: 1, function_name: "name", fun_obj_var_id: 23, source_loc: "a.cc:4" }], "callgraph", { intent: "find_functions" });
  assert.strictEqual(functions.edges.length, 0);
  assert.strictEqual(functions.nodes[0].properties.fun_obj_var_id, 23);
  assert.strictEqual(functions.nodes[0].properties.name, "name");
  const nodes = buildEvidenceGraph([{ seed_id: 0 }, { seed_id: 1 }], "icfg", Object.assign({}, icfgPlan, { intent: "find_nodes" }));
  assert.strictEqual(nodes.nodes.length, 2);
  assert.strictEqual(nodes.edges.length, 0);
});

check("Repeated rows merge while retaining evidence row references and edge kinds", () => {
  const row = { caller_id: 1, caller_name: "a", callee_id: 2, callee_name: "b", edge_kind: 0 };
  const data = buildEvidenceGraph([row, row, Object.assign({}, row, { edge_kind: 1 })], "callgraph");
  assert.strictEqual(data.nodes.length, 2);
  assert.strictEqual(data.edges.length, 2);
  assert.deepStrictEqual(data.edges[0].records, [0, 1]);
  assert.deepStrictEqual(data.nodes[0].records, [0, 1, 2]);
});

check("PAG previews retain known endpoints and hop counts without guessing edge types", () => {
  const data = buildEvidenceGraph([{ argument_id: 7, value_id: 8, value_kind: "ValVar", source_location: "a.cc:12", path_length: 2 }], "pag");
  assert.strictEqual(data.edges[0].path, true);
  assert.strictEqual(data.edges[0].caption, "2 hops");
  assert.strictEqual(data.nodes[1].properties.source_loc, "a.cc:12");
  assert.strictEqual(data.edges[0].label, "Value-flow path");
});

check("Live PAG rows retain statement edge identity, direction and schema labels", () => {
  const plan = { intent: "find_predecessors", seed_label: "ValVar", result_label: "ArgValVar", edge_label: "StoreStmt" };
  const row = { seed_id: 99134, seed_source_loc: "file.cc:127", result_id: 99131, result_arg_no: 0, flow_id: 78663, flow_kind: 2 };
  const data = buildEvidenceGraph([row, Object.assign({}, row, { flow_id: 78664 })], "pag", plan);
  assert.strictEqual(data.nodes.length, 2);
  assert.strictEqual(data.edges.length, 2);
  assert.strictEqual(data.nodes.find(n => n.key === data.edges[0].source).label, "ArgValVar");
  assert.strictEqual(data.nodes.find(n => n.key === data.edges[0].target).id, 99134);
  assert.strictEqual(data.edges[0].properties.id, 78663);
  assert.strictEqual(data.edges[0].label, "StoreStmt");
  assert.strictEqual(data.edges[0].path, false);
});

check("Live PAG reachability is a path summary and node lookups invent no edges", () => {
  const plan = { intent: "find_reachable_successors", seed_label: "ArgValVar", result_label: "ValVar", edge_label: "CopyStmt", depth: 3 };
  const data = buildEvidenceGraph([{ seed_id: 1, result_id: 2 }], "pag", plan);
  assert.strictEqual(data.edges[0].path, true);
  assert.strictEqual(data.edges[0].properties.max_hops, 3);
  const nodes = buildEvidenceGraph([{ seed_id: 1, seed_arg_no: 0 }], "pag", { intent: "find_nodes", seed_label: "ArgValVar" });
  assert.strictEqual(nodes.nodes[0].properties.arg_no, 0);
  assert.strictEqual(nodes.edges.length, 0);
});

check("Missing IDs, empty and malformed rows produce no invented elements", () => {
  [undefined, [], [null, 3, [], { caller_id: null, callee_id: "" }]].forEach((records) => {
    assert.deepStrictEqual(buildEvidenceGraph(records, "callgraph"), { nodes: [], edges: [] });
  });
});

check("Self calls remain real self loops", () => {
  const data = buildEvidenceGraph([{ caller_id: 1, callee_id: 1 }], "callgraph");
  assert.strictEqual(data.nodes.length, 1);
  assert.strictEqual(data.edges[0].source, data.edges[0].target);
});

function evidenceNode(label, id, records = [0], properties = {}) {
  return { label, id, records, properties };
}
function evidenceEdge(label, source, target, records = [0]) {
  return { label, source: { label: source.label, id: source.id }, target: { label: target.label, id: target.id }, records, properties: {} };
}

check("Complete ICFG paths render every hop and intermediate concrete label", () => {
  const nodes = [evidenceNode("IntraICFGNode", 0), evidenceNode("CallICFGNode", 3, [0], { source_loc: "a.cc:12" }), evidenceNode("IntraICFGNode", 5)];
  const evidence = { nodes, edges: [evidenceEdge("IntraCFGEdge", nodes[0], nodes[1]), evidenceEdge("IntraCFGEdge", nodes[1], nodes[2])] };
  const data = buildEvidenceGraph([{ seed_id: 0, result_id: 5, path_length: 2 }], "icfg", { intent: "find_reachable_successors" }, evidence);
  assert.strictEqual(data.nodes.length, 3);
  assert.strictEqual(data.edges.length, 2);
  assert.strictEqual(data.nodes[1].label, "CallICFGNode");
  assert.strictEqual(data.nodes[1].properties.source_loc, "a.cc:12");
  assert(data.edges.every(edge => !edge.path && edge.caption === "IntraCFGEdge"));
  assert(!data.edges.some(edge => edge.source === data.nodes[0].key && edge.target === data.nodes[2].key));
});

check("Complete PAG predecessor paths preserve every arrow and colliding typed IDs", () => {
  const nodes = [evidenceNode("ArgValVar", 1), evidenceNode("ObjVar", 1), evidenceNode("ValVar", 3)];
  const evidence = { nodes, edges: [evidenceEdge("StoreStmt", nodes[0], nodes[1]), evidenceEdge("StoreStmt", nodes[1], nodes[2])] };
  const data = buildEvidenceGraph([{ seed_id: 3, result_id: 1, path_length: 2 }], "pag", { intent: "find_reachable_predecessors", seed_label: "ValVar", result_label: "ArgValVar" }, evidence);
  assert.strictEqual(data.nodes.length, 3);
  assert.notStrictEqual(data.nodes[0].key, data.nodes[1].key);
  assert.strictEqual(data.edges[0].source, data.nodes[0].key);
  assert.strictEqual(data.edges[0].target, data.nodes[1].key);
  assert.strictEqual(data.edges[1].target, data.nodes[2].key);
  assert(data.edges.every(edge => !edge.path));
});

check("Shared CallGraph hops merge and retain matching record references", () => {
  const nodes = [evidenceNode("CallGraphNode", 1, [0, 1]), evidenceNode("CallGraphNode", 2, [0, 1], { name: "middle" }),
    evidenceNode("CallGraphNode", 3, [0, 1]), evidenceNode("CallGraphNode", 4, [1])];
  const edges = [evidenceEdge("CallGraphEdge", nodes[0], nodes[1], [0, 1]), evidenceEdge("CallGraphEdge", nodes[1], nodes[2], [0, 1]), evidenceEdge("CallGraphEdge", nodes[2], nodes[3], [1])];
  const data = buildEvidenceGraph([{ source_id: 1, callee_id: 3, call_distance: 2 }, { source_id: 1, callee_id: 4, call_distance: 3 }], "callgraph", { intent: "find_reachable_callees" }, { nodes, edges: edges.concat(edges[0]) });
  assert.strictEqual(data.nodes.length, 4);
  assert.strictEqual(data.edges.length, 3);
  assert.strictEqual(data.nodes[1].properties.name, "middle");
  assert.deepStrictEqual(data.edges[0].records, [0, 1]);
  assert.deepStrictEqual(data.edges[2].records, [1]);
  assert(data.edges.every(edge => !edge.path));
});

check("Missing expanded endpoints and malformed row references invent no elements", () => {
  const node = evidenceNode("CallGraphNode", 0, [-1, 0, 9, "0", 0]);
  const evidence = { nodes: [null, {}, node], edges: [null, {}, evidenceEdge("CallGraphEdge", node, { label: "CallGraphNode", id: 9 })] };
  const data = buildEvidenceGraph([{ source_id: 0, callee_id: 9 }], "callgraph", {}, evidence);
  assert.strictEqual(data.nodes.length, 1);
  assert.strictEqual(data.edges.length, 0);
  assert.deepStrictEqual(data.nodes[0].records, [0]);
});

check("Selecting a path includes only its own nodes and ordered edges", () => {
  const nodes = [0, 1, 2, 3].map(id => evidenceNode("CallGraphNode", id, [0, 1]));
  const edges = [evidenceEdge("CallGraphEdge", nodes[0], nodes[1]), evidenceEdge("CallGraphEdge", nodes[1], nodes[3]),
    evidenceEdge("CallGraphEdge", nodes[0], nodes[2]), evidenceEdge("CallGraphEdge", nodes[2], nodes[3])];
  const evidence = { nodes, edges };
  const selected = evidenceForPath(evidence, { nodes: [nodes[0], nodes[2], nodes[3]], edge_indices: [2, 3] });
  assert.deepStrictEqual(selected.nodes.map(n => n.id), [0, 2, 3]);
  assert.deepStrictEqual(selected.edges, [edges[2], edges[3]]);
  assert.strictEqual(evidence.nodes.length, 4, "Selecting a path must not change the shared evidence");
});

check("Path selection distinguishes parallel edges and keeps edge properties", () => {
  const nodes = [evidenceNode("ValVar", 0), evidenceNode("ValVar", 1)];
  const edges = [evidenceEdge("CopyStmt", nodes[0], nodes[1]), evidenceEdge("CopyStmt", nodes[0], nodes[1])];
  edges[0].properties = { edge_id: 21 };
  edges[1].properties = { edge_id: 22 };
  const selected = evidenceForPath({ nodes, edges }, { nodes, edge_indices: [1] });
  const data = buildEvidenceGraph([{}], "pag", {}, selected);
  assert.strictEqual(data.edges.length, 1);
  assert.deepStrictEqual(data.edges[0].properties, { edge_id: 22 });
});

console.log(`${checks.length} graph evidence checks passed.`);
