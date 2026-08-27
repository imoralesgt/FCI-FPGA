"""Exceptions raised by fci_api.

Maps directly onto docs/CLI_documentation.md:
  - section 1.3 (error replies, `!XX <code>`) -> FciUnknownCommandError / FciParamError
  - the "-1 = not present in the loaded bitstream" sentinel used throughout section 2
    -> FciNotPresentError, raised by the client so callers don't have to remember to check
    for -1 themselves on every affected field.
"""

from __future__ import annotations


class FciError(Exception):
    """Base class for every exception this package raises."""


class FciProtocolError(FciError):
    """The device's reply did not match what the request should produce.

    Covers malformed/truncated replies and any reply whose leading token isn't the expected
    echoed command code -- something a well-behaved device per the CLI spec should never send,
    so this indicates a framing problem (wrong baud, a stray byte, a firmware/library version
    mismatch) rather than an ordinary error the device reported on purpose.
    """


class FciCommandError(FciError):
    """Base class for the two `!XX <code>` error replies (CLI doc section 1.3)."""

    def __init__(self, request: str):
        self.request = request
        super().__init__(f"{request!r} -> {self._reason()}")

    def _reason(self) -> str:  # pragma: no cover - overridden below
        raise NotImplementedError


class FciUnknownCommandError(FciCommandError):
    """`!XX 0` -- the command code was not recognised."""

    def _reason(self) -> str:
        return "command code not recognised (!XX 0)"


class FciParamError(FciCommandError):
    """`!XX 1` -- wrong number of parameters, or a value out of the documented range."""

    def _reason(self) -> str:
        return "wrong parameter count or value out of range (!XX 1)"


class FciTimeoutError(FciError):
    """No reply line arrived within the transport's timeout."""


class FciNotPresentError(FciError):
    """The requested result path is not present in the loaded bitstream.

    Raised wherever the CLI doc documents a literal -1 sentinel: `$RV`/`$RB`'s leading field,
    `$RN`/`$RS`/`$RC`'s FCI-side fields, and `$GF`/`$SF` index 4 (the watermark, which does not
    exist at all in a build without fci_sink). See CLI_documentation.md section 2 for the exact
    list -- this is not a general "value is -1" check, it only fires where the spec defines -1
    as this specific sentinel.
    """
