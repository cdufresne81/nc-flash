"""The app's WiCAN driver for Dynamic Flash by block compare (WiCANFastFlasher)
and its wiring in the ECU window.

The firmware op itself is covered by test_ecu_wican_fast_flash.py (fake WiCAN);
here the client and the SD flasher it composes are fakes, so the test checks
the order of the host steps, the archive rule, the capability gate and how the
window reacts.
"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ecu import fast_flash_kernel as fk
from src.ecu import wican_fast_flash as wff
from src.ecu.exceptions import FastFlashUnavailable, ROMValidationError
from src.ecu.flash_manager import ARCHIVE_ASIDE_SUFFIX, FlashState
from src.ui.ecu_window import ECUProgrammingWindow, _FlashWorker
from tests.test_ecu_fast_flash import make_kernel

EXAMPLE_ROM = Path(__file__).parent.parent / "examples" / "lf9veb.bin"
KERNEL = fk.LoadedKernel(make_kernel(), "test", fk.KernelPin(0xC00, "", 3))
CAPS = {"fast_flash": 1, "ncfr_rev": 6, "ff_live": 1}


@pytest.fixture(scope="module")
def rom():
    return EXAMPLE_ROM.read_bytes()


class FakeSd:
    """Stands in for WiCANSdFlasher: records the shared host steps."""

    def __init__(self, calls):
        self.calls = calls
        self.prompt = None

    @contextmanager
    def _datalog_fence(self):
        self.calls.append("fence+")
        try:
            yield
        finally:
            self.calls.append("fence-")

    @property
    def _safeguards(self):
        return SimpleNamespace(_gate=lambda: self.calls.append("gate"))

    def _authenticate_ecu(self, *, flash_counter=True):
        self.calls.append(f"auth(counter={flash_counter})")

    def _check_ecu_generation(self, pkg):
        assert pkg.corrected_rom
        self.calls.append("generation")

    def _check_flash_counter(self):
        self.calls.append("counter")

    def set_unverified_generation_prompt(self, p):
        self.prompt = p

    def set_generation_verified_hook(self, h):
        pass


def _done(nothing=False):
    ev = wff.FFEvent("done", "NCFFDONE", {"nothing": "1" if nothing else "0"})
    return wff.FastFlashOutcome("L", ev, [ev])


class FakeClient:
    def __init__(self, calls, caps=CAPS, outcome=None, error=None, events=()):
        self.calls, self._caps = calls, caps
        self.outcome = outcome or _done()
        self.error, self.events = error, list(events)

    def caps(self):
        return self._caps

    def upload(self, pkg):
        self.calls.append("upload")

    def run(self, name, mode, *, on_event=None, progress=None):
        self.calls.append(f"run({mode})")
        for ev in self.events:
            on_event(ev)
            msg, pct = wff._progress_of(ev)
            if msg:
                progress(msg, pct)
        if self.error:
            raise self.error
        return self.outcome


@pytest.fixture(autouse=True)
def no_real_prep(monkeypatch):
    monkeypatch.setattr(
        "src.ecu.flash_prep.prepare_flash_image",
        lambda rom, start: (bytes(rom), b"", "NC2", []),
    )
    monkeypatch.setattr(
        "src.ecu.fast_flash_package.package_rom",
        lambda rom, kernel, version, **kw: SimpleNamespace(name="ff_test.ffp"),
    )


def _flasher(calls, **client_kw):
    return wff.WiCANFastFlasher(
        object(), client=FakeClient(calls, **client_kw), sd_flasher=FakeSd(calls)
    )


def _archive(tmp_path):
    path = tmp_path / "ecu.rda"
    path.write_bytes(b"old archive")
    return path


def test_live_mode_runs_the_session_steps_inside_the_fence(rom, tmp_path):
    calls = []
    archive = _archive(tmp_path)
    _flasher(calls).fast_flash(rom, str(archive), kernel=KERNEL)
    assert calls == [
        "fence+",
        "gate",
        "upload",
        "auth(counter=False)",
        "generation",
        "counter",
        "run(L)",
        "fence-",
    ]
    assert archive.read_bytes() == rom  # written only after the op is done


def test_resume_needs_no_session_and_keeps_the_archive_aside(rom, tmp_path):
    calls = []
    archive = _archive(tmp_path)
    _flasher(calls).fast_flash(rom, str(archive), kernel=KERNEL, resume=True)
    assert "run(R)" in calls
    assert not any(c.startswith("auth") or c == "counter" for c in calls)


@pytest.mark.parametrize(
    "caps", [None, {"fast_flash": 1, "ncfr_rev": 6, "ff_live": 0}, {"ncfr_rev": 6}]
)
def test_without_ff_live_nothing_is_sent(rom, tmp_path, caps):
    calls = []
    archive = _archive(tmp_path)
    with pytest.raises(FastFlashUnavailable, match="Nothing was changed"):
        _flasher(calls, caps=caps).fast_flash(rom, str(archive), kernel=KERNEL)
    assert calls == [] and archive.read_bytes() == b"old archive"


def test_refused_before_the_write_puts_the_archive_back(rom, tmp_path):
    calls = []
    archive = _archive(tmp_path)
    err = wff.WiCANFastFlashError(
        "Dynamic Flash did not start",
        state=wff.EcuState.UNTOUCHED,
        ponr=False,
        untouched_proof=True,  # a streamed FFERR with ponr=0
    )
    with pytest.raises(wff.WiCANFastFlashError):
        _flasher(calls, error=err).fast_flash(rom, str(archive), kernel=KERNEL)
    assert archive.read_bytes() == b"old archive"
    assert calls[-1] == "fence-"


def test_failure_after_the_write_started_leaves_no_archive(rom, tmp_path):
    calls = []
    archive = _archive(tmp_path)
    err = wff.WiCANFastFlashError(
        "Dynamic Flash stopped", state=wff.EcuState.NO_APPLICATION, ponr=True
    )
    events = [wff.FFEvent("step", "NCFFSTEP write", text="write")]
    with pytest.raises(wff.WiCANFastFlashError):
        _flasher(calls, error=err, events=events).fast_flash(
            rom, str(archive), kernel=KERNEL
        )
    assert not archive.exists()
    assert Path(str(archive) + ARCHIVE_ASIDE_SUFFIX).read_bytes() == b"old archive"


def test_nothing_to_do_is_identical(rom, tmp_path):
    calls = []
    archive = _archive(tmp_path)
    with pytest.raises(ROMValidationError, match="identical"):
        _flasher(calls, outcome=_done(nothing=True)).fast_flash(
            rom, str(archive), kernel=KERNEL
        )
    assert archive.read_bytes() == rom


def test_progress_follows_the_firmware_steps(rom):
    calls, seen = [], []
    events = [
        wff.FFEvent("step", "", text="compare"),
        wff.FFEvent("step", "", text="write"),
        wff.FFEvent("prog", "", {"done": "1", "total": "3"}),
        wff.FFEvent("step", "", text="verify"),
    ]
    _flasher(calls, events=events).fast_flash(
        rom, None, lambda p: seen.append(p.state), kernel=KERNEL
    )
    assert FlashState.COMPARING in seen and FlashState.PROGRAMMING in seen
    assert FlashState.VERIFYING in seen and seen[-1] == FlashState.COMPLETE


def test_generation_prompt_is_forwarded():
    calls = []
    sd = FakeSd(calls)
    f = wff.WiCANFastFlasher(object(), client=FakeClient(calls), sd_flasher=sd)
    f.set_unverified_generation_prompt("prompt")
    assert sd.prompt == "prompt"


# --- the window ---------------------------------------------------------------


class _OfferWindow(SimpleNamespace):
    _fast_flash_offer = ECUProgrammingWindow._fast_flash_offer
    _in_recovery = ECUProgrammingWindow._in_recovery
    _fast_kernel_running = ECUProgrammingWindow._fast_kernel_running


def _offer_window(caps):
    settings = MagicMock()
    settings.get_fast_flash_enabled.return_value = True
    return _OfferWindow(
        _main_window=SimpleNamespace(settings=settings),
        _session=SimpleNamespace(adapter_kind="wican"),
        _recovery=None,
        _fast_flash_refused=False,
        _wican_fast_flash_caps=lambda: caps,
    )


def test_window_offers_the_block_compare_on_wican_with_ff_live(rom):
    with patch("src.ui.ecu_window.load_kernel", return_value=KERNEL):
        kernel, _ = ECUProgrammingWindow._fast_flash_offer(_offer_window(CAPS), rom)
    assert kernel is KERNEL


@pytest.mark.parametrize("caps", [None, {"fast_flash": 1, "ncfr_rev": 6}])
def test_window_falls_back_silently_without_ff_live(rom, caps):
    with patch("src.ui.ecu_window.load_kernel", return_value=KERNEL):
        kernel, reason = ECUProgrammingWindow._fast_flash_offer(
            _offer_window(caps), rom
        )
    assert kernel is None and reason


def test_driver_for_wican_block_compare():
    settings = MagicMock()
    settings.get_wican_trim_staged_roms.return_value = True
    settings.get_wican_keep_staged_roms.return_value = 5
    fake = SimpleNamespace(
        _is_wican=lambda: True,
        _is_obdx=lambda: False,
        _session=MagicMock(is_connected=True),
        _session_acquired=False,
        _guard_override=True,  # the operator's override at the RPM gate
        _main_window=SimpleNamespace(settings=settings),
    )
    fake._session.acquire.return_value = (None, None, None, MagicMock())
    with (
        patch("src.ecu.wican_fast_flash.WiCANFastFlasher") as driver,
        patch("src.ecu.wican_sd_flash.WiCANSdFlasher") as sd,
    ):
        result = ECUProgrammingWindow._build_flash_driver(fake, "fast_flash", "t.bin")
    assert result is driver.return_value
    # Review L8: the override and the staged-ROM trim reach the inner flasher.
    sd.assert_called_once_with(
        fake._session.transport,
        source_name="t.bin",
        datalog=fake._session.wican_datalog,
        keep_staged_roms=5,
        allow_unread_voltage=True,
    )
    driver.assert_called_once_with(
        fake._session.transport,
        datalog=fake._session.wican_datalog,
        source_name="t.bin",
        sd_flasher=sd.return_value,
    )
    assert fake._guard_override is False  # covers this one flash only
    with patch("src.ecu.wican_flash.WICAN_WRITE_ENABLED", False):
        assert ECUProgrammingWindow._build_flash_driver(fake, "fast_flash") is None


@pytest.mark.parametrize(
    "state, unavailable",
    [(wff.EcuState.UNTOUCHED, True), (wff.EcuState.NO_APPLICATION, False)],
)
def test_worker_maps_wican_errors(qapp, state, unavailable):
    manager = MagicMock()
    err = wff.WiCANFastFlashError("Dynamic Flash: x", state=state, ponr=not unavailable)
    manager.fast_flash.side_effect = err
    worker = _FlashWorker(manager, "fast_flash", rom_data=b"", kernel=KERNEL)
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert worker.wican_ff_error is err
    assert worker.fast_flash_unavailable is unavailable
    assert errors == ["Dynamic Flash: x"]


def test_host_caps_never_read_on_the_calling_thread():
    """The /host_caps HTTP read runs on a background thread; until it arrives
    the block compare is not offered (None)."""
    import threading

    gate = threading.Event()
    seen_threads = []

    def slow_caps(self):
        seen_threads.append(threading.current_thread())
        gate.wait(2)
        return CAPS

    fake = SimpleNamespace(
        _session=SimpleNamespace(transport=SimpleNamespace(host="192.0.2.1"))
    )
    with patch("src.ecu.wican_config.WiCANConfigurator.host_caps", slow_caps):
        assert ECUProgrammingWindow._wican_fast_flash_caps(fake) is None
        assert ECUProgrammingWindow._wican_fast_flash_caps(fake) is None  # pending
        gate.set()
        for _ in range(100):
            if fake._wican_caps_cache[1].get("caps"):
                break
            threading.Event().wait(0.02)
        assert ECUProgrammingWindow._wican_fast_flash_caps(fake) == CAPS
    assert seen_threads and seen_threads[0] is not threading.current_thread()
    assert len(seen_threads) == 1  # read once per connection


def test_untouched_wican_refusal_gives_one_instruction(qtbot):
    """The firmware asks for an ignition cycle; the window says the same and
    then the standard method, never 'the ECU is restarting'."""
    from tests.test_ecu_window_post_flash import (
        _MainWindow,
        _session,
    )
    from src.ui import ecu_window

    shown = []
    with (
        patch.object(
            ecu_window.QMessageBox,
            "information",
            lambda *a, **k: shown.append(a),
        ),
        patch.object(ECUProgrammingWindow, "_on_connect", lambda self: None),
    ):
        window = ECUProgrammingWindow(_MainWindow("wican"))
        window._ap_switch = MagicMock()
        qtbot.addWidget(window)
        window._session = _session("wican")
        window._session_acquired = True
        window._current_operation = "fast_flash"
        window._stack.setCurrentIndex(1)
        err = wff.WiCANFastFlashError(
            "Dynamic Flash did not start: x. The ECU was not changed. Switch the "
            "ignition off and on before trying again.",
            state=wff.EcuState.UNTOUCHED,
        )
        worker = MagicMock(
            result=None,
            needs_ecu_reset=False,
            error_kind="",
            fast_flash_unavailable=True,
            wican_ff_error=err,
        )
        window._on_flash_finished(False, None, worker, str(err))
        qtbot.waitUntil(lambda: bool(shown), timeout=2000)
    text = shown[0][2]
    assert "restarting" not in text
    assert "Switch the ignition off and on, then click Connect" in text
    assert "standard method" in text
