#!/usr/bin/env python3
"""Device-level verification of the dead-man's-switch firmware (#36 GAP-2).

Run AFTER OTA-ing the dead-man's-switch coexistence firmware, BEFORE any ECU
flash. This exercises the NEW /datalog ops (bus_claim / bus_release / keepalive)
and the firmware dead-man REAPER on real hardware **without ever putting the ECU
into a programming session** — so it is brick-safe to run against the live bench.

Checks (each independent, all non-destructive, NO ECU write):
  1. Dedicated SLCAN port 35001 version ping -> NCFRv >= COEXIST_MIN_FW_REV.
  2. GET /datalog exposes the dead-man state fields (host_bus_claimed, park_token,
     claim_token, lease_ttl_ms, claim_ttl_ms, bus_idle_ms, stuck_flash_alarm) with
     the expected lease TTLs.
  3. Full lease round-trip over raw HTTP (no host keepalive daemon, full control):
     bus_claim -> claim_token + host_bus_claimed; pause -> park_token + parked;
     keepalive(both) renews; bus_release(claim) clears claim; resume(park) clears
     park; a STALE-token resume returns HTTP 409.
  4. (--reaper, opt-in, slow) The dead-man reaper itself: raise bus_claim + pause
     with NO 35001 client connected (owner 'gone') and send NO keepalives, then
     poll GET /datalog until the firmware reaper auto-clears BOTH the claim and the
     park (host vanished -> lease expiry -> bus idle -> auto-resume). Proves the
     wireless analog of "cable unplugged -> auto-recover" with zero ECU contact.

Usage:
    python tools/wican_deadman_verify.py [--host 192.168.1.169] [--reaper]
                                         [--reaper-timeout 110]
Exit code 0 iff every requested check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.constants import (  # noqa: E402
    COEXIST_MIN_FW_REV,
    HOST_CLAIM_LEASE_TTL_S,
    PARK_LEASE_TTL_S,
    WICAN_DEDICATED_SLCAN_PORT,
)
from src.ecu.wican_sd_flash import _parse_fw_rev  # noqa: E402
from src.ecu.wican_transport import WiCANTransport  # noqa: E402

_DEADMAN_FIELDS = (
    "flash_active",
    "datalog_parked",
    "host_bus_claimed",
    "park_token",
    "claim_token",
    "lease_ttl_ms",
    "claim_ttl_ms",
    "bus_idle_ms",
    "stuck_flash_alarm",
)


def _ok(label: str, passed: bool, detail: str = "") -> bool:
    print(
        f"  [{'PASS' if passed else 'FAIL'}] {label}"
        + (f" — {detail}" if detail else ""),
        flush=True,
    )
    return passed


def _req(host: str, op_query: str, method: str = "POST", timeout: float = 5.0):
    """Raw /datalog request -> (status, dict|None). Never raises (so a 409 is data, not an error)."""
    url = f"http://{host}:80/datalog" + (f"?{op_query}" if op_query else "")
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001
        print(
            f"    (request {op_query!r} failed: {type(exc).__name__}: {exc})",
            flush=True,
        )
        return None, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def check_version(host: str) -> bool:
    print(
        f"[1] Dedicated SLCAN port {WICAN_DEDICATED_SLCAN_PORT} version ping",
        flush=True,
    )
    t = WiCANTransport(host, WICAN_DEDICATED_SLCAN_PORT)
    try:
        t.open()
    except Exception as exc:  # noqa: BLE001
        return _ok(
            "connect 35001",
            False,
            f"{type(exc).__name__}: {exc} (deadman firmware flashed?)",
        )
    try:
        rev = _parse_fw_rev(t.version_ping(window_ms=3000))
        return _ok(
            "version ping", rev is not None and rev >= COEXIST_MIN_FW_REV, f"rev={rev}"
        )
    finally:
        try:
            t.close()
        except Exception:
            pass


def check_state_fields(host: str) -> bool:
    print("[2] GET /datalog exposes dead-man state fields", flush=True)
    status, s = _req(host, "", method="GET")
    if not _ok(
        "GET /datalog", status == 200 and isinstance(s, dict), f"status={status}"
    ):
        return False
    missing = [f for f in _DEADMAN_FIELDS if f not in s]
    ok_fields = _ok(
        "all deadman fields present",
        not missing,
        f"missing={missing}" if missing else f"{s}",
    )
    ok_ttl = _ok(
        "lease TTLs match host contract",
        s.get("lease_ttl_ms") == int(PARK_LEASE_TTL_S * 1000)
        and s.get("claim_ttl_ms") == int(HOST_CLAIM_LEASE_TTL_S * 1000),
        f"lease_ttl_ms={s.get('lease_ttl_ms')} claim_ttl_ms={s.get('claim_ttl_ms')}",
    )
    return ok_fields and ok_ttl


def check_roundtrip(host: str) -> bool:
    print(
        "[3] Lease round-trip: bus_claim/pause/keepalive/bus_release/resume + 409",
        flush=True,
    )
    # bus_claim -> claim_token
    st, claim = _req(host, "op=bus_claim")
    claim_tok = (claim or {}).get("claim_token")
    ok_claim = _ok(
        "bus_claim issues claim_token + host_bus_claimed",
        st == 200
        and isinstance(claim_tok, int)
        and claim.get("host_bus_claimed") is True,
        f"-> {claim}",
    )
    # pause -> park_token
    st, park = _req(host, "op=pause")
    park_tok = (park or {}).get("park_token")
    ok_pause = _ok(
        "pause issues park_token + datalog_parked",
        st == 200 and isinstance(park_tok, int) and park.get("datalog_parked") is True,
        f"-> park_token={park_tok} parked={(park or {}).get('datalog_parked')}",
    )
    # keepalive(both) renews
    st, ka = _req(host, f"op=keepalive&park_token={park_tok}&claim_token={claim_tok}")
    ok_ka = _ok("keepalive(both) accepted", st == 200, f"status={st}")
    # bus_release(claim)
    st, rel = _req(host, f"op=bus_release&token={claim_tok}")
    ok_rel = _ok(
        "bus_release clears claim",
        st == 200 and (rel or {}).get("host_bus_claimed") is False,
        f"-> host_bus_claimed={(rel or {}).get('host_bus_claimed')}",
    )
    # resume(park)
    st, res = _req(host, f"op=resume&token={park_tok}")
    ok_res = _ok(
        "resume clears park",
        st == 200 and (res or {}).get("datalog_parked") is False,
        f"-> datalog_parked={(res or {}).get('datalog_parked')}",
    )
    # a stale resume (re-use the now-cleared park token) must 409
    st_stale, _ = _req(host, f"op=resume&token={park_tok}")
    ok_409 = _ok(
        "stale-token resume -> HTTP 409", st_stale == 409, f"status={st_stale}"
    )
    return ok_claim and ok_pause and ok_ka and ok_rel and ok_res and ok_409


def check_reaper(host: str, timeout_s: float) -> bool:
    print(
        f"[4] Dead-man REAPER auto-resume (host 'vanishes', no keepalive; <= {timeout_s:.0f}s)",
        flush=True,
    )
    # Arm claim+park with NO 35001 client open -> owner 'gone' from the start; send NO keepalives.
    _req(host, "op=bus_claim")
    _req(host, "op=pause")
    print(
        "    armed claim+park, no keepalive -> waiting for firmware reaper...",
        flush=True,
    )
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        st, s = _req(host, "", method="GET")
        if isinstance(s, dict):
            last = s
            print(
                f"    t+{timeout_s - (deadline - time.monotonic()):5.0f}s  "
                f"claimed={s.get('host_bus_claimed')} parked={s.get('datalog_parked')} "
                f"bus_idle_ms={s.get('bus_idle_ms')}",
                flush=True,
            )
            if s.get("host_bus_claimed") is False and s.get("datalog_parked") is False:
                return _ok("reaper auto-resumed (claim + park cleared)", True)
        time.sleep(5)
    # Timed out: clean up so we never leave the bench parked.
    _req(host, "op=bus_release&token=0")
    _req(host, "op=resume&token=0")
    detail = (
        f"still claimed/parked after {timeout_s:.0f}s; last bus_idle_ms="
        f"{(last or {}).get('bus_idle_ms')} (a continuously busy bus correctly blocks resume)"
    )
    return _ok("reaper auto-resumed", False, detail)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="192.168.1.169")
    ap.add_argument(
        "--reaper",
        action="store_true",
        help="also run the slow live reaper auto-resume test",
    )
    ap.add_argument(
        "--reaper-timeout",
        type=float,
        default=HOST_CLAIM_LEASE_TTL_S + 30.0,  # claim TTL + grace + margin
        help="seconds to wait for the firmware reaper",
    )
    args = ap.parse_args(argv)

    print(
        f"=== #36 dead-man's-switch verify against {args.host} (READ-ONLY, no ECU write) ==="
    )
    results = [
        check_version(args.host),
        check_state_fields(args.host),
        check_roundtrip(args.host),
    ]
    if args.reaper:
        results.append(check_reaper(args.host, args.reaper_timeout))
    else:
        print(
            "[4] reaper auto-resume test SKIPPED (pass --reaper to run; ~100s)",
            flush=True,
        )

    passed = all(results)
    print()
    print(
        "=== RESULT:",
        "ALL PASS" if passed else "FAILURES — do NOT proceed to a flash",
        "===",
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
