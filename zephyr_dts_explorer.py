#!/usr/bin/env python3
"""Zephyr DTS Explorer.

GUI tool to inspect Zephyr devicetree data with EDT-first loading and
plain-DTS fallback.
"""

from __future__ import annotations

import argparse
import importlib
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
    phandle_refs: list[str] = field(default_factory=list)


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
                        stack[-1].phandle_refs.extend(self._extract_phandles(value))

        if root is None:
            raise ValueError("Unable to parse DTS root node")

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

        refs: list[str] = []
        for prop_name, prop in getattr(enode, "props", {}).items():
            pval = getattr(prop, "val", None)
            dnode.properties[prop_name] = _format_edt_prop_value(pval)

            for item in _iter_nested_values(pval):
                if hasattr(item, "path"):
                    refs.append(str(item.path))
                elif hasattr(item, "controller") and hasattr(item.controller, "path"):
                    refs.append(str(item.controller.path))

        for intr in getattr(enode, "interrupts", []) or []:
            controller = getattr(intr, "controller", None)
            if controller is not None and hasattr(controller, "path"):
                refs.append(str(controller.path))
        for pc in getattr(enode, "pinctrls", []) or []:
            for conf_node in getattr(pc, "conf_nodes", []) or []:
                if hasattr(conf_node, "path"):
                    refs.append(str(conf_node.path))

        dedup = []
        seen_ref = set()
        for r in refs:
            if r in seen_ref:
                continue
            seen_ref.add(r)
            dedup.append(r)
        dnode.phandle_refs = dedup

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

        ttk.Label(info, text="Phandle references (double click to jump):").pack(anchor=tk.W, pady=(10, 4))
        self.refs_list = tk.Listbox(info, height=8)
        self.refs_list.pack(fill=tk.BOTH, expand=False)
        self.refs_list.bind("<Double-Button-1>", self._jump_to_reference)

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

        self.refs_list.delete(0, tk.END)
        unique_refs = []
        seen = set()
        for ref in node.phandle_refs:
            if ref in seen:
                continue
            seen.add(ref)
            if ref.startswith("/"):
                target = self.node_by_path.get(ref)
                if target:
                    unique_refs.append(f"{ref} -> {target.path}")
                else:
                    unique_refs.append(f"{ref} -> <not found>")
            else:
                target = None
                for n in self.node_by_path.values():
                    if n.label == ref:
                        target = n
                        break
                if target:
                    unique_refs.append(f"&{ref} -> {target.path}")
                else:
                    unique_refs.append(f"&{ref} -> <not found>")

        for item in unique_refs:
            self.refs_list.insert(tk.END, item)

    def _jump_to_reference(self, _event) -> None:
        selected = self.refs_list.curselection()
        if not selected:
            return
        txt = self.refs_list.get(selected[0])

        if txt.startswith("/"):
            path = txt.split(" -> ", 1)[0]
            target_item = None
            for item_id, node in self.node_by_tree_id.items():
                if node.path == path:
                    target_item = item_id
                    break
            if not target_item:
                messagebox.showinfo("Reference", f"No visible node found for path '{path}'")
                return

            cur = target_item
            while cur:
                parent = self.tree.parent(cur)
                if parent:
                    self.tree.item(parent, open=True)
                cur = parent

            self.tree.selection_set(target_item)
            self.tree.focus(target_item)
            self.tree.see(target_item)
            return

        m = re.match(r"&([A-Za-z_][A-Za-z0-9_]*)", txt)
        if not m:
            return
        label = m.group(1)

        target_item = None
        for item_id, node in self.node_by_tree_id.items():
            if node.label == label:
                target_item = item_id
                break

        if not target_item:
            messagebox.showinfo("Reference", f"No visible node found for label '&{label}'")
            return

        cur = target_item
        while cur:
            parent = self.tree.parent(cur)
            if parent:
                self.tree.item(parent, open=True)
            cur = parent

        self.tree.selection_set(target_item)
        self.tree.focus(target_item)
        self.tree.see(target_item)

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
        self._index_nodes(root)
        self._populate_tree()
        self.summary_var.set("Select a node")
        self.prop_tree.delete(*self.prop_tree.get_children(""))
        self.refs_list.delete(0, tk.END)

        self.tk.title(f"Zephyr DTS Explorer - {input_path.name}")
        self.status_var.set(f"Loaded: {input_path}  backend={backend}")


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
