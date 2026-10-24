"use strict";
const byId = (id) => document.getElementById(id);

async function requestSettings(options = {}) {
  const response = await fetch("/api/codegraphhunter/icse-demo/config", options);
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || "Unable to save experiment settings.");
  return payload;
}

function showSettingsError(error) {
  byId("experimentConfigError").textContent = error.message || "Unable to load experiment settings.";
  byId("experimentConfigError").classList.remove("hidden");
}

async function loadSettings() {
  try {
    const payload = await requestSettings();
    IcseHeader.update(payload);
    const settings = payload.configuration || {};
    const runtime = payload.runtime || {};
    byId("runtimeVersion").textContent = runtime.demo_version || "Unavailable";
    byId("runtimeRunner").textContent = runtime.runner || "Unavailable";
    byId("experimentGraphUri").value = settings.graphdb_uri || "bolt://localhost:7687";
    byId("experimentGraphUser").value = settings.graphdb_user || "admin";
    byId("experimentApiKey").required = !settings.api_key_configured;
    byId("experimentGraphPassword").required = !settings.graphdb_password_configured;
    byId("experimentApiKey").placeholder = settings.api_key_configured ? "Leave empty to keep the current API key" : "Enter your API key";
    byId("experimentGraphPassword").placeholder = settings.graphdb_password_configured ? "Leave empty to keep the current password" : "Enter your database password";
    byId("apiKeyHint").textContent = settings.api_key_configured
      ? "An API key is configured. Leave empty to keep it, or enter a replacement."
      : "Enter the API key to use for this experiment.";
    byId("experimentConfigStatus").textContent = payload.settings_saved ? "Saved settings loaded for this browser." : "Server defaults loaded. Save to use these settings.";
  } catch (error) {
    IcseHeader.unavailable();
    showSettingsError(error);
    byId("apiKeyHint").textContent = "Enter the API key to use for this experiment.";
    byId("experimentConfigStatus").textContent = "Enter all credentials to save your settings.";
  } finally {
    byId("experimentConfigFields").disabled = false;
    byId("saveExperimentConfig").disabled = false;
  }
}

byId("experimentConfigForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const configuration = {
    api_key: byId("experimentApiKey").value.trim(),
    graphdb_uri: byId("experimentGraphUri").value.trim(),
    graphdb_user: byId("experimentGraphUser").value.trim(),
    graphdb_password: byId("experimentGraphPassword").value,
  };
  byId("experimentConfigError").classList.add("hidden");
  byId("saveExperimentConfig").disabled = true;
  byId("experimentConfigFields").disabled = true;
  byId("experimentConfigStatus").textContent = "Saving settings…";
  try {
    const payload = await requestSettings({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ configuration }) });
    IcseHeader.update(payload);
    window.location.assign("/icse-demo");
  } catch (error) {
    showSettingsError(error);
    byId("experimentConfigStatus").textContent = "Settings were not saved. Check the fields and try again.";
    byId("saveExperimentConfig").disabled = false;
    byId("experimentConfigFields").disabled = false;
  }
});

loadSettings();
