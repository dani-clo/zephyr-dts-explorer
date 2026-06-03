from __future__ import annotations

import importlib
import os
import pickle
import re
import sys
from pathlib import Path

from dts_models import Dependency, DTSNode, infer_dependency_type


class DTSParser:
    def __init__(self, text: str):
        self.text = text

    @staticmethod
    def _strip_comments(lines: list[str]) -> list[str]:
        cleaned: list[str] = []
        in_block = False

        for line in lines:
            i = 0
            out = []
            while i < len(line):
                if in_block:
                    end = line.find("*/", i)
                    if end == -1:
                        i = len(line)
                    else:
                        in_block = False
                        i = end + 2
                else:
                    start_block = line.find("/*", i)
                    if start_block == -1:
                        out.append(line[i:])
                        break
                    out.append(line[i:start_block])
                    i = start_block + 2
                    in_block = True
            cleaned.append("".join(out))
        return cleaned

    @staticmethod
    def _parse_node_header(header: str) -> tuple[str | None, str]:
        s = header.strip()
        label = None

        if s.startswith("/") and s != "/":
            return None, s

        if ":" in s:
            left, right = s.split(":", 1)
            maybe_label = left.strip()
            maybe_name = right.strip()
            if maybe_label and maybe_name:
                label = maybe_label
                s = maybe_name

        return label, s

    @staticmethod
    def _parse_property(stmt: str) -> tuple[str | None, str | bool | None]:
        raw = stmt.strip().rstrip(";").strip()
        if not raw:
            return None, None

        if raw.startswith("/delete-") or raw.startswith("/omit-if-no-ref/"):
            return None, None

        if "=" in raw:
            key, value = raw.split("=", 1)
            return key.strip(), value.strip()

        return raw, True

    @staticmethod
    def _extract_compat(value: str) -> list[str]:
        return re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', value)

    @staticmethod
    def _extract_phandles(value: str) -> list[str]:
        return re.findall(r"&([A-Za-z_][A-Za-z0-9_]*)", value)

    @staticmethod
    def _extract_phandle_paths(value: str) -> list[str]:
        return re.findall(r"&\{([^}]+)\}", value)

    def parse(self) -> DTSNode:
        lines = self.text.splitlines()
        lines = self._strip_comments(lines)

        root: DTSNode | None = None
        stack: list[DTSNode] = []
        pending_prop: list[str] = []

        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("/") and stripped.endswith(";") and "{" not in stripped:
                continue

            if stripped.startswith("};") or stripped == "}":
                if pending_prop:
                    pending_prop = []
                if stack:
                    stack.pop()
                continue

            if stripped.endswith("{"):
                header = stripped[:-1].strip()
                label, node_name = self._parse_node_header(header)

                if node_name == "/":
                    path = "/"
                elif stack and stack[-1].path == "/":
                    path = f"/{node_name}"
                elif stack:
                    path = f"{stack[-1].path}/{node_name}"
                else:
                    path = f"/{node_name}"

                unit_addr = None
                if "@" in node_name:
                    unit_addr = node_name.split("@", 1)[1]

                node = DTSNode(
                    name=node_name,
                    path=path,
                    label=label,
                    unit_addr=unit_addr,
                    line_start=idx,
                )

                if stack:
                    node.parent = stack[-1]
                    stack[-1].children.append(node)
                else:
                    root = node

                stack.append(node)
                continue

            if stack:
                pending_prop.append(stripped)
                if stripped.endswith(";"):
                    full_stmt = " ".join(pending_prop)
                    pending_prop = []

                    key, value = self._parse_property(full_stmt)
                    if key is None:
                        continue

                    stack[-1].properties[key] = value
                    if key == "compatible" and isinstance(value, str):
                        stack[-1].compat = self._extract_compat(value)
                    if isinstance(value, str):
                        labels = self._extract_phandles(value)
                        paths = self._extract_phandle_paths(value)
                        for label in labels:
                            stack[-1].dependencies.append(
                                Dependency(
                                    property=key,
                                    target=f"&{label}",
                                    dep_type=infer_dependency_type(key),
                                )
                            )
                        for path_ref in paths:
                            normalized = path_ref if path_ref.startswith("/") else f"/{path_ref}"
                            stack[-1].dependencies.append(
                                Dependency(
                                    property=key,
                                    target=normalized,
                                    dep_type=infer_dependency_type(key),
                                )
                            )

        def _collect_phandle_refs(node: DTSNode) -> None:
            node.phandle_refs = [dep.target for dep in node.dependencies]
            for child in node.children:
                _collect_phandle_refs(child)

        if root is None:
            raise ValueError("Unable to parse DTS root node")

        _collect_phandle_refs(root)

        return root


