"""corsair-psu-monitor: Read telemetry from Corsair AXi/HXi power supplies over USB.

Quick start:
    >>> from corsair_psu_monitor import CorsairPSU
    >>> with CorsairPSU() as psu:
    ...     stats = psu.read_all()
    ...     print(f"Power: {stats['input_power']:.0f}W")
"""

__version__ = "0.1.0"

from .psu import CorsairPSU
from .protocol import (
    CORSAIR_VENDOR_ID,
    SUPPORTED_DEVICES,
    balanced_encode,
    balanced_decode,
    linear11_to_float,
)

__all__ = [
    "CorsairPSU",
    "CORSAIR_VENDOR_ID",
    "SUPPORTED_DEVICES",
    "balanced_encode",
    "balanced_decode",
    "linear11_to_float",
    "__version__",
]
