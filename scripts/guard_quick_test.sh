#!/usr/bin/env bash
set -euo pipefail

# Resolve project root as: parent dir of this script's directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

python3 - << 'EOF'
from codegraphhunter.catalog.schema_guard import SchemaGuard
from codegraphhunter.query.guard import QueryGuard

sg = SchemaGuard(
    normalized_schema_path="codegraphhunter/resources/normalized_schema.v1.json",
    cypher_caps_path="codegraphhunter/datasource/tugraph_cypher_capabilities.v1.json",
    strict_unknown_functions=True,
)
qg = QueryGuard(sg, policy={"max_limit": 50, "max_var_length_upper": 10})

print(qg.validate_for_execution("MATCH (n:FunExitICFGNode {id: 1}) RETURN n LIMIT 3"))
print(qg.validate_for_execution("MATCH (n:FunExitICFGNode) RETURN n"))  # should fail (LIMIT required)
print(qg.validate_for_execution("MATCH (n:FunExitICFGNode)-[:IntraCFGEdge*1..10]->(m:FunExitICFGNode) RETURN m LIMIT 3"))  # should fail depth
EOF
