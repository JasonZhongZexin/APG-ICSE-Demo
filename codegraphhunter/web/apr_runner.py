"""Run APR with the selected GPT model and publish evidence of actual calls."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from codegraphhunter.web.apr_models import DEFAULT_MODEL, model_environment

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASE = Path("/tmp/codegraphhunter_swe/yhirose__cpp-httplib-1765")


def controller_command(case, out, model):
    return [sys.executable, "-m", "codegraphhunter.controller.attempt_controller_cli",
            "--schema", str(PROJECT_ROOT / "codegraphhunter/resources/normalized_schema.v1.json"),
            "--caps", str(PROJECT_ROOT / "codegraphhunter/datasource/tugraph_cypher_capabilities.v1.json"),
            "--semantics-catalog", str(PROJECT_ROOT / "codegraphhunter/resources/graph_semantics_catalog.v1.json"),
            "--use-builder", "--use-db", "--use-graph-localizer", "--candidate-top-k", "5",
            "--use-llm-info-planner", "--info-planner-model", model,
            "--use-llm-summary", "--llm-summary-model", model,
            "--use-llm-patch", "--require-llm-patch", "--patch-model", model,
            "--patch-target-repo", str(case), "--patch-source-file", "httplib.h",
            "--patch-problem-file", str(case / ".codegraphhunter/problem_statement.txt"),
            "--patch-workdir", str(out / "workdirs"),
            "--patch-test-cmd", shlex.join(["bash", str(out / "validate.sh")]),
            "--allow_unlabeled_nodes", "--allow-wildcard-edge", "--max-iterations", "1",
            "--max-queries-total", "8", "--max-replans", "2", "--max-depth-increases", "1"]


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def patch_result(log):
    marker = "=== PATCH ATTEMPT RESULT ==="
    if marker not in log:
        return {}
    try:
        return json.JSONDecoder().raw_decode(log.rsplit(marker, 1)[1].lstrip())[0]
    except ValueError:
        return {}


def llm_report(path, model):
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    finished = [event for event in events if event.get("status") != "started"]
    started = [event for event in events if event.get("status") == "started"]
    completed = [event for event in finished if event.get("status") == "completed"]
    stages = {}
    for name in ("information_planner", "local_summary", "patch"):
        calls = [event for event in finished if event.get("stage") == name]
        stages[name] = {"completed": sum(event["status"] == "completed" for event in calls), "calls": calls}
    status = "NOT_CALLED" if not started else "FAILED" if not completed else "COMPLETED"
    if completed and (len(completed) != len(started) or not all(stage["completed"] for stage in stages.values())):
        status = "PARTIAL"
    return {"requested_model": model, "status": status, "attempted_calls": len(started),
            "completed_calls": len(completed), "stages": stages, "calls": finished}


def publish_result(out, model, return_code):
    log = (out / "pipeline.log").read_text(errors="replace")
    patch = patch_result(log)
    generation, validation = patch.get("generation", {}), patch.get("validation", {})
    summary = load_json(out / "debug/attempt_001/global_summary.json")
    localizer = load_json(out / "debug/graph_fault_localization.json")
    location = localizer.get("selected_location", {})
    llm = llm_report(out / "llm_calls.jsonl", model)
    passed = bool(return_code == 0 and patch.get("ok") and generation.get("status") == "GENERATED" and
                  generation.get("model") == model and llm["stages"]["patch"]["completed"])
    artifacts = {"run_dir": str(out), "pipeline_log": str(out / "pipeline.log"),
                 "llm_calls": str(out / "llm_calls.jsonl")}
    for name, relative in (("global_summary", "debug/attempt_001/global_summary.json"),
                           ("fault_diagnosis", "debug/fault_diagnosis.txt")):
        source = out / relative
        if source.exists():
            target = out / source.name
            shutil.copyfile(source, target)
            artifacts[name] = str(target)
    source = Path(validation.get("patch_file", ""))
    final_patch = out / "codegraphhunter_patch.diff"
    if source.is_file():
        shutil.copyfile(source, final_patch)
        artifacts["final_patch"] = str(final_patch)
    if validation.get("workdir"):
        artifacts["workdir"] = validation["workdir"]
    diagnosis = Path(artifacts["fault_diagnosis"]).read_text() if artifacts.get("fault_diagnosis") else ""
    accounting = summary.get("retrieval_accounting", {})
    result = {
        "run_id": out.name, "demo_version": "APR GPT", "model": model,
        "status": "PASSED" if passed else "FAILED", "case": "yhirose__cpp-httplib-1765",
        "bug_report": "Treat paths with embedded NUL bytes as invalid in detail::is_valid_path",
        "localized_function": location.get("function"),
        "patch_source": f"{location.get('patch_source_file') or location.get('source_file', 'httplib.h')}:{location.get('patch_start_line') or location.get('start_line', '')}",
        "graph_function": location.get("graph_function_name"),
        "graph_ids": {"callgraph_node_id": location.get("callgraph_node_id"),
                      "fun_obj_var_id": location.get("fun_obj_var_id"),
                      "primary_arg_node_id": location.get("primary_arg_node_id"), "primary_icfg_node_id": location.get("primary_icfg_node_id")},
        "retrieval": {"status": summary.get("stop_reason", "NOT_COMPLETED"), "stop_reason": summary.get("stop_reason"),
                      "graphs_touched": accounting.get("graphs_touched", []),
                      "queries_by_graph_type": accounting.get("queries_by_graph_type", {}),
                      "objective_met_any": summary.get("aggregated_signals", {}).get("objective_met_any"),
                      "path_count": len(summary.get("evidence_pool", {}).get("paths", []))},
        "diagnosis": {"status": "GENERATED" if diagnosis else "MISSING", "preview": diagnosis[:1200]},
        "patch": {"status": generation.get("status", "NOT_GENERATED"), "model": generation.get("model"),
                  "validation": validation.get("status", "NOT_RUN"),
                  "error": generation.get("error"),
                  "patch_text": final_patch.read_text() if final_patch.exists() else ""},
        "llm": llm, "artifacts": artifacts, "controller_return_code": return_code,
        "validation_replayed": bool(patch.get("validation_replayed", False)),
    }
    (out / "frontend_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def run(environ=None):
    env = dict(os.environ if environ is None else environ)
    model = env.get("CGH_APR_MODEL", DEFAULT_MODEL)
    env = model_environment(env, model)
    case = Path(env.get("CGH_DEMO_CASE", str(DEFAULT_CASE))).resolve()
    if not (case / "httplib.h").is_file() or not (case / ".codegraphhunter/problem_statement.txt").is_file():
        raise RuntimeError("APR requires the cpp-httplib source and its problem statement.")
    root = Path(env.get("CGH_DEMO_OUT_ROOT", "/tmp/codegraphhunter_frontend_demo")).resolve()
    out = root / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_apr")
    (out / "debug").mkdir(parents=True)
    env.update(CODEGRAPHHUNTER_PATCH_DEBUG_DIR=str(out / "debug"), CGH_APR_LLM_AUDIT=str(out / "llm_calls.jsonl"), PYTHONUNBUFFERED="1")
    test_source = out / "embedded_nul_test.cc"
    test_source.write_text('''#include "httplib.h"
#include <string>
int main() {
  if (!httplib::detail::is_valid_path("/tmp/codegraphhunter-demo")) return 10;
  for (auto path : {std::string("/tmp/codegraphhunter\\0demo", 25), std::string("\\0bad", 4)}) {
    if (httplib::detail::is_valid_path(path)) return 20;
  }
  return 0;
}
''')
    binary = out / "embedded_nul_test"
    (out / "validate.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" +
                                    shlex.join(["g++", "-std=c++11", "-I.", str(test_source), "-o", str(binary)]) + "\n" +
                                    shlex.quote(str(binary)) + "\n")
    print(f"[APR] model={model} output={out}", flush=True)
    with (out / "pipeline.log").open("w") as log:
        try:
            process = subprocess.run(controller_command(case, out, model), cwd=PROJECT_ROOT, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, timeout=1500, check=False)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = 124
            log.write("\n[APR] Controller exceeded its time limit.\n")
    result = publish_result(out, model, code)
    # Replace the latest link only after a complete result has been written.
    pending = root / (".latest-" + out.name)
    pending.symlink_to(out, target_is_directory=True)
    pending.replace(root / "latest")
    print(json.dumps({"run_id": result["run_id"], "status": result["status"], "model": model,
                      "llm_status": result["llm"]["status"], "completed_calls": result["llm"]["completed_calls"],
                      "patch_status": result["patch"]["status"], "validation": result["patch"]["validation"]}), flush=True)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(run())
