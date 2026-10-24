"use strict";
window.QueryLocalSummary = (() => {
  const byId = id => document.getElementById(id);
  let current = null;

  function clear() {
    current = null;
    byId("queryLocalSummary").classList.add("hidden");
    ["localSummaryOverview", "localSummaryMessage", "localSummaryCoverage", "localSummaryModel", "localSummaryStatus", "localSummaryScope"].forEach(id => { byId(id).textContent = ""; });
    byId("localSummaryFindings").replaceChildren();
    byId("localSummaryLimitations").replaceChildren();
  }

  function render(summary) {
    clear();
    if (!summary || summary.status === "skipped") return;
    current = summary;
    byId("queryLocalSummary").classList.remove("hidden");
    byId("queryLocalSummary").dataset.state = summary.status;
    byId("localSummaryStatus").textContent = summary.status === "pending" ? "GENERATING" : summary.status === "fallback" ? "EVIDENCE OVERVIEW" : "GENERATED";
    byId("localSummaryModel").textContent = summary.status === "fallback" ? "Deterministic overview" : `Model: ${summary.model || "selected model"}`;
    byId("localSummaryOverview").textContent = summary.overview || "";
    byId("localSummaryMessage").textContent = summary.status === "pending"
      ? "Query results are ready. Summarizing all returned records and paths…"
      : summary.message || "";
    (summary.findings || []).forEach(finding => {
      const item = document.createElement("li");
      const text = document.createElement("p"); text.textContent = finding.text;
      const refs = document.createElement("small"); refs.textContent = `Evidence: ${(finding.evidence_refs || []).join(", ")}`;
      item.append(text, refs); byId("localSummaryFindings").append(item);
    });
    const limits = summary.limitations || [];
    byId("localSummaryLimits").classList.toggle("hidden", !limits.length);
    limits.forEach(note => {
      const item = document.createElement("li"); item.textContent = note;
      byId("localSummaryLimitations").append(item);
    });
    const coverage = summary.coverage;
    if (coverage) {
      byId("localSummaryScope").textContent = `Query-wide summary · All ${coverage.records_returned} returned records · All ${coverage.paths_returned} returned paths`;
      byId("localSummaryCoverage").textContent = summary.status === "fallback"
        ? "Counts cover all returned evidence. A query-wide model interpretation did not complete."
        : summary.status === "pending"
          ? "All returned records and every path hop are included across the evidence batches. Large property values may be compacted."
          : `Model context: ${coverage.records_included} of ${coverage.records_returned} returned records · ${coverage.paths_included} of ${coverage.paths_returned} returned paths. Every path hop is included. Large property values may be compacted.`;
    }
  }

  function progress(payload) {
    if (!current || payload.stage !== "summarizing") return;
    if (payload.message) byId("localSummaryMessage").textContent = payload.message;
    if (payload.coverage) {
      current = {...current, coverage:payload.coverage};
      byId("localSummaryCoverage").textContent = `Evidence batches reviewed: ${payload.coverage.batches_processed} of ${payload.coverage.batches_total}. The summary covers the entire query, independently of the expanded path.`;
    }
  }

  function unavailable() {
    if (!current) return;
    render({...current, status:"fallback", provider:"deterministic", findings:[],
      message:"Local summary generation did not complete. Query evidence remains available below."});
  }

  return {clear, render, progress, unavailable};
})();
