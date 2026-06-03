from __future__ import annotations

from dts_graph import is_deferred_init, iter_nodes, status_is_okay
from dts_models import DTSNode


def node_display_name(node: DTSNode) -> str:
    if node.name == "/":
        return "/"
    if node.label:
        return f"{node.name} ({node.label})"
    return node.name


def node_summary_text(node: DTSNode) -> str:
    parent_path = node.parent.path if node.parent else "<none>"
    compat = ", ".join(node.compat) if node.compat else "<none>"
    return "\n".join(
        [
            f"Name: {node.name}",
            f"Path: {node.path}",
            f"Label: {node.label or '<none>'}",
            f"Parent: {parent_path}",
            f"Children: {len(node.children)}",
            f"Compatible: {compat}",
            f"Line/Ordinal: {node.line_start}",
        ]
    )


def node_matches(node: DTSNode, query: str, only_okay: bool) -> bool:
    if only_okay:
        status = node.properties.get("status")
        if isinstance(status, str) and "okay" not in status:
            return False

    if not query:
        return True

    q = query.lower()
    haystacks = [
        node.name.lower(),
        node.path.lower(),
        (node.label or "").lower(),
        " ".join(node.compat).lower(),
        " ".join(node.properties.keys()).lower(),
    ]

    prop_vals = []
    for key, value in node.properties.items():
        prop_vals.append(f"{key}={value}")
    haystacks.append(" ".join(prop_vals).lower())

    return any(q in h for h in haystacks)


def collect_matching_paths(root: DTSNode | None, query: str, only_okay: bool) -> set[str]:
    if root is None:
        return set()

    matched = set()
    for node in iter_nodes(root):
        if node_matches(node, query, only_okay):
            matched.add(node.path)
            parent = node.parent
            while parent is not None:
                matched.add(parent.path)
                parent = parent.parent
    return matched


def build_dependency_items(node: DTSNode, resolve_target) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for dep in node.dependencies:
        dep_key = (dep.property, dep.dep_type, dep.target)
        if dep_key in seen:
            continue
        seen.add(dep_key)

        resolved = resolve_target(dep)
        if resolved is None:
            label = f"{dep.dep_type:10s} {dep.property:20s} -> {dep.target} (unresolved)"
            jump_target = ""
        else:
            label = f"{dep.dep_type:10s} {dep.property:20s} -> {resolved}"
            jump_target = resolved
        rows.append((label, jump_target))
    return rows


def build_consumer_items(node_path: str, reverse_graph: dict[str, list[str]], node_by_path: dict[str, DTSNode]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for consumer_path in reverse_graph.get(node_path, []):
        consumer = node_by_path.get(consumer_path)
        if consumer is None:
            display = consumer_path
        elif consumer.label:
            display = f"{consumer.name} ({consumer.label})  [{consumer.path}]"
        else:
            display = f"{consumer.name}  [{consumer.path}]"
        rows.append((display, consumer_path))
    return rows


def build_initialization_items(node: DTSNode, resolve_target, node_by_path: dict[str, DTSNode]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if is_deferred_init(node):
        rows.append(("[NODE] deferred-init enabled", node.path))
    else:
        rows.append(("[NODE] deferred-init disabled", node.path))

    for dep in node.dependencies:
        resolved = resolve_target(dep)
        if resolved is None:
            rows.append((f"[MISSING ] {dep.dep_type:10s} {dep.property:20s} -> {dep.target}", ""))
            continue

        target_node = node_by_path.get(resolved)
        if is_deferred_init(target_node):
            state = "DEFERRED"
        elif status_is_okay(target_node):
            state = "OK"
        else:
            state = "DISABLED"

        rows.append((f"[{state:8s}] {dep.dep_type:10s} {dep.property:20s} -> {resolved}", resolved))

    return rows


def build_graph_labels(paths: list[str], node_by_path: dict[str, DTSNode]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for path in paths:
        node = node_by_path.get(path)
        if node is None:
            labels[path] = path
        elif node.label:
            labels[path] = f"{node.name}\n({node.label})"
        else:
            labels[path] = node.name
    return labels
