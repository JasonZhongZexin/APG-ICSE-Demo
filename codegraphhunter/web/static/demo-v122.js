const byId = (id) => document.getElementById(id);

const runButton = byId("runDemo");
const resultContent = byId("resultContent");
const isIcseDemo = window.location.pathname.replace(/\/$/, "") === "/icse-demo";
let icseSettingsReady = !isIcseDemo;
let currentResult = null;
let diagnosisLoaded = false;
let choosingModel = false;

const artifactLabels = {
  pipeline_log: "Pipeline log",
  global_summary: "Global summary",
  fault_diagnosis: "Fault diagnosis",
  repair_sketch_patch: "Repair sketch patch",
  final_patch: "Final patch",
  llm_calls: "GPT call records",
  workdir: "Patched workdir",
  run_dir: "Run directory",
};

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* handled below */ }
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function valueOrDash(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function setText(id, value) {
  const element = byId(id);
  if (element) element.textContent = valueOrDash(value);
}

function setStatus(status) {
  const normalized = String(status || "UNKNOWN").toUpperCase();
  const badge = byId("finalStatus");
  badge.textContent = normalized;
  badge.className = "final-status";
  if (normalized === "PASSED") badge.classList.add("passed");
  else if (normalized === "FAILED" || normalized === "ERROR") badge.classList.add("failed");
  else badge.classList.add("waiting");
}

function setRunning(running) {
  runButton.disabled = running || !icseSettingsReady;
  runButton.querySelector("span").textContent = running
    ? isIcseDemo ? "Running APR…" : "Running frozen v1.2.2 demo…"
    : isIcseDemo ? "Run APR" : "Run CodeGraphHunter Demo";
  document.querySelector(".v122-run-panel").classList.toggle("running", running);
  if (running) {
    byId("demoResults").classList.add("hidden");
    setStatus("RUNNING");
    byId("runMeta").textContent = isIcseDemo
      ? "Running with your saved experiment settings. This may take several minutes."
      : "The stable black-box runner is executing. This may take several minutes.";
    resetTimeline();
  }
}

function resetTimeline() {
  document.querySelectorAll("#pipelineTimeline li").forEach((item) => {
    item.classList.remove("completed", "validation-passed");
    item.querySelector("i").textContent = "WAITING";
  });
  byId("pipelineValidation").textContent = "Validation";
  byId("pipelineValidation").classList.remove("passed");
  document.querySelector(".pipeline-flow").classList.remove("loaded");
}

function completeTimeline(validation) {
  const passed = String(validation || "").toUpperCase() === "PASSED";
  document.querySelectorAll("#pipelineTimeline li").forEach((item) => {
    item.classList.add("completed");
    item.querySelector("i").textContent = "COMPLETED";
  });
  const validationItem = document.querySelector('[data-step="validation"]');
  validationItem.querySelector("i").textContent = valueOrDash(validation).toUpperCase();
  if (passed) validationItem.classList.add("validation-passed");
  byId("timelineValidationDetail").textContent = `Validation: ${valueOrDash(validation)}`;
  byId("pipelineValidation").textContent = `Validation ${valueOrDash(validation)}`;
  byId("pipelineValidation").classList.toggle("passed", passed);
  document.querySelector(".pipeline-flow").classList.add("loaded");
}

function showError(error) {
  const payload = error && error.payload ? error.payload : {};
  const details = payload.details || {};
  byId("demoError").classList.remove("hidden");
  byId("demoErrorTitle").textContent = payload.code || (isIcseDemo ? "APR run failed" : "Demo run failed");
  byId("demoErrorMessage").textContent = error.message || (isIcseDemo ? "APR could not complete." : "The demo could not complete.");
  const logTail = details.log_tail || "";
  byId("errorLogDetails").classList.toggle("hidden", !logTail);
  byId("errorLogTail").textContent = logTail;
}

function clearError() {
  byId("demoError").classList.add("hidden");
  byId("errorLogDetails").classList.add("hidden");
  byId("errorLogTail").textContent = "";
}

function queryCount(queries, name) {
  if (!queries || typeof queries !== "object") return 0;
  const candidates = [name, name.toUpperCase(), name.toLowerCase()];
  for (const candidate of candidates) {
    if (Object.prototype.hasOwnProperty.call(queries, candidate)) return queries[candidate];
  }
  return 0;
}

function renderResult(result, execution = null) {
  currentResult = result || {};
  diagnosisLoaded = false;
  byId("demoResults").classList.remove("hidden");
  const graphIds = currentResult.graph_ids || {};
  const retrieval = currentResult.retrieval || {};
  const diagnosis = currentResult.diagnosis || {};
  const patch = currentResult.patch || {};
  const artifacts = currentResult.artifacts || {};
  const queries = retrieval.queries_by_graph_type || {};
  const graphs = Array.isArray(retrieval.graphs_touched) ? retrieval.graphs_touched : [];
  const validation = patch.validation || currentResult.status;

  setText("demoVersion", currentResult.demo_version || "v1.2.2");
  setStatus(currentResult.status || validation);
  setText("runId", currentResult.run_id ? `RUN ${currentResult.run_id}` : "NO RUN ID");
  setText("caseName", currentResult.case);
  setText("bugReport", currentResult.bug_report);

  setText("highlightFunction", currentResult.localized_function);
  setText("highlightGraphs", graphs.join(", "));
  setText("highlightStop", retrieval.stop_reason);
  setText("highlightValidation", validation);
  byId("highlightValidation").classList.toggle("passed", String(validation).toUpperCase() === "PASSED");

  setText("localizedFunction", currentResult.localized_function);
  setText("patchSource", currentResult.patch_source);
  setText("graphFunction", currentResult.graph_function);
  setText("callgraphNodeId", graphIds.callgraph_node_id);
  setText("funObjVarId", graphIds.fun_obj_var_id);
  setText("argNodeId", graphIds.primary_arg_node_id);
  setText("icfgNodeId", graphIds.primary_icfg_node_id);

  setText("retrievalStatus", retrieval.status);
  setText("pagFunction", currentResult.localized_function);
  setText("pagQueryCount", queryCount(queries, "PAG"));
  setText("icfgQueryCount", queryCount(queries, "ICFG"));
  setText("callgraphQueryCount", queryCount(queries, "CALLGRAPH"));
  setText("pagSeed", `ArgValVar ${valueOrDash(graphIds.primary_arg_node_id)}`);
  setText("icfgSeed", `ICFG node ${valueOrDash(graphIds.primary_icfg_node_id)}`);
  setText("callgraphSeed", `CallGraph node ${valueOrDash(graphIds.callgraph_node_id)}`);
  setText("graphsTouched", graphs.join(", "));
  setText("stopReason", retrieval.stop_reason);
  setText("objectiveMet", retrieval.objective_met_any);
  setText("pathCount", retrieval.path_count);

  setText("summaryStop", retrieval.stop_reason);
  setText("summaryGraphs", graphs.join(", "));
  setText(
    "summaryQueries",
    `PAG=${queryCount(queries, "PAG")}, ICFG=${queryCount(queries, "ICFG")}, CALLGRAPH=${queryCount(queries, "CALLGRAPH")}`,
  );
  byId("globalSummaryRaw").textContent = "Loading the global summary artifact…";
  loadGlobalSummary(artifacts.global_summary);

  setText("diagnosisStatus", diagnosis.status);
  renderMarkdown(byId("diagnosisPreview"), diagnosis.preview || "No diagnosis preview was provided.");
  const diagnosisPath = artifacts.fault_diagnosis || diagnosis.file || "";
  byId("viewDiagnosis").disabled = !diagnosisPath;
  byId("viewDiagnosis").dataset.path = diagnosisPath;
  byId("viewDiagnosis").textContent = "View full diagnosis";
  byId("fullDiagnosis").classList.add("hidden");
  byId("fullDiagnosis").textContent = "";

  setText("patchStatus", patch.status);
  setText("patchValidation", `Validation: ${valueOrDash(validation)}`);
  byId("patchValidation").classList.toggle("passed", String(validation).toUpperCase() === "PASSED");
  renderDiff(patch.patch_text || "");
  renderArtifactPaths(artifacts);
  completeTimeline(validation);

  if (execution) {
    const code = execution.return_code === null || execution.return_code === undefined
      ? "unknown"
      : execution.return_code;
    const llm = currentResult.llm;
    const calls = llm ? `GPT calls: ${llm.completed_calls}/${llm.attempted_calls} completed (${llm.status}).` : "GPT calls: unverified.";
    byId("runMeta").textContent = isIcseDemo
      ? `Model: ${currentResult.model || execution.model || "gpt-4o"}. ${calls} Exit code: ${code}.`
      : `Runner finished with exit code ${code}. Result: ${execution.result_file || "frontend_result.json"}`;
  } else {
    const llm = currentResult.llm;
    byId("runMeta").textContent = isIcseDemo
      ? llm ? `Model: ${currentResult.model}. GPT calls: ${llm.completed_calls}/${llm.attempted_calls} completed (${llm.status}).` : "Showing the latest result. GPT calls: unverified."
      : "Showing the latest frozen v1.2.2 demo result.";
  }
}

function renderMarkdown(container, markdown) {
  container.textContent = "";
  const fragment = document.createDocumentFragment();
  let list = null;
  String(markdown || "").split("\n").forEach((rawLine) => {
    const line = rawLine.trimEnd();
    if (/^#{1,4}\s+/.test(line)) {
      list = null;
      const heading = document.createElement("b");
      heading.className = "markdown-heading";
      heading.textContent = line.replace(/^#{1,4}\s+/, "");
      fragment.appendChild(heading);
    } else if (/^[-*]\s+/.test(line)) {
      if (!list) {
        list = document.createElement("ul");
        fragment.appendChild(list);
      }
      const item = document.createElement("li");
      item.textContent = line.replace(/^[-*]\s+/, "");
      list.appendChild(item);
    } else {
      list = null;
      const paragraph = document.createElement("p");
      paragraph.textContent = line || " ";
      fragment.appendChild(paragraph);
    }
  });
  container.appendChild(fragment);
}

function renderDiff(content) {
  const viewer = byId("patchDiff");
  viewer.textContent = "";
  if (!content) {
    const placeholder = document.createElement("span");
    placeholder.className = "diff-placeholder";
    placeholder.textContent = "No patch text was provided in frontend_result.json.";
    viewer.appendChild(placeholder);
    return;
  }
  String(content).split("\n").forEach((line) => {
    const row = document.createElement("span");
    row.className = "diff-line";
    if (line.startsWith("@@")) row.classList.add("diff-hunk");
    else if (line.startsWith("+++ ") || line.startsWith("--- ")) row.classList.add("diff-file");
    else if (line.startsWith("+")) row.classList.add("diff-add");
    else if (line.startsWith("-")) row.classList.add("diff-delete");
    row.textContent = line || " ";
    viewer.appendChild(row);
  });
}

async function loadGlobalSummary(path) {
  if (!path) {
    byId("globalSummaryRaw").textContent = "No global summary artifact path was provided.";
    return;
  }
  try {
    const artifact = await loadArtifact(path);
    try {
      byId("globalSummaryRaw").textContent = JSON.stringify(JSON.parse(artifact.content), null, 2);
    } catch (_) {
      byId("globalSummaryRaw").textContent = artifact.content || "(Empty global summary.)";
    }
  } catch (error) {
    byId("globalSummaryRaw").textContent = `Unable to load global summary: ${error.message}`;
  }
}

function renderArtifactPaths(artifacts) {
  const container = byId("artifactPaths");
  container.textContent = "";
  Object.entries(artifactLabels).forEach(([key, label]) => {
    const path = artifacts && artifacts[key] ? String(artifacts[key]) : "";
    const row = document.createElement("div");
    row.className = "artifact-path-row";
    const name = document.createElement("span");
    name.textContent = label;
    const code = document.createElement("code");
    code.textContent = path || "—";
    code.title = path;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Copy";
    copy.disabled = !path;
    copy.addEventListener("click", () => copyPath(path, copy));
    const view = document.createElement("button");
    view.type = "button";
    view.textContent = "View";
    view.disabled = !path || key === "workdir" || key === "run_dir";
    view.addEventListener("click", () => openArtifact(path));
    row.append(name, code, copy, view);
    container.appendChild(row);
  });
}

async function copyPath(path, button) {
  try {
    await navigator.clipboard.writeText(path);
    button.textContent = "Copied";
  } catch (_) {
    button.textContent = "Unavailable";
  }
  setTimeout(() => { button.textContent = "Copy"; }, 1200);
}

async function loadArtifact(path) {
  return requestJson(`/api/codegraphhunter/demo/artifact?path=${encodeURIComponent(path)}`);
}

async function openArtifact(path) {
  const preview = byId("artifactPreview");
  preview.classList.remove("hidden");
  byId("artifactPreviewName").textContent = "Loading artifact…";
  byId("artifactPreviewContent").textContent = "Loading…";
  try {
    const artifact = await loadArtifact(path);
    byId("artifactPreviewName").textContent = `${artifact.name}${artifact.truncated ? " · TRUNCATED" : ""}`;
    byId("artifactPreviewContent").textContent = artifact.content || "(This artifact is empty.)";
    preview.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    byId("artifactPreviewName").textContent = "Unable to load artifact";
    byId("artifactPreviewContent").textContent = error.message;
  }
}

function chooseRunModel() {
  const dialog = byId("runModelDialog");
  const select = byId("runModel");
  const confirm = byId("confirmRunModel");
  const status = byId("runModelStatus");
  select.value = "gpt-4o";
  select.disabled = false;
  confirm.disabled = false;
  status.textContent = "Applies to this run only. Default: gpt-4o.";
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue || null);
    }, { once: true });
    dialog.showModal();
    select.focus();
  });
}

