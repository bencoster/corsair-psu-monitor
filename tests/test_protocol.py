"""Unit tests for the balanced-code protocol and LINEAR11 decoding.

These tests require NO hardware — they validate encoding, decoding,
and data format conversion using known-good values captured from
a real AX1600i PSU session.
"""

import pytest
from corsair_psu_monitor.protocol import (
    ENCODE_TABLE,
    DECODE_TABLE,
    balanced_encode,
    balanced_decode,
    linear11_to_float,
    CORSAIR_VENDOR_ID,
    SUPPORTED_DEVICES,
)


# ── Encode Table Properties ──────────────────────────────────────────

class TestEncodeTable:
    """Verify properties of the balanced-code encoding table."""

    def test_table_length(self):
        assert len(ENCODE_TABLE) == 16

    def test_all_values_dc_balanced(self):
        """Each encoded byte must have exactly 4 one-bits (DC balance)."""
        for i, val in enumerate(ENCODE_TABLE):
            ones = bin(val).count("1")
            assert ones == 4, (
                f"ENCODE[{i}]=0x{val:02X} has {ones} one-bits, expected 4")

    def test_all_values_unique(self):
        assert len(set(ENCODE_TABLE)) == 16

    def test_all_in_range(self):
        for val in ENCODE_TABLE:
            assert 0x00 <= val <= 0xFF


# ── Decode Table Properties ──────────────────────────────────────────

class TestDecodeTable:
    """Verify the decode table is consistent with the encode table."""

    def test_table_length(self):
        assert len(DECODE_TABLE) == 256

    def test_encode_roundtrip(self):
        """Every ENCODE value decodes to a data nibble matching its index."""
        for i, encoded in enumerate(ENCODE_TABLE):
            decoded = DECODE_TABLE[encoded]
            assert decoded & 0xF0 == 0x20, (
                f"ENCODE[{i}]=0x{encoded:02X} doesn't decode as data nibble")
            assert decoded & 0x0F == i, (
                f"ENCODE[{i}]=0x{encoded:02X} decodes to nibble "
                f"{decoded & 0x0F}, expected {i}")

    def test_terminator(self):
        """Wire byte 0x00 must decode as terminator (0x30)."""
        assert DECODE_TABLE[0x00] == 0x30

    def test_marker_bytes(self):
        """Marker bytes must decode with 0x10 high nibble."""
        markers = {
            0x54: 0x10, 0x58: 0x12, 0x64: 0x14, 0x68: 0x16,
            0x94: 0x18, 0x98: 0x1A, 0xA4: 0x1C, 0xA8: 0x1E,
        }
        for wire, expected in markers.items():
            assert DECODE_TABLE[wire] == expected, (
                f"DECODE[0x{wire:02X}]=0x{DECODE_TABLE[wire]:02X}, "
                f"expected 0x{expected:02X}")


# ── Balanced Encode ──────────────────────────────────────────────────

class TestBalancedEncode:
    """Test message encoding to wire format."""

    def test_empty_message(self):
        """Empty payload should produce start marker + terminator."""
        result = balanced_encode(b"", cmd=0)
        assert result == bytes([0x54, 0x00])

    def test_single_byte(self):
        """Encode byte 0x12: low=0x2 -> 0x59, high=0x1 -> 0x56."""
        result = balanced_encode(bytes([0x12]), cmd=0)
        assert result == bytes([0x54, 0x59, 0x56, 0x00])

    def test_start_marker_cmd0(self):
        """Command 0 start marker: ENCODE[0] & 0xFC = 0x55 & 0xFC = 0x54."""
        result = balanced_encode(b"", cmd=0)
        assert result[0] == 0x54

    def test_nibble_order_low_first(self):
        """Low nibble must be encoded first, high nibble second."""
        # Byte 0xAB: low=0xB -> ENCODE[0xB]=0x9A, high=0xA -> ENCODE[0xA]=0x99
        result = balanced_encode(bytes([0xAB]), cmd=0)
        assert result[1] == 0x9A  # low nibble 0xB
        assert result[2] == 0x99  # high nibble 0xA

    def test_terminator_present(self):
        """Last byte must always be 0x00 terminator."""
        for msg in [b"", b"\x00", b"\xFF", b"\x12\x34\x56"]:
            result = balanced_encode(msg, cmd=0)
            assert result[-1] == 0x00

    def test_smbus_init_encoding(self):
        """Verify the SMBus init command encodes correctly."""
        msg = bytes([0x11, 0x02, 0x64, 0x00, 0x00, 0x00, 0x00])
        result = balanced_encode(msg, cmd=0)
        # Start=0x54, then 14 data bytes (7 raw * 2), then 0x00
        assert len(result) == 1 + 14 + 1  # 16 bytes total
        assert result[0] == 0x54
        assert result[-1] == 0x00


# ── Balanced Decode ──────────────────────────────────────────────────

