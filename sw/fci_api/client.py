"""FciClient: a typed method per command in docs/CLI_documentation.md, built entirely on top of
FciTransport.transact(). No method here touches the serial port directly -- that keeps the
thread-safety story in one place (transport.py) rather than duplicated per method.

Two return-value conventions used consistently, both explained where the ambiguity actually
matters rather than restated on every method:
  - "Nothing is available right now, try again later" -> None (read_value(), read_trace()).
  - "This result path does not exist in the loaded bitstream" -> raised FciNotPresentError
    (read_value(), read_batch(), read_stats()) where the WHOLE reply is invalidated, or an
    `| None` field (read_pending(), read_counts()) where only PART of the reply is -- see
    Pending's docstring in types.py for why those two are treated differently.
"""

from __future__ import annotations

import logging
import struct

from .exceptions import FciNotPresentError, FciProtocolError
from .transport import FciTransport
from .types import (
    AcqEvent,
    BlrConfig,
    Counts,
    FciConfig,
    Pending,
    PsdConfig,
    ShaperConfig,
    Stats,
    TraceResult,
    TriggerConfig,
    VgaConfig,
)


def _bool(token: str) -> bool:
    return int(token) != 0


def _opt_int(token: str) -> int | None:
    v = int(token)
    return None if v == -1 else v


logger = logging.getLogger(__name__)


