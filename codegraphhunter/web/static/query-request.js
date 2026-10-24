"use strict";

window.GraphQueryRequest = (() => {
  async function run(url, body, onProgress) {
    const controller = new AbortController();
    let timer;
    let stage = "planning";
    function arm(milliseconds) {
      clearTimeout(timer);
      timer = setTimeout(() => controller.abort(), milliseconds);
    }
    function errorFrom(payload) {
      const details = payload.details || {};
      const error = new Error(payload.error || details.detail || details.reason || "Graph query failed.");
      error.stage = stage;
      error.code = payload.code;
      return error;
    }
    arm(180000);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
        body: JSON.stringify(body), signal: controller.signal,
      });
      const contentType = response.headers && response.headers.get("Content-Type") || "";
      if (!contentType.includes("application/x-ndjson")) {
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw errorFrom(payload);
        return payload;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      for (;;) {
        const chunk = await reader.read();
        pending += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        const lines = pending.split("\n");
        pending = lines.pop();
        if (chunk.done && pending.trim()) lines.push(pending);
        for (const line of lines) {
          if (!line.trim()) continue;
          const item = JSON.parse(line);
          if (item.type === "progress") {
            stage = item.stage || stage;
            if (stage === "database" && item.timeout_s) arm(item.timeout_s * 1000 + 5000);
            if (stage === "summarizing" && item.timeout_s) arm(item.timeout_s * 1000 + 5000);
            onProgress(item);
          } else if (item.type === "error") {
            throw errorFrom(item.payload);
          } else if (item.type === "result") {
            return item.payload;
          }
        }
        if (chunk.done) throw new Error("The connection closed before the query completed.");
      }
    } catch (error) {
      if (error.name === "AbortError") {
        const timeout = new Error(stage === "summarizing" ? "Local summary generation timed out. Query evidence has already been retrieved." : stage === "database" ? "Database query timed out. Please try again with a narrower request." : "The model request timed out. Please try again.");
        timeout.stage = stage;
        throw timeout;
      }
      throw error;
    } finally {
      clearTimeout(timer);
      controller.abort();
    }
  }

  function displayCypher(payload) {
    if (!payload.execution_strategy) return payload.cypher || "// No Cypher returned.";
    const trace = (payload.execution_queries || []).map((item, index) => `// Read ${index + 1} · parameters: ${JSON.stringify(item.params)}\n${item.cypher}`).join("\n\n");
    return `// Reachability plan, evaluated by enumerating paths over cached one-hop reads.\n${payload.cypher}\n\n// Executed read queries:\n${trace}`;
  }
  return { run, displayCypher };
})();
