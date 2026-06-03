#!/usr/bin/env python3
"""Zephyr DTS Explorer.

GUI tool to inspect Zephyr devicetree data with EDT-first loading and
plain-DTS fallback.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import importlib
import json
import os
import pickle
import re
import sys
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


@dataclass
class DTSNode:
    name: str
    path: str
    label: str | None = None
    unit_addr: str | None = None
    properties: dict[str, str | bool] = field(default_factory=dict)
    children: list["DTSNode"] = field(default_factory=list)
    parent: "DTSNode | None" = None
    line_start: int = 0
    compat: list[str] = field(default_factory=list)
    dependencies: list["Dependency"] = field(default_factory=list)
    phandle_refs: list[str] = field(default_factory=list)


@dataclass
class Dependency:
    property: str
    target: str
    dep_type: str = "other"


def _infer_dependency_type(prop_name: str) -> str:
    key = prop_name.lower().strip()

    if key.startswith("pinctrl"):
        return "pinctrl"
    if key.startswith("interrupt"):
        return "interrupt"
    if key in {"clocks", "clock", "assigned-clocks", "assigned-clock-parents"}:
        return "clock"
    if key in {"resets", "reset-gpios", "reset"}:
        return "reset"
    if key in {"dmas", "dma"}:
        return "dma"
    if key in {"gpios", "gpio", "cs-gpios", "irq-gpios", "reset-gpios", "enable-gpios"}:
        return "gpio"
    if key in {"pwms", "pwm"}:
        return "pwm"
    if key in {"iommus", "mboxes", "io-channels", "phys", "power-domains", "vin-supply", "vdd-supply"}:
        return "provider"
    if key in {"bus", "parent-bus", "on-bus"}:
        return "bus"
    return "other"


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
                                    dep_type=_infer_dependency_type(key),
                                )
                            )
                        for path_ref in paths:
                            normalized = path_ref if path_ref.startswith("/") else f"/{path_ref}"
                            stack[-1].dependencies.append(
                                Dependency(
                                    property=key,
                                    target=normalized,
                                    dep_type=_infer_dependency_type(key),
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
                            dep_type=_infer_dependency_type(prop_name),
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


class DTSExplorerApp:
    def __init__(self, root_tk: tk.Tk, initial_source: Path | None):
        self.tk = root_tk

        self.source_path: Path | None = None
        self.backend = "<none>"
        self.dts_root: DTSNode | None = None

        self.node_by_tree_id: dict[str, DTSNode] = {}
        self.node_by_path: dict[str, DTSNode] = {}
        self.node_by_label: dict[str, DTSNode] = {}

        self.dependency_graph: dict[str, list[str]] = {}
        self.reverse_graph: dict[str, list[str]] = {}
        self.compat_index: dict[str, list[str]] = {}
        self.compat_search_var = tk.StringVar()
        self.graph_depth_var = tk.IntVar(value=1)
        self.dep_jump_targets: list[str] = []
        self.consumer_jump_targets: list[str] = []
        self.init_jump_targets: list[str] = []

        self.search_var = tk.StringVar()
        self.filter_okay_var = tk.BooleanVar(value=False)
        self.input_var = tk.StringVar(value=str(initial_source) if initial_source else "")

        self._build_ui()

        if initial_source and initial_source.exists():
            self._load_source(initial_source)
        else:
            self.status_var.set("Select a .dts or edt.pickle file, then click Load")

    def _index_nodes(self, node: DTSNode) -> None:
        self.node_by_path[node.path] = node
        if node.label:
            self.node_by_label[node.label] = node
        for child in node.children:
            self._index_nodes(child)

    def _build_ui(self) -> None:
        self.tk.title("Zephyr DTS Explorer")
        self.tk.geometry("1280x800")

        source_bar = ttk.Frame(self.tk, padding=8)
        source_bar.pack(fill=tk.X)

        ttk.Label(source_bar, text="Input:").pack(side=tk.LEFT)
        src_entry = ttk.Entry(source_bar, textvariable=self.input_var)
        src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        src_entry.bind("<Return>", lambda _: self._on_load_clicked())

        ttk.Button(source_bar, text="Browse", command=self._on_browse_clicked).pack(side=tk.LEFT)
        ttk.Button(source_bar, text="Load", command=self._on_load_clicked).pack(side=tk.LEFT, padx=(8, 0))

        top = ttk.Frame(self.tk, padding=(8, 0, 8, 8))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Search:").pack(side=tk.LEFT)
        entry = ttk.Entry(top, textvariable=self.search_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        entry.bind("<Return>", lambda _: self._run_search())

        ttk.Checkbutton(
            top,
            text="Only status=okay",
            variable=self.filter_okay_var,
            command=self._run_search,
        ).pack(side=tk.LEFT, padx=8)

        ttk.Button(top, text="Search", command=self._run_search).pack(side=tk.LEFT)
        ttk.Button(top, text="Reset", command=self._reset_search).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(top, text="Compatible:").pack(side=tk.LEFT, padx=(16, 4))
        compat_entry = ttk.Entry(top, textvariable=self.compat_search_var, width=30)
        compat_entry.pack(side=tk.LEFT)
        compat_entry.bind("<Return>", lambda _: self._find_compatible())
        ttk.Button(top, text="Find", command=self._find_compatible).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(top, text="Depth:").pack(side=tk.LEFT, padx=(16, 4))
        depth_spin = ttk.Spinbox(top, from_=1, to=6, textvariable=self.graph_depth_var, width=4)
        depth_spin.pack(side=tk.LEFT)
        ttk.Button(top, text="Show dependency graph", command=self._show_dependency_graph).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(top, text="Export JSON", command=self._export_selected_json).pack(side=tk.LEFT, padx=(8, 0))

        main = ttk.Panedwindow(self.tk, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main, padding=8)
        right = ttk.Frame(main, padding=8)
        main.add(left, weight=3)
        main.add(right, weight=2)

        self.tree = ttk.Treeview(left, columns=("path", "label"), show="tree headings")
        self.tree.heading("#0", text="Node")
        self.tree.heading("path", text="Path")
        self.tree.heading("label", text="Label")
        self.tree.column("#0", width=260, anchor=tk.W)
        self.tree.column("path", width=520, anchor=tk.W)
        self.tree.column("label", width=180, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_node)

        yscroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        yscroll.pack(fill=tk.Y, side=tk.RIGHT)
        self.tree.configure(yscrollcommand=yscroll.set)

        info = ttk.LabelFrame(right, text="Node Details", padding=8)
        info.pack(fill=tk.BOTH, expand=True)

        self.summary_var = tk.StringVar(value="No data loaded")
        ttk.Label(info, textvariable=self.summary_var, justify=tk.LEFT).pack(fill=tk.X, anchor=tk.W)

        ttk.Label(info, text="Properties:").pack(anchor=tk.W, pady=(10, 4))
        self.prop_tree = ttk.Treeview(info, columns=("value",), show="tree headings", height=16)
        self.prop_tree.heading("#0", text="Property")
        self.prop_tree.heading("value", text="Value")
        self.prop_tree.column("#0", width=220, anchor=tk.W)
        self.prop_tree.column("value", width=520, anchor=tk.W)
        self.prop_tree.pack(fill=tk.BOTH, expand=True)

        prop_scroll = ttk.Scrollbar(info, orient=tk.VERTICAL, command=self.prop_tree.yview)
        prop_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.prop_tree.configure(yscrollcommand=prop_scroll.set)

        ttk.Label(info, text="Dependencies (double click to jump):").pack(anchor=tk.W, pady=(10, 4))
        self.deps_list = tk.Listbox(info, height=8)
        self.deps_list.pack(fill=tk.BOTH, expand=False)
        self.deps_list.bind("<Double-Button-1>", self._jump_to_dependency)

        ttk.Label(info, text="Consumers / Used By (double click to jump):").pack(anchor=tk.W, pady=(10, 4))
        self.consumers_list = tk.Listbox(info, height=8)
        self.consumers_list.pack(fill=tk.BOTH, expand=False)
        self.consumers_list.bind("<Double-Button-1>", self._jump_to_consumer)

        ttk.Label(info, text="Initialization checks (double click to jump):").pack(anchor=tk.W, pady=(10, 4))
        self.init_list = tk.Listbox(info, height=8)
        self.init_list.pack(fill=tk.BOTH, expand=False)
        self.init_list.bind("<Double-Button-1>", self._jump_to_initialization_target)

        status = ttk.Frame(self.tk, padding=(8, 0, 8, 8))
        status.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var).pack(anchor=tk.W)

    def _node_display_name(self, node: DTSNode) -> str:
        if node.name == "/":
            return "/"
        if node.label:
            return f"{node.name} ({node.label})"
        return node.name

    def _insert_tree_node(self, parent_id: str, node: DTSNode) -> None:
        item_id = self.tree.insert(
            parent_id,
            tk.END,
            text=self._node_display_name(node),
            values=(node.path, node.label or ""),
            open=False,
        )
        self.node_by_tree_id[item_id] = node
        for child in node.children:
            self._insert_tree_node(item_id, child)

    def _populate_tree(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        self.node_by_tree_id.clear()

        if self.dts_root is None:
            return

        self._insert_tree_node("", self.dts_root)
        top = self.tree.get_children("")
        if top:
            self.tree.item(top[0], open=True)

    def _iter_nodes(self, node: DTSNode):
        yield node
        for child in node.children:
            yield from self._iter_nodes(child)

    def _resolve_dependency_target(self, dep: Dependency) -> str | None:
        target = dep.target.strip()
        if not target:
            return None
        if target.startswith("/"):
            return target if target in self.node_by_path else None
        if target.startswith("&"):
            label = target[1:]
            node = self.node_by_label.get(label)
            return node.path if node else None
        node = self.node_by_label.get(target)
        if node:
            return node.path
        if target in self.node_by_path:
            return target
        return None

    def _rebuild_indexes(self) -> None:
        if self.dts_root is None:
            self.dependency_graph = {}
            self.reverse_graph = {}
            self.compat_index = {}
            return

        dep_graph: dict[str, list[str]] = defaultdict(list)
        rev_graph: dict[str, list[str]] = defaultdict(list)
        comp_index: dict[str, list[str]] = defaultdict(list)

        for node in self._iter_nodes(self.dts_root):
            seen_targets: set[str] = set()
            for dep in node.dependencies:
                resolved = self._resolve_dependency_target(dep)
                if resolved is None or resolved == node.path or resolved in seen_targets:
                    continue
                seen_targets.add(resolved)
                dep_graph[node.path].append(resolved)
                rev_graph[resolved].append(node.path)

            for comp in node.compat:
                c = comp.strip().lower()
                if c:
                    comp_index[c].append(node.path)

        self.dependency_graph = {k: sorted(v) for k, v in dep_graph.items()}
        self.reverse_graph = {k: sorted(v) for k, v in rev_graph.items()}
        self.compat_index = {k: sorted(v) for k, v in comp_index.items()}

    def _select_node_by_path(self, path: str) -> bool:
        target_item = None
        for item_id, node in self.node_by_tree_id.items():
            if node.path == path:
                target_item = item_id
                break

        if not target_item:
            return False

        cur = target_item
        while cur:
            parent = self.tree.parent(cur)
            if parent:
                self.tree.item(parent, open=True)
            cur = parent

        self.tree.selection_set(target_item)
        self.tree.focus(target_item)
        self.tree.see(target_item)
        return True

    def _status_is_okay(self, node: DTSNode | None) -> bool:
        if node is None:
            return False
        status = node.properties.get("status", "okay")
        if isinstance(status, bool):
            return bool(status)
        return str(status).strip().lower() == "okay"

    def _is_deferred_init(self, node: DTSNode | None) -> bool:
        if node is None:
            return False
        val = node.properties.get("zephyr,deferred-init")
        if isinstance(val, bool):
            return val
        if val is None:
            return False
        return str(val).strip().lower() in {"1", "true", "yes", "y"}

    def _get_selected_node(self) -> DTSNode | None:
        selected = self.tree.selection()
        if not selected:
            return None
        return self.node_by_tree_id.get(selected[0])

    def _collect_subgraph(self, root_path: str, depth: int) -> dict[str, list[str]]:
        if depth < 1:
            depth = 1

        out: dict[str, list[str]] = {}
        frontier: list[tuple[str, int]] = [(root_path, 0)]
        seen = {(root_path, 0)}

        while frontier:
            current, dist = frontier.pop(0)
            deps = self.dependency_graph.get(current, [])
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

    def _export_selected_json(self) -> None:
        node = self._get_selected_node()
        if node is None:
            messagebox.showwarning("Export", "Select a node first")
            return

        depth = max(1, int(self.graph_depth_var.get()))
        deps_payload = []
        for dep in node.dependencies:
            resolved = self._resolve_dependency_target(dep)
            deps_payload.append(
                {
                    "property": dep.property,
                    "type": dep.dep_type,
                    "target": resolved or dep.target,
                    "resolved": resolved is not None,
                }
            )

        payload = {
            "node": node.path,
            "name": node.name,
            "label": node.label,
            "compatible": list(node.compat),
            "status": node.properties.get("status", "okay"),
            "deferred_init": self._is_deferred_init(node),
            "dependencies": deps_payload,
            "used_by": list(self.reverse_graph.get(node.path, [])),
            "subgraph_depth": depth,
            "subgraph": self._collect_subgraph(node.path, depth),
        }

        suggested = node.name.replace("/", "_").replace("@", "_") or "node"
        out_path = filedialog.asksaveasfilename(
            title="Export dependency JSON",
            defaultextension=".json",
            initialfile=f"{suggested}_deps.json",
            filetypes=[("JSON", "*.json"), ("All files", "*")],
        )
        if not out_path:
            return

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
        except Exception as exc:
            messagebox.showerror("Export", f"Failed to export JSON:\n{exc}")
            return

        self.status_var.set(f"Exported JSON: {out_path}")

    def _show_dependency_graph(self) -> None:
        node = self._get_selected_node()
        if node is None:
            messagebox.showwarning("Graph", "Select a node first")
            return

        depth = max(1, int(self.graph_depth_var.get()))
        subgraph = self._collect_subgraph(node.path, depth)

        edges = []
        nodes = set()
        for src, targets in subgraph.items():
            nodes.add(src)
            for dst in targets:
                if src == dst:
                    continue
                nodes.add(dst)
                edges.append((src, dst))

        if len(nodes) <= 1:
            messagebox.showinfo("Graph", "No resolved dependencies to draw for selected depth")
            return

        try:
            nx = importlib.import_module("networkx")
            plt = importlib.import_module("matplotlib.pyplot")
        except Exception:
            messagebox.showinfo(
                "Graph",
                "Graph view requires optional packages:\n  pip install networkx matplotlib",
            )
            return

        g = nx.DiGraph()
        g.add_nodes_from(sorted(nodes))
        g.add_edges_from(edges)

        fig = plt.figure(figsize=(10, 7))
        ax = fig.add_subplot(111)
        pos = nx.spring_layout(g, seed=7)

        root_nodes = [n for n in g.nodes if n == node.path]
        other_nodes = [n for n in g.nodes if n != node.path]

        nx.draw_networkx_nodes(g, pos, nodelist=other_nodes, node_size=900, node_color="#8dc6ff", ax=ax)
        nx.draw_networkx_nodes(g, pos, nodelist=root_nodes, node_size=1200, node_color="#ffb26b", ax=ax)
        nx.draw_networkx_edges(g, pos, arrows=True, arrowstyle="-|>", arrowsize=14, width=1.6, ax=ax)

        labels = {}
        for p in g.nodes:
            n = self.node_by_path.get(p)
            if n is None:
                labels[p] = p
            elif n.label:
                labels[p] = f"{n.name}\n({n.label})"
            else:
                labels[p] = n.name

        nx.draw_networkx_labels(g, pos, labels=labels, font_size=8, ax=ax)
        ax.set_title(f"Dependency graph from {node.path} (depth={depth})")
        ax.axis("off")
        fig.tight_layout()
        plt.show()

    def _find_compatible(self) -> None:
        if self.dts_root is None:
            self.status_var.set("No data loaded")
            return

        query = self.compat_search_var.get().strip().lower()
        if not query:
            self.status_var.set("Insert a compatible string to search")
            return

        exact = self.compat_index.get(query, [])
        if exact:
            self._populate_tree()
            if self._select_node_by_path(exact[0]):
                self.status_var.set(f"Compatible '{query}' matches={len(exact)}")
            else:
                self.status_var.set(f"Compatible '{query}' matches={len(exact)} (not visible in current tree)")
            return

        partial_matches: list[str] = []
        for comp, paths in self.compat_index.items():
            if query in comp:
                partial_matches.extend(paths)

        dedup_partial = sorted(set(partial_matches))
        if not dedup_partial:
            self.status_var.set(f"Compatible '{query}' matches=0")
            return

        self._populate_tree()
        self._select_node_by_path(dedup_partial[0])
        self.status_var.set(f"Compatible '{query}' partial_matches={len(dedup_partial)}")

    def _node_matches(self, node: DTSNode, query: str, only_okay: bool) -> bool:
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

    def _collect_matching_paths(self, query: str, only_okay: bool) -> set[str]:
        if self.dts_root is None:
            return set()

        matched = set()
        for node in self._iter_nodes(self.dts_root):
            if self._node_matches(node, query, only_okay):
                matched.add(node.path)
                p = node.parent
                while p is not None:
                    matched.add(p.path)
                    p = p.parent
        return matched

    def _filter_tree_recursive(self, item_id: str, allowed_paths: set[str]) -> bool:
        node = self.node_by_tree_id[item_id]
        keep_self = node.path in allowed_paths

        keep_any_child = False
        for child_id in self.tree.get_children(item_id):
            child_keep = self._filter_tree_recursive(child_id, allowed_paths)
            keep_any_child = keep_any_child or child_keep

        visible = keep_self or keep_any_child
        if visible:
            self.tree.reattach(item_id, self.tree.parent(item_id), tk.END)
            if keep_any_child:
                self.tree.item(item_id, open=True)
        else:
            self.tree.detach(item_id)
        return visible

    def _run_search(self) -> None:
        if self.dts_root is None:
            self.status_var.set("No data loaded")
            return

        query = self.search_var.get().strip()
        only_okay = self.filter_okay_var.get()
        self._populate_tree()

        allowed = self._collect_matching_paths(query, only_okay)
        roots = self.tree.get_children("")
        for root_id in roots:
            self._filter_tree_recursive(root_id, allowed)

        total_matches = sum(
            1 for n in self._iter_nodes(self.dts_root) if self._node_matches(n, query, only_okay)
        )
        self.status_var.set(
            f"Query='{query or '*'}'  matches={total_matches}  filter_okay={only_okay}"
        )

    def _reset_search(self) -> None:
        self.search_var.set("")
        self.filter_okay_var.set(False)
        self._populate_tree()
        if self.source_path:
            self.status_var.set(f"Loaded: {self.source_path}  backend={self.backend}")

    def _on_select_node(self, _event) -> None:
        selected = self.tree.selection()
        if not selected:
            return

        node = self.node_by_tree_id[selected[0]]
        parent_path = node.parent.path if node.parent else "<none>"
        compat = ", ".join(node.compat) if node.compat else "<none>"

        self.summary_var.set(
            "\n".join(
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
        )

        self.prop_tree.delete(*self.prop_tree.get_children(""))
        for key in sorted(node.properties.keys()):
            val = node.properties[key]
            self.prop_tree.insert("", tk.END, text=key, values=(str(val),))

        self.deps_list.delete(0, tk.END)
        self.dep_jump_targets = []

        seen_deps: set[tuple[str, str, str]] = set()
        for dep in node.dependencies:
            dep_key = (dep.property, dep.dep_type, dep.target)
            if dep_key in seen_deps:
                continue
            seen_deps.add(dep_key)

            resolved = self._resolve_dependency_target(dep)
            if resolved is None:
                label = f"{dep.dep_type:10s} {dep.property:20s} -> {dep.target} (unresolved)"
                jump_target = ""
            else:
                label = f"{dep.dep_type:10s} {dep.property:20s} -> {resolved}"
                jump_target = resolved

            self.deps_list.insert(tk.END, label)
            self.dep_jump_targets.append(jump_target)

        self.consumers_list.delete(0, tk.END)
        self.consumer_jump_targets = []
        for consumer_path in self.reverse_graph.get(node.path, []):
            consumer = self.node_by_path.get(consumer_path)
            if consumer is None:
                display = consumer_path
            elif consumer.label:
                display = f"{consumer.name} ({consumer.label})  [{consumer.path}]"
            else:
                display = f"{consumer.name}  [{consumer.path}]"

            self.consumers_list.insert(tk.END, display)
            self.consumer_jump_targets.append(consumer_path)

        self.init_list.delete(0, tk.END)
        self.init_jump_targets = []

        if self._is_deferred_init(node):
            self.init_list.insert(tk.END, "[NODE] deferred-init enabled")
        else:
            self.init_list.insert(tk.END, "[NODE] deferred-init disabled")
        self.init_jump_targets.append(node.path)

        for dep in node.dependencies:
            resolved = self._resolve_dependency_target(dep)
            if resolved is None:
                row = f"[MISSING ] {dep.dep_type:10s} {dep.property:20s} -> {dep.target}"
                self.init_list.insert(tk.END, row)
                self.init_jump_targets.append("")
                continue

            target_node = self.node_by_path.get(resolved)
            if self._is_deferred_init(target_node):
                state = "DEFERRED"
            elif self._status_is_okay(target_node):
                state = "OK"
            else:
                state = "DISABLED"

            row = f"[{state:8s}] {dep.dep_type:10s} {dep.property:20s} -> {resolved}"
            self.init_list.insert(tk.END, row)
            self.init_jump_targets.append(resolved)

    def _jump_to_dependency(self, _event) -> None:
        selected = self.deps_list.curselection()
        if not selected:
            return
        idx = selected[0]
        if idx >= len(self.dep_jump_targets):
            return

        target = self.dep_jump_targets[idx]
        if not target:
            messagebox.showinfo("Dependency", "Cannot jump to unresolved dependency")
            return

        if self._select_node_by_path(target):
            return

        messagebox.showinfo("Dependency", f"No visible node found for path '{target}'")

    def _jump_to_consumer(self, _event) -> None:
        selected = self.consumers_list.curselection()
        if not selected:
            return
        idx = selected[0]
        if idx >= len(self.consumer_jump_targets):
            return

        target = self.consumer_jump_targets[idx]
        if self._select_node_by_path(target):
            return

        messagebox.showinfo("Consumers", f"No visible node found for path '{target}'")

    def _jump_to_initialization_target(self, _event) -> None:
        selected = self.init_list.curselection()
        if not selected:
            return
        idx = selected[0]
        if idx >= len(self.init_jump_targets):
            return

        target = self.init_jump_targets[idx]
        if not target:
            messagebox.showinfo("Initialization", "Cannot jump to unresolved dependency")
            return

        if self._select_node_by_path(target):
            return

        messagebox.showinfo("Initialization", f"No visible node found for path '{target}'")

    def _on_browse_clicked(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Select Zephyr devicetree input",
            filetypes=[
                ("Devicetree files", "*.dts *.pickle"),
                ("DTS", "*.dts"),
                ("EDT pickle", "*.pickle"),
                ("All files", "*"),
            ],
        )
        if file_path:
            self.input_var.set(file_path)

    def _on_load_clicked(self) -> None:
        raw = self.input_var.get().strip()
        if not raw:
            messagebox.showwarning("Input", "Select a .dts or edt.pickle file")
            return

        path = Path(raw).expanduser().resolve()
        self._load_source(path)

    def _load_source(self, input_path: Path) -> None:
        if not input_path.exists():
            messagebox.showerror("Load error", f"Input file not found:\n{input_path}")
            return

        try:
            root, backend = load_tree(input_path)
        except Exception as exc:
            messagebox.showerror("Load error", str(exc))
            return

        self.dts_root = root
        self.source_path = input_path
        self.backend = backend

        self.node_by_path.clear()
        self.node_by_label.clear()
        self._index_nodes(root)
        self._rebuild_indexes()
        self._populate_tree()
        self.summary_var.set("Select a node")
        self.prop_tree.delete(*self.prop_tree.get_children(""))
        self.deps_list.delete(0, tk.END)
        self.consumers_list.delete(0, tk.END)
        self.init_list.delete(0, tk.END)
        self.dep_jump_targets = []
        self.consumer_jump_targets = []
        self.init_jump_targets = []

        self.tk.title(f"Zephyr DTS Explorer - {input_path.name}")
        self.status_var.set(
            f"Loaded: {input_path}  backend={backend}  nodes={len(self.node_by_path)}  compatibles={len(self.compat_index)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Explore Zephyr devicetree data with a GUI (EDT preferred, DTS fallback)"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Optional path to .dts or edt.pickle to load at startup",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    initial = Path(args.input_file).expanduser().resolve() if args.input_file else None

    tk_root = tk.Tk()
    app = DTSExplorerApp(tk_root, initial)
    tk_root._app = app  # type: ignore[attr-defined]
    tk_root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