class FciClient:
    """High-level API. Construct with an FciTransport (already open or not -- this class never
    opens/closes it; that lifecycle belongs to whoever owns the transport, e.g. a GUI worker
    thread's connect/disconnect handling)."""

    def __init__(self, transport: FciTransport):
        self._t = transport

    # ------------------------------------------------------------------ system / acquisition

    def ping(self) -> None:
        """`$~~`. Raises on failure; a successful call needs no return value."""
        self._t.transact("~~")

    def identify(self) -> tuple[str, int, int]:
        """`$ID` -> (name, protocol_major, protocol_minor)."""
        tokens = self._t.transact("ID")
        return tokens[0], int(tokens[1]), int(tokens[2])

    def enable_acquisition(self) -> None:
        """`$AE`. Clears both result FIFOs and the statistics counters."""
        self._t.transact("AE")

    def disable_acquisition(self) -> None:
        """`$AD`. FIFO contents are retained."""
        self._t.transact("AD")

    def is_enabled(self) -> bool:
        """`$ES`."""
        (v,) = self._t.transact("ES")
        return _bool(v)

    def reset(self) -> None:
        """`$AR`. Clears both result FIFOs and the statistics counters; configuration unchanged."""
        self._t.transact("AR")

    # ------------------------------------------------------------------------------- results

    @staticmethod
    def _parse_event(fields: list[str]) -> AcqEvent:
        ts_lo, ts_hi, psa_l, psa_w, fci_scaled, es, el, psd_scaled = (int(x) for x in fields)
        return AcqEvent(
            timestamp=(ts_hi << 32) | ts_lo,
            psa_l=psa_l,
            psa_w=psa_w,
            fci=fci_scaled / 10000.0,
            energy_short=es,
            energy_long=el,
            psd=psd_scaled / 10000.0,
        )

    def read_value(self) -> AcqEvent | None:
        """`$RV`. None if no event was pending (including while acquisition is disabled).

        Raises FciNotPresentError if the FCI result path is not present in the loaded bitstream
        -- that is a build-time fact, not a transient "nothing right now" state, so it is not
        folded into the None case: a caller polling in a loop should learn about it once and
        stop, not spin forever treating it as an empty poll.
        """
        tokens = self._t.transact("RV")
        valid = int(tokens[0])
        if valid == -1:
            raise FciNotPresentError("$RV: FCI result path not present in this bitstream")
        if valid == 0:
            return None
        return self._parse_event(tokens[1:9])

    def read_batch(self, n: int = 32) -> list[AcqEvent]:
        """`$RB [n]`. Pops up to `n` paired events (device-side range 1..32) in one round trip,
        stopping early if the FIFO empties. An empty list means nothing was pending -- not an
        error, and not distinguished from "n was small and got fully satisfied but nothing more
        was queued"; check `len()` if that distinction matters to you.

        This is the method to poll for live acquisition, not read_value() in a loop: it costs the
        same one round trip but amortizes that cost across up to `n` events instead of one. See
        CLI_documentation.md section 2.5 and the project log's throughput measurements.

        Raises FciNotPresentError if the FCI result path is not present in the loaded bitstream.
        """
        tokens = self._t.transact("RB", n)
        if len(tokens) == 1 and tokens[0] == "-1":
            raise FciNotPresentError("$RB: FCI result path not present in this bitstream")
        # Validated rather than int()'d directly. A desynced line (two replies glued together
        # after a host stall, e.g. '... 12690!AE') otherwise raises a bare ValueError out of the
        # library, which is not an FciError and so escaped the GUI worker's handler and killed the
        # acquisition thread outright. A framing problem is exactly what FciProtocolError is for,
        # and raising it also marks the transport for resync.
        try:
            count = int(tokens[-1])
        except (ValueError, IndexError):
            raise FciProtocolError(
                f"$RB reply has no valid trailing count -- last token {tokens[-1]!r}; "
                f"likely two replies merged after a stalled read"
            ) from None
        body = tokens[:-1]
        if count < 0 or len(body) < count * 8:
            raise FciProtocolError(
                f"$RB reply claims {count} events but carries {len(body)} value tokens "
                f"({len(body) / 8:.1f} events' worth)"
            )
        return [self._parse_event(body[i * 8 : (i + 1) * 8]) for i in range(count)]

    def read_batch_binary(self, n: int = 1024) -> list[AcqEvent]:
        """`$RQ [n]`. Same events as read_batch(), in a binary frame roughly half the size.

        ASCII costs a measured 49.4 bytes per event; this costs 25 (24 payload + 1 frame tag),
        which at 921600 baud moves the readout ceiling from ~1871 to ~3686 events/s. With the FTDI
        latency timer at 1 ms the link runs at ~98% utilisation, so bytes on the wire are the
        binding constraint and encoding is the only lever left short of a higher baud rate (which
        needs a bitstream rebuild -- axi_uartlite's C_BAUDRATE is synthesis-time).

        `fci` and `psd` are RECOMPUTED here rather than transmitted. Both are exact functions of
        the other fields, verified against 120,000 live events to agree with the values firmware
        used to send to the last digit of their 1e-4 wire quantum, so sending them would have cost
        8 of every 32 bytes to carry nothing new -- and would have created a way for the two to
        disagree. The 1e-4 quantisation firmware applied is NOT reproduced: these come back at full
        float precision, so values may differ from read_batch()'s in the fifth decimal.

        Raises FciProtocolError on a checksum or framing failure -- see transact_framed().
        """
        rec_size, records = self._t.transact_framed("RQ", n)
        truncated = getattr(self._t, "_last_frame_truncated", None)
        if truncated:
            # Reported, not raised: the records that did arrive are good, and losing them too
            # would turn partial loss into total loss. The caller decides whether the rate matters.
            logger.warning("$RQ: %s", truncated)
        if rec_size != 24:
            raise FciProtocolError(f"$RQ: expected 24-byte records, device reports {rec_size}")
        out: list[AcqEvent] = []
        for rec in records:
            ts_lo, ts_hi, psa_l, psa_w = struct.unpack_from("<4I", rec, 0)
            es, el = struct.unpack_from("<2i", rec, 16)
            out.append(
                AcqEvent(
                    timestamp=(ts_hi << 32) | ts_lo,
                    psa_l=psa_l,
                    psa_w=psa_w,
                    fci=(psa_l / psa_w) if psa_w else 0.0,
                    energy_short=es,
                    energy_long=el,
                    # Matches firmware's own guard: a non-positive long-gate integral makes the
                    # ratio undefined, and 0.0 is the documented sentinel for that.
                    psd=((el - es) / el) if el > 0 else 0.0,
                )
            )
        return out

    def read_pending(self) -> Pending:
        """`$RN`. See Pending's docstring for why only the FCI side can be None here."""
        fci_s, psd_s = self._t.transact("RN")
        return Pending(fci_level=_opt_int(fci_s), psd_level=int(psd_s))

    def read_stats(self) -> Stats:
        """`$RS`. Raises FciNotPresentError if the FCI result path is not present -- unlike
        read_pending()/read_counts(), every field here is pairing-derived, so absence invalidates
        the whole reply rather than just one side of it."""
        tokens = self._t.transact("RS")
        if tokens[0] == "-1":
            raise FciNotPresentError("$RS: FCI result path not present in this bitstream")
        paired, dfci, dpsd, ofci, opsd, ferr = (int(x) for x in tokens)
        return Stats(
            paired=paired,
            dropped_fci=dfci,
            dropped_psd=dpsd,
            overflow_fci=ofci,
            overflow_psd=opsd,
            framing_errors=ferr,
        )

    def read_counts(self) -> Counts:
        """`$RC`. The field to poll for a live event rate -- see Counts' docstring. Unlike
        read_stats(), only the FCI side can be absent (Pending's docstring explains why)."""
        fci_s, psd_s = self._t.transact("RC")
        return Counts(fci_event_count=_opt_int(fci_s), psd_event_count=int(psd_s))

    def read_trace(self, n: int = 2048) -> TraceResult | None:
        """`$RT [n]`. None if no trace has been captured yet (device-side range for n is 1..2048).
        Samples are signed."""
        tokens = self._t.transact("RT", n)
        count = int(tokens[0])
        if count == 0:
            return None
        return TraceResult(samples=[int(x) for x in tokens[1 : 1 + count]])

    # ------------------------------------------------------------------------ configuration
    #
    # Every subsystem below shares one shape: get_x() sends the bare "$Gx" (no index) and gets
    # every field back in index order (CLI doc section 3's "a get with no index returns every
    # parameter... in index order"); set_x(**kwargs) sends one "$Sx <index> <value>" per non-None
    # keyword argument -- there is no bulk-set on the wire, so there is none here either. Device
    # range-checking is the single source of truth for valid values (it already returns `!XX 1`,
    # which transact() raises as FciParamError) -- duplicating those ranges here would be exactly
    # the kind of second copy of a hardware fact that caused a real bug earlier in this project
    # (the PSD long-gate constant drifting between two files -- see project log section 8d).

    def get_trigger(self) -> TriggerConfig:
        """`$GT` (CLI doc section 3.1)."""
        threshold, polarity, delay, depth = self._t.transact("GT")
        return TriggerConfig(
            threshold=int(threshold), rising=_bool(polarity), delay=int(delay), depth=int(depth)
        )

    def set_trigger(
        self,
        threshold: int | None = None,
        rising: bool | None = None,
        delay: int | None = None,
        depth: int | None = None,
    ) -> None:
        """`$ST` (CLI doc section 3.1). Only arguments given are written."""
        if threshold is not None:
            self._t.transact("ST", 0, threshold)
        if rising is not None:
            self._t.transact("ST", 1, int(rising))
        if delay is not None:
            self._t.transact("ST", 2, delay)
        if depth is not None:
            self._t.transact("ST", 3, depth)

    def get_blr(self) -> BlrConfig:
        """`$GB` (CLI doc section 3.2)."""
        shift, gate_thr, holdoff, bypass, hold, baseline, gate_open = self._t.transact("GB")
        return BlrConfig(
            shift=int(shift),
            gate_thr=int(gate_thr),
            holdoff=int(holdoff),
            bypass=_bool(bypass),
            hold=_bool(hold),
            baseline=int(baseline),
            gate_open=_bool(gate_open),
        )

    def set_blr(
        self,
        shift: int | None = None,
        gate_thr: int | None = None,
        holdoff: int | None = None,
        bypass: bool | None = None,
        hold: bool | None = None,
    ) -> None:
        """`$SB` (CLI doc section 3.2). `baseline`/`gate_open` are read-only on the device (writing
        index 5 or 6 gets `!XX 1`) and deliberately have no parameter here at all."""
        if shift is not None:
            self._t.transact("SB", 0, shift)
        if gate_thr is not None:
            self._t.transact("SB", 1, gate_thr)
        if holdoff is not None:
            self._t.transact("SB", 2, holdoff)
        if bypass is not None:
            self._t.transact("SB", 3, int(bypass))
        if hold is not None:
            self._t.transact("SB", 4, int(hold))

    def get_psd(self) -> PsdConfig:
        """`$GP` (CLI doc section 3.3)."""
        pre_trigger, pre_gate, short_gate, long_gate, baseline_ref, watermark = self._t.transact(
            "GP"
        )
        return PsdConfig(
            pre_trigger=int(pre_trigger),
            pre_gate=int(pre_gate),
            short_gate=int(short_gate),
            long_gate=int(long_gate),
            baseline_ref=int(baseline_ref),
            watermark=int(watermark),
        )

    def set_psd(
        self,
        pre_trigger: int | None = None,
        pre_gate: int | None = None,
        short_gate: int | None = None,
        long_gate: int | None = None,
        baseline_ref: int | None = None,
        watermark: int | None = None,
    ) -> None:
        """`$SP` (CLI doc section 3.3). `pre_trigger` must equal TriggerConfig.delay -- see
        PsdConfig's docstring; this method does not enforce that for you."""
        if pre_trigger is not None:
            self._t.transact("SP", 0, pre_trigger)
        if pre_gate is not None:
            self._t.transact("SP", 1, pre_gate)
        if short_gate is not None:
            self._t.transact("SP", 2, short_gate)
        if long_gate is not None:
            self._t.transact("SP", 3, long_gate)
        if baseline_ref is not None:
            self._t.transact("SP", 4, baseline_ref)
        if watermark is not None:
            self._t.transact("SP", 5, watermark)

    def get_fci(self) -> FciConfig:
        """`$GF` (CLI doc section 3.4). Returns 4 or 5 device fields depending on whether the FCI
        result path is present -- FciConfig.watermark is None in the 4-field case."""
        tokens = self._t.transact("GF")
        watermark = int(tokens[4]) if len(tokens) >= 5 else None
        return FciConfig(
            psa_l_lo=int(tokens[0]),
            psa_l_hi=int(tokens[1]),
            psa_w_lo=int(tokens[2]),
            psa_w_hi=int(tokens[3]),
            watermark=watermark,
        )

    def set_fci(
        self,
        psa_l_lo: int | None = None,
        psa_l_hi: int | None = None,
        psa_w_lo: int | None = None,
        psa_w_hi: int | None = None,
        watermark: int | None = None,
    ) -> None:
        """`$SF` (CLI doc section 3.4). Setting `watermark` on a build without the FCI result
        path raises FciParamError (the device's own `!XX 1` for that index in that build) --
        check get_fci().watermark is not None first if you want to distinguish that from an
        ordinary out-of-range value."""
        if psa_l_lo is not None:
            self._t.transact("SF", 0, psa_l_lo)
        if psa_l_hi is not None:
            self._t.transact("SF", 1, psa_l_hi)
        if psa_w_lo is not None:
            self._t.transact("SF", 2, psa_w_lo)
        if psa_w_hi is not None:
            self._t.transact("SF", 3, psa_w_hi)
        if watermark is not None:
            self._t.transact("SF", 4, watermark)

    def get_vga(self) -> VgaConfig:
        """`$GV` (CLI doc section 3.5)."""
        fine, coarse, code = self._t.transact("GV")
        return VgaConfig(
            fine_gain_milli=int(fine), coarse_gain_milli=int(coarse), fine_dac_code=_opt_int(code)
        )

    def set_vga(
        self,
        fine_gain_milli: int | None = None,
        coarse_gain_milli: int | None = None,
        fine_dac_code: int | None = None,
    ) -> None:
        """`$SV` (CLI doc section 3.5). Gains are in milli-units (1500 means x1.50) -- see
        VgaConfig's docstring for why this stays in wire units rather than becoming a float."""
        if fine_gain_milli is not None:
            self._t.transact("SV", 0, fine_gain_milli)
        if coarse_gain_milli is not None:
            self._t.transact("SV", 1, coarse_gain_milli)
        if fine_dac_code is not None:
            self._t.transact("SV", 2, fine_dac_code)

    def get_shaper(self) -> ShaperConfig:
        """`$GH` (CLI doc section 3.6). See ShaperConfig.present."""
        present, peaking, gap, decay, enable = self._t.transact("GH")
        return ShaperConfig(
            present=_bool(present),
            peaking=int(peaking),
            gap=int(gap),
            decay=int(decay),
            enable=_bool(enable),
        )

    def set_shaper(
        self,
        peaking: int | None = None,
        gap: int | None = None,
        decay: int | None = None,
        enable: bool | None = None,
    ) -> None:
        """`$SH` (CLI doc section 3.6). Values are stored and read back even while
        ShaperConfig.present is False, but have no effect on acquisition until the core actually
        exists in the loaded bitstream."""
        if peaking is not None:
            self._t.transact("SH", 0, peaking)
        if gap is not None:
            self._t.transact("SH", 1, gap)
        if decay is not None:
            self._t.transact("SH", 2, decay)
        if enable is not None:
            self._t.transact("SH", 3, int(enable))