if (isIcseDemo) {
  byId("runModelForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!byId("confirmRunModel").disabled) byId("runModelDialog").close(byId("runModel").value);
  });
  byId("cancelRunModel").addEventListener("click", () => byId("runModelDialog").close(""));
}

runButton.addEventListener("click", async () => {
  if (!icseSettingsReady || choosingModel || runButton.disabled) return;
  const options = { method: "POST" };
  if (isIcseDemo) {
    choosingModel = true;
    let model;
    try {
      model = await chooseRunModel();
    } finally {
      choosingModel = false;
    }
    if (!model) return;
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify({ model });
  }
  clearError();
  setRunning(true);
  try {
    const payload = await requestJson(isIcseDemo ? "/api/codegraphhunter/icse-demo/run" : "/api/codegraphhunter/demo/run", options);
    renderResult(payload.result || {}, payload.execution || null);
  } catch (error) {
    showError(error);
    setStatus("FAILED");
    byId("runMeta").textContent = isIcseDemo ? "APR did not produce a usable new result." : "The frozen runner did not produce a usable new result.";
  } finally {
    setRunning(false);
  }
});

byId("viewDiagnosis").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const viewer = byId("fullDiagnosis");
  if (diagnosisLoaded) {
    viewer.classList.toggle("hidden");
    button.textContent = viewer.classList.contains("hidden") ? "View full diagnosis" : "Hide full diagnosis";
    return;
  }
  button.disabled = true;
  button.textContent = "Loading diagnosis…";
  try {
    const artifact = await loadArtifact(button.dataset.path);
    viewer.textContent = artifact.content || "(The diagnosis file is empty.)";
    viewer.classList.remove("hidden");
    diagnosisLoaded = true;
    button.textContent = "Hide full diagnosis";
  } catch (error) {
    viewer.textContent = error.message;
    viewer.classList.remove("hidden");
    button.textContent = "Retry full diagnosis";
  } finally {
    button.disabled = false;
  }
});

