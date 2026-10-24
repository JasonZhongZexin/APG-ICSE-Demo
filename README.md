# PAG Subgraph Summary Demo

This repository provides a small Python script that demonstrates how to summarize the
information flow inside a PAG (Program Assignment Graph) subgraph using OpenAI's
ChatGPT API.

## Requirements

* Python 3.9+
* `openai` Python SDK (`pip install openai`)
* An OpenAI API key exposed as the `OPENAI_API_KEY` environment variable

## Files

* `pag_summary_demo.py` — CLI that loads a PAG subgraph JSON file, retrieves
  supporting node and edge definitions, and requests an LLM summary.
* `sample_graph.json` — Example PAG subgraph that walks through an argument binding,
  heap write, heap read, and return flow.
* `sample_definitions.json` — Rich metadata for PAG node and edge types, including
  inheritance, alias normalization, and reasoning hints used as RAG context.

## Usage

Install dependencies:

```bash
pip install -r requirements.txt
```

Export your API key (replace `...` with your value):

```bash
export OPENAI_API_KEY=...
```

Run the demo with the sample inputs:

```bash
python pag_summary_demo.py sample_graph.json sample_definitions.json
```

You can also specify a different OpenAI model:

```bash
python pag_summary_demo.py sample_graph.json sample_definitions.json --model gpt-4.1-mini
```

The script prints a model-friendly graph structure and the generated summary.

## Local web interface

The current graph-guided localizer includes a local web interface. A user only
needs to select an LLVM bitcode file and a problem-statement file. The server
imports the bitcode into TuGraph, infers the source repository from the bitcode
path, and then starts fault localization and graph retrieval.

Configure credentials and start the server from the repository root:

```bash
export OPENAI_API_KEY=...
export TUGRAPH_BOLT_PASS=...
export GRAPHDB_WPA=/path/to/graphdb-wpa
python -m codegraphhunter.web.app
```

Open <http://127.0.0.1:8765>. Keep the service bound to localhost: its file
picker intentionally browses paths on the machine running CodeGraphHunter.

To record the demonstration, open **APR** at <http://127.0.0.1:8765/icse-demo>.
It runs the current workspace's APR pipeline with real GPT information planning,
evidence summary refinement, and patch generation. Open
**APR settings** using the gear button available on every APR page, or visit
<http://127.0.0.1:8765/icse-demo/settings>, to configure
the GPT API key and TuGraph Bolt URI, username, and password, then select
**Save and return to APR**. Subsequent APR runs and live graph queries
use those settings. Empty secret fields keep saved credentials, or use server
credentials when no settings have been saved. Credentials are masked and kept
in server memory for this browser session (up to eight hours of inactivity);
restarting the server clears them. Only an opaque HttpOnly session cookie is
stored in the browser. The server environment and `/demo` defaults are unchanged.
The APR header shows separate labeled lights for Graph DB and OpenAI API key.
Each light is green when that setting is configured and red when it is missing,
with a link to configure missing settings.
Runner version and executable information appear only on the settings page.

