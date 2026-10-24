import random
import unittest

from codegraphhunter.web.path_search import iter_paths


class PathSearchTests(unittest.TestCase):
    def test_all_paths_match_an_independent_oracle_with_diamonds_and_cycles(self):
        rng = random.Random(734)
        for _ in range(30):
            adjacency = {a: [(b, (a, b)) for b in range(5) if rng.random() < .3] for a in range(5)}
            for depth in (1, 2, 4, None):
                expected = set()

                def walk(nodes, edges):
                    if depth is not None and len(edges) >= depth:
                        return
                    for target, edge in adjacency[nodes[-1]]:
                        if target in nodes and target != 0:
                            continue
                        next_nodes, next_edges = nodes + [target], edges + [edge]
                        if target % 2 == 0:
                            expected.add((tuple(next_nodes), tuple(next_edges)))
                        if target != 0:
                            walk(next_nodes, next_edges)

                walk([0], [])
                actual = list(iter_paths(0, lambda key, hop: adjacency[key], lambda key: key % 2 == 0,
                                         max_depth=depth, check_deadline=lambda: None))
                actual_set = {(tuple(nodes), tuple(edges)) for nodes, edges in actual}
                self.assertEqual(actual_set, expected)
                self.assertEqual(len(actual), len(actual_set))

    def test_deep_paths_do_not_use_python_recursion(self):
        paths = iter_paths(0, lambda key, hop: [(key + 1, (key, key + 1))] if key < 1200 else [],
                           lambda key: key == 1200, max_depth=None, check_deadline=lambda: None)
        nodes, edges = next(paths)
        self.assertEqual(nodes, list(range(1201)))
        self.assertEqual(len(edges), 1200)
        self.assertEqual(list(paths), [])

    def test_parallel_edges_remain_distinct_paths(self):
        paths = list(iter_paths(0, lambda key, hop: [(1, "call-site-a"), (1, "call-site-b")] if key == 0 else [],
                                lambda key: True, max_depth=None, check_deadline=lambda: None))
        self.assertEqual(paths, [([0, 1], ["call-site-a"]), ([0, 1], ["call-site-b"])])
