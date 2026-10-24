const byId = (id) => document.getElementById(id);

const form = byId("runForm");
const pageMode = form.dataset.mode || "build";
const submitButton = byId("submitButton");
const formError = byId("formError");
const resultPanel = byId("resultPanel");
const logOutput = byId("logOutput");
const cancelButton = byId("cancelButton");
const dialog = byId("fileDialog");
const fileList = byId("fileList");
const dialogError = byId("dialogError");

let activeJobId = null;
let logCursor = 0;
let pollTimer = null;
let pickerTarget = null;
let pickerKind = "problem";
let pickerParent = "";
let displayedJobId = null;
let artifactSignature = "";
let selectedArtifactPath = "";

const statusText = {
  queued: "Queued",
  running: "Running",
  succeeded: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

function setError(message) {
  formError.textContent = message || "";
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* no-op */ }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function checkHealth() {
  const badge = byId("healthBadge");
  try {
    const health = await getJson("/api/health");
    const missing = [];
    if (pageMode !== "demo" && !health.graphdb_ready) missing.push("graphdb-wpa");
    if (pageMode === "demo" && !health.demo_script_ready) missing.push("demo script");
    if (pageMode === "demo" && !health.demo_case_ready) missing.push("demo case");
    if (!health.schema_ready) missing.push("schema");
    if (!health.tugraph_password_ready) missing.push("TuGraph password");
    badge.className = `health-badge ${missing.length ? "warning" : "ready"}`;
    badge.innerHTML = `<span></span>${missing.length ? `Missing ${missing.join(" / ")}` : "Environment ready"}`;
  } catch (error) {
    badge.className = "health-badge warning";
    badge.innerHTML = "<span></span>Local service unavailable";
  }
}

function renderJob(job) {
  resultPanel.classList.remove("hidden");
  const badge = byId("statusBadge");
  badge.className = `status-badge ${job.status}`;
  badge.textContent = statusText[job.status] || job.status;
  byId("stageValue").textContent = job.stage || "—";
  byId("functionValue").textContent = job.selected_function || "—";
  byId("fileValue").textContent = job.selected_file || "—";
  byId("reasonValue").textContent = job.stop_reason || "—";
  byId("repoValue").textContent = job.repo_root || "—";
  byId("artifactDirectory").textContent = job.artifact_dir || "—";
  renderArtifacts(job.artifacts || []);
  if (job.log_truncated && logOutput.textContent) {
    logOutput.textContent += "\n[Earlier logs were truncated by the server]\n";
  }
  if (job.logs && job.logs.length) {
    const nearBottom = logOutput.scrollHeight - logOutput.scrollTop - logOutput.clientHeight < 80;
    logOutput.textContent += `${logOutput.textContent ? "\n" : ""}${job.logs.join("\n")}`;
    if (nearBottom) logOutput.scrollTop = logOutput.scrollHeight;
  }
  logCursor = job.cursor == null ? logCursor : job.cursor;
  const terminal = ["succeeded", "failed", "cancelled"].includes(job.status);
  submitButton.disabled = !terminal;
  cancelButton.disabled = terminal;
  if (terminal) {
    activeJobId = null;
    clearTimeout(pollTimer);
    if (job.error) setError(job.error);
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderArtifacts(artifacts) {
  const signature = artifacts.map((item) => `${item.path}:${item.size}:${item.modified_ns}`).join("|");
  byId("artifactCount").textContent = `${artifacts.length} ${artifacts.length === 1 ? "FILE" : "FILES"}`;
  if (signature === artifactSignature) return;
  artifactSignature = signature;
  const list = byId("artifactList");
  list.innerHTML = "";
  if (!artifacts.length) {
    list.innerHTML = '<div class="artifact-empty">The patch, logs, and intermediate summaries will appear here when the run completes.</div>';
    return;
  }
  artifacts.forEach((artifact) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.path = artifact.path;
    button.className = `artifact-item${artifact.path === selectedArtifactPath ? " selected" : ""}`;
    const icon = document.createElement("span");
    icon.className = "artifact-icon";
    const artifactIcons = {
      "Global summary": "GS",
      "Local summary": "LS",
      "Patch diff": "DF",
      "Full pipeline log": "FL",
      "Key pipeline log": "KL",
      "Pre-patch test": "PRE",
      "Post-patch test": "POST",
      "Fault localization": "LOC",
      "Fault diagnosis": "DIA",
      "Stage summary": "ST",
    };
    icon.textContent = artifactIcons[artifact.kind] || "ST";
    const details = document.createElement("span");
    details.className = "artifact-details";
    const name = document.createElement("b");
    name.textContent = artifact.name;
    name.title = artifact.path;
    const meta = document.createElement("small");
    meta.textContent = `${artifact.kind} · ${formatBytes(artifact.size)}`;
    details.append(name, meta);
    button.append(icon, details);
    button.addEventListener("click", () => openArtifact(artifact.path));
    list.appendChild(button);
  });
  const generatedDiff = artifacts.find((artifact) => artifact.kind === "Patch diff");
  if (!selectedArtifactPath && generatedDiff) openArtifact(generatedDiff.path);
}

function renderDiff(content) {
  const viewer = byId("artifactContent");
  viewer.className = "artifact-diff";
  viewer.textContent = "";
  const lines = content.split("\n");
  lines.forEach((line) => {
    const row = document.createElement("span");
    row.className = "diff-line";
    if (line.startsWith("@@")) row.classList.add("diff-hunk");
    else if (line.startsWith("+++ ") || line.startsWith("--- ")) row.classList.add("diff-file");
    else if (line.startsWith("+")) row.classList.add("diff-add");
    else if (line.startsWith("-")) row.classList.add("diff-delete");
    else row.classList.add("diff-context");
    row.textContent = line || " ";
    viewer.appendChild(row);
  });
}

async function openArtifact(path) {
  if (!displayedJobId) return;
  selectedArtifactPath = path;
  document.querySelectorAll(".artifact-item").forEach((button) => {
    button.classList.toggle("selected", button.dataset.path === path);
  });
  byId("artifactName").textContent = "Loading…";
  byId("artifactContent").textContent = "Loading artifact content…";
  try {
    const artifact = await getJson(`/api/jobs/${displayedJobId}/artifact?path=${encodeURIComponent(path)}`);
    byId("artifactKind").textContent = artifact.kind.toUpperCase();
    byId("artifactName").textContent = artifact.name;
    byId("artifactSize").textContent = `${formatBytes(artifact.size)}${artifact.truncated ? " · TRUNCATED" : ""}`;
    const viewer = byId("artifactContent");
    if (artifact.kind === "Patch diff" || artifact.name.endsWith(".diff")) {
      renderDiff(artifact.content);
    } else {
      viewer.className = "";
      viewer.textContent = artifact.content || "(This file is empty.)";
    }
    artifactSignature = "";
  } catch (error) {
    byId("artifactName").textContent = "Unable to load artifact";
    byId("artifactContent").className = "";
    byId("artifactContent").textContent = error.message;
  }
}

async function pollJob() {
  if (!activeJobId) return;
  try {
    const job = await getJson(`/api/jobs/${activeJobId}?cursor=${logCursor}`);
    renderJob(job);
    if (activeJobId) pollTimer = setTimeout(pollJob, 750);
  } catch (error) {
    setError(error.message);
    submitButton.disabled = false;
    pollTimer = setTimeout(pollJob, 1800);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  submitButton.disabled = true;
  logOutput.textContent = "";
  logCursor = 0;
  try {
    const payload = pageMode === "demo"
      ? {
          test_case_path: byId("testCasePath").value,
          problem_path: byId("problemPath").value,
        }
      : {
          bc_path: byId("bcPath").value,
          problem_path: byId("problemPath").value,
        };
    const endpoint = pageMode === "demo" ? "/api/demo/jobs" : "/api/jobs";
    const job = await getJson(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    activeJobId = job.id;
    displayedJobId = job.id;
    artifactSignature = "";
    selectedArtifactPath = "";
    byId("artifactKind").textContent = "PREVIEW";
    byId("artifactName").textContent = "Select a summary file";
    byId("artifactSize").textContent = "—";
    byId("artifactContent").className = "";
    byId("artifactContent").textContent = "Choose an artifact on the left to inspect its contents.";
    renderJob(job);
    pollJob();
  } catch (error) {
    submitButton.disabled = false;
    setError(error.message);
  }
});

cancelButton.addEventListener("click", async () => {
  if (!activeJobId) return;
  cancelButton.disabled = true;
  try {
    await getJson(`/api/jobs/${activeJobId}/cancel`, { method: "POST" });
    pollJob();
  } catch (error) {
    cancelButton.disabled = false;
    setError(error.message);
  }
});

byId("clearLog").addEventListener("click", () => { logOutput.textContent = ""; });

async function browse(path) {
  dialogError.textContent = "";
  byId("selectDirectory").classList.toggle("hidden", pickerKind !== "directory");
  fileList.innerHTML = '<div class="empty">Loading directory…</div>';
  try {
    const data = await getJson(`/api/browse?kind=${encodeURIComponent(pickerKind)}&path=${encodeURIComponent(path || "")}`);
    byId("currentDirectory").textContent = data.path;
    pickerParent = data.parent;
    fileList.innerHTML = "";
    if (!data.entries.length) fileList.innerHTML = '<div class="empty">No selectable files in this directory</div>';
    data.entries.forEach((entry) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-entry";
      const kind = document.createElement("span");
      kind.textContent = entry.is_dir ? "DIR" : pickerKind === "bc" ? "BC" : "FILE";
      const name = document.createElement("span");
      name.textContent = entry.name;
      button.append(kind, name);
      button.addEventListener("click", () => {
        if (entry.is_dir) browse(entry.path);
        else {
          byId(pickerTarget).value = entry.path;
          dialog.close();
        }
      });
      fileList.appendChild(button);
    });
  } catch (error) {
    fileList.innerHTML = "";
    dialogError.textContent = error.message;
  }
}

document.querySelectorAll(".browse-button").forEach((button) => {
  button.addEventListener("click", () => {
    pickerTarget = button.dataset.target;
    pickerKind = button.dataset.kind;
    dialog.showModal();
    browse(byId(pickerTarget).value);
  });
});

byId("parentDirectory").addEventListener("click", () => pickerParent && browse(pickerParent));
byId("selectDirectory").addEventListener("click", () => {
  if (!pickerTarget || pickerKind !== "directory") return;
  byId(pickerTarget).value = byId("currentDirectory").textContent;
  dialog.close();
});
byId("closeDialog").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

checkHealth();
