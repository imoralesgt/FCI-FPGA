"""Pure-Python client for the FCI-FPGA MicroBlaze CLI (docs/CLI_documentation.md).

No PySide6/Qt dependency anywhere in this package -- see gui/ for the application built on top
of it. Typical use:

    from fci_api import FciClient, FciTransport, find_port

    port = find_port()  # Digilent FT2232H, VID:PID 0403:6010, by default
    with FciTransport(port) as transport:
        client = FciClient(transport)
        client.enable_acquisition()
        for event in client.read_batch():
            print(event.timestamp, event.fci, event.psd)
"""

from .client import FciClient
from .exceptions import (
    FciCommandError,
    FciError,
    FciNotPresentError,
    FciParamError,
    FciProtocolError,
    FciTimeoutError,
    FciUnknownCommandError,
)
from .transport import FciTransport, find_port, list_matching_ports
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

__all__ = [
    "FciClient",
    "FciTransport",
    "find_port",
    "list_matching_ports",
    "AcqEvent",
    "BlrConfig",
    "Counts",
    "FciConfig",
    "Pending",
    "PsdConfig",
    "ShaperConfig",
    "Stats",
    "TraceResult",
    "TriggerConfig",
    "VgaConfig",
    "FciError",
    "FciCommandError",
    "FciUnknownCommandError",
    "FciParamError",
    "FciTimeoutError",
    "FciProtocolError",
    "FciNotPresentError",
]
