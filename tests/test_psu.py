"""Tests for CorsairPSU class using mocked USB.

These tests verify the PSU class logic without requiring actual hardware.
USB operations are mocked to simulate device responses.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from corsair_psu_monitor.psu import CorsairPSU
from corsair_psu_monitor.protocol import (
    balanced_encode,
    balanced_decode,
    ENCODE_TABLE,
)


def make_encoded_response(raw_bytes):
    """Create a balanced-code encoded response from raw bytes.

    Simulates what the PSU dongle would send back on the wire.
    Response marker is 0xA8 (cmd=0 response marker).
    """
    out = bytearray([0xA8])  # response marker
    for b in raw_bytes:
        out.append(ENCODE_TABLE[b & 0x0F])
        out.append(ENCODE_TABLE[(b >> 4) & 0x0F])
    out.append(0x00)
    return bytes(out)


@pytest.fixture
def mock_usb():
    """Set up mock USB device, endpoints, and backend."""
    with patch("corsair_psu_monitor.psu.usb") as mock_usb_module, \
         patch("corsair_psu_monitor.psu.libusb_package") as mock_libusb:

        mock_device = MagicMock()
        mock_device.idVendor = 0x1B1C
        mock_device.idProduct = 0x1C11

        mock_ep_out = MagicMock()
        mock_ep_out.bEndpointAddress = 0x02
        mock_ep_in = MagicMock()
        mock_ep_in.bEndpointAddress = 0x82

        # Configure mock USB discovery
        mock_usb_module.core.find.return_value = mock_device

        # Configure mock interface/endpoints
        mock_intf = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.__getitem__ = MagicMock(return_value=mock_intf)
        mock_device.get_active_configuration.return_value = mock_cfg

        # Make find_descriptor return our mock endpoints
        def find_ep(intf, custom_match=None):
            if custom_match:
                # Simulate OUT endpoint check
                mock_out_ep = MagicMock()
                mock_out_ep.bEndpointAddress = 0x02
                mock_in_ep = MagicMock()
                mock_in_ep.bEndpointAddress = 0x82
                if custom_match(mock_out_ep):
                    return mock_ep_out
                if custom_match(mock_in_ep):
                    return mock_ep_in
            return None

        mock_usb_module.util.find_descriptor.side_effect = find_ep
        mock_usb_module.util.endpoint_direction.side_effect = lambda addr: addr & 0x80
        mock_usb_module.util.ENDPOINT_OUT = 0x00
        mock_usb_module.util.ENDPOINT_IN = 0x80

        yield {
            "device": mock_device,
            "ep_out": mock_ep_out,
            "ep_in": mock_ep_in,
            "usb": mock_usb_module,
            "libusb": mock_libusb,
        }


class TestCorsairPSUInit:
    """Test CorsairPSU initialization and properties."""

    def test_default_init(self):
        psu = CorsairPSU()
        assert psu.model == "Unknown"
        assert psu.connected is False

    def test_custom_pid(self):
        psu = CorsairPSU(pid=0x1C11)
        assert psu._pid == 0x1C11

    def test_context_manager_protocol(self):
        """Verify __enter__ and __exit__ call open/close."""
        psu = CorsairPSU()
        psu.open = MagicMock()
        psu.close = MagicMock()

        with psu:
            psu.open.assert_called_once()

        psu.close.assert_called_once()


class TestCorsairPSUConnection:
    """Test USB connection lifecycle."""

    def test_open_not_found_raises(self):
        """open() raises RuntimeError when no device found."""
        mock_usb = MagicMock()
        mock_usb.core.find.return_value = None
        mock_libusb = MagicMock()

        with patch.dict("sys.modules", {
            "usb": mock_usb,
            "usb.core": mock_usb.core,
            "usb.util": mock_usb.util,
            "usb.backend": mock_usb.backend,
            "usb.backend.libusb1": mock_usb.backend.libusb1,
            "libusb_package": mock_libusb,
        }):
            psu = CorsairPSU()
            with pytest.raises(RuntimeError, match="not found"):
                psu.open()

    def test_close_idempotent(self):
        """Calling close() multiple times should not raise."""
        psu = CorsairPSU()
        psu.close()
        psu.close()  # should not raise


class TestReadAll:
    """Test the read_all() aggregate method."""

    def test_read_all_returns_dict(self):
        """read_all() should return a dict with expected keys."""
        psu = CorsairPSU()
        psu._dev = MagicMock()
        psu._ep_out = MagicMock()
        psu._ep_in = MagicMock()
        psu._model = "AX1600i"
        psu._pid = 0x1C11

        # Mock _send_recv to return dummy LINEAR11 data
        # LINEAR11 for 200.0: mantissa=200, exponent=0 -> raw=0x00C8
        dummy_response = bytes([0xC8, 0x00])
        psu._send_recv = MagicMock(return_value=dummy_response)

        stats = psu.read_all()

        assert "input_power" in stats
        assert "output_power" in stats
        assert "input_voltage" in stats
        assert "input_current" in stats
        assert "temp1" in stats
        assert "temp2" in stats
        assert "fan_rpm" in stats
        assert "12v_voltage" in stats
        assert "efficiency" in stats
        assert "model" in stats
        assert stats["model"] == "AX1600i"
        assert stats["psu_watts"] == 1600

    def test_read_all_handles_error(self):
        """read_all() should catch exceptions and return error key."""
        psu = CorsairPSU()
        psu._dev = MagicMock()
        psu._ep_out = MagicMock()
        psu._ep_in = MagicMock()
        psu._model = "AX1600i"
        psu._pid = 0x1C11

        psu._send_recv = MagicMock(side_effect=Exception("USB timeout"))

        stats = psu.read_all()
        assert "error" in stats


class TestFormatStatus:
    """Test status string formatting."""

    def test_format_with_stats(self):
        """format_status with pre-computed stats."""
        psu = CorsairPSU()
        stats = {
            "model": "AX1600i",
            "input_power": 250.0,
            "output_power": 220.0,
            "efficiency": 88.0,
            "input_voltage": 240.0,
            "temp1": 45.0,
            "fan_rpm": 0.0,
        }
        result = psu.format_status(stats)
        assert "AX1600i" in result
        assert "250W" in result
        assert "220W" in result
        assert "88%" in result

    def test_format_with_error(self):
        """format_status with error in stats."""
        psu = CorsairPSU()
        stats = {"error": "USB timeout"}
        result = psu.format_status(stats)
        assert "ERROR" in result
        assert "USB timeout" in result
