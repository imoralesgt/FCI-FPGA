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
import time

import serial
import serial.tools.list_ports

from .exceptions import (
    FciNotPresentError,
    FciParamError,
    FciProtocolError,
    FciTimeoutError,
    FciUnknownCommandError,
)

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

    RESYNC_DRAIN_S = 0.15
    """How long to keep reading and discarding after a failed transaction before issuing the next
    command. Must exceed the USB latency timer (typically 16 ms on FTDI) so that bytes already in
    flight when the failure happened are consumed rather than surfacing inside the next reply."""

    def __init__(self, port: str, baudrate: int = BAUDRATE, timeout: float = DEFAULT_TIMEOUT_S):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: serial.Serial | None = None
        self._lock = threading.RLock()
        self._needs_resync = False
        """Set while the last transaction did not complete cleanly; makes the next one drain
        first. See transact()."""

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

    MAX_BLANK_LINES = 4
    """How many empty/NUL-only lines to skip while looking for a reply header. Opening the port
    toggles DTR/RTS, which the adapter can present as a framing error and deliver as a stray NUL;
    observed both as a prefix inside the first reply and, on the binary path, as a line of its own
    (`b'\\x00\\n'`). Bounded so a genuinely silent device still times out rather than spinning."""

    def _read_reply_line(self, request: str) -> bytes:
        """Reads one reply line, skipping stray NUL-only or empty lines. See MAX_BLANK_LINES."""
        for _ in range(self.MAX_BLANK_LINES + 1):
            raw = self._ser.readline()
            if not raw.endswith(b"\n"):
                raise FciTimeoutError(
                    f"no complete reply to {request!r} within {self.timeout}s (got {raw!r})"
                )
            if raw.lstrip(b"\x00").strip():
                return raw
        raise FciProtocolError(
            f"only blank/NUL lines in reply to {request!r} after "
            f"{self.MAX_BLANK_LINES} attempts"
        )

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

            # reset_input_buffer() only discards what the OS has ALREADY buffered; it cannot
            # cancel bytes still in flight across the USB link, which arrive afterwards and get
            # read as part of the next reply. That is how a single timeout turns into a corrupted
            # line like '!RB ... 12690!AE' -- the tail of one reply glued to the head of the next.
            # So after any failed transaction, drain actively until the line goes quiet before
            # issuing the next command. Only on the failure path: this costs nothing in normal
            # operation, where the flag is never set.
            if self._needs_resync:
                deadline = time.monotonic() + self.RESYNC_DRAIN_S
                while time.monotonic() < deadline:
                    if not self._ser.read(4096):
                        break
                self._ser.reset_input_buffer()
                self._needs_resync = False

            # Assume failure until a well-formed reply has been parsed; every raise below leaves
            # this set, so the NEXT call drains first.
            self._needs_resync = True

            self._ser.write((request + "\n").encode("ascii"))
            self._ser.flush()

            raw = self._read_reply_line(request)

            # A freshly-opened connection has occasionally been observed to prepend one or two
            # stray NUL bytes to the very first reply, still within this same line -- a
            # USB-serial buffering/timing race (bytes already in flight over USB when
            # reset_input_buffer() above ran) rather than anything the firmware sends. NUL is
            # never valid content in this ASCII-only protocol, so stripping it here is safe: it
            # can only turn an otherwise-good reply into a parseable one, never mask a genuine
            # mismatch.
            raw = raw.lstrip(b"\x00")

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

        self._needs_resync = False
        return tokens[1:]

    # ---- binary framed transactions ($RQ) -------------------------------------------------
    RQ_TAG_EVENT = 0xA5
    RQ_TAG_END = 0x5A

    def transact_framed(self, code: str, *args: int) -> tuple[int, list[bytes]]:
        """Sends `$<code> ...` and reads a self-delimiting BINARY frame, for $RQ.

        Frame (see cli.c's h_rq): an ASCII header `!<code> <bytes_per_event>\n`, then a sequence of
        0xA5-tagged fixed-size records, terminated by a 0x5A tag followed by a u16 count and a u32
        additive checksum, both little-endian.

        Self-delimiting rather than length-prefixed because firmware cannot know the count until it
        has drained the FIFO, and staging the batch to find out would not fit in its remaining RAM.

        The checksum is verified here and a mismatch raises FciProtocolError, marking the transport
        for resync. That matters more than it would for ASCII: a corrupted binary frame is otherwise
        indistinguishable from valid measurements, whereas every desync this protocol has hit so far
        announced itself by failing to parse.
        """
        if len(code) != 2:
            raise ValueError(f"command code must be exactly two characters, got {code!r}")
        request = " ".join([f"${code}", *(str(int(a)) for a in args)])

        with self._lock:
            if not self.is_open:
                raise FciProtocolError(f"transport for {self.port} is not open")
            if self._needs_resync:
                deadline = time.monotonic() + self.RESYNC_DRAIN_S
                while time.monotonic() < deadline:
                    if not self._ser.read(4096):
                        break
                self._ser.reset_input_buffer()
                self._needs_resync = False
            self._needs_resync = True

            self._ser.write((request + "\n").encode("ascii"))
            self._ser.flush()

            head = self._read_reply_line(request)
            tokens = head.lstrip(b"\x00").decode("ascii", errors="replace").split()
            if not tokens or tokens[0] != f"!{code}":
                raise FciProtocolError(f"reply {head!r} does not echo {request!r}")
            if len(tokens) == 2 and tokens[1] == "-1":
                raise FciNotPresentError(f"${code}: result path not present in this bitstream")
            try:
                rec_size = int(tokens[-1])
            except ValueError:
                raise FciProtocolError(f"bad header for {request!r}: {head!r}") from None
            if not 1 <= rec_size <= 256:
                raise FciProtocolError(f"implausible record size {rec_size} for {request!r}")

            records: list[bytes] = []
            checksum = 0
            while True:
                tag = self._ser.read(1)
                if len(tag) != 1:
                    raise FciTimeoutError(
                        f"frame for {request!r} truncated after {len(records)} records"
                    )
                if tag[0] == self.RQ_TAG_EVENT:
                    rec = self._ser.read(rec_size)
                    if len(rec) != rec_size:
                        raise FciTimeoutError(
                            f"record {len(records)} of {request!r} truncated "
                            f"({len(rec)}/{rec_size} bytes)"
                        )
                    checksum = (checksum + sum(rec)) & 0xFFFFFFFF
                    records.append(rec)
                elif tag[0] == self.RQ_TAG_END:
                    trailer = self._ser.read(6)
                    if len(trailer) != 6:
                        raise FciTimeoutError(f"trailer for {request!r} truncated")
                    count = int.from_bytes(trailer[:2], "little")
                    want_sum = int.from_bytes(trailer[2:], "little")
                    if count != len(records):
                        raise FciProtocolError(
                            f"{request!r}: trailer says {count} records, read {len(records)}"
                        )
                    if want_sum != checksum:
                        raise FciProtocolError(
                            f"{request!r}: checksum mismatch over {count} records "
                            f"(device {want_sum:#010x}, computed {checksum:#010x})"
                        )
                    break
                else:
                    raise FciProtocolError(
                        f"{request!r}: unexpected frame tag {tag[0]:#04x} after "
                        f"{len(records)} records"
                    )

        self._needs_resync = False
        return rec_size, records
