from __future__ import annotations

from collections import defaultdict

from dts_models import DTSNode, Dependency


def iter_nodes(node: DTSNode):
    yield node
    for child in node.children:
        yield from iter_nodes(child)


def resolve_dependency_target(
    dep: Dependency,
    node_by_path: dict[str, DTSNode],
    node_by_label: dict[str, DTSNode],
) -> str | None:
    target = dep.target.strip()
    if not target:
        return None
    if target.startswith("/"):
        return target if target in node_by_path else None
    if target.startswith("&"):
        label = target[1:]
        node = node_by_label.get(label)
        return node.path if node else None

    node = node_by_label.get(target)
    if node:
        return node.path
    if target in node_by_path:
        return target
    return None


def rebuild_indexes(
    dts_root: DTSNode | None,
    node_by_path: dict[str, DTSNode],
    node_by_label: dict[str, DTSNode],
    allow_self_loops: bool,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, list[str]]]:
    if dts_root is None:
        return {}, {}, {}

    dep_graph: dict[str, list[str]] = defaultdict(list)
    rev_graph: dict[str, list[str]] = defaultdict(list)
    comp_index: dict[str, list[str]] = defaultdict(list)

    for node in iter_nodes(dts_root):
        seen_targets: set[str] = set()
        for dep in node.dependencies:
            resolved = resolve_dependency_target(dep, node_by_path, node_by_label)
            if resolved is None:
                continue
            if not allow_self_loops and resolved == node.path:
                continue
            if resolved in seen_targets:
                continue
            seen_targets.add(resolved)
            dep_graph[node.path].append(resolved)
            rev_graph[resolved].append(node.path)

        for comp in node.compat:
            c = comp.strip().lower()
            if c:
                comp_index[c].append(node.path)

    return (
        {k: sorted(v) for k, v in dep_graph.items()},
        {k: sorted(v) for k, v in rev_graph.items()},
        {k: sorted(v) for k, v in comp_index.items()},
    )


def collect_subgraph(dependency_graph: dict[str, list[str]], root_path: str, depth: int) -> dict[str, list[str]]:
    if depth < 1:
        depth = 1

    out: dict[str, list[str]] = {}
    frontier: list[tuple[str, int]] = [(root_path, 0)]
    seen = {(root_path, 0)}

    while frontier:
        current, dist = frontier.pop(0)
        deps = dependency_graph.get(current, [])
        out[current] = list(deps)
        if dist >= depth:
            continue

        for dep in deps:
            state = (dep, dist + 1)
            if state in seen:
                continue
            seen.add(state)
            frontier.append(state)

    return out


def status_is_okay(node: DTSNode | None) -> bool:
    if node is None:
        return False
    status = node.properties.get("status", "okay")
    if isinstance(status, bool):
        return bool(status)
    return str(status).strip().lower() == "okay"


def is_deferred_init(node: DTSNode | None) -> bool:
    if node is None:
        return False
    val = node.properties.get("zephyr,deferred-init")
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "y"}