Each **Run APR** opens a GPT model dropdown, initially set to `gpt-4o`.
Model options arrive with the page, so opening the dropdown requires no
additional model-list request.
Confirming starts that experiment with the selected model for information
planning, summary generation, and patch generation, overriding all three model
environment settings for that run. Canceling starts no experiment. Model
selection resets to `gpt-4o` before each run and does not change saved settings.
The list contains general GPT text models in the
[OpenAI model directory](https://developers.openai.com/api/docs/models/all),
checked on 2026-10-24, excluding retired models and audio/image/ChatGPT aliases.
For reasoning models, a child-process SDK adapter removes unsupported sampling
parameters and provides room for reasoning tokens. APR requires a model-generated
patch: a repair sketch remains prompt context and cannot bypass the GPT request.
The result shows attempted/completed GPT calls; **GPT call records** contains
per-stage response IDs, request IDs, returned models, token usage, and errors.
No prompts, API keys, passwords, or response text are included in this call audit.
`/demo` continues to use its original v1.2.2 runner. APR's runner is
`codegraphhunter/web/apr_runner.py`; an explicit server-side override may be set
with `CODEGRAPHHUNTER_APR_RUNNER`.

The APR workspace has independent query pages at `/icse-demo/query/icfg`,
`/icse-demo/query/pag`, and `/icse-demo/query/callgraph`. All three query
pages have a GPT model dropdown before **Run constrained query**, initially set
to `gpt-4o`. Each request uses that selection without changing server defaults
or saved credentials. The result shows the model used. Each page generates
guarded Cypher and retrieves actual records from the selected TuGraph instance.
The selected TuGraph instance must already contain the `PAG`, `ICFG`, and
`CallGraph` databases used by the runner. Restart an already-running web server
after updating the Python routes.

To run the prepared cpp-httplib embedded-NUL repair demonstration, open
<http://127.0.0.1:8765/demo> and select **Run full demo**. The button executes
`/tmp/cgh_demo_cpphttplib_full_patch.sh`, which performs graph-guided
localization, generates and applies a patch in an isolated worktree, and runs
the post-patch validation. The page copies and displays these allowlisted
outputs from `/tmp/cgh_demo_cpphttplib_final`:

* `final_patch.diff` (automatically opened with diff highlighting)
* `pipeline_key.log`
* `pipeline_full.log`
* `pre_patch_test.log`
* `post_patch_test.log`
* supported summary and diagnostic files below `debug/`, including per-step
  local summaries, attempt-level global summaries, fault localization, and
  diagnosis artifacts

The recursive `debug/` collector accepts only JSON, text, Markdown, patch,
diff, and log files. It rejects symlinks, paths outside the declared directory,
overly deep trees, oversized files, and executable scripts.

Override the fixed paths with `CODEGRAPHHUNTER_DEMO_SCRIPT`,
`CODEGRAPHHUNTER_DEMO_CASE`, `CODEGRAPHHUNTER_DEMO_PROBLEM`, and
`CODEGRAPHHUNTER_DEMO_OUTPUT` when testing another prepared script.

The demo navigation also includes English-language query pages for each
graph:

* <http://127.0.0.1:8765/query/icfg>
* <http://127.0.0.1:8765/query/pag>
* <http://127.0.0.1:8765/query/callgraph>

Each page uses a constrained natural-language-to-Cypher workflow grounded in
the selected graph's schema, with live backends at `POST /api/query/icfg`,
`POST /api/query/pag`, and `POST /api/query/callgraph`.

The PAG backend supports node lookup, incoming/outgoing statement edges, and
bounded reachability (at most four hops and 50 records). Filters include node
id, stored `cg_node_id`, argument number, associated ICFG id, source-location
substring, and `val_name` for labels that own that property. Numeric function
association fields must use the values actually stored in PAG; do not infer
them from another graph's numbering. For named function arguments, the backend
first queries `FunObjVar.val_name` and parses `all_args_node_ids`, then executes
the guarded argument/value-flow query with the resolved ids. Both queries must
pass validation; function matches and argument seeds are also bounded.
The response contains the executed primary Cypher, bound parameters, optional
`lookup_queries`, records, model and guard metadata. The graph viewer displays
direct statement edges and expandable path lists with every intermediate hop. An empty
result or query failure never falls back to sample records.

The CallGraph backend asks the configured OpenAI model for a strict JSON query
plan, not arbitrary executable Cypher. It then compiles parameterized Cypher
from fixed CallGraph templates and requires both `SchemaGuard` and `QueryGuard`
to pass before querying TuGraph. The normalized schema, TuGraph Cypher
capabilities, and inheritance files are loaded from the project. The current
`supported_ops.v1.json` is empty, so the response reports it as an inactive
auxiliary constraint rather than silently treating it as valid policy.

The backend uses the existing `OPENAI_API_KEY`, `CODEGRAPHHUNTER_LLM_MODEL`,
`TUGRAPH_BOLT_URI`, `TUGRAPH_BOLT_USER`, and `TUGRAPH_BOLT_PASS` settings. It
connects to the `CallGraph`, `ICFG`, and `PAG` databases by default; override
their names with `CODEGRAPHHUNTER_CALLGRAPH_DB`, `CODEGRAPHHUNTER_ICFG_DB`, and
`CODEGRAPHHUNTER_PAG_DB`. PAG query timeout is configurable through
`CODEGRAPHHUNTER_PAG_TIMEOUT_S` (1–120 seconds, default 30).

The source root is inferred from the nearest parent Git repository. For a
bitcode artifact stored elsewhere, set `CODEGRAPHHUNTER_SOURCE_ROOT` before
starting the server. Optional run limits can be configured through
`CODEGRAPHHUNTER_WEB_MAX_ITERATIONS`, `CODEGRAPHHUNTER_WEB_MAX_QUERIES`, and
`CODEGRAPHHUNTER_WEB_MAX_REPLANS`.

Each run exposes its generated stage summaries, per-step local summaries,
global summary, localization result, and diagnosis files in the web interface.
By default these artifacts are stored below
`/tmp/codegraphhunter_web_artifacts`; set `CODEGRAPHHUNTER_WEB_ARTIFACT_ROOT`
to use a persistent location.

### Named query capabilities

`/icse-demo/query/capabilities` (also `/query/capabilities`) provides 45 named
capabilities: source/symbol search (5), CallGraph (8), ICFG (8), PAG (12),
CHG/type metadata (5), and cross-graph evidence (7). On every query page the
selected model automatically chooses a capability from the user's request.
Each graph page limits selection to that graph's capabilities; the capability
workspace includes all 45. The result identifies the model's choice.
All APR pages link to the capability workspace.
The catalog is available at `GET /api/codegraphhunter/capabilities`.

`PAG.POINTS_TO` and `PAG.PARAMETER_ALIAS` compute pointer facts from read-only,
paged scans of the exported PAG, followed by worklist iteration in the backend.
Ask, for example, `What objects can argument 0 of has_crlf point to?` or
`May argument 0 of has_crlf and argument 0 of is_valid_path alias?`.
The planner resolves separate function names/IDs and argument indices for both
operands; explicit PAG value IDs are also accepted. Argument indices are
exported zero-based LLVM indices, which may include hidden C++ parameters.

This Andersen-style model is flow-insensitive, context-insensitive and
field-insensitive: GEP objects share their base allocation. Address, copy,
load/store, Phi/Select operands, actual/formal and return constraints propagate
points-to sets to a fixed point. Missing inputs, undecoded conversions and
external effects introduce unknowns. This does not reproduce every SVF analysis
or infer runtime aliasing. Call/return propagation uses exported bindings;
the solver does not reconstruct new indirect-call targets. `MayAlias` means a common concrete abstract object;
`NoAlias` requires a fully loaded, converged exported model and nonempty,
disjoint sets without unknown objects. Otherwise the answer is `Unknown`.
The existing `PAG.ALIAS_APPROX` remains a separate connectivity-witness query.

The result reports inferred object IDs/source locations, alias decisions and
solver completeness in `analysis.pointer_analysis`. Derived points-to facts
appear in the records; the graph identifies the values and objects without
inventing stored PAG edges. Display limits never truncate constraint solving,
and the form's hop limit is not used to approximate fixed-point iteration.
Time/resource exhaustion reports partial results and disables negative alias
conclusions. `CODEGRAPHHUNTER_PTA_TIMEOUT_S` sets the whole pointer-analysis
stage's time limit (1–120 seconds; defaults to the explicitly configured
capability timeout, or 120 seconds). Other capability queries retain their
30-second default. Solver budgets are configurable with `CODEGRAPHHUNTER_PTA_MAX_NODES`
(250000), `CODEGRAPHHUNTER_PTA_MAX_EDGES` (1000000),
`CODEGRAPHHUNTER_PTA_MAX_FACTS` (10000000, up to 50000000),
`CODEGRAPHHUNTER_PTA_MAX_DERIVED_EDGES` (1000000), and
`CODEGRAPHHUNTER_PTA_MAX_BITSET_BYTES` (268435456, 256 MiB).
Points-to sets propagate as compact bitsets; object source text is fetched only
for displayed objects.

