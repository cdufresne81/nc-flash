"""
Which Datalogger channel drives the live cursor on a table axis.

The shipped map (``examples/axis_channels.json``) is keyed by the exact axis
name and covers every definition at once; a ``logparam="CHANNEL"`` attribute on
an axis in a definition overrides it for that ROM. The ``_XXXX`` suffixes in
axis names (``RPM_6DD8``) are labels copied between definitions, not the RAM
addresses the ECU reads the axis from, so nothing is derived from them.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from ..utils.paths import get_app_root
from .datalog_channels import ChannelError, compile_formula
from .rom_definition import Table

logger = logging.getLogger(__name__)

MAP_FILE = Path("examples") / "axis_channels.json"


@dataclass(frozen=True)
class AxisMapping:
    """The channel behind an axis, and how to put its value in axis units."""

    channel: str
    convert: Callable[[float], float]
    approx: bool = False


def _identity(x: float) -> float:
    return x


def load_map(path: Path) -> Dict[str, AxisMapping]:
    """Read a map file; a missing or bad file (or entry) is skipped with a warning."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        axes = raw["axes"]
        if not isinstance(axes, dict):
            raise ValueError("'axes' is not an object")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(f"Live cursor axis map {path} not loaded: {exc}")
        return {}
    mapping = {}
    for name, entry in axes.items():
        try:
            channel = str(entry["channel"]).strip()
            if not channel:
                raise ValueError("empty channel")
            expr = entry.get("expr", "")
            convert = compile_formula(expr) if expr else _identity
            mapping[name] = AxisMapping(channel, convert, bool(entry.get("approx")))
        except (ChannelError, KeyError, TypeError, ValueError, AttributeError) as exc:
            logger.warning(f"Live cursor axis map: skipping {name!r}: {exc}")
    return mapping


_shipped: Optional[Dict[str, AxisMapping]] = None


def shipped_map() -> Dict[str, AxisMapping]:
    """The map shipped with the app, loaded once."""
    global _shipped
    if _shipped is None:
        _shipped = load_map(get_app_root() / MAP_FILE)
    return _shipped


def uses_shared_map(romid) -> bool:
    """The shared map describes the engine ECU, which the Datalogger polls.
    A transmission (TCM) definition reuses names like "RPM" for its own
    signals, so only its own ``logparam`` overrides apply."""
    model = getattr(romid, "model", "") or ""
    return "TCM" not in model.upper()


def resolve(
    axis: Optional[Table],
    mapping: Optional[Dict[str, AxisMapping]] = None,
    *,
    shared: bool = True,
) -> Optional[AxisMapping]:
    """The channel behind *axis*: its ``logparam`` first, then (when *shared*)
    the shipped map."""
    if axis is None:
        return None
    if axis.logparam:
        return AxisMapping(axis.logparam, _identity)
    if not shared:
        return None
    if mapping is None:
        mapping = shipped_map()
    return mapping.get(axis.name)
