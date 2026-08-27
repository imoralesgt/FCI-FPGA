"""Low-level framing over the CLI's wire protocol (docs/CLI_documentation.md section 1).

This module is the entire thread-safety mechanism for fci_api: FciTransport.transact() takes an
RLock for the full round trip of one request, so concurrent callers serialize instead of
interleaving their bytes on the wire. Nothing above this layer needs its own locking, because
nothing above this layer touches the serial port directly.

This is safe and simple specifically because the CLI's own transaction model (section 1.4) is
strict request/response with nothing sent unsolicited: there is no pipelining and no need for a
per-request correlation id -- the next line in is always the reply to the last line out.
"""

from __future__ import annotations

import threading

import serial
import serial.tools.list_ports

from .exceptions import FciParamError, FciProtocolError, FciTimeoutError, FciUnknownCommandError

BAUDRATE = 921_600
DEFAULT_TIMEOUT_S = 1.0
"""Generous relative to measured hardware: round trips run ~16 ms in practice (dominated by the
USB-serial adapter's own latency timer, not the link), and the largest reply this protocol can
produce (`$RB`'s full 32-event batch) is on the order of 3 KB, ~30 ms to transmit at 921600 baud.
This is margin, not a tuned budget -- see the project log's CLI throughput measurements."""


def find_port(vid_hex: str = "0403", pid_hex: str = "6010") -> str | None:
    """Returns the device path of the first serial port matching the given USB VID:PID, or None.

    Defaults match this project's board (Digilent FT2232H, confirmed via `udevadm`). That chip is
    dual-UART -- JTAG and the CLI UART enumerate as two ports sharing the same VID:PID -- so when
    more than one match exists, callers that need to distinguish them should use
    list_matching_ports() instead and let the user pick by description.
    """
    for p in serial.tools.list_ports.comports():
        if p.vid is None or p.pid is None:
            continue
        if f"{p.vid:04x}".lower() == vid_hex.lower() and f"{p.pid:04x}".lower() == pid_hex.lower():
            return p.device
    return None


def list_matching_ports(vid_hex: str = "0403", pid_hex: str = "6010"):
    """Returns every `serial.tools.list_ports_common.ListPortInfo` matching the given VID:PID.

    Use this (rather than find_port()) wherever a human needs to pick between the JTAG and UART
    interfaces of the same dual-UART chip -- `.description` is what distinguishes them.
    """
    out = []
    for p in serial.tools.list_ports.comports():
        if p.vid is None or p.pid is None:
            continue
        if f"{p.vid:04x}".lower() == vid_hex.lower() and f"{p.pid:04x}".lower() == pid_hex.lower():
            out.append(p)
    return out


class FciTransport:
    """Owns one serial port and one RLock. Not a context manager for the port's whole lifetime by
    accident -- `with FciTransport(port) as t:` opens on enter and closes on exit, matching the
    common case of a short script; a GUI instead calls open()/close() explicitly around a much
    longer-lived object, which works identically since __enter__/__exit__ just call them too.
    """

    def __init__(self, port: str, baudrate: int = BAUDRATE, timeout: float = DEFAULT_TIMEOUT_S):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: serial.Serial | None = None
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> None:
        with self._lock:
            if self.is_open:
                return
            self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
            # Discard whatever the OS-level input buffer already holds (e.g. leftover bring-up
            # text from a reboot that happened before this connection was opened). Without this,
            # the first transact() below can read stale bytes as if they were the reply to its own
            # request, and since every reply after that is now offset by however many stray lines
            # preceded it, the desync cascades to every subsequent call, not just the first one.
            self._ser.reset_input_buffer()

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    if self._ser.is_open:
                        self._ser.close()
                finally:
                    self._ser = None

    def __enter__(self) -> "FciTransport":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def transact(self, code: str, *args: int) -> list[str]:
        """Sends `$<code> arg0 arg1 ...\\n`, blocks for the matching `!<code> ...\\n` reply, and
        returns the tokens after the echoed code. Raises FciUnknownCommandError/FciParamError for
        `!XX 0`/`!XX 1`, FciTimeoutError if no complete line arrives within `self.timeout`, and
        FciProtocolError if a reply line arrives but doesn't echo the code we sent -- a framing
        problem (wrong baud, a stray byte, a version mismatch), not an ordinary device error.
        """
        if len(code) != 2:
            raise ValueError(f"command code must be exactly two characters, got {code!r}")

        request = " ".join([f"${code}", *(str(int(a)) for a in args)])

        with self._lock:
            if not self.is_open:
                raise FciProtocolError(f"transport for {self.port} is not open")

            # Defensive resync: the protocol guarantees nothing arrives unsolicited (section 1.4),
            # so under normal operation this buffer is already empty by the time we get here --
            # the previous transact() call's readline() consumed exactly one full reply and
            # nothing more. But if a prior call timed out, hit a framing mismatch, or a device
            # reboot spewed unsolicited bring-up text mid-session, stray bytes can be left sitting
            # here; reading them as this call's reply would misalign every call after it too, not
            # just this one. Clearing the buffer right before every write keeps a single bad
            # transaction from cascading into a permanently desynced session.
            self._ser.reset_input_buffer()

            self._ser.write((request + "\n").encode("ascii"))
            self._ser.flush()

            raw = self._ser.readline()
            if not raw.endswith(b"\n"):
                raise FciTimeoutError(
                    f"no complete reply to {request!r} within {self.timeout}s "
                    f"(got {raw!r})"
                )

        line = raw.decode("ascii", errors="replace").strip()
        tokens = line.split()
        if not tokens:
            raise FciProtocolError(f"empty reply to {request!r}")

        head = tokens[0]
        if head == "!XX":
            if len(tokens) < 2:
                raise FciProtocolError(f"malformed error reply to {request!r}: {line!r}")
            err_code = tokens[1]
            if err_code == "0":
                raise FciUnknownCommandError(request)
            if err_code == "1":
                raise FciParamError(request)
            raise FciProtocolError(f"unknown error code {err_code!r} replying to {request!r}")

        if head != f"!{code}":
            raise FciProtocolError(
                f"reply {line!r} does not echo the code we sent ({request!r})"
            )

        return tokens[1:]