Every query page displays a `Capability used` card below the input form,
showing the selected ID, name and current status as soon as planning completes.
The `Help` button opens an English guide for that page's capabilities, with
example questions, expected outputs and approximation notes. Search the guide
or select an example to fill the query box; this does not run the query or
override automatic capability selection. `View capability help` opens the
guide at the capability selected for the current query.

Every nonempty capability query also generates an English `Local summary`
using the model selected for that query and the browser's saved API key.
The summary covers all records and all paths returned by the query, independently
of the path currently expanded in the viewer. It uses the question, typed paths,
graph semantics and query limitations, with evidence references and counts.
Large results are processed in bounded batches and combined into one query-wide
summary. Long paths are split into contiguous fragments covering every hop;
large property values may be compacted with explicit omission markers.
Every returned path is listed with its hop count and expands to its complete
graph. Partial-result limitations remain visible.

The stream delivers query evidence before the summary call, so paths and
records remain usable while summarization runs. Empty results skip that call.
An unavailable model summary falls back to an explicitly labeled deterministic
overview without discarding the query results. Small nonempty queries add one
summary call; larger queries use batch calls followed by summary merging.
`CODEGRAPHHUNTER_QUERY_SUMMARY_TIMEOUT_S` sets each call's time limit
(default 60 seconds, range 5–120), and
`CODEGRAPHHUNTER_QUERY_SUMMARY_TOTAL_TIMEOUT_S` bounds the entire summary stage
(default 180 seconds, range 5–600). Calls do not retry automatically.
An interrupted summary reports incomplete model coverage explicitly.
The response exposes the summary in `local_summary`.