def _iter_nested_values(value):
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_nested_values(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_nested_values(item)
        return
    yield value


def _format_edt_prop_value(value) -> str | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return "[" + ", ".join(str(_format_edt_prop_value(v)) for v in value) + "]"
    return str(value)


def _resolve_edtlib_for_source(source_path: Path):
    try:
        return importlib.import_module("devicetree.edtlib")
    except Exception:
        pass

    candidates: list[Path] = []

    zbase = os.environ.get("ZEPHYR_BASE")
    if zbase:
        candidates.append(Path(zbase) / "scripts" / "dts" / "python-devicetree" / "src")

    cwd = Path.cwd()
    candidates.append(cwd / "zephyr" / "scripts" / "dts" / "python-devicetree" / "src")
    candidates.append(cwd.parent / "zephyr" / "scripts" / "dts" / "python-devicetree" / "src")

    for parent in source_path.resolve().parents:
        candidates.append(parent / "scripts" / "dts" / "python-devicetree" / "src")
        candidates.append(parent / "zephyr" / "scripts" / "dts" / "python-devicetree" / "src")

    seen = set()
    for candidate in candidates:
        c = candidate.resolve()
        if c in seen:
            continue
        seen.add(c)
        if c.exists():
            sys.path.insert(0, str(c))
            try:
                return importlib.import_module("devicetree.edtlib")
            except Exception:
                continue

    return None


def _build_tree_from_edt(edt) -> DTSNode:
    node_map: dict[object, DTSNode] = {}

    def _extract_path_ref(obj) -> str | None:
        if obj is None:
            return None
        if hasattr(obj, "path"):
            return str(obj.path)
        node = getattr(obj, "node", None)
        if node is not None and hasattr(node, "path"):
            return str(node.path)
        controller = getattr(obj, "controller", None)
        if controller is not None and hasattr(controller, "path"):
            return str(controller.path)
        return None

    for enode in edt.nodes:
        label = enode.labels[0] if getattr(enode, "labels", None) else None
        unit_addr = None
        if getattr(enode, "unit_addr", None) is not None:
            unit_addr = str(enode.unit_addr)

        dnode = DTSNode(
            name=enode.name,
            path=enode.path,
            label=label,
            unit_addr=unit_addr,
            line_start=getattr(enode, "dep_ordinal", 0),
            compat=list(getattr(enode, "compats", []) or []),
        )

        dnode.properties["status"] = str(getattr(enode, "status", "okay"))
        if dnode.compat:
            dnode.properties["compatible"] = ", ".join(dnode.compat)

        deps: list[Dependency] = []
        for prop_name, prop in getattr(enode, "props", {}).items():
            pval = getattr(prop, "val", None)
            dnode.properties[prop_name] = _format_edt_prop_value(pval)

            for item in _iter_nested_values(pval):
                ref_path = _extract_path_ref(item)
                if ref_path is not None:
                    deps.append(
                        Dependency(
                            property=prop_name,
                            target=ref_path,
                            dep_type=infer_dependency_type(prop_name),
                        )
                    )

        for intr in getattr(enode, "interrupts", []) or []:
            controller = getattr(intr, "controller", None)
            if controller is not None and hasattr(controller, "path"):
                deps.append(
                    Dependency(
                        property="interrupts",
                        target=str(controller.path),
                        dep_type="interrupt",
                    )
                )
        for pc in getattr(enode, "pinctrls", []) or []:
            for conf_node in getattr(pc, "conf_nodes", []) or []:
                if hasattr(conf_node, "path"):
                    deps.append(
                        Dependency(
                            property="pinctrl",
                            target=str(conf_node.path),
                            dep_type="pinctrl",
                        )
                    )

        bus = getattr(enode, "bus", None)
        if bus is not None and hasattr(bus, "path"):
            deps.append(Dependency(property="bus", target=str(bus.path), dep_type="bus"))

        on_buses = list(getattr(enode, "on_buses", []) or [])
        if on_buses:
            dnode.properties["on-buses"] = ", ".join(str(x) for x in on_buses)
            for on_bus in on_buses:
                ref = _extract_path_ref(on_bus)
                if ref is not None:
                    deps.append(Dependency(property="on_buses", target=ref, dep_type="bus"))

        depends_on = getattr(enode, "depends_on", None)
        for dep_node in depends_on or []:
            ref = _extract_path_ref(dep_node)
            if ref is not None:
                deps.append(
                    Dependency(
                        property="depends_on",
                        target=ref,
                        dep_type="other",
                    )
                )

        dedup: list[Dependency] = []
        seen_ref: set[tuple[str, str, str]] = set()
        for dep in deps:
            key = (dep.property, dep.target, dep.dep_type)
            if key in seen_ref:
                continue
            seen_ref.add(key)
            dedup.append(dep)

        dnode.dependencies = dedup
        dnode.phandle_refs = [dep.target for dep in dedup]

        node_map[enode] = dnode

    root = None
    for enode, dnode in node_map.items():
        parent = getattr(enode, "parent", None)
        if parent is None:
            root = dnode
            continue
        pnode = node_map.get(parent)
        if pnode is not None:
            dnode.parent = pnode
            pnode.children.append(dnode)

    if root is None:
        raise ValueError("EDT did not provide a root node")
    return root


def load_tree(source_path: Path) -> tuple[DTSNode, str]:
    edt_pickle = source_path if source_path.suffix == ".pickle" else source_path.with_name("edt.pickle")

    if edt_pickle.exists():
        edtlib = _resolve_edtlib_for_source(source_path)
        if edtlib is not None:
            try:
                with open(edt_pickle, "rb") as f:
                    edt = pickle.load(f)
                return _build_tree_from_edt(edt), f"EDT ({edt_pickle})"
            except Exception:
                pass

    if source_path.suffix == ".pickle":
        raise ValueError("Unable to load edt.pickle (missing/incompatible edtlib)")

    text = source_path.read_text(encoding="utf-8", errors="replace")
    parser = DTSParser(text)
    return parser.parse(), f"DTS ({source_path})"
