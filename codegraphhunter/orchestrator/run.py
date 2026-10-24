# codegraphhunter/orchestrator/run.py
from codegraphhunter.orchestrator.graph import build_app
from codegraphhunter.catalog.schema_registry import SchemaRegistry
from codegraphhunter.query.guard import SchemaGuard
from codegraphhunter.query.executor import TuGraphExecutor

def main():
    schema_registry = SchemaRegistry(mongo_uri="mongodb://localhost:27017", db="schema", col="registry")
    guard = SchemaGuard(full_schema={})
    executor = TuGraphExecutor(endpoint="http://localhost:7070", user="admin", password="pwd")

    app = build_app(schema_registry, guard, executor)

    bug_desc = "Segfault when accessing pointer x in foo()"
    code_ctx = "void foo(int* x){ *x = 1; }\n// crash when x is null"

    inp = {"bug_desc": bug_desc, "code_ctx": code_ctx, "patch": None}
    # LangGraph: state 是 RetrievalState，这里用 invoke 走完整图
    final_state = app.invoke(inp)  # 你也可以改成 stream 看每步输出

    print("DONE:", final_state.done)
    print("PATCH_ATTEMPTED:", final_state.patch_attempted)
    print("LOCAL_SUMMARIES:", list(final_state.local_summaries.keys()))
    print("GLOBAL_SUMMARY:", (final_state.global_summary.summary[:200] if final_state.global_summary else None))

if __name__ == "__main__":
    main()