byId("closeArtifactPreview").addEventListener("click", () => {
  byId("artifactPreview").classList.add("hidden");
});

resetTimeline();

async function initializeIcseDemo() {
  if (!isIcseDemo) return;
  document.title = "CodeGraphHunter · APR";
  document.body.classList.add("icse-demo-page");
  const frozenLink = document.querySelector('.mode-nav a[href="/demo"]');
  if (frozenLink) frozenLink.classList.remove("active");
  byId("icseDemoNav").classList.add("active");
  const overviewLink = document.querySelector('.graph-menu a[href="/demo"]');
  if (overviewLink) overviewLink.href = "/icse-demo";
  document.querySelectorAll('.graph-menu a[href^="/query/"]').forEach((link) => {
    link.href = `/icse-demo${link.getAttribute("href")}`;
  });
  byId("icseSettingsSummary").classList.remove("hidden");
  await loadIcseDemoSettings();
}

async function loadIcseDemoSettings() {
  runButton.disabled = true;
  icseSettingsReady = false;
  try {
    const payload = await requestJson("/api/codegraphhunter/icse-demo/config");
    IcseHeader.update(payload);
    icseSettingsReady = payload.ready === true;
    byId("icseSettingsStatus").textContent = icseSettingsReady
      ? payload.settings_saved ? "Saved settings ready." : "Server settings ready."
      : "Open Settings to configure API key and Graph DB.";
    byId("runMeta").textContent = icseSettingsReady
      ? "Ready to run with the configured API key and Graph DB."
      : "Open APR settings to configure this run.";
  } catch (_) {
    IcseHeader.unavailable();
    byId("icseSettingsStatus").textContent = "Unable to load settings.";
    byId("runMeta").textContent = "Open APR settings and try again.";
  } finally {
    runButton.disabled = !icseSettingsReady;
  }
}

initializeIcseDemo();
window.addEventListener("pageshow", (event) => {
  if (isIcseDemo && event.persisted) loadIcseDemoSettings();
});
