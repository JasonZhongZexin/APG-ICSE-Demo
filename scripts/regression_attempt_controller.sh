#!/usr/bin/env bash
# scripts/regression_attempt_controller.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
CLI_MOD="codegraphhunter.controller.attempt_controller_cli"

SCHEMA_DEFAULT="codegraphhunter/resources/normalized_schema.v1.json"
CAPS_DEFAULT="codegraphhunter/datasource/tugraph_cypher_capabilities.v1.json"

SCHEMA="${SCHEMA:-$SCHEMA_DEFAULT}"
CAPS="${CAPS:-$CAPS_DEFAULT}"

fail() { echo "[FAIL] $*" >&2; exit 1; }
pass() { echo "[PASS] $*"; }

require_file() {
  local f="$1"
  [[ -f "$f" ]] || fail "Missing file: $f"
}

json_get() {
  local logfile="$1"
  local expr="$2"
  $PYTHON_BIN - <<'PY' "$logfile" "$expr"
import json,sys,re
logfile=sys.argv[1]
expr=sys.argv[2]
txt=open(logfile,'r',encoding='utf-8',errors='ignore').read()

marker = "=== FINAL STATE (summary) ==="
idx = txt.rfind(marker)
if idx < 0:
    print("")
    sys.exit(0)

tail = txt[idx + len(marker):]
m = re.search(r"\{.*\}", tail, re.S)
if not m:
    print("")
    sys.exit(0)

d = json.loads(m.group(0))
print(eval(expr, {"__builtins__":{}}, {"d":d}))
PY
}

run_case() {
  local name="$1"; shift
  local logfile="$ROOT/.regtest_${name}.log"

  echo "========== CASE: $name ==========" >&2

  set +e
  $PYTHON_BIN -m "$CLI_MOD" "$@" >"$logfile" 2>&1
  local rc=$?
  set -e

  echo "log: $logfile (rc=$rc)" >&2

  if [[ $rc -ne 0 ]]; then
    echo "---- tail($logfile) ----" >&2
    tail -n 160 "$logfile" >&2 || true
    exit 1
  fi

  echo "$logfile"
  return 0
}

run_case_expect_fail() {
  local name="$1"; shift
  local logfile="$ROOT/.regtest_${name}.log"

  echo "========== CASE: $name (expect fail) ==========" >&2

  set +e
  $PYTHON_BIN -m "$CLI_MOD" "$@" >"$logfile" 2>&1
  local rc=$?
  set -e

  [[ $rc -ne 0 ]] || fail "case=$name expected failure but rc=0"
  echo "$logfile"
}

require_file "$SCHEMA"
require_file "$CAPS"

echo "=== Regression test: AttemptController (Patch-Level Iteration) ==="
echo "ROOT  : $ROOT"
echo "PY    : $PYTHON_BIN"
echo "SCHEMA: $SCHEMA"
echo "CAPS  : $CAPS"
echo

# --------------------------
# CASE 0: schema required
# --------------------------
LOG0="$(run_case_expect_fail schema_required \
  --schema "$ROOT/not_exist.json" \
  --caps "$CAPS" \
  --max-iterations 1
)"
grep -qi "normalized_schema" "$LOG0" || fail "schema_required failed"
pass "CASE 0: schema required enforced"

# --------------------------
# CASE 1: smoke (legacy)
# --------------------------
LOG1="$(run_case smoke \
  --schema "$SCHEMA" \
  --caps "$CAPS" \
  --max-iterations 1 \
  --max-queries-total 5 \
  --max-replans 1 \
  --seed 0
)"
grep -q "=== FINAL STATE (summary) ===" "$LOG1" || fail "smoke missing FINAL STATE"
GS_ID="$(json_get "$LOG1" 'd["artifacts"]["global_summary"].get("global_summary_id") if d["artifacts"].get("global_summary") else ""')"
[[ "$GS_ID" == "GS-0001" ]] || fail "global summary missing or wrong id: $GS_ID"

GS_FIX_CTX="$(json_get "$LOG1" '"markdown" in d["artifacts"]["global_summary"]["fix_context"]')"
[[ "$GS_FIX_CTX" == "True" ]] || fail "global summary missing fix_context.markdown"
pass "CASE 1: smoke"

# --------------------------
# CASE 2: replan via guard error injection
# --------------------------
LOG2="$(run_case replan_guard \
  --schema "$SCHEMA" \
  --caps "$CAPS" \
  --max-iterations 1 \
  --max-queries-total 10 \
  --max-replans 1 \
  --inject-guard-error \
  --seed 0
)"
grep -q "\[GUARD ERROR\]" "$LOG2" || fail "replan_guard missing GUARD ERROR"
STOP2="$(json_get "$LOG2" 'd.get("stop_reason")')"
[[ "$STOP2" == "REPLAN_LIMIT_REACHED" || "$STOP2" == "QUERY_BUDGET_EXHAUSTED" || "$STOP2" == "OBJECTIVE_MET" ]] || fail "replan_guard unexpected stop_reason=$STOP2"
pass "CASE 2: replan_guard"

# --------------------------
# CASE 3: deepen path
# --------------------------
LOG3="$(run_case deepen \
  --schema "$SCHEMA" \
  --caps "$CAPS" \
  --max-iterations 1 \
  --max-queries-total 10 \
  --max-replans 0 \
  --default-depth 4 \
  --max-depth 8 \
  --depth-step 2 \
  --seed 0
)"
grep -q "\[DEEPEN\]" "$LOG3" || fail "deepen missing marker"
pass "CASE 3: deepen path works"

