from pathlib import Path
import os

# project root dir
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# graph db schema dir
DBSCHEMA_DIR = PROJECT_ROOT / "codegraphhunter" / "datasource" / "DBSchema"

# resources dir
RESOURCES_DIR = PROJECT_ROOT / "codegraphhunter" / "resources"

SCHEMA_VERSION = "svf_graphdb.v1"

INHERITANCE_LABELS_PATH = (
    PROJECT_ROOT
    / "codegraphhunter"
    / "datasource"
    / "inheritance_labels.v1.json"
)

NORMALIZED_SCHEMA_OUT_PATH = Path(
    os.environ.get(
        "CODEGRAPHHUNTER_NORMALIZED_SCHEMA_OUT_PATH",
        str(RESOURCES_DIR / "normalized_schema.v1.json"),
    )
)

TUGRAPH_CYPHER_CAPABILITIES_PATH = (
    PROJECT_ROOT / "codegraphhunter" / "datasource" / "tugraph_cypher_capabilities.v1.json"
)