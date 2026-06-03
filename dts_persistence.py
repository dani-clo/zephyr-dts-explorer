from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def ui_state_path() -> Path:
    return Path.home() / ".zephyr_dts_explorer_state.json"


def load_ui_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def save_ui_state(path: Path, state: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception:
        # State persistence is best-effort and must not block the GUI.
        pass


def _tail_path(path: Path, parts: int) -> str:
    chunks = path.parts
    if len(chunks) <= parts:
        return "/".join(chunks)
    return ".../" + "/".join(chunks[-parts:])


def build_recent_labels(paths: list[str]) -> list[str]:
    labels: list[str] = []
    for raw in paths:
        p = Path(raw)
        board = ""
        if "build" in p.parts:
            i = p.parts.index("build")
            if i + 1 < len(p.parts):
                board = p.parts[i + 1]

        short_tail = _tail_path(p, 3)
        if board:
            labels.append(f"{board} | {short_tail}")
        else:
            labels.append(short_tail)

    counts: dict[str, int] = defaultdict(int)
    for label in labels:
        counts[label] += 1

    for idx, label in enumerate(labels):
        if counts[label] <= 1:
            continue
        p = Path(paths[idx])
        board = ""
        if "build" in p.parts:
            i = p.parts.index("build")
            if i + 1 < len(p.parts):
                board = p.parts[i + 1]
        long_tail = _tail_path(p, 5)
        labels[idx] = f"{board} | {long_tail}" if board else long_tail

    seen: dict[str, int] = defaultdict(int)
    unique_labels: list[str] = []
    for label in labels:
        seen[label] += 1
        if seen[label] == 1:
            unique_labels.append(label)
        else:
            unique_labels.append(f"{label} ({seen[label]})")

    return unique_labels
