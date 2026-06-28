#!/usr/bin/env python3
"""Meter raw WiFi+TCP upload throughput to a WiCAN PRO over HTTP.

This answers a single question that gates the "Option B" SD-staged flash design:
**how long does it take to push ~1 MB to the WiCAN over WiFi?** All of our other
throughput numbers (fast-read ~214 s, SLCAN ~1.4 KB/s) are ECU-limited, not
WiFi-limited, so they don't answer it.

Safety / method
---------------
The deployed firmware has no generic ROM->/sdcard upload endpoint (FTP is
compiled out; ``/upload/car_data.json`` targets the small internal LittleFS and
clobbers config; ``/upload/ota.bin`` is a destructive OTA + reboot). So instead
we abuse a *non-destructive* endpoint purely as a byte sink:

``POST /store_car_data`` receives the **entire** request body into PSRAM (up to
``MAX_FILE_SIZE`` = 2000 KB) *before* it validates the payload as JSON. We send
**invalid** JSON (a run of ``x`` bytes), so the device:
  * reads every byte off the wire (this is the transfer we time), then
  * fails ``cJSON_Parse`` and returns ``400`` **without** writing any file.

Net effect: a full N-byte upload is metered with zero persistence, no CAN/BLE
disable, and no reboot -- the ECU link is left untouched. A ``400`` response is
the *expected, healthy* result and is asserted as proof nothing was written.

This isolates WiFi + TCP + PSRAM-copy throughput. It does NOT include the SD
write (invalid JSON is rejected before the ``fwrite``). That's the right proxy
for the Option B upload step: SD write (~10-50 MB/s) is 1-2 orders of magnitude
faster than the WiFi link, so the network is the bottleneck and this number is
the dominant term.

Usage
-----
    python tools/wican_upload_meter.py                 # default sweep against .169
    python tools/wican_upload_meter.py --sizes 1024 --repeat 5
    python tools/wican_upload_meter.py --host 192.168.4.1
"""

from __future__ import annotations

import argparse
import http.client
import statistics
import sys
import time

DEFAULT_HOST = "192.168.1.169"
DEFAULT_PORT = 80
SINK_ENDPOINT = "/store_car_data"  # drains body to PSRAM, rejects non-JSON w/o writing
# Firmware guard: store_car_data rejects content_len > MAX_FILE_SIZE (2000 KB)
# before draining, so keep every payload under that ceiling.
MAX_PAYLOAD_KB = 2000


def _post_sink(host: str, port: int, endpoint: str, size: int, timeout: float):
    """POST `size` bytes of invalid JSON; return (seconds, http_status).

    Times the whole request: body send + device drain + response. The body is a
    run of 'x' (not valid JSON) so the firmware rejects it with 400 after
    reading every byte -- nothing is persisted.
    """
    body = b"x" * size
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        t0 = time.monotonic()
        conn.request(
            "POST",
            endpoint,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(size),
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        resp.read()  # ensure the response is fully drained before stopping the clock
        dt = time.monotonic() - t0
        return dt, resp.status
    finally:
        conn.close()


def _fmt_rate(size: int, seconds: float) -> str:
    mbps = (size / (1024 * 1024)) / seconds
    return f"{mbps:6.2f} MB/s  ({mbps * 8:6.2f} Mbit/s)"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--host", default=DEFAULT_HOST, help="WiCAN HTTP host (default %(default)s)"
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="HTTP port (default %(default)s)"
    )
    p.add_argument(
        "--endpoint",
        default=SINK_ENDPOINT,
        help="byte-sink endpoint (default %(default)s)",
    )
    p.add_argument(
        "--sizes",
        default="256,512,1024",
        help="comma-separated payload sizes in KB (default %(default)s)",
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="timed runs per size (default %(default)s)",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, help="per-request timeout seconds"
    )
    p.add_argument(
        "--no-warmup", action="store_true", help="skip the discarded warm-up run"
    )
    args = p.parse_args(argv)

    try:
        sizes_kb = [int(s) for s in args.sizes.split(",") if s.strip()]
    except ValueError:
        print(
            f"ERROR: --sizes must be comma-separated integers, got {args.sizes!r}",
            file=sys.stderr,
        )
        return 2
    for kb in sizes_kb:
        if kb <= 0 or kb > MAX_PAYLOAD_KB:
            print(
                f"ERROR: size {kb} KB out of range (1..{MAX_PAYLOAD_KB})",
                file=sys.stderr,
            )
            return 2

    print(f"WiCAN upload meter -> http://{args.host}:{args.port}{args.endpoint}")
    print(
        "  (invalid-JSON byte sink: device drains the body then 400s, nothing written)\n"
    )

    # Warm-up: first request pays TCP/WiFi ramp-up; discard it so it doesn't skew medians.
    if not args.no_warmup:
        try:
            dt, status = _post_sink(
                args.host, args.port, args.endpoint, 64 * 1024, args.timeout
            )
            print(
                f"  warm-up   64 KB -> HTTP {status} in {dt * 1000:6.0f} ms (discarded)\n"
            )
        except OSError as e:
            print(
                f"ERROR: cannot reach device for warm-up: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return 1

    one_mb_projection = None
    for kb in sizes_kb:
        size = kb * 1024
        times = []
        bad_status = None
        for i in range(args.repeat):
            try:
                dt, status = _post_sink(
                    args.host, args.port, args.endpoint, size, args.timeout
                )
            except OSError as e:
                print(
                    f"  {kb:5d} KB run {i + 1}: ERROR {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return 1
            # 400 == healthy (invalid JSON rejected, nothing written). Anything
            # else means the device may have persisted -- flag it loudly.
            if status != 400:
                bad_status = status
            times.append(dt)
            print(
                f"  {kb:5d} KB run {i + 1}/{args.repeat}: {dt * 1000:7.1f} ms  {_fmt_rate(size, dt)}  [HTTP {status}]"
            )

        med = statistics.median(times)
        best = min(times)
        print(
            f"  {kb:5d} KB  median {med * 1000:7.1f} ms -> {_fmt_rate(size, med)}   (best {_fmt_rate(size, best)})"
        )
        if bad_status is not None:
            print(
                f"  !! WARNING: saw HTTP {bad_status} (expected 400). The body may have been "
                f"persisted -- verify car_data.json was not overwritten.",
                file=sys.stderr,
            )
        # Project a 1 MB upload time from this size's median throughput.
        rate = size / med  # bytes/sec
        one_mb_projection = (1024 * 1024) / rate
        print(
            f"        -> projected 1 MB upload at this rate: {one_mb_projection:5.2f} s\n"
        )

    if one_mb_projection is not None:
        print(
            "Summary: the WiFi+TCP link sustains the rate above. Option B total budget\n"
            f"  ~= upload (~{one_mb_projection:.1f} s for 1 MB) + ~55 s firmware flash."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
