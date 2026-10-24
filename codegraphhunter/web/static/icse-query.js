const graphConfigs = {
  capabilities: {
    index: "04", title: "codebase",
    description: "Describe your question. The selected model automatically chooses a supported capability. Query source, call paths, control flow, value flow, points-to sets, aliasing, class hierarchy, types and cross-graph evidence.",
    nodes: ["Source", "CallGraph", "ICFG", "PAG", "CHG", "SVFType"],
    edges: ["CallGraphEdge", "IntraCFGEdge", "CallCFGEdge", "RetCFGEdge", "PAG statements", "CHEdge"],
    properties: ["Exported IDs", "Source locations", "Path lengths", "Type metadata"],
    examples: ["Map decode_url to CallGraph, PAG and ICFG IDs.", "Find call paths from decode_url to is_hex.", "Find uses of strcmp in the source."],
  },
  icfg: {
    index: "01",
    title: "ICFG",
    description: "Describe the control-flow evidence you need. The backend turns it into a constrained query plan, compiles schema-valid Cypher, validates it, and queries the existing ICFG database.",
    nodes: ["CallICFGNode", "FunEntryICFGNode", "FunExitICFGNode", "GlobalICFGNode", "InterICFGNode", "IntraICFGNode", "RetICFGNode"],
    edges: ["IntraCFGEdge", "CallCFGEdge", "RetCFGEdge"],
    properties: ["id", "kind", "source_loc", "pag_edge_ids", "fun_obj_var_id", "bb_id", "is_return"],
    examples: [
      "Show IntraCFGEdge successors of ICFG node 83372.",
      "Find IntraICFGNode records in function 29066 near source line 132.",
      "Which FunEntryICFGNode records can reach ICFG node 83372 within four IntraCFGEdge hops?",
    ],
    cypher: `MATCH p=(entry:FunEntryICFGNode)-[:IntraCFGEdge*1..4]->(node:IntraICFGNode)\nWHERE entry.fun_obj_var_id = $function_id\n  AND node.source_loc CONTAINS $source_file\nRETURN entry.id AS entry_id,\n       node.id AS node_id,\n       node.source_loc AS source_location,\n       length(p) AS path_length\nORDER BY path_length ASC\nLIMIT 25`,
    columns: ["entry_id", "node_id", "source_location", "path_length"],
    rows: [
      [83371, 83372, "out/httplib.cc:127", 1],
      [83371, 83374, "out/httplib.cc:128", 2],
      [83371, 83378, "out/httplib.cc:138", 4],
    ],
  },
  pag: {
    index: "02",
    title: "PAG",
    description: "Describe the pointer or value-flow evidence you need. The backend creates a constrained PAG query plan, compiles schema-valid Cypher, validates it, and queries the existing PAG database.",
    nodes: ["ArgValVar", "ValVar", "GepValVar", "FunObjVar", "FunValVar", "ObjVar", "GepObjVar", "RetValPN", "VarArgValPN", "ConstIntValVar", "GlobalValVar", "StackObjVar"],
    edges: ["AddrStmt", "CopyStmt", "LoadStmt", "StoreStmt", "GepStmt", "CallPE", "RetPE", "CmpStmt", "BinaryOPStmt", "PhiStmt", "BranchStmt", "SelectStmt"],
    properties: ["id", "kind", "source_loc", "icfg_node_id", "arg_no", "cg_node_id", "svf_type_id", "val_name"],
    examples: [
      "Find argument zero of is_valid_path as ArgValVar nodes.",
      "Show StoreStmt successors of argument zero in is_valid_path that are ValVar nodes.",
      "Find FunObjVar records whose val_name contains is_valid_path.",
    ],
  },
  callgraph: {
    index: "03",
    title: "CallGraph",
    description: "Describe the caller/callee relationship you need. The backend turns it into a constrained query plan, compiles schema-valid Cypher, validates it, and queries the existing CallGraph database.",
    nodes: ["CallGraphNode"],
    edges: ["CallGraphEdge"],
    properties: ["id", "fun_name", "fun_obj_var_id", "source_loc"],
    examples: [
      "Who calls is_valid_path, and what functions does it call?",
      "Find entry functions that can reach the selected candidate within three calls.",
      "Show the immediate caller and callee neighborhood of handle_file_request.",
    ],
    cypher: `MATCH p=(caller:CallGraphNode)-[:CallGraphEdge*1..2]->(callee:CallGraphNode)\nWHERE callee.fun_name CONTAINS $function_name\nRETURN caller.id AS caller_id,\n       caller.fun_name AS caller,\n       callee.id AS callee_id,\n       callee.fun_name AS callee,\n       length(p) AS call_distance\nORDER BY call_distance ASC\nLIMIT 25`,
    columns: ["caller_id", "caller", "callee_id", "callee", "call_distance"],
    rows: [
      [870, "httplib::Server::handle_file_request", 3167, "httplib::detail::is_valid_path", 1],
      [206, "httplib::Server::process_request", 870, "httplib::Server::handle_file_request", 1],
      [206, "httplib::Server::process_request", 3167, "httplib::detail::is_valid_path", 2],
    ],
  },
};

