# zephyr-dts-explorer

Small Python GUI to explore Zephyr devicetree data from either:

- `zephyr.dts`
- `edt.pickle`

The tool shows nodes as a parent/child tree, allows text search, and displays
properties and references for the selected node.

## Features

- Tree navigation of devicetree nodes
- Property viewer for each node
- Search by name, path, label, compatible, property keys and values
- Double-click navigation on node references
- GUI input selection (`Browse` + `Load`) for `.dts` or `edt.pickle`

## Backend Logic

When you load a file:

1. If input is `edt.pickle`: use EDT backend.
2. If input is `.dts`: try sibling `edt.pickle` first (same folder), then fallback to plain DTS parsing.

This gives richer data when EDT is available, but still works with DTS-only inputs.

## Requirements

- Python 3.10+
- Tkinter (usually included with standard Python)
- For EDT backend: Zephyr `python-devicetree` package importable (the tool tries to auto-locate it via:
	- `ZEPHYR_BASE`
	- sibling `zephyr/` checkout
	- parent folder heuristics)

## Run

From this repository folder:

```bash
python zephyr_dts_explorer.py
```

Optional startup input:

```bash
python zephyr_dts_explorer.py /path/to/zephyr.dts
python zephyr_dts_explorer.py /path/to/edt.pickle
```

## Typical Workflow

1. Build your Zephyr target so `zephyr.dts` and `edt.pickle` are generated.
2. Start the tool.
3. Click `Browse` and choose `.dts` or `edt.pickle`.
4. Click `Load`.
5. Use search (`spi`, full path, label, etc.) and inspect properties.

## Notes

- `edt.pickle` should be considered trusted input (pickle security model).
- On EDT backend, some values are already interpreted by Zephyr and appear richer than plain DTS text parsing.