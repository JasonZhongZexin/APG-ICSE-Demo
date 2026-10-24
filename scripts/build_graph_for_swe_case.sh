#!/usr/bin/env bash
set -euo pipefail

INSTANCE_ID="${1:-redis__redis-10095}"
SWE_ROOT="${CODEGRAPHHUNTER_SWE_ROOT:-/tmp/codegraphhunter_swe}"
CASE_DIR="${SWE_ROOT}/${INSTANCE_ID}"
CGH_DIR="${CASE_DIR}/.codegraphhunter"

GRAPHDB_WPA="${GRAPHDB_WPA:-/home/zexin/dev/svf/SVF-GraphDB/build/bin/graphdb-wpa}"

BC_PATH="${CGH_DIR}/redis-server.bc"
LOG_PATH="${CGH_DIR}/graphdb_wpa_write2db.log"

mkdir -p "${CGH_DIR}"

echo "[CASE] ${INSTANCE_ID}"
echo "[CASE_DIR] ${CASE_DIR}"
echo "[BC] ${BC_PATH}"
echo "[GRAPHDB_WPA] ${GRAPHDB_WPA}"

if [[ ! -f "${BC_PATH}" ]]; then
  echo "[INFO] ${BC_PATH} not found. Trying src/redis-server.bc ..."
  if [[ -f "${CASE_DIR}/src/redis-server.bc" ]]; then
    cp "${CASE_DIR}/src/redis-server.bc" "${BC_PATH}"
  else
    echo "ERROR: bitcode not found:"
    echo "  ${BC_PATH}"
    echo "  ${CASE_DIR}/src/redis-server.bc"
    exit 1
  fi
fi

if [[ ! -x "${GRAPHDB_WPA}" ]]; then
  echo "ERROR: graphdb-wpa not executable: ${GRAPHDB_WPA}"
  exit 1
fi

echo "[WARN] graphdb-wpa -write2db will overwrite/refresh TuGraph DB contents."
echo "[RUN] ${GRAPHDB_WPA} -ander -write2db ${BC_PATH}"

"${GRAPHDB_WPA}" \
  -ander \
  -write2db \
  "${BC_PATH}" \
  2>&1 | tee "${LOG_PATH}"

cat > "${CGH_DIR}/graph_metadata.json" <<EOF
{
  "instance_id": "${INSTANCE_ID}",
  "repo": "redis/redis",
  "base_commit": "1e25bdf7808bb400f2dc552ec0d6690d1b340d23",
  "source_file": "src/t_list.c",
  "bc_path": "${BC_PATH}",
  "graphdb_wpa": "${GRAPHDB_WPA}",
  "graph_build_log": "${LOG_PATH}",
  "graph_backend": "TuGraph",
  "graph_status": "write2db_finished"
}
EOF

echo "[DONE] Graph written to TuGraph."
echo "[META] ${CGH_DIR}/graph_metadata.json"