const byId = (id) => document.getElementById(id);
const graphKey = window.location.pathname.split("/").filter(Boolean).pop().toLowerCase();
const config = graphConfigs[graphKey] || graphConfigs.icfg;
const evidenceGraph = new GraphEvidence.EvidenceGraphView();

function renderPage() {
  QueryLimits.initialize();
  QueryCapabilities.initialize(graphKey);
  document.title = `APR · Query on ${config.title}`;
  byId("graphIndex").textContent = config.index;
  byId("graphTitle").textContent = config.title;
  byId("graphBadge").textContent = config.title;
  byId("graphDescription").textContent = config.description;
  byId("schemaTitle").textContent = graphKey === "capabilities" ? "Capability scope" : `${config.title} schema`;
  byId(`nav${graphKey === "callgraph" ? "Callgraph" : graphKey[0].toUpperCase() + graphKey.slice(1)}`).classList.add("active");

  if (["callgraph", "icfg", "pag", "capabilities"].includes(graphKey)) {
    byId("queryModelField").classList.remove("hidden");
    byId("queryModel").value = "gpt-4o";
    byId("backendStatus").classList.add("connected");
    byId("backendStatus").lastChild.textContent = "Backend query enabled";
    byId("workspaceModeTag").textContent = "CONSTRAINED BACKEND";
    byId("prototypeNoteText").textContent = "The model selects a strict query plan. The server compiles parameterized Cypher and executes it only after SchemaGuard and QueryGuard pass.";
    byId("previewQuery").querySelector("span").textContent = "Run constrained query";
    byId("resultMode").textContent = "LIVE RESULT";
    byId("resultMode").classList.add("live");
  }

  renderPills("nodeLabels", config.nodes);
  renderPills("edgeLabels", config.edges);
  renderPills("propertyLabels", config.properties, "property");

  const examples = byId("exampleQueries");
  config.examples.forEach((example) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = example;
    button.addEventListener("click", () => {
      byId("naturalQuery").value = example;
      byId("naturalQuery").focus();
    });
    examples.appendChild(button);
  });
}

function renderPills(containerId, values, className = "") {
  const container = byId(containerId);
  values.forEach((value) => {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = value;
    container.appendChild(span);
  });
}


function renderResults(columns, rows, countLabel, plan = {}, graphEvidence = null) {
  const table = byId("resultTable");
  table.innerHTML = "";
  if (!rows.length) {
    evidenceGraph.clear();
    byId("resultTableWrap").classList.add("hidden");
    byId("resultEmpty").classList.remove("hidden");
    byId("resultEmpty").querySelector("b").textContent = "No matching records";
    byId("resultEmpty").querySelector("p").textContent = `The guarded query completed successfully, but TuGraph returned no matching ${config.title} records.`;
    byId("resultCount").textContent = countLabel;
    return;
  }
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column;
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  const body = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    row.forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value !== null && typeof value === "object" ? JSON.stringify(value) : String(value == null ? "" : value);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.append(head, body);
  byId("resultEmpty").classList.add("hidden");
  byId("resultTableWrap").classList.remove("hidden");
  byId("resultCount").textContent = countLabel;
  byId("tableRecordCount").textContent = countLabel;
  const records = rows.map((row) => Object.fromEntries(columns.map((column, index) => [column, row[index]])));
  evidenceGraph.render(records, graphKey, plan, graphEvidence);
  if (evidenceGraph.paths.length) {
    byId("resultCount").textContent = `${evidenceGraph.paths.length} path${evidenceGraph.paths.length === 1 ? "" : "s"}`;
  }
  byId("resultTableWrap").open = !evidenceGraph.nodes.length && !evidenceGraph.paths.length;
}

function renderQueryFailure(message) {
  QueryCapabilities.failed();
  QueryLocalSummary.clear();
  evidenceGraph.clear();
  const empty = byId("resultEmpty");
  byId("resultTable").innerHTML = "";
  byId("resultTableWrap").classList.add("hidden");
  empty.classList.remove("hidden");
  empty.querySelector("b").textContent = "Query execution failed";
  empty.querySelector("p").textContent = message;
  byId("resultCount").textContent = "FAILED";
}

