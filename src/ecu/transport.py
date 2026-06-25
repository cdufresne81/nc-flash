"""
ECU transport abstraction.

Defines the seam between the UDS protocol layer and the underlying
physical/link transport (J2534 PassThru today; Python ISO-TP over
SLCAN/WiCAN in future). The seam is deliberately placed at the
**UDS-message level**: callers send and receive one complete UDS
payload at a time and never see ISO-TP framing, CAN arbitration IDs,
or PassThru structures.

A "UDS payload" is the Service Identifier byte (SID) followed by the
service data — exactly what UDSConnection.send_request builds as
``bytes([service_id]) + data`` and what it parses on receive. The
transport is responsible for adding transport framing on send and
stripping it on receive, so that what crosses this boundary is always
raw UDS bytes.

Core modules in this package (transport/isotp/slcan/wican) MUST remain
headless and unit-testable: they do not import PySide6.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable, Mapping, Optional

logger = logging.getLogger(__name__)


class EcuTransport(ABC):
    """
    Abstract transport for exchanging complete UDS messages with an ECU.

    Implementations own the transport framing (ISO-TP segmentation,
    CAN IDs, device structures). Callers work purely in UDS payload
    bytes.

    Contract:
        - A "payload" passed to :meth:`send_message` is a complete UDS
          request: the SID byte followed by the service data, with NO
          transport framing (no ISO-TP PCI, no CAN arbitration ID).
        - A payload returned by :meth:`receive_message` is a complete,
          reassembled UDS response in the same form: positive/negative
          SID byte followed by its data, with all transport framing
          stripped.
        - Higher layers (UDSConnection) handle UDS semantics:
          positive/negative response matching, NRC 0x78 (response
          pending) retries, and timeout policy. The transport performs
          a single send and a single receive per call and does NOT
          interpret UDS contents.
    """

    @abstractmethod
    def open(self) -> None:
        """
        Prepare the transport for message exchange.

        Implementations that own no resources (e.g. a wrapper around an
        already-open device) may implement this as a no-op. Must be
        safe to call before the first send/receive.
        """
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """
        Release any resources owned by the transport.

        Should be safe to call multiple times. Implementations that do
        not own the underlying device lifecycle may implement this as a
        no-op.
        """
        raise NotImplementedError

    @abstractmethod
    def send_message(self, payload: bytes, timeout_ms: int) -> None:
        """
        Send one complete UDS request payload.

        The implementation adds transport framing (ISO-TP segmentation,
        CAN ID) as needed. This performs a single transmit; it does not
        wait for or interpret any response.

        Args:
            payload: Complete UDS request bytes (SID + data), with no
                transport framing.
            timeout_ms: Transmit timeout in milliseconds.

        Raises:
            Transport-specific errors (e.g. J2534Error) propagate
            unchanged so the caller can react to bus/device failures.
        """
        raise NotImplementedError

    @abstractmethod
    def receive_message(self, timeout_ms: int) -> Optional[bytes]:
        """
        Receive one complete, reassembled UDS response payload.

        The implementation strips transport framing and returns only the
        UDS bytes. A single read is attempted; the caller is responsible
        for any retry/response-pending loop.

        Args:
            timeout_ms: Receive timeout in milliseconds.

        Returns:
            Complete UDS response bytes (SID + data), or ``None`` if no
            message arrived within the timeout. ``None`` is also returned
            when a frame arrives that carries no UDS payload (the caller
            treats this the same as a timeout and may read again).

        Raises:
            Transport-specific errors (e.g. J2534Error) propagate
            unchanged.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable transport name for logging/UI (e.g. ``"J2534"``)."""
        raise NotImplementedError

    def flush(self) -> None:
        """
        Discard any buffered/in-flight RX data so the next receive starts clean.

        Default is a no-op: a reliable link (J2534) has nothing to flush. A
        lossy link (WiCAN) overrides this to drain stale frames left over from a
        timed-out/aborted exchange, so a *read retry* re-requesting the same
        block is not corrupted by leftover frames from the failed attempt. Only
        ever called on the idempotent read path — never mid-flash.
        """
        return None


class J2534Transport(EcuTransport):
    """
    EcuTransport backed by a J2534 PassThru ISO-TP channel.

    Behaviour-identical to the raw I/O that ``UDSConnection.send_request``
    performs today:

        - Send: build an ISO-15765 message with :func:`build_isotp_msg`
          (which prepends the 4-byte CAN request ID) and write it on the
          channel.
        - Receive: read one message; the J2534 device returns the CAN
          response ID as a 4-byte prefix, so the UDS payload is
          ``Data[4:DataSize]``.

    Device lifecycle: this transport does NOT own the
    :class:`~src.ecu.j2534.J2534Device`. The device is opened, connected,
    and closed by :class:`~src.ecu.session.ECUSession` /
    :class:`~src.ecu.flash_manager.FlashManager`. Accordingly
    :meth:`open` and :meth:`close` are no-ops here.
    """

    def __init__(self, device, channel_id: int):
        """
        Args:
            device: An open ``J2534Device`` instance.
            channel_id: Connected channel ID from ``device.connect()``.
        """
        self._device = device
        self._channel_id = channel_id

    def open(self) -> None:
        """No-op: the J2534 device lifecycle is owned by ECUSession/FlashManager."""
        return None

    def close(self) -> None:
        """No-op: the J2534 device lifecycle is owned by ECUSession/FlashManager."""
        return None

    def send_message(self, payload: bytes, timeout_ms: int) -> None:
        """Send one UDS payload as a single ISO-15765 message on the channel.

        Lets ``J2534Error`` (and any other device error) propagate.
        """
        # Imported here to keep import-time side effects out of this module.
        from .j2534 import build_isotp_msg

        msg = build_isotp_msg(payload)
        self._device.write_msgs(self._channel_id, [msg], timeout_ms)

    def receive_message(self, timeout_ms: int) -> Optional[bytes]:
        """Read one message and return its UDS payload (``Data[4:DataSize]``).

        Returns ``None`` if no message arrived (read returned ``[]``) or
        the message carried no payload beyond the 4-byte CAN ID prefix
        (``DataSize <= 4``). Lets ``J2534Error`` propagate.
        """
        msgs = self._device.read_msgs(self._channel_id, 1, timeout_ms)
        if not msgs:
            return None

        msg = msgs[0]
        # First 4 bytes are the CAN arbitration ID; UDS payload follows.
        if msg.DataSize <= 4:
            return None

        return bytes(msg.Data[4 : msg.DataSize])

    @property
    def description(self) -> str:
        return "J2534"


class FakeTransport(EcuTransport):
    """
    In-memory transport for unit tests (no hardware, no device).

    Records every payload passed to :meth:`send_message` and serves
    responses for :meth:`receive_message` from either a scripted queue
    or a callable.

    Two modes (mutually compatible — the callable wins if provided):

        - Scripted queue: pass ``responses=[b"...", b"...", ...]``.
          Each :meth:`receive_message` pops the next payload from the
          front of the queue. When the queue is empty it returns
          ``None`` (modelling a timeout). A queued value of ``None`` is
          a valid scripted "timeout" entry.
        - Callable: pass ``responder=fn`` where ``fn(timeout_ms) ->
          bytes | None``. It is invoked on every :meth:`receive_message`
          and its return value is used directly.

    Attributes:
        sent: List of ``(payload, timeout_ms)`` tuples recorded in send
            order. ``sent_payloads`` is a convenience view of just the
            payloads.
    """

    def __init__(
        self,
        responses: Optional[list[Optional[bytes]]] = None,
        responder: Optional[Callable[[int], Optional[bytes]]] = None,
        description: str = "Fake",
    ):
        """
        Args:
            responses: Optional scripted list of response payloads (or
                ``None`` entries to model timeouts), served front-to-back.
            responder: Optional callable ``fn(timeout_ms) -> bytes | None``
                invoked per receive. Takes precedence over ``responses``.
            description: Name returned by :attr:`description`.
        """
        self._responses: list[Optional[bytes]] = list(responses or [])
        self._responder = responder
        self._description = description
        self.sent: list[tuple[bytes, int]] = []
        self.opened = False
        self.closed = False

    def open(self) -> None:
        """Mark the transport opened (records the call for assertions)."""
        self.opened = True

    def close(self) -> None:
        """Mark the transport closed (records the call for assertions)."""
        self.closed = True

    def send_message(self, payload: bytes, timeout_ms: int) -> None:
        """Record the sent payload and timeout; sends nowhere."""
        self.sent.append((bytes(payload), timeout_ms))

    def receive_message(self, timeout_ms: int) -> Optional[bytes]:
        """Return the next scripted/computed response, or ``None`` if exhausted."""
        if self._responder is not None:
            return self._responder(timeout_ms)
        if not self._responses:
            return None
        return self._responses.pop(0)

    def queue_response(self, payload: Optional[bytes]) -> None:
        """Append a response to the scripted queue (ignored in responder mode)."""
        self._responses.append(payload)

    @property
    def sent_payloads(self) -> list[bytes]:
        """Just the payloads from :attr:`sent`, in send order."""
        return [payload for payload, _ in self.sent]

    @property
    def description(self) -> str:
        return self._description


def create_ecu_transport(config: Mapping[str, object]) -> EcuTransport:
    """
    Build an :class:`EcuTransport` from a simple, serialisable config.

    This is the single place that knows how to map a transport selection
    (e.g. saved in app settings) to a concrete transport. J2534 is the
    default everywhere; WiCAN is opt-in.

    Config schema (``config["kind"]`` selects the transport):

        - ``{"kind": "j2534", "device": <J2534Device>, "channel_id": <int>}``
          Wraps an already-open J2534 device/channel in a
          :class:`J2534Transport`. The device lifecycle stays with the
          caller (ECUSession/FlashManager); this only adapts I/O.

        - ``{"kind": "wican", "host": <str>, "port": <int>, ...}``
          Builds a :class:`~src.ecu.wican_transport.WiCANTransport` over an
          SLCAN-over-TCP socket. Optional keys ``tx_id``, ``rx_id``,
          ``connect_timeout_ms``, and ``padding`` are forwarded to the
          constructor. The caller is responsible for calling
          :meth:`~EcuTransport.open` / :meth:`~EcuTransport.close`.

    Args:
        config: Mapping with a ``"kind"`` key and kind-specific parameters.

    Returns:
        A concrete :class:`EcuTransport` instance (not yet opened).

    Raises:
        ValueError: If ``kind`` is missing/unknown or required keys are absent.
    """
    kind = config.get("kind")
    if kind is None:
        raise ValueError("transport config missing required 'kind'")

    if kind == "j2534":
        try:
            device = config["device"]
            channel_id = config["channel_id"]
        except KeyError as exc:
            raise ValueError(
                f"j2534 transport config missing required key: {exc}"
            ) from exc
        return J2534Transport(device, int(channel_id))  # type: ignore[arg-type]

    if kind == "wican":
        # Imported lazily: wican_transport imports this module, so importing
        # it at module scope would create a circular import.
        from .wican_transport import WiCANTransport, DEFAULT_CONNECT_TIMEOUT_MS

        try:
            host = config["host"]
            port = config["port"]
        except KeyError as exc:
            raise ValueError(
                f"wican transport config missing required key: {exc}"
            ) from exc

        from .constants import CAN_REQUEST_ID, CAN_RESPONSE_ID

        return WiCANTransport(
            host=str(host),
            port=int(port),  # type: ignore[arg-type]
            tx_id=int(config.get("tx_id", CAN_REQUEST_ID)),  # type: ignore[arg-type]
            rx_id=int(config.get("rx_id", CAN_RESPONSE_ID)),  # type: ignore[arg-type]
            connect_timeout_ms=int(
                config.get("connect_timeout_ms", DEFAULT_CONNECT_TIMEOUT_MS)
            ),  # type: ignore[arg-type]
            padding=int(config.get("padding", 0x00)),  # type: ignore[arg-type]
        )

    raise ValueError(f"unknown transport kind: {kind!r}")
