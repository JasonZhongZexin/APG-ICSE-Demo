"use strict";
window.QueryFlow = (() => {
  const stages = ["request", "schema", "matching", "compiling", "applying", "result"];
  const messages = {
    schema: "Grounding the request in the supported schema…",
    matching: "Matching your request to suitable capabilities…",
    compiling: "Preparing and validating the selected capability's query…",
    applying: "Applying the selected capability to retrieve evidence…",
    result: "Query evidence retrieved. Summarizing all returned results…",
  };
  let current = "request";

  function show(stage, message, state = "running") {
    current = stage;
    const index = stages.indexOf(stage);
    document.querySelector(".query-flow").setAttribute("aria-busy", String(state === "running"));
    document.querySelectorAll(".flow-node").forEach((node, position) => {
      const atStage = position === index;
      node.classList.toggle("active", position <= index);
      node.classList.toggle("current", atStage && state === "running");
      node.classList.toggle("failed", atStage && state === "failed");
      node.dataset.state = position < index || state === "completed" ? "completed" : atStage ? state : "pending";
      if (atStage && state !== "completed") node.setAttribute("aria-current", "step");
      else node.removeAttribute("aria-current");
    });
    const status = document.getElementById("queryFlowStatus");
    if (status.textContent !== message) status.textContent = message;
  }

  function start() { show("schema", messages.schema); }
  function progress(payload) {
    const stage = payload.stage === "database" ? "applying" : payload.stage === "planning"
      ? payload.capability ? "compiling" : "matching" : payload.stage === "summarizing" ? "result" : payload.stage;
    if (!messages[stage]) return;
    const message = stage === "compiling" && payload.capability
      ? `Matched ${payload.capability.id}. Preparing its query…` : payload.message || messages[stage];
    show(stage, message);
  }
  function result(payload) {
    if (payload.local_summary && payload.local_summary.status === "pending") {
      show("result", messages.result);
      return;
    }
    const paths = (payload.evidence_graph && payload.evidence_graph.paths || []).length;
    const records = (payload.records || []).length;
    show("result", `${payload.status === "partial" ? "Partial query results" : "Query complete"} · ${paths} returned paths · ${records} returned records.`, "completed");
  }
  function fail() { show(current, `Query stopped during ${current === "matching" ? "capability matching" : current === "applying" ? "capability application" : "query preparation"}.`, "failed"); }
  return {start, progress, result, fail};
})();
