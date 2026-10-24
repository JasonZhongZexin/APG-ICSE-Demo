# -*- coding: utf-8 -*-
"""
SchemaGuard CLI for CodeGraphHunter

Validate a Cypher / CALL query against:
  - normalized_schema.v1.json
  - tugraph_cypher_capabilities.v1.json

Usage:
  python3 -m codegraphhunter.catalog.schema_guard_cli \
    --query 'MATCH (n:FunExitICFGNode {id: 1}) RETURN n LIMIT 3'

Optional:
  --schema /path/to/normalized_schema.v1.json
  --caps   /path/to/tugraph_cypher_capabilities.v1.json
  --strict-unknown-functions
"""

import argparse
import json
import os
import sys
from pathlib import Path

from codegraphhunter.catalog.schema_guard import SchemaGuard

# ---------------------------------------------------------
# Resolve default paths from config.py (preferred)
# ---------------------------------------------------------
def _resolve_default_paths():
    try:
        from codegraphhunter.config import (
            NORMALIZED_SCHEMA_OUT_PATH,
            TUGRAPH_CYPHER_CAPABILITIES_PATH,
        )
        return str(NORMALIZED_SCHEMA_OUT_PATH), str(TUGRAPH_CYPHER_CAPABILITIES_PATH)
    except Exception:
        # Fallback: resolve relative to project root
        this_file = Path(__file__).resolve()
        project_root = this_file.parents[2]
        schema = project_root / "codegraphhunter" / "resources" / "normalized_schema.v1.json"
        caps = project_root / "codegraphhunter" / "datasource" / "tugraph_cypher_capabilities.v1.json"
        return str(schema), str(caps)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="CodeGraphHunter SchemaGuard CLI"
    )

    parser.add_argument(
        "--query",
        required=True,
        help="Cypher query to validate (MATCH / CALL ...)"
    )

    parser.add_argument(
        "--schema",
        default=None,
        help="Path to normalized_schema.v1.json (optional)"
    )

    parser.add_argument(
        "--caps",
        default=None,
        help="Path to tugraph_cypher_capabilities.v1.json (optional)"
    )

    parser.add_argument(
        "--strict-unknown-functions",
        action="store_true",
        default=False,
        help="Treat unknown functions as unsupported (recommended)"
    )

    args = parser.parse_args(argv)

    default_schema, default_caps = _resolve_default_paths()
    schema_path = args.schema or default_schema
    caps_path = args.caps or default_caps

    # Sanity check paths
    if not os.path.exists(schema_path):
        print("ERROR: normalized schema not found:", schema_path, file=sys.stderr)
        return 2
    if not os.path.exists(caps_path):
        print("ERROR: cypher capabilities not found:", caps_path, file=sys.stderr)
        return 2

    guard = SchemaGuard(
        normalized_schema_path=schema_path,
        cypher_caps_path=caps_path,
        strict_unknown_functions=args.strict_unknown_functions,
    )

    result = guard.validate(args.query)

    print("=== CodeGraphHunter SchemaGuard ===")
    print("Schema :", schema_path)
    print("Caps   :", caps_path)
    print("Query  :", args.query)
    print("----------------------------------")
    print(json.dumps(result, indent=2, sort_keys=True))

    # exit code: 0 = ok, 2 = validation error
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
