# zephyr-dts-explorer

Small Python GUI to explore Zephyr devicetree data from either:

- `zephyr.dts`
- `edt.pickle`

The tool shows nodes as a parent/child tree, allows text search, builds a
dependency graph, and displays rich node details for the selected item.

## Features

- Tree navigation of devicetree nodes
- Property viewer for each node
- Search by name, path, label, compatible, property keys and values
- Dedicated compatible search
- Typed dependencies (`clock`, `gpio`, `interrupt`, `pinctrl`, `bus`, etc.)
- Reverse dependency view (`Consumers / Used By`)
- Initialization checks showing `OK`, `DEFERRED`, `DISABLED`, or `MISSING`
- Double-click navigation on dependencies, consumers, and initialization targets
- Dependency subgraph view for the selected node with configurable depth
- JSON export for the selected node and its dependency subgraph
- GUI input selection (`Browse` + `Load`) for `.dts` or `edt.pickle`
- Recent files list with compact disambiguated labels
- Browse dialog reopens the last directory used
- Startup input from CLI with positional path or `--file`

## Backend Logic

When you load a file:

1. If input is `edt.pickle`: use EDT backend.
2. If input is `.dts`: try sibling `edt.pickle` first (same folder), then fallback to plain DTS parsing.

This gives richer data when EDT is available, but still works with DTS-only inputs.

When EDT metadata is available, the explorer also tries to extract richer
relationships such as bus membership and resolved dependencies from EDT objects.

## Requirements

- Python 3.10+
- Tkinter (usually included with standard Python)
- For EDT backend: Zephyr `python-devicetree` package importable (the tool tries to auto-locate it via:
	- `ZEPHYR_BASE`
	- sibling `zephyr/` checkout
	- parent folder heuristics)
- Optional for graph rendering:
	- `networkx`
	- `matplotlib`

Install optional graph packages with:

```bash
pip install networkx matplotlib
```

## Run

From this repository folder:

```bash
python zephyr_dts_explorer.py
```

Optional startup input:

```bash
python zephyr_dts_explorer.py /path/to/zephyr.dts
python zephyr_dts_explorer.py /path/to/edt.pickle
python zephyr_dts_explorer.py --file /path/to/edt.pickle
```

## Typical Workflow

1. Build your Zephyr target so `zephyr.dts` and `edt.pickle` are generated.
2. Start the tool.
3. Load an input with one of these methods:
	- click `Browse` and choose `.dts` or `edt.pickle`
	- select a path from `Recent`
	- start the app directly with a file path or `--file`
4. Use text search (`spi`, full path, label, property value, etc.) or compatible search.
5. Select a node and inspect:
	- properties
	- typed dependencies
	- consumers / used by
	- initialization checks
6. Optionally open `Show dependency graph` for a depth-limited view.
7. Optionally use `Export JSON` for external processing.

## UI Notes

- `Recent` entries are shown with compact labels derived from the build path so similar
  `edt.pickle` files are easier to distinguish.
- `Show self loops` is disabled by default to keep dependency graphs readable.
- Graph rendering is intentionally limited to the selected node and a small depth,
  which is more practical than rendering a whole board DTS.

## Notes

- `edt.pickle` should be considered trusted input (pickle security model).
- On EDT backend, some values are already interpreted by Zephyr and appear richer than plain DTS text parsing.
- UI state is stored locally in `~/.zephyr_dts_explorer_state.json`.