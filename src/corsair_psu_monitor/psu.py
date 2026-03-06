"""Corsair AXi/HXi PSU telemetry reader over USB.

This module provides the CorsairPSU class for reading real-time power,
voltage, current, temperature, and fan speed from Corsair digital PSUs.

The PSU dongle uses a Silicon Labs SiUSBXpress microcontroller as a
USB-to-SMBus bridge. Communication requires:
  1. SiUSBXpress Enable vendor control transfer
  2. Balanced-code encoding on all bulk data
  3. Three-step SMBus bridge sequence for PMBus register reads

Supported models: AX1600i, AX1300i, AX1000i, AX850i, AX860i, AX760i,
                  HX1200i, HX1000i, HX850i, HX750i, HX650i

Platform requirements:
  - Windows: WinUSB driver via Zadig (see drivers/windows/)
  - Linux:   udev rules for non-root access (see drivers/linux/)
  - macOS:   libusb via Homebrew (brew install libusb)

Usage:
    psu = CorsairPSU()
    psu.open()
    stats = psu.read_all()
    print(f"Input power: {stats['input_power']:.1f}W")
    psu.close()

    # Or as context manager:
    with CorsairPSU() as psu:
        stats = psu.read_all()
"""

import logging
import sys
import time
from typing import Dict, Optional

from .protocol import (
    CORSAIR_VENDOR_ID,
    SUPPORTED_DEVICES,
    ENCODE_TABLE,
    DECODE_TABLE,
    SMBUS_INIT_PAYLOAD,
    SIUSBXP_ENABLE,
    SIUSBXP_FLUSH,
    SIUSBXP_DISABLE,
    SIUSBXP_REQUEST_TYPE,
    SIUSBXP_REQUEST,
    CMD_PAGE,
    CMD_READ_VIN,
    CMD_READ_IIN,
    CMD_READ_VOUT,
    CMD_READ_IOUT,
    CMD_READ_TEMP1,
    CMD_READ_TEMP2,
    CMD_READ_FAN_SPEED,
    CMD_READ_POUT,
    CMD_READ_PIN,
    CMD_MFR_TOTAL_POUT,
    ACT_READ_MEMORY,
    ACT_WRITE_SMBUS_SETTINGS,
    ACT_READ_SMBUS_COMMAND,
    ACT_WRITE_SMBUS_COMMAND,
    balanced_encode,
    balanced_decode,
    linear11_to_float,
)

logger = logging.getLogger(__name__)