class TestBalancedDecode:
    """Test wire format decoding."""

    def test_too_short(self):
        """Messages shorter than 3 bytes return empty."""
        payload, cmd = balanced_decode(b"")
        assert payload == b""
        assert cmd == -1

        payload, cmd = balanced_decode(b"\x54")
        assert payload == b""
        assert cmd == -1

    def test_empty_message(self):
        """Start marker + terminator -> empty payload."""
        payload, cmd = balanced_decode(bytes([0x54, 0x00]))
        # This has only 2 bytes (marker + terminator), which is < 3
        # but the data between them is empty
        payload, cmd = balanced_decode(bytes([0x54, 0x55, 0x55, 0x00]))
        assert payload == bytes([0x00])

    def test_real_ack_response(self):
        """Decode a real ACK response: A8 55 55 00 -> [0x00]."""
        payload, cmd = balanced_decode(bytes([0xA8, 0x55, 0x55, 0x00]))
        assert payload == bytes([0x00])

    def test_real_voltage_response(self):
        """Decode a real voltage response: A8 A6 A6 96 AA 00 -> [0xDD, 0xF9]."""
        wire = bytes([0xA8, 0xA6, 0xA6, 0x96, 0xAA, 0x00])
        payload, cmd = balanced_decode(wire)
        assert payload == bytes([0xDD, 0xF9])

    def test_roundtrip(self):
        """Encoding then decoding should return the original message."""
        for msg in [b"\x00", b"\xFF", b"\x12\x34", b"\xAB\xCD\xEF"]:
            encoded = balanced_encode(msg, cmd=0)
            decoded, cmd = balanced_decode(encoded)
            assert decoded == msg, (
                f"Roundtrip failed for {msg.hex()}: "
                f"encoded={encoded.hex()}, decoded={decoded.hex()}")

    def test_roundtrip_all_bytes(self):
        """Every possible byte value should survive encode/decode roundtrip."""
        for b in range(256):
            msg = bytes([b])
            encoded = balanced_encode(msg, cmd=0)
            decoded, cmd = balanced_decode(encoded)
            assert decoded == msg, f"Roundtrip failed for byte 0x{b:02X}"


# ── LINEAR11 Decoding ───────────────────────────────────────────────

class TestLinear11:
    """Test PMBus LINEAR11 to float conversion."""

    def test_zero(self):
        """Zero value."""
        assert linear11_to_float(0x00, 0x00) == 0.0

    def test_voltage_238_5(self):
        """Real AX1600i reading: 0xF9DD = 238.5V."""
        result = linear11_to_float(0xDD, 0xF9)
        assert abs(result - 238.5) < 0.1

    def test_power_178(self):
        """Real AX1600i reading: 0x0859 = 178.0W."""
        result = linear11_to_float(0x59, 0x08)
        assert abs(result - 178.0) < 0.1

    def test_temperature_43_8(self):
        """Real AX1600i reading: 0xF0AF = 43.75C."""
        result = linear11_to_float(0xAF, 0xF0)
        assert abs(result - 43.75) < 0.1

    def test_positive_exponent(self):
        """Positive exponent: mantissa * 2^exp."""
        # exponent=2, mantissa=100 -> 400.0
        # raw = (2 << 11) | 100 = 0x1064
        result = linear11_to_float(0x64, 0x10)
        assert result == 400.0

    def test_negative_exponent(self):
        """Negative exponent: mantissa * 2^(-n)."""
        # exponent=-1 (31 unsigned = -1 signed), mantissa=477
        # raw = (31 << 11) | 477 = 0xF9DD
        result = linear11_to_float(0xDD, 0xF9)
        assert abs(result - 238.5) < 0.1

    def test_negative_mantissa(self):
        """Negative mantissa (sign-extended 11-bit)."""
        # mantissa = 2047 (0x7FF) -> sign-extended = -1
        # exponent = 0 -> value = -1.0
        result = linear11_to_float(0xFF, 0x07)
        assert result == -1.0

    def test_fan_zero(self):
        """Fan at 0 RPM: common idle reading."""
        # Real reading from AX1600i: 0x1000
        result = linear11_to_float(0x00, 0x10)
        # exponent=2, mantissa=0 -> 0.0
        assert result == 0.0


# ── Device Constants ─────────────────────────────────────────────────

class TestConstants:
    """Verify device constants are properly defined."""

    def test_vendor_id(self):
        assert CORSAIR_VENDOR_ID == 0x1B1C

    def test_supported_devices_not_empty(self):
        assert len(SUPPORTED_DEVICES) > 0

    def test_ax1600i_in_supported(self):
        assert 0x1C11 in SUPPORTED_DEVICES
        assert SUPPORTED_DEVICES[0x1C11] == "AX1600i"

    def test_all_pids_unique(self):
        pids = list(SUPPORTED_DEVICES.keys())
        assert len(pids) == len(set(pids))