The workflow shows schema grounding, capability matching, guarded query
preparation, capability application and the result stage using server progress
events. Matching and application remain visibly active while their work runs.

The selected GPT model produces a strict capability/parameter plan. The server
executes only its parameterized templates after both guards pass. The form's
depth and result limits apply to that plan; blank depth is unbounded and blank
result count is 1000. Traversals share a 30-second deadline by default, set by
`CODEGRAPHHUNTER_CAPABILITY_TIMEOUT_S` (1–120). Partial results explain their
scope. Paths list their hop counts and expand to show all physical edges.
Cycle queries return representative cycle witnesses, rather than every cycle.

Set `CODEGRAPHHUNTER_SOURCE_ROOT` to the original C/C++ repository for source
and localization queries. CHG defaults to the `CHG` database and type metadata
to `SVFType`; override these with `CODEGRAPHHUNTER_CHG_DB` and
`CODEGRAPHHUNTER_TYPE_DB`. Missing databases return `requires_graph`.
The APR workspace uses the current browser's saved DB/API settings and model.

Alias, unreachable-code, sanitizer, override and localization results are
explicitly approximate. Graph connectivity does not establish execution
feasibility, dominance, a complete points-to solution, or a vulnerability.
See [query_capabilities.txt](query_capabilities.txt) for all contracts,
examples, data requirements and verification scope. The original 16 query
intents remain available through their existing API endpoints.

## Definition File Format

The definition JSON provides more than basic descriptions. It includes:

* `inheritance` — Parent/child relationships for node and edge types so the demo can
  surface lineage information.
* `edge_types` / `node_types` — Detailed metadata (description, value-flow rules,
  common attributes, etc.) keyed by `name`.
* `alias_map` — Canonicalization hints that normalize alternate spellings before
  lookup.
* `reasoning_hints` — High-level guidance that is appended to the LLM context.

When you supply your own definition file, keep the same schema so the script can
assemble comprehensive retrieval snippets for the language model.
