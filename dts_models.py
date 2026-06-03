from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Dependency:
    property: str
    target: str
    dep_type: str = "other"


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
    dependencies: list[Dependency] = field(default_factory=list)
    phandle_refs: list[str] = field(default_factory=list)


def infer_dependency_type(prop_name: str) -> str:
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
