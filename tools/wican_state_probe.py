"""Fast bootloader-vs-app state probe over WiCAN (short timeouts, no 60s hang).

Discriminator: a no-auth ReadMemoryByAddress (0x23).
  * NRC 0x11 (serviceNotSupported)      -> ECU is in the BOOTLOADER (app not booted).
  * NRC 0x33 (securityAccessDenied)     -> ECU is running the APP (service exists,
                                           just needs auth) -> flash booted fine.
  * positive response                   -> APP, unprotected read.
Also fires TesterPresent and tries ROM-ID for extra signal.
"""

import argparse

from src.ecu.protocol import UDSConnection
from src.ecu.exceptions import NegativeResponseError
from src.ecu.wican_transport import WiCANTransport

T = 3000  # ms per request — fail fast


def probe(uds, label, fn):
    try:
        r = fn()
        print(
            f"  {label}: POSITIVE  resp={r.hex() if isinstance(r, (bytes, bytearray)) else r!r}",
            flush=True,
        )
        return ("pos", r)
    except NegativeResponseError as e:
        print(
            f"  {label}: NRC 0x{e.nrc:02X} ({getattr(e, 'description', '')})",
            flush=True,
        )
        return ("nrc", e.nrc)
    except Exception as e:  # noqa: BLE001
        print(f"  {label}: {type(e).__name__}: {e}", flush=True)
        return ("err", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.169")
    ap.add_argument("--port", type=int, default=35000)
    args = ap.parse_args()

    t = WiCANTransport(host=args.host, port=args.port)
    t.open()
    uds = UDSConnection(t)

    print("=== WiCAN ECU state probe ===", flush=True)
    probe(
        uds,
        "TesterPresent (0x3E)",
        lambda: uds.send_request(0x3E, b"\x00", timeout=T, pending_max=T),
    )
    kind, val = probe(
        uds,
        "ReadMemoryByAddress (0x23 @0)",
        lambda: uds.read_memory_by_address(0x00000000, 0x10, timeout=T, pending_max=T),
    )
    probe(
        uds,
        "ReadDataByIdentifier ROM-ID",
        lambda: uds.send_request(0x22, bytes([0xE6, 0x11]), timeout=T, pending_max=T),
    )

    print("\n=== verdict ===")
    if kind == "nrc" and val == 0x11:
        print("  RMBA -> NRC 0x11: ECU is in the BOOTLOADER. The flashed image did")
        print("  NOT take over as the running application after the power cycle.")
    elif kind == "nrc" and val == 0x33:
        print("  RMBA -> NRC 0x33: ECU is running the APPLICATION (auth required).")
        print("  The flash booted; 'no broadcast' is a separate/benign question.")
    elif kind == "pos":
        print("  RMBA positive: APPLICATION running (unprotected read).")
    else:
        print("  Inconclusive (no clean RMBA response). See lines above.")

    t.close()


if __name__ == "__main__":
    main()
