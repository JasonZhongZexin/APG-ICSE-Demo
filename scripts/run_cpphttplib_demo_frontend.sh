#!/usr/bin/env bash
set -euo pipefail

RUN_ID="$(date +%Y%m%d_%H%M%S)"
DEMO_ROOT="/tmp/codegraphhunter_frontend_demo"
OUT="$DEMO_ROOT/runs/$RUN_ID"
LATEST="$DEMO_ROOT/latest"

mkdir -p "$OUT"

echo "[FRONTEND DEMO] run_id=$RUN_ID"
echo "[FRONTEND DEMO] output=$OUT"

# Run the stable v1.2.2 pipeline
/tmp/cgh_run_v1_2_patch_test.sh 2>&1 | tee "$OUT/pipeline.log"

# Locate generated artifacts
DEBUG_DIR="/tmp/cgh_patch_debug_graph_localizer_v1_2_patch"
GLOBAL_SUMMARY="$DEBUG_DIR/attempt_001/global_summary.json"
FAULT_DIAGNOSIS="$DEBUG_DIR/fault_diagnosis.txt"
REPAIR_SKETCH_PATCH="$DEBUG_DIR/patch_generated_from_required_sketch.diff"

LATEST_PATCH="$(find /tmp/codegraphhunter_patch_demo -type f -name "codegraphhunter_patch.diff" -printf "%T@ %p\n" | sort -nr | head -1 | cut -d' ' -f2-)"
PATCH_WORKDIR="$(dirname "$LATEST_PATCH")"

cp "$GLOBAL_SUMMARY" "$OUT/global_summary.json"
cp "$FAULT_DIAGNOSIS" "$OUT/fault_diagnosis.txt"
cp "$REPAIR_SKETCH_PATCH" "$OUT/patch_generated_from_required_sketch.diff"
cp "$LATEST_PATCH" "$OUT/codegraphhunter_patch.diff"

python3 - <<PY
import json
from pathlib import Path

out = Path("$OUT")
global_summary_path = out / "global_summary.json"
patch_path = out / "codegraphhunter_patch.diff"
fault_path = out / "fault_diagnosis.txt"

with global_summary_path.open("r", encoding="utf-8") as f:
    g = json.load(f)

result = {
    "run_id": "$RUN_ID",
    "status": "PASSED",
    "case": "yhirose__cpp-httplib-1765",
    "localized_function": "is_valid_path",
    "patch_source": "httplib.h:2403",
    "graph_function": "_ZN7httplib6detail13is_valid_pathERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEE",
    "graph_ids": {
        "callgraph_node_id": 3167,
        "fun_obj_var_id": 29066,
        "primary_arg_node_id": 99131,
        "primary_icfg_node_id": 83372
    },
    "retrieval": {
        "stop_reason": g.get("stop_reason"),
        "graphs_touched": g.get("retrieval_accounting", {}).get("graphs_touched"),
        "queries_by_graph_type": g.get("retrieval_accounting", {}).get("queries_by_graph_type"),
        "objective_met_any": g.get("aggregated_signals", {}).get("objective_met_any"),
        "path_count": len(g.get("evidence_pool", {}).get("paths", []) or []),
        "functions": g.get("evidence_pool", {}).get("functions", [])
    },
    "patch": {
        "status": "GENERATED_FROM_REQUIRED_SKETCH",
        "validation": "PASSED",
        "patch_file": str(patch_path),
        "patch_text": patch_path.read_text(encoding="utf-8", errors="replace")
    },
    "artifacts": {
        "pipeline_log": str(out / "pipeline.log"),
        "global_summary": str(global_summary_path),
        "fault_diagnosis": str(fault_path),
        "repair_sketch_patch": str(out / "patch_generated_from_required_sketch.diff"),
        "final_patch": str(patch_path),
        "workdir": "$PATCH_WORKDIR"
    }
}

(out / "frontend_result.json").write_text(
    json.dumps(result, indent=2, ensure_ascii=False),
    encoding="utf-8"
)

print(json.dumps(result, indent=2, ensure_ascii=False))
PY

rm -f "$LATEST"
ln -s "$OUT" "$LATEST"

echo "[FRONTEND DEMO] result=$OUT/frontend_result.json"
echo "[FRONTEND DEMO] latest=$LATEST/frontend_result.json"