# --------------------------
# CASE 4: query budget exhausted report
# --------------------------
LOG4="$(run_case query_budget_exhausted_report \
  --schema "$SCHEMA" \
  --caps "$CAPS" \
  --max-iterations 1 \
  --max-queries-total 1 \
  --max-replans 3 \
  --seed 0
)"
grep -q "QUERY_BUDGET_EXHAUSTED" "$LOG4" || fail "expected QUERY_BUDGET_EXHAUSTED in stop report"
STOP4="$(json_get "$LOG4" 'd.get("stop_reason")')"
[[ "$STOP4" == "QUERY_BUDGET_EXHAUSTED" || "$STOP4" == "OBJECTIVE_MET" ]] || fail "unexpected stop_reason=$STOP4"
pass "CASE 4: query budget exhausted stop report ok"

# --------------------------
# CASE 5: builder smoke
# --------------------------
LOG5="$(run_case builder_smoke \
  --schema "$SCHEMA" \
  --caps "$CAPS" \
  --use-builder \
  --max-iterations 1 \
  --max-queries-total 5 \
  --max-replans 1 \
  --seed 0
)"
grep -q "Mode     : BUILDER" "$LOG5" || fail "builder smoke failed"
grep -q "=== FINAL STATE (summary) ===" "$LOG5" || fail "builder smoke missing FINAL STATE"
pass "CASE 5: builder smoke"

# --------------------------
# CASE 6: patch outer loop runs 2 iterations (simulated patch always fails)
# --------------------------
LOG6="$(run_case patch_outer_loop_iter_2 \
  --schema "$SCHEMA" \
  --caps "$CAPS" \
  --max-iterations 2 \
  --max-queries-total 1 \
  --max-replans 0 \
  --seed 0
)"
# We should see both Patch Iter: 1 and Patch Iter: 2
grep -q "Patch Iter: 1" "$LOG6" || fail "patch loop missing Patch Iter: 1"
grep -q "Patch Iter: 2" "$LOG6" || fail "patch loop missing Patch Iter: 2"
grep -q "\[PATCH LOOP STOP\] reached max patch iterations: 2" "$LOG6" || fail "patch loop did not stop at 2"
pass "CASE 6: patch outer loop (2 iterations) works"

# --------------------------
# CASE 7: unlimited flags parse (sanity)
# --------------------------
LOG7="$(run_case unlimited_flag_parse \
  --schema "$SCHEMA" \
  --caps "$CAPS" \
  --max-iterations 1 \
  --max-queries-total -1 \
  --max-replans -1 \
  --max-depth-increases -1 \
  --seed 0
)"
grep -q "=== FINAL STATE (summary) ===" "$LOG7" || fail "unlimited parse missing FINAL STATE"
pass "CASE 7: -1 unlimited flags accepted"

# --------------------------
# CASE 8: builder_db_smoke (optional; requires env)
# --------------------------
if [[ "${CODEGRAPHHUNTER_TEST_DB:-0}" == "1" ]]; then
  echo "========== CASE: builder_db_smoke =========="

  : "${TUGRAPH_BOLT_PASS:?Need TUGRAPH_BOLT_PASS for DB test}"

  LOG8="${ROOT}/.regtest_builder_db_smoke.log"

  set +e
  $PYTHON_BIN -m "$CLI_MOD" \
    --schema "${SCHEMA}" --caps "${CAPS}" \
    --use-builder --use-db \
    --bolt-uri "${TUGRAPH_BOLT_URI:-bolt://localhost:7687}" \
    --bolt-user "${TUGRAPH_BOLT_USER:-admin}" \
    --bolt-pass "${TUGRAPH_BOLT_PASS}" \
    --bolt-db "${TUGRAPH_BOLT_DB:-default}" \
    --print-db-debug \
    --print-db-records 1 --print-db-records-max 5 \
    --regtest-relax-traversal \
    --allow-wildcard-edge \
    --allow_unlabeled_nodes \
    --max-iterations 1 \
    --max-queries-total 10 \
    --max-replans 10 \
    > "${LOG8}" 2>&1
  rc=$?
  set -e

  echo "log: ${LOG8} (rc=$rc)" >&2

  if [[ $rc -ne 0 ]]; then
    echo "[FAIL] case=builder_db_smoke exited with rc=$rc" >&2
    echo "---- tail($LOG8) ----" >&2
    tail -n 160 "${LOG8}" >&2 || true
    exit 1
  fi

  grep -q "=== FINAL STATE (summary) ===" "${LOG8}" || fail "builder_db_smoke: expected FINAL STATE"
  grep -q "\[DB PING\]" "${LOG8}" || fail "builder_db_smoke: expected [DB PING] marker"
  grep -q "\[DB EXEC\] rows=" "${LOG8}" || fail "builder_db_smoke: expected [DB EXEC] marker"

  pass "CASE 8: builder_db_smoke (DB ping + exec markers present)"
else
  echo "========== CASE: builder_db_smoke (skipped) =========="
  echo "[SKIP] set CODEGRAPHHUNTER_TEST_DB=1 to enable"
fi

echo
echo "=== ALL REGRESSION TESTS PASSED ✅ ==="