class CorsairPSU:
    """Corsair AXi/HXi PSU telemetry reader.

    Communicates with the PSU via the SiUSBXpress USB dongle using
    balanced-code encoding over the SMBus bridge protocol.

    Attributes:
        model: PSU model name (e.g. "AX1600i").
        connected: True if USB device is open and initialized.
    """

    WRITE_TIMEOUT_MS = 2000
    READ_TIMEOUT_MS = 2000
    SYNC_WAIT = 0.003       # 3 ms between SMBus protocol steps
    CTRL_TIMEOUT_MS = 1000  # Control transfer timeout

    def __init__(self, vid: int = CORSAIR_VENDOR_ID, pid: Optional[int] = None):
        """Initialize PSU connection parameters.

        Args:
            vid: USB Vendor ID (default: Corsair 0x1B1C).
            pid: USB Product ID. If None, auto-detects first supported device.
        """
        self._vid = vid
        self._pid = pid
        self._dev = None
        self._ep_out = None
        self._ep_in = None
        self._model = "Unknown"

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open USB connection to the PSU.

        Finds the USB device, claims the interface, enables SiUSBXpress,
        and initializes the SMBus bridge.

        Raises:
            RuntimeError: If no supported PSU is found.
            usb.core.USBError: On USB communication failure.
        """
        import usb.core
        import usb.util
        import usb.backend.libusb1
        import libusb_package

        backend = libusb_package.get_libusb1_backend()

        if self._pid:
            self._dev = usb.core.find(
                idVendor=self._vid, idProduct=self._pid, backend=backend)
        else:
            for pid in SUPPORTED_DEVICES:
                self._dev = usb.core.find(
                    idVendor=self._vid, idProduct=pid, backend=backend)
                if self._dev:
                    self._pid = pid
                    break

        if self._dev is None:
            raise RuntimeError(
                "Corsair PSU not found. Check connection and driver:\n"
                "  Windows: Install WinUSB via Zadig (see drivers/windows/)\n"
                "  Linux:   Install udev rules (see drivers/linux/)\n"
                "  macOS:   brew install libusb")

        self._model = SUPPORTED_DEVICES.get(self._pid, f"PID_{self._pid:04X}")

        # Claim the USB interface
        try:
            self._dev.set_configuration()
        except usb.core.USBError as e:
            if sys.platform == "win32" and (
                "Entity not found" in str(e) or getattr(e, "errno", 0) == 2
            ):
                raise RuntimeError(
                    f"Cannot configure {self._model} USB device: {e}\n"
                    "This usually means the WinUSB driver is not installed.\n"
                    "Fix with: corsair-psu-monitor install-driver --elevate\n"
                    "Or manually install WinUSB via Zadig "
                    "(see drivers/windows/)"
                ) from e
            raise
        cfg = self._dev.get_active_configuration()
        intf = cfg[(0, 0)]

        import usb.util as uu
        self._ep_out = uu.find_descriptor(
            intf,
            custom_match=lambda e:
                uu.endpoint_direction(e.bEndpointAddress) == uu.ENDPOINT_OUT)
        self._ep_in = uu.find_descriptor(
            intf,
            custom_match=lambda e:
                uu.endpoint_direction(e.bEndpointAddress) == uu.ENDPOINT_IN)

        if not self._ep_out or not self._ep_in:
            raise RuntimeError("USB bulk endpoints not found")

        # SiUSBXpress: enable device
        self._dev.ctrl_transfer(
            SIUSBXP_REQUEST_TYPE, SIUSBXP_REQUEST,
            SIUSBXP_ENABLE, 0, timeout=self.CTRL_TIMEOUT_MS)
        time.sleep(0.050)

        # SiUSBXpress: flush buffers
        self._dev.ctrl_transfer(
            SIUSBXP_REQUEST_TYPE, SIUSBXP_REQUEST,
            SIUSBXP_FLUSH, 0, timeout=self.CTRL_TIMEOUT_MS)
        time.sleep(0.050)

        # Drain any stale data from previous session
        for _ in range(5):
            try:
                self._ep_in.read(64, timeout=200)
            except Exception:
                break

        # Initialize SMBus bridge (100 KHz standard mode)
        self._send_recv(SMBUS_INIT_PAYLOAD)
        time.sleep(self.SYNC_WAIT)

        logger.info("Connected to Corsair %s (VID=0x%04X PID=0x%04X)",
                     self._model, self._vid, self._pid)

    def close(self) -> None:
        """Close USB connection and release resources."""
        if self._dev:
            try:
                self._dev.ctrl_transfer(
                    SIUSBXP_REQUEST_TYPE, SIUSBXP_REQUEST,
                    SIUSBXP_DISABLE, 0, timeout=self.CTRL_TIMEOUT_MS)
            except Exception:
                pass
            try:
                import usb.util
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None
            self._ep_out = None
            self._ep_in = None
            logger.info("Disconnected from Corsair %s", self._model)

    def __enter__(self) -> "CorsairPSU":
        self.open()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @property
    def model(self) -> str:
        """PSU model name (e.g. 'AX1600i')."""
        return self._model

    @property
    def connected(self) -> bool:
        """True if USB device is open."""
        return self._dev is not None

    # ------------------------------------------------------------------
    # Low-level I/O (balanced-code encoded)
    # ------------------------------------------------------------------

    def _send_recv(self, msg: bytes) -> Optional[bytes]:
        """Encode, write to bulk OUT, read from bulk IN, decode.

        Args:
            msg: Raw (unencoded) message bytes.

        Returns:
            Decoded response bytes, or None if no response.
        """
        encoded = balanced_encode(msg)
        self._ep_out.write(encoded, timeout=self.WRITE_TIMEOUT_MS)

        resp_buf = bytearray()
        for _ in range(5):
            time.sleep(0.005)
            try:
                chunk = bytes(self._ep_in.read(64, timeout=self.READ_TIMEOUT_MS))
                resp_buf.extend(chunk)
                if chunk[-1] == 0x00:  # terminator -> message complete
                    break
            except Exception:
                break

        if not resp_buf:
            return None

        payload, _cmd = balanced_decode(bytes(resp_buf))
        return payload

    # ------------------------------------------------------------------
    # PMBus register access (three-step SMBus bridge)
    # ------------------------------------------------------------------

    def read_register(self, command: int, length: int = 2) -> bytes:
        """Read a PMBus register via the dongle's SMBus bridge.

        Uses the three-step bridge protocol:
          1. WriteSMBusCommand - queue a read of `length` bytes from `command`
          2. ReadSMBusCommand  - execute the queued SMBus transaction
          3. ReadMemory        - retrieve the result from the dongle buffer

        Args:
            command: PMBus command/register address (e.g. 0x88 for VIN).
            length: Number of bytes to read (default 2 for LINEAR11).

        Returns:
            Raw register bytes (little-endian), or empty bytes on failure.
        """
        # Step 1: Queue the read command
        self._send_recv(bytes([
            ACT_WRITE_SMBUS_COMMAND,
            0x03, 0x06, 0x01, 0x07, length, command]))
        time.sleep(self.SYNC_WAIT)

        # Step 2: Execute the SMBus transaction
        self._send_recv(bytes([ACT_READ_SMBUS_COMMAND]))
        time.sleep(self.SYNC_WAIT)

        # Step 3: Read the result from dongle buffer
        resp = self._send_recv(bytes([ACT_READ_MEMORY, 0x07, length]))
        time.sleep(self.SYNC_WAIT)

        return resp if resp else b""

    def write_register(self, command: int, data: bytes) -> None:
        """Write a PMBus register via the dongle's SMBus bridge.

        Args:
            command: PMBus command/register address.
            data: Bytes to write.
        """
        self._send_recv(bytes([
            ACT_WRITE_SMBUS_COMMAND,
            0x01, 0x04, len(data) + 1, command]) + data)
        time.sleep(self.SYNC_WAIT)
        self._send_recv(bytes([ACT_READ_SMBUS_COMMAND]))
        time.sleep(0.008)  # page switch needs extra settle time

    def read_linear11(self, command: int) -> float:
        """Read a 2-byte LINEAR11 register and return float.

        Args:
            command: PMBus command/register address.

        Returns:
            Decoded float value, or 0.0 on read failure.
        """
        data = self.read_register(command, 2)
        if len(data) < 2:
            return 0.0
        return linear11_to_float(data[0], data[1])

    # ------------------------------------------------------------------
    # Sensor readings
    # ------------------------------------------------------------------

    def read_input_power(self) -> float:
        """Total input power in watts (AC side, PMBus READ_PIN 0x97)."""
        return self.read_linear11(CMD_READ_PIN)

    def read_output_power(self) -> float:
        """Total output power in watts (DC side, MFR register 0xEE)."""
        return self.read_linear11(CMD_MFR_TOTAL_POUT)

    def read_apparent_power(self) -> float:
        """Apparent power V*I in VA (upper bound of real power)."""
        return self.read_input_voltage() * self.read_input_current()

    def read_input_voltage(self) -> float:
        """Input voltage in volts (AC side)."""
        return self.read_linear11(CMD_READ_VIN)

    def read_input_current(self) -> float:
        """Input current in amps (AC side)."""
        return self.read_linear11(CMD_READ_IIN)

    def read_temp1(self) -> float:
        """Temperature sensor 1 in Celsius."""
        return self.read_linear11(CMD_READ_TEMP1)

    def read_temp2(self) -> float:
        """Temperature sensor 2 in Celsius."""
        return self.read_linear11(CMD_READ_TEMP2)

    def read_fan_rpm(self) -> float:
        """Fan speed in RPM."""
        return self.read_linear11(CMD_READ_FAN_SPEED)

    def read_rail(self, page: int) -> Dict[str, float]:
        """Read voltage, current, and power for a specific rail.

        Args:
            page: Rail page number (0=12V, 1=5V, 2=3.3V).

        Returns:
            Dict with keys: voltage, current, power.
        """
        self.write_register(CMD_PAGE, bytes([page]))
        time.sleep(0.005)
        return {
            "voltage": self.read_linear11(CMD_READ_VOUT),
            "current": self.read_linear11(CMD_READ_IOUT),
            "power": self.read_linear11(CMD_READ_POUT),
        }

    def read_12v_rail(self) -> Dict[str, float]:
        """Read 12V rail: voltage, current, power."""
        return self.read_rail(0)

    def read_5v_rail(self) -> Dict[str, float]:
        """Read 5V rail: voltage, current, power."""
        return self.read_rail(1)

    def read_3v3_rail(self) -> Dict[str, float]:
        """Read 3.3V rail: voltage, current, power."""
        return self.read_rail(2)

    # ------------------------------------------------------------------
    # Aggregate readings
    # ------------------------------------------------------------------

    def read_all(self) -> Dict:
        """Read all sensors at once.

        Returns a dict with all telemetry values:
            input_power, output_power, efficiency,
            input_voltage, input_current,
            temp1, temp2, fan_rpm,
            12v_voltage, 12v_current, 12v_power,
            5v_voltage, 5v_current, 5v_power,
            3v3_voltage, 3v3_current, 3v3_power,
            rail_power_sum, model, psu_watts,
            error (only if a read failed)
        """
        result: Dict = {}
        try:
            # Global readings (not page-dependent)
            result["input_power"] = self.read_input_power()
            result["output_power"] = self.read_output_power()
            result["input_voltage"] = self.read_input_voltage()
            result["input_current"] = self.read_input_current()
            result["temp1"] = self.read_temp1()
            result["temp2"] = self.read_temp2()
            result["fan_rpm"] = self.read_fan_rpm()

            # Per-rail readings (page-dependent)
            for page, rail in [(0, "12v"), (1, "5v"), (2, "3v3")]:
                self.write_register(CMD_PAGE, bytes([page]))
                time.sleep(0.005)
                result[f"{rail}_voltage"] = self.read_linear11(CMD_READ_VOUT)
                result[f"{rail}_current"] = self.read_linear11(CMD_READ_IOUT)
                result[f"{rail}_power"] = self.read_linear11(CMD_READ_POUT)

            # Rail sum as cross-check against MFR register 0xEE
            result["rail_power_sum"] = (
                result.get("12v_power", 0) +
                result.get("5v_power", 0) +
                result.get("3v3_power", 0))

            # Efficiency = DC output / AC input
            pin = result.get("input_power", 0)
            pout = result.get("output_power", 0)
            result["efficiency"] = (pout / pin * 100) if pin > 0 else 0.0

            result["model"] = self._model
            psu_watts_map = {
                "AX1600i": 1600, "AX1300i": 1300, "AX1000i": 1000,
                "AX850i": 850, "AX860i": 860, "AX760i": 760,
                "HX1200i": 1200, "HX1000i": 1000, "HX850i": 850,
                "HX750i": 750, "HX650i": 650,
            }
            result["psu_watts"] = psu_watts_map.get(self._model, 0)

        except Exception as e:
            logger.warning("PSU read error: %s", e)
            result["error"] = str(e)

        return result

    def format_status(self, stats: Optional[Dict] = None) -> str:
        """Return a formatted one-line status string.

        Args:
            stats: Pre-computed dict from read_all(). If None, reads fresh.
        """
        s = stats if stats is not None else self.read_all()
        if "error" in s:
            return f"PSU: ERROR ({s['error']})"
        return (
            f"PSU {s.get('model', '?')}: "
            f"{s.get('input_power', 0):.0f}W in / "
            f"{s.get('output_power', 0):.0f}W out "
            f"({s.get('efficiency', 0):.0f}%) "
            f"{s.get('input_voltage', 0):.0f}V "
            f"{s.get('temp1', 0):.0f}C "
            f"Fan:{s.get('fan_rpm', 0):.0f}rpm")