function renderLiveRecords(records, plan, graphEvidence) {
  const safeRecords = Array.isArray(records) ? records : [];
  const columns = [];
  safeRecords.forEach((record) => {
    if (!record || typeof record !== "object" || Array.isArray(record)) return;
    Object.keys(record).forEach((key) => {
      if (!columns.includes(key)) columns.push(key);
    });
  });
  const rows = safeRecords.map((record) => columns.map((column) => record && record[column]));
  renderResults(columns, rows, `${safeRecords.length} records`, plan, graphEvidence);
}

function renderQueryPending() {
  QueryCapabilities.pending();
  QueryLocalSummary.clear();
  evidenceGraph.clear();
  byId("resultTable").replaceChildren();
  byId("resultTableWrap").classList.add("hidden");
  byId("resultEmpty").classList.remove("hidden");
  byId("resultEmpty").querySelector("b").textContent = "Query running…";
  byId("resultEmpty").querySelector("p").textContent = "The evidence graph and records will appear when this query completes.";
  byId("resultCount").textContent = "RUNNING";
}

function renderQueryResult(payload, preserveEvidence = false) {
  if (!preserveEvidence) byId("cypherOutput").textContent = GraphQueryRequest.displayCypher(payload);
  byId("queryParams").textContent = JSON.stringify(payload.params || {}, null, 2);
  byId("cypherStatus").textContent = `Validated ${(payload.plan && payload.plan.intent) || config.title} query · ${payload.model || byId("queryModel").value}`;
  byId("guardStatus").textContent = payload.guard && payload.guard.status === "ok" ? "PASSED" : "REJECTED";
  if (!preserveEvidence) renderLiveRecords(payload.records, payload.plan, payload.evidence_graph);
  QueryCapabilities.render(payload);
  QueryLocalSummary.render(payload.local_summary);
  QueryFlow.result(payload);
}

async function runConstrainedQuery(question, limits) {
  let lastProgress = null;
  let retrievedPayload = null;
  QueryLimits.setDisabled(true);
  const model = byId("queryModel").value;
  byId("queryModel").disabled = true;
  byId("previewQuery").disabled = true;
  byId("cypherStatus").textContent = "The model is selecting a capability for your request…";
  byId("cypherOutput").textContent = `// The model may choose only an allowlisted ${config.title} query intent.`;
  byId("queryParams").textContent = "Waiting for the server to bind parameters…";
  byId("guardStatus").textContent = "PENDING";
  QueryFlow.start();
  try {
    const request = QueryCapabilities.request(graphKey, question, model, limits, true);
    const payload = await GraphQueryRequest.run(request.url, request.body, (progress) => {
      lastProgress = progress;
      QueryCapabilities.progress(progress);
      QueryFlow.progress(progress);
      if (progress.stage === "summarizing" && progress.result) {
        retrievedPayload = progress.result;
        renderQueryResult(retrievedPayload);
      }
      QueryLocalSummary.progress(progress);
      byId("cypherStatus").textContent = progress.message;
      if (progress.cypher) {
        byId("cypherOutput").textContent = progress.cypher;
        byId("queryParams").textContent = JSON.stringify(progress.params || {}, null, 2);
        byId("guardStatus").textContent = "PASSED";
      }
      if (progress.stage === "database") {

        byId("resultEmpty").querySelector("b").textContent = "Querying TuGraph…";
        byId("resultEmpty").querySelector("p").textContent = progress.message;
      }
    });
    renderQueryResult(payload, !!retrievedPayload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (retrievedPayload) {
      QueryLocalSummary.unavailable();
      byId("cypherStatus").textContent = "Query completed · Local summary unavailable";
      QueryFlow.result({...retrievedPayload, local_summary:{status:"fallback"}});
      return;
    }
    byId("queryError").textContent = message;
    const databaseStarted = (lastProgress && lastProgress.stage === "database") || error.stage === "database";
    byId("cypherStatus").textContent = databaseStarted ? "Database query failed or timed out" : "Query did not execute";
    if (!databaseStarted) {
      byId("queryParams").textContent = "Parameters were not executed.";
      byId("guardStatus").textContent = "NOT EXECUTED";
    }
    renderQueryFailure(message);
    QueryFlow.fail();
  } finally {
    QueryLimits.setDisabled(false);
    byId("queryModel").disabled = false;
    byId("previewQuery").disabled = false;
  }
}

byId("queryForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = byId("naturalQuery").value.trim();
  if (!question) {
    byId("queryError").textContent = "Describe what you want to query before running it.";
    return;
  }
  const limits = QueryLimits.read();
  if (!limits) return;
  byId("queryError").textContent = "";
  renderQueryPending();
  runConstrainedQuery(question, limits);
});

byId("copyCypher").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(byId("cypherOutput").textContent);
    byId("copyCypher").textContent = "Copied";
    setTimeout(() => { byId("copyCypher").textContent = "Copy"; }, 1200);
  } catch (_) {
    byId("copyCypher").textContent = "Unavailable";
  }
});

renderPage();
