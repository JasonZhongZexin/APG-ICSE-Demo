"""Assertions that a returned graph contains a real, complete path for each row."""


def assert_evidence_paths(test, result, actual_edges):
    evidence = result["evidence_graph"]
    key = lambda item: (item["label"], item["id"])
    node_keys = {key(item) for item in evidence["nodes"]}
    test.assertEqual(evidence["path_policy"], "node_simple_paths")
    test.assertEqual(len(evidence["paths"]), len(result["records"]))
    test.assertEqual(len(node_keys), len(evidence["nodes"]))
    for edge in evidence["edges"]:
        test.assertIn((key(edge["source"]), key(edge["target"]), edge["label"]), actual_edges)
        test.assertIn(key(edge["source"]), node_keys)
        test.assertIn(key(edge["target"]), node_keys)
    for index, row in enumerate(result["records"]):
        if result["graph"] == "CallGraph":
            incoming = result["plan"]["intent"] == "find_reachable_callers"
            start = ("CallGraphNode", row["caller_id"] if incoming else row["source_id"])
            end = ("CallGraphNode", row["target_id"] if incoming else row["callee_id"])
            hops = row["call_distance"]
        else:
            start = (result["plan"]["seed_label"], row["seed_id"])
            end = (result["plan"]["result_label"], row["result_id"])
            if "predecessors" in result["plan"]["intent"]:
                start, end = end, start
            hops = row["path_length"]
        path = evidence["paths"][index]
        test.assertEqual(path["record_index"], index)
        test.assertEqual(path["id"], row["path_id"])
        test.assertEqual(path["hop_count"], hops)
        test.assertEqual(len(path["nodes"]), hops + 1)
        test.assertEqual(key(path["nodes"][0]), start)
        test.assertEqual(key(path["nodes"][-1]), end)
        # Only the starting node may repeat, once, at the end of a cycle.
        test.assertEqual(len({key(node) for node in path["nodes"][:-1]}), hops)
        edges = [evidence["edges"][edge_index] for edge_index in path["edge_indices"]]
        test.assertEqual(len(edges), hops)
        for hop, edge in enumerate(edges):
            test.assertIn(index, edge["records"])
            test.assertEqual(key(edge["source"]), key(path["nodes"][hop]))
            test.assertEqual(key(edge["target"]), key(path["nodes"][hop + 1]))
        for endpoint in (start, end):
            test.assertTrue(any(key(node) == endpoint and index in node["records"] for node in evidence["nodes"]))
