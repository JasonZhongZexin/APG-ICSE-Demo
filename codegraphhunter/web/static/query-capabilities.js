"use strict";
window.QueryCapabilities = (() => {
  const groups = {icfg:"ICFG", pag:"PAG", callgraph:"CallGraph"};
  const byId = id => document.getElementById(id);
  let selected = null;
  let scope = "";
  let helpCatalog = null;
  let helpRequest = null;

  function summary(state, status, capability, description) {
    byId("usedCapability").dataset.state = state;
    byId("usedCapabilityStatus").textContent = status;
    byId("usedCapabilityId").textContent = capability ? capability.id : state === "planning" ? "Selecting…" : "Not selected yet";
    byId("usedCapabilityTitle").textContent = capability ? capability.title : state === "planning"
      ? "Matching your request to a capability" : state === "failed" ? "Capability selection did not complete" : "Submit a query to select a capability";
    byId("usedCapabilityDescription").textContent = description;
    byId("usedCapabilityHelp").classList.toggle("hidden", !capability);
  }

  function initialize(graph) {
    scope = groups[graph] || "";
    byId("queryCapabilityHint").textContent = scope
      ? `The selected model automatically chooses a ${scope} capability based on your request.`
      : "The selected model automatically chooses from the supported capabilities across source, graphs, types and cross-graph evidence.";
    byId("queryHelpTitle").textContent = scope ? `${scope} query help` : "All capabilities · Query help";
    byId("openQueryHelp").addEventListener("click", () => openHelp());
    byId("usedCapabilityHelp").addEventListener("click", () => openHelp(selected ? selected.id : ""));
    byId("closeQueryHelp").addEventListener("click", () => byId("queryHelpDialog").close());
    byId("retryQueryHelp").addEventListener("click", loadHelp);
    byId("queryHelpSearch").addEventListener("input", renderHelp);
    byId("queryHelpDialog").addEventListener("click", event => {
      const dialog = byId("queryHelpDialog");
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
  }

  function request(graph, question, model, limits, apr) {
    return {url: apr ? "/api/codegraphhunter/icse-demo/query/capabilities" : "/api/query/capabilities",
      body: {query:question, model, ...limits, capability:"auto", scope:groups[graph] || ""}};
  }

  function pending() {
    selected = null;
    render(null);
    summary("planning", "PLANNING", null, "The selected model is choosing a capability for this request.");
  }

  function progress(payload) {
    if (payload.capability) {
      selected = payload.capability;
      summary("selected", "SELECTED", selected, selected.expected_output || "Selected automatically from your request.");
    } else if (payload.stage === "database" && selected && byId("usedCapability").dataset.state !== "running") {
      summary("running", "RUNNING", selected, selected.expected_output || "Retrieving evidence with this capability.");
    }
  }

  function failed() {
    render(null);
    summary("failed", "FAILED", selected, selected
      ? "The query failed after this capability was selected. See the query error for details."
      : "The model did not return a selected capability for this request.");
  }

  function render(payload) {
    const root = byId("capabilityReport");
    root.replaceChildren();
    root.classList.toggle("hidden", !payload || !payload.capability);
    if (!payload || !payload.capability) return;
    selected = payload.capability;
    summary(payload.status || "completed", (payload.status || "completed").replace(/_/g, " ").toUpperCase(), selected,
      selected.expected_output || "Selected automatically from your request.");
    const title = document.createElement("b");
    title.textContent = "Selected by the model: " + payload.capability.id + " · " + payload.capability.title;
    const status = document.createElement("p");
    status.textContent = (payload.status || "completed") + " · " + ((payload.analysis || {}).conclusion || "evidence") +
      (payload.capability.analysis === "approximate" ? " · Approximate analysis" : "");
    const notes = document.createElement("ul");
    ((payload.analysis || {}).limitations || []).forEach(note => {
      const li = document.createElement("li"); li.textContent = note; notes.append(li);
    });
    root.append(title, status, notes);
    if ((payload.analysis || {}).pointer_analysis) renderPointerAnalysis(root, payload);
    if (!payload.record_count && (!payload.records || !payload.records.length) && payload.status !== "completed") {
      const empty = document.getElementById("resultEmpty");
      empty.querySelector("b").textContent = payload.status === "requires_graph" ? "Required graph is unavailable" : "Analysis incomplete";
      empty.querySelector("p").textContent = "No records were collected within the available data and limits. See the analysis details above.";
    }
  }

  function renderPointerAnalysis(root, payload) {
    const analysis = payload.analysis.pointer_analysis;
    const panel = document.createElement("section");
    panel.id = "pointerAnalysisReport";
    panel.className = "pointer-analysis-report";
    panel.dataset.complete = String(analysis.complete);
    const heading = document.createElement("h3"); heading.textContent = "Pointer analysis result";
    const scope = document.createElement("p");
    scope.textContent = "Flow-insensitive · Context-insensitive · Field-insensitive · Exported PAG model";
    const progress = document.createElement("p");
    progress.textContent = `${analysis.fixed_point_reached ? "Fixed point reached" : "Solving incomplete"} · ${analysis.constraints_complete ? "Constraint scan complete" : "Constraint scan incomplete"} · ${analysis.constraint_count} constraints · ${analysis.fact_count} inferred facts`;
    const sets = document.createElement("ul");
    (analysis.points_to_sets || []).forEach(set => {
      const item = document.createElement("li");
      item.textContent = `${set.node_label} #${set.node_id}${set.arg_no == null ? "" : ` · argument ${set.arg_no}`} · ${set.object_count} inferred concrete object${set.object_count === 1 ? "" : "s"}${set.contains_unknown ? " · Includes unknown objects" : ""}${set.complete ? "" : " · Set incomplete or unknown"}${set.function_name ? ` · ${set.function_name}` : ""}`;
      sets.append(item);
    });
    const decisions = document.createElement("ul");
    (payload.records || []).filter(row => row.relation === "parameter_alias").forEach(row => {
      const item = document.createElement("li");
      const result = document.createElement("strong"); result.textContent = row.alias_result;
      result.dataset.result = row.alias_result;
      item.append(result, document.createTextNode(` · ${row.left_label} #${row.left_node_id} ↔ ${row.right_label} #${row.right_node_id} · ${row.common_object_count} common concrete object${row.common_object_count === 1 ? "" : "s"}`));
      if (row.alias_result === "NoAlias") item.append(document.createTextNode(" · Within the converged exported model"));
      if (row.alias_result === "Unknown") item.append(document.createTextNode(" · Available evidence cannot establish disjoint points-to sets"));
      decisions.append(item);
    });
    const hint = document.createElement("p");
    hint.textContent = "The graph identifies the queried values and inferred objects. The records describe the inferred relations. Display limits do not truncate constraint solving.";
    panel.append(heading, scope, progress, sets, decisions, hint);
    root.append(panel);
  }

  function openHelp(capabilityId = "") {
    byId("queryHelpSearch").value = capabilityId;
    if (!byId("queryHelpDialog").open) byId("queryHelpDialog").showModal();
    loadHelp();
  }

  async function loadHelp() {
    byId("retryQueryHelp").classList.add("hidden");
    if (helpCatalog) { renderHelp(); return; }
    byId("queryHelpStatus").textContent = "Loading supported capabilities…";
    byId("queryHelpSearch").disabled = true;
    try {
      if (!helpRequest) {
        helpRequest = (async () => {
          const response = await fetch("/api/codegraphhunter/capabilities", {headers: {Accept:"application/json"}});
          const payload = await response.json();
          if (!response.ok || !payload.ok || !Array.isArray(payload.capabilities) || !payload.capabilities.length ||
              payload.capabilities.some(item => !item.id || !item.title || !item.group || !Array.isArray(item.examples) || !item.examples.length || !item.expected_output)) {
            throw new Error("Help is unavailable.");
          }
          return payload.capabilities;
        })();
      }
      helpCatalog = await helpRequest;
      renderHelp();
    } catch (_) {
      helpRequest = null;
      byId("queryHelpStatus").textContent = "Unable to load query help. Please try again.";
      byId("retryQueryHelp").classList.remove("hidden");
    } finally {
      byId("queryHelpSearch").disabled = false;
      if (byId("queryHelpDialog").open) byId("queryHelpSearch").focus();
    }
  }

  function renderHelp() {
    if (!helpCatalog) return;
    const root = byId("queryHelpContent");
    root.replaceChildren();
    const term = byId("queryHelpSearch").value.trim().toLowerCase();
    const available = helpCatalog.filter(item => !scope || item.group === scope);
    const matches = available.filter(item => [item.id, item.title, item.group, item.expected_output, ...item.examples].join(" ").toLowerCase().includes(term));
    byId("queryHelpStatus").textContent = matches.length ? `${matches.length} of ${available.length} capabilities` : "No capabilities match your search.";
    let group = "";
    matches.forEach(item => {
      if (item.group !== group) {
        group = item.group;
        const heading = document.createElement("h2");
        heading.className = "capability-help-group";
        heading.textContent = group;
        root.append(heading);
      }
      const card = document.createElement("article");
      card.className = "capability-help-card";
      card.dataset.capability = item.id;
      const id = document.createElement("code"); id.textContent = item.id;
      const title = document.createElement("h3"); title.textContent = item.title;
      card.append(id, title);
      if (item.analysis === "approximate") {
        const note = document.createElement("p");
        note.className = "capability-help-approximation"; note.textContent = "Approximate analysis";
        card.append(note);
      }
      const ask = document.createElement("h4"); ask.textContent = "How to ask";
      card.append(ask);
      item.examples.forEach(example => {
        const button = document.createElement("button");
        button.type = "button"; button.className = "capability-help-example";
        button.textContent = example; button.title = "Use this example as your query";
        button.addEventListener("click", () => {
          byId("naturalQuery").value = example;
          byId("queryHelpDialog").close();
          byId("naturalQuery").focus();
        });
        card.append(button);
      });
      const hint = document.createElement("p"); hint.className = "capability-help-example-hint";
      hint.textContent = "Select an example to fill the query box. The model will still choose the capability automatically.";
      const expected = document.createElement("h4"); expected.textContent = "Expected output";
      const output = document.createElement("p"); output.textContent = item.expected_output;
      card.append(hint, expected, output);
      root.append(card);
    });
  }

  return {initialize, request, pending, progress, failed, render};
})();
