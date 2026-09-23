"""Process-wide GPU graph runtime: engine choice and adapter probe.

**All pygfx/wgpu work happens on the GUI thread.** An earlier version warmed the
GPU up on a background thread shortly after launch (probe, device creation,
offscreen pre-render). With the user clicking around meanwhile it crashed the
interpreter natively — access violation in python314.dll, no traceback, twice
in the field (Sep 23 2026). Stress test (GUI opening/clicking table windows
while a background thread ran): render+text 2/4 crashes, render without text
1/6, the production warm-up body 2/6; a pure-Python background thread or no
thread 0/12. Do not reintroduce background GPU work.

Cost: the first graph of a session pays adapter + device + shader compile
(~4-5 s) on the GUI thread behind the "Preparing graph…" placeholder; later
graphs reuse the device.

Nothing here imports pygfx/wgpu at module import time (startup stays lean).
"""

import logging
import os
from typing import Callable

from PySide6.QtCore import QTimer

logger = logging.getLogger(__name__)

ENGINE_AUTO = "auto"
ENGINE_CLASSIC = "classic"
ENGINE_GPU = "gpu"

#: Render-target pixel ratio cap. pygfx defaults to the screen DPR (2.25 on a
#: typical hi-DPI laptop → ~92 MB GPU per graph); 1.5 measured ~57 MB with no
#: visible quality loss (tools/graph_eval, Sep 2026).
PIXEL_RATIO = 1.5

_state = {"probed": False, "available": False, "reason": "not probed"}


def requested_engine() -> str:
    """Engine requested by env (tests/support) or settings; default auto."""
    env = os.environ.get("NCFLASH_GRAPH_ENGINE", "").strip().lower()
    if env in (ENGINE_AUTO, ENGINE_CLASSIC):
        return env
    try:
        from ..utils.settings import get_settings

        value = get_settings().get_graph_engine()
    except Exception:  # noqa: BLE001 - settings must never break graphs
        value = ENGINE_AUTO
    return value if value in (ENGINE_AUTO, ENGINE_CLASSIC) else ENGINE_AUTO


def _probe() -> None:
    """Import pygfx and create the shared adapter + device (GUI thread only)."""
    if _state["probed"]:
        return
    try:
        import wgpu
        import pygfx

        adapter = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
        if adapter is None:
            raise RuntimeError("no WebGPU adapter")
        info = adapter.info
        reason = f"{info.get('device', '?')} ({info.get('backend_type', '?')})"
        pygfx.renderers.wgpu.get_shared()  # adapter + device, reused by graphs
        available = True
        logger.info("GPU graph ready on %s", reason)
    except Exception as exc:  # noqa: BLE001 - any failure means "use classic"
        available, reason = False, f"{type(exc).__name__}: {exc}"
        logger.info("GPU graph unavailable (%s); classic graph will be used", reason)
    _state.update(probed=True, available=available, reason=reason)


def is_ready() -> bool:
    """True once the engine decision is final."""
    return requested_engine() == ENGINE_CLASSIC or _state["probed"]


def engine_decision() -> tuple:
    """(engine, reason). Only meaningful once :func:`is_ready` is True."""
    if requested_engine() == ENGINE_CLASSIC:
        return ENGINE_CLASSIC, "classic engine selected in settings/env"
    if _state["available"]:
        return ENGINE_GPU, _state["reason"]
    return ENGINE_CLASSIC, _state["reason"]


def when_ready(callback: Callable[[], None], context) -> None:
    """Run ``callback`` once the engine decision is final.

    The first time, the (blocking, ~1-2.5 s) probe runs on the next event-loop
    turn so the caller's "Preparing graph…" placeholder paints first.
    ``context`` (a QObject) cancels the callback if it is destroyed first.
    """
    if is_ready():
        callback()
        return

    def _run():
        _probe()
        callback()

    QTimer.singleShot(30, context, _run)  # placeholder paints before the stall


def wait_until_ready() -> bool:
    """Blocking probe for tests/tools (the app itself uses :func:`when_ready`)."""
    if requested_engine() != ENGINE_CLASSIC:
        _probe()
    return is_ready()
