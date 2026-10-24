"""Memory-bounded enumeration of distinct paths over cached one-hop reads."""

from __future__ import annotations


def iter_paths(root, neighbors, matches, *, max_depth, check_deadline):
    """Yield alternatives with a visited set local to each individual path.

    A cycle may close at its starting node but cannot expand it again. An
    iterative DFS avoids an exponentially growing queue of partial paths.
    """
    keys, relationships, visited = [root], [], {root}

    def steps(key, hop):
        # List immediate matches first. This reaches the result cap on wide
        # frontiers without querying every leaf just to enumerate its siblings.
        children = []
        for neighbor, edge in neighbors(key, hop):
            check_deadline()
            if neighbor in visited and neighbor != root:
                continue
            if matches(neighbor):
                yield False, neighbor, edge
            if neighbor != root and (max_depth is None or hop < max_depth):
                children.append((neighbor, edge))
        for neighbor, edge in children:
            yield True, neighbor, edge

    stack = [iter(steps(root, 1))]
    while stack:
        check_deadline()
        try:
            expand, neighbor, edge = next(stack[-1])
        except StopIteration:
            stack.pop()
            if relationships:
                visited.remove(keys.pop())
                relationships.pop()
            continue
        if not expand:
            yield keys + [neighbor], relationships + [edge]
            continue
        keys.append(neighbor)
        relationships.append(edge)
        visited.add(neighbor)
        stack.append(iter(steps(neighbor, len(relationships) + 1)))
