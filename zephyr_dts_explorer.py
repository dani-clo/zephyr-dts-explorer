#!/usr/bin/env python3
"""Zephyr DTS Explorer.

GUI tool to inspect Zephyr devicetree data with EDT-first loading and
plain-DTS fallback.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dts_graph import (
    collect_subgraph,
    is_deferred_init,
    iter_nodes,
    rebuild_indexes,
    resolve_dependency_target,
    status_is_okay,
)
from dts_models import DTSNode, Dependency
from dts_parsers import load_tree
from dts_persistence import build_recent_labels, load_ui_state, save_ui_state, ui_state_path
from dts_ui_support import (
    build_consumer_items,
    build_dependency_items,
    build_graph_labels,
    build_initialization_items,
    collect_matching_paths,
    node_display_name,
    node_matches,
    node_summary_text,
)


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
        self.show_self_loops_var = tk.BooleanVar(value=False)
        self.recent_var = tk.StringVar()
        self.dep_jump_targets: list[str] = []
        self.consumer_jump_targets: list[str] = []
        self.init_jump_targets: list[str] = []

        self._state_path = ui_state_path()
        state = load_ui_state(self._state_path)
        self.last_dir = str(state.get("last_dir", ""))
        raw_recent = state.get("recent_files", [])
        self.recent_files = [str(x) for x in raw_recent if isinstance(x, str)]
        self.recent_files = [x for x in self.recent_files if Path(x).exists()]
        if len(self.recent_files) > 12:
            self.recent_files = self.recent_files[:12]
        self.recent_display_files = build_recent_labels(self.recent_files)

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

        ttk.Label(source_bar, text="Recent:").pack(side=tk.LEFT, padx=(16, 4))
        self.recent_combo = ttk.Combobox(
            source_bar,
            textvariable=self.recent_var,
            values=self.recent_display_files,
            state="readonly",
            width=44,
        )
        self.recent_combo.pack(side=tk.LEFT)
        self.recent_combo.bind("<<ComboboxSelected>>", self._on_recent_selected)

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
        ttk.Checkbutton(
            top,
            text="Show self loops",
            variable=self.show_self_loops_var,
            command=self._on_graph_options_changed,
        ).pack(side=tk.LEFT, padx=(8, 0))
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
        return node_display_name(node)

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
        yield from iter_nodes(node)

    def _resolve_dependency_target(self, dep: Dependency) -> str | None:
        return resolve_dependency_target(dep, self.node_by_path, self.node_by_label)

    def _rebuild_indexes(self) -> None:
        self.dependency_graph, self.reverse_graph, self.compat_index = rebuild_indexes(
            self.dts_root,
            self.node_by_path,
            self.node_by_label,
            self.show_self_loops_var.get(),
        )

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

    def _get_selected_node(self) -> DTSNode | None:
        selected = self.tree.selection()
        if not selected:
            return None
        return self.node_by_tree_id.get(selected[0])

    def _collect_subgraph(self, root_path: str, depth: int) -> dict[str, list[str]]:
        return collect_subgraph(self.dependency_graph, root_path, depth)

    def _on_graph_options_changed(self) -> None:
        if self.dts_root is None:
            return

        self._rebuild_indexes()
        selected = self.tree.selection()
        if selected:
            self._on_select_node(None)

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
            "deferred_init": is_deferred_init(node),
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
        allow_self_loops = self.show_self_loops_var.get()
        for src, targets in subgraph.items():
            nodes.add(src)
            for dst in targets:
                if src == dst and not allow_self_loops:
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
        labels = build_graph_labels(list(g.nodes), self.node_by_path)

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

        allowed = collect_matching_paths(self.dts_root, query, only_okay)
        roots = self.tree.get_children("")
        for root_id in roots:
            self._filter_tree_recursive(root_id, allowed)

        total_matches = sum(
            1 for n in self._iter_nodes(self.dts_root) if node_matches(n, query, only_okay)
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
        self.summary_var.set(node_summary_text(node))

        self.prop_tree.delete(*self.prop_tree.get_children(""))
        for key in sorted(node.properties.keys()):
            val = node.properties[key]
            self.prop_tree.insert("", tk.END, text=key, values=(str(val),))

        self.deps_list.delete(0, tk.END)
        dep_items = build_dependency_items(node, self._resolve_dependency_target)
        self.dep_jump_targets = []
        for label, jump_target in dep_items:
            self.deps_list.insert(tk.END, label)
            self.dep_jump_targets.append(jump_target)

        self.consumers_list.delete(0, tk.END)
        consumer_items = build_consumer_items(node.path, self.reverse_graph, self.node_by_path)
        self.consumer_jump_targets = []
        for display, consumer_path in consumer_items:
            self.consumers_list.insert(tk.END, display)
            self.consumer_jump_targets.append(consumer_path)

        self.init_list.delete(0, tk.END)
        init_items = build_initialization_items(node, self._resolve_dependency_target, self.node_by_path)
        self.init_jump_targets = []
        for row, jump_target in init_items:
            self.init_list.insert(tk.END, row)
            self.init_jump_targets.append(jump_target)

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
        initial_dir = self.last_dir if self.last_dir and Path(self.last_dir).exists() else None
        file_path = filedialog.askopenfilename(
            title="Select Zephyr devicetree input",
            initialdir=initial_dir,
            filetypes=[
                ("Devicetree files", "*.dts *.pickle"),
                ("DTS", "*.dts"),
                ("EDT pickle", "*.pickle"),
                ("All files", "*"),
            ],
        )
        if file_path:
            self.input_var.set(file_path)

    def _on_recent_selected(self, _event) -> None:
        idx = self.recent_combo.current()
        if idx < 0 or idx >= len(self.recent_files):
            selected = self.recent_var.get().strip()
            if not selected:
                return
            try:
                idx = self.recent_display_files.index(selected)
            except ValueError:
                return

        chosen = self.recent_files[idx]
        if not chosen:
            return
        self.input_var.set(chosen)
        self._on_load_clicked()

    def _remember_loaded_file(self, input_path: Path) -> None:
        self.last_dir = str(input_path.parent)

        loaded = str(input_path)
        ordered = [loaded]
        for p in self.recent_files:
            if p != loaded and Path(p).exists():
                ordered.append(p)
        self.recent_files = ordered[:12]
        self.recent_display_files = build_recent_labels(self.recent_files)

        self.recent_combo["values"] = self.recent_display_files
        if self.recent_display_files:
            self.recent_var.set(self.recent_display_files[0])
        else:
            self.recent_var.set("")

        save_ui_state(
            self._state_path,
            {
                "last_dir": self.last_dir,
                "recent_files": self.recent_files,
            },
        )

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
        self._remember_loaded_file(input_path)
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
    parser.add_argument(
        "--file",
        dest="input_file_opt",
        help="Optional path to .dts or edt.pickle to load at startup",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requested = args.input_file_opt or args.input_file
    initial = Path(requested).expanduser().resolve() if requested else None

    tk_root = tk.Tk()
    app = DTSExplorerApp(tk_root, initial)
    tk_root._app = app  # type: ignore[attr-defined]
    tk_root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
