#!/usr/bin/env bash
set -euo pipefail

# Resolve project root as: parent dir of this script's directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

python3 - << 'EOF'
import json
d=json.load(open("codegraphhunter/resources/normalized_schema.v1.json"))
n=d["node_types"]["FunObjVar"]
print("extends:", n.get("extends"))
print("own props:", sorted(list((n.get("properties") or {}).keys()))[:50])
print("extra props:", sorted(list((n.get("extra_properties") or {}).keys()))[:50])
print("expanded props:", sorted(list((n.get("expanded_properties") or {}).keys()))[:50])
EOF
