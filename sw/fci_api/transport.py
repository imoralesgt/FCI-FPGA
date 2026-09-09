"""Low-level framing over the CLI's wire protocol (docs/sw/CLI_documentation.md section 1).

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

BAUDRATE = 4_000_000
"""MUST match the rate firmware programs -- see fpga/ublaze_sw/uart.h's UART_BAUD_HZ.

921600 was the axi_uartlite ceiling (a fixed choice list in that IP, not a computed range). The
axi_uart16550 that replaced it takes its baud from a runtime divisor over a 64 MHz external clock,
where divisor 1 gives exactly 4 Mbaud and divisor 2 exactly 2 Mbaud -- both also generated exactly
by the FT2232H, so neither end accumulates sampling error. Set this back to 921_600 for an older
axi_uartlite bitstream; there is no negotiation, a mismatch just reads as line noise."""

DEFAULT_TIMEOUT_S = 1.0
"""Generous relative to measured hardware. Round trips are dominated by the USB-serial adapter's
own latency timer (16 ms by default and NOT safe to assume reconfigurable -- the instrument has to
work on an off-the-shelf host), not by the link. The largest reply this protocol can produce is a
full 1024-event batch: ~50 KB in ASCII `$RB`, ~26 KB in binary `$RQ`, so ~250 ms and ~130 ms
respectively at 2 Mbaud. This is margin, not a tuned budget -- see the project log's throughput
measurements."""


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

    FRAME_CHUNK_BYTES = 4096
    """Bytes requested per read while pulling a binary frame. Large enough that one call covers
    many records; the short timeout below is what stops it stalling when fewer remain."""

    FRAME_CHUNK_TIMEOUT_S = 0.005
    """Per-read timeout while pulling a binary frame. Bounds both the chunk size (~T x link rate)
    and the stall at a frame's end. The port's normal timeout is restored afterwards."""

    DRAIN_READ_TIMEOUT_S = 0.02
    """Per-read timeout while draining. Short so an already-quiet line costs one of these, not the
    port's full operating timeout."""

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
        self._last_frame_truncated: str | None = None
        """Set by transact_framed() when a binary frame desynced mid-stream and was returned
        short. Read it after a call to distinguish 'the FIFO was empty' from 'we lost the rest'."""

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

            # reset_input_buffer() only clears what has ALREADY arrived; opening the port toggles
            # DTR/RTS (pyserial does this as part of construction above), and the adapter's own
            # reaction to that toggle can still be in flight over USB at this exact instant, landing
            # a moment later as one or two garbage bytes prefixed onto whatever this connection's
            # first real command receives back. _read_reply_line()/transact()/transact_framed() all
            # only strip a LEADING NUL here, on the documented assumption that this glitch decodes
            # as NUL -- true often enough to have gone unnoticed, but not guaranteed: a genuine
            # framing-error byte can be any value (observed once as a two-byte b'\x00\xe1' prefix,
            # which defeated that NUL-only stripping and surfaced as "$RQ 1024" appearing to get a
            # reply that didn't echo its own code, on literally the first request after every
            # connect). Draining here -- actively waiting out the settle window and discarding
            # whatever lands in it, using the same machinery a post-failure resync already uses --
            # fixes it at the one place it can be fixed for certain, instead of guessing at every
            # possible corrupted byte value in every reply-parsing path downstream.
            self._drain()

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

    def _drain(self) -> None:
        """Reads and discards until the line goes quiet, before issuing the next command.

        Uses a SHORT per-read timeout and only asks for bytes that have actually arrived.
        `read(n)` blocks until it has n bytes or the port timeout expires, so draining with
        `read(4096)` against the normal multi-second timeout stalled for that whole timeout
        whenever fewer than 4096 bytes were left -- exactly the case a drain is for. Meanwhile the
        device carried on streaming the remainder of the abandoned frame, so the resync made the
        desync worse and delayed the next command by seconds.

        The bound matters because after a truncated binary frame the device may still be sending
        up to a full batch (a 1024-record $RQ frame is 25.6 kB, ~64 ms at 4 Mbaud), and none of it
        is wanted.
        """
        saved = self._ser.timeout
        try:
            self._ser.timeout = self.DRAIN_READ_TIMEOUT_S
            deadline = time.monotonic() + self.RESYNC_DRAIN_S
            while time.monotonic() < deadline:
                pending = self._ser.in_waiting
                if not self._ser.read(pending if pending else 1):
                    break  # quiet: nothing arrived within DRAIN_READ_TIMEOUT_S
        finally:
            self._ser.timeout = saved
        self._ser.reset_input_buffer()

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
                self._drain()
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

        # Reject control characters anywhere in the line. This protocol is printable ASCII plus
        # the terminating newline, so a NUL or stray control byte inside a reply is corruption --
        # and it is not caught by parsing, because str.split() does not split on NUL: a corrupted
        # sample arrived as the single token '2866\x00' and reached int(), raising a bare
        # ValueError out of the library. That is the wrong exception in two ways: it is not an
        # FciError, so callers filtering on FciError let it escape (it killed the acquisition
        # thread once), and it never marks the transport for resync, so the next call inherits the
        # misalignment.
        #
        # Checked here rather than at each int() because there are a dozen such conversions across
        # the client and every one of them has this failure mode. One choke point, and the resync
        # flag is already set on this path.
        bad = next((c for c in line if c < " "), None)
        if bad is not None:
            raise FciProtocolError(
                f"control character {ord(bad):#04x} in reply to {request!r}: {line!r}"
            )

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
        """Sends `$<code> ...` and reads a self-delimiting BINARY frame, for $RQ and $RA (any
        future binary-batch command can reuse this too -- it is generic over the record size, which
        the device's own header line reports).

        Frame (see cli.c's h_rq/h_ra): an ASCII header `!<code> <bytes_per_event>\n`, then a sequence of
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
                self._drain()
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

            # Read the frame in BULK and parse from memory. The obvious loop -- read(1) for the
            # tag, then read(rec_size) for the record -- costs two syscalls per event, i.e. 2048
            # per 1024-record frame, and every one of them has to reacquire the GIL. A GUI redraw
            # holding the GIL for a few milliseconds therefore delays the reader thousands of
            # times per frame rather than once, which is why readout throughput was visibly
            # sensitive to which plot type was on screen (a scatter redraw measured 3-5x the cost
            # of a heatmap, and the observed rate moved ~10k -> ~12k ev/s between them). Reading in
            # chunks cuts it to a handful of syscalls per frame.
            #
            # Each read asks for exactly what is still needed PLUS whatever is already waiting.
            # Never more: read(n) blocks until it has n bytes or the port timeout expires, so
            # optimistically asking for a round number stalls for the full timeout whenever the
            # device sends less -- the same trap that made the resync drain take seconds.
            buf = bytearray()
            pos = 0
            saved_timeout = self._ser.timeout
            self._ser.timeout = self.FRAME_CHUNK_TIMEOUT_S

            def need(k: int) -> bool:
                nonlocal buf
                # Block on a CHUNK, not on a byte and not on exactly-what-is-needed.
                #
                # The parser consumes far faster than the link delivers (400 kB/s at 4 Mbaud), so
                # it is always waiting. The only question is whether it waits once per chunk or
                # once per field. Per-field reads cost ~2048 syscalls per 1024-record frame, and
                # since every one reacquires the GIL, a few-millisecond GUI redraw delays the
                # reader thousands of times rather than once -- which is why throughput was
                # visibly sensitive to the plot type on screen.
                #
                # read(n) returns when it has n bytes OR the port timeout expires, so a large n
                # with a SHORT timeout yields "whatever arrived in T", bounded both ways: about
                # T x 400 kB/s per call, and never stalling longer than T at the end of a frame
                # where fewer than n bytes remain. Waiting is not wasted -- the bytes have to
                # arrive regardless.
                stall_budget = int(self.timeout / self.FRAME_CHUNK_TIMEOUT_S) + 1
                while len(buf) - pos < k:
                    chunk = self._ser.read(self.FRAME_CHUNK_BYTES)
                    if chunk:
                        buf.extend(chunk)
                        continue
                    stall_budget -= 1
                    if stall_budget <= 0:
                        return False  # genuinely silent for the port's whole timeout
                return True

            records: list[bytes] = []
            checksum = 0
            truncated = None
            try:
              while True:
                if not need(1):
                    raise FciTimeoutError(
                        f"frame for {request!r} truncated after {len(records)} records"
                    )
                tag = buf[pos]
                pos += 1
                if tag == self.RQ_TAG_EVENT:
                    if not need(rec_size):
                        raise FciTimeoutError(
                            f"record {len(records)} of {request!r} truncated"
                        )
                    rec = bytes(buf[pos:pos + rec_size])
                    pos += rec_size
                    checksum = (checksum + sum(rec)) & 0xFFFFFFFF
                    records.append(rec)
                elif tag == self.RQ_TAG_END:
                    if not need(6):
                        raise FciTimeoutError(f"trailer for {request!r} truncated")
                    trailer = bytes(buf[pos:pos + 6])
                    pos += 6
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
                    # Keep what was decoded; the next call resyncs before issuing its command.
                    truncated = (
                        f"frame desync after {len(records)} records (tag {tag:#04x}); "
                        f"remainder of this batch discarded"
                    )
                    break
            finally:
                self._ser.timeout = saved_timeout

        if truncated:
            self._last_frame_truncated = truncated
            return rec_size, records

        self._needs_resync = False
        self._last_frame_truncated = None
        return rec_size, records
