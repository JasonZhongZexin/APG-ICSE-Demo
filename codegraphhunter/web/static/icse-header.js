/* Shared configuration lamps for the APR overview, settings and query pages. */
(() => {
  "use strict";
  const dbLight = document.getElementById("icseDbLight");
  const apiLight = document.getElementById("icseApiLight");
  const prompt = document.getElementById("icseConfigPrompt");
  if (!dbLight || !apiLight || !prompt) return;

  function renderLight(light, ready, detail) {
    light.className = `icse-config-light ${ready ? "configured" : "not-configured"}`;
    light.setAttribute("aria-label", detail);
    light.setAttribute("title", detail);
  }

  function renderPrompt(ready, message) {
    prompt.textContent = message;
    prompt.classList.toggle("hidden", ready);
  }

  function update(payload) {
    const configuration = payload.configuration || {};
    const dbReady = Boolean(configuration.graphdb_password_configured && configuration.graphdb_uri && configuration.graphdb_user);
    const apiReady = Boolean(configuration.api_key_configured);
    renderLight(dbLight, dbReady, `Graph DB ${dbReady ? "configured" : "not configured"}`);
    renderLight(apiLight, apiReady, `OpenAI API key ${apiReady ? "configured" : "not configured"}`);
    const missing = [];
    if (!dbReady) missing.push("Graph DB");
    if (!apiReady) missing.push("OpenAI API key");
    const ready = missing.length === 0;
    renderPrompt(ready, ready ? "" : `Open Settings to configure ${missing.join(" and ")}.`);
  }

  function unavailable() {
    renderLight(dbLight, false, "Graph DB configuration status unavailable");
    renderLight(apiLight, false, "OpenAI API key configuration status unavailable");
    renderPrompt(false, "Unable to load configuration. Open Settings to check.");
  }

  async function refresh() {
    try {
      const response = await fetch("/api/codegraphhunter/icse-demo/config");
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error("Settings unavailable");
      update(payload);
    } catch (_) {
      unavailable();
    }
  }

  globalThis.IcseHeader = { update, unavailable, refresh };
  if (document.body.classList.contains("icse-query-page")) {
    refresh();
    window.addEventListener("pageshow", (event) => { if (event.persisted) refresh(); });
  }
})();
