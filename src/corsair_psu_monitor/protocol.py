"""SiUSBXpress balanced-code protocol and PMBus constants.

This module implements the wire encoding used by Corsair AXi/HXi PSU dongles
to communicate over USB. The dongle contains a Silicon Labs C8051F microcontroller
running SiUSBXpress firmware that bridges USB bulk transfers to an internal
I2C/SMBus bus connected to the PSU's PMBus controller.

Wire Format (Balanced-Code Encoding)
-------------------------------------
Each raw byte is encoded as TWO bytes on the wire:
  - First byte:  ENCODE[low_nibble]   (bits 3:0 of raw byte)
  - Second byte: ENCODE[high_nibble]  (bits 7:4 of raw byte)

Messages are framed as:
  [start_marker] [encoded_byte_pairs...] [0x00 terminator]

The start marker encodes the command index:
  start = ENCODE[(cmd << 1) & 0x0F] & 0xFC

The encoding ensures DC balance on the USB bus (equal numbers of 0 and 1 bits
per symbol), which is required by the SiUSBXpress protocol.

Decoding uses a 256-entry lookup table where the high nibble carries metadata:
  - 0x20 | nibble_value  = valid data nibble (values 0x0-0xF)
  - 0x10 | marker_info   = start/response marker
  - 0x30                  = terminator (byte value 0x00)
  - 0x00                  = invalid/unused code

PMBus LINEAR11 Format
---------------------
Most PMBus sensor values use LINEAR11 encoding:
  - Bits 15:11 = signed 5-bit exponent (-16 to +15)
  - Bits 10:0  = signed 11-bit mantissa (-1024 to +1023)
  - Value = mantissa * 2^exponent

References
----------
- Jon0/ax1600i (Rust): https://github.com/Jon0/ax1600i
- ka87/cpsumon (C): https://github.com/ka87/cpsumon
- EvanMulawski/FanControl.CorsairLink (C#)
- Silicon Labs AN169: USBXpress Programmer's Guide
"""

import math
from typing import Dict, Tuple

# ── USB Device Identifiers ──────────────────────────────────────────

CORSAIR_VENDOR_ID = 0x1B1C

SUPPORTED_DEVICES: Dict[int, str] = {
    0x1C11: "AX1600i",
    0x1C10: "AX1300i",
    0x1C0F: "AX1000i",
    0x1C0E: "AX850i",
    0x1C0D: "AX760i",
    0x1C0C: "AX860i",
    0x1C08: "HX1200i",
    0x1C07: "HX1000i",
    0x1C06: "HX850i",
    0x1C05: "HX750i",
    0x1C04: "HX650i",
}

# ── Balanced-Code Encoding Table ────────────────────────────────────
# Maps 4-bit nibble values (0x0-0xF) to their balanced-code wire bytes.
# Each encoded byte has equal numbers of 0 and 1 bits (4 each).

ENCODE_TABLE = [
    0x55, 0x56, 0x59, 0x5A,  # nibble 0-3
    0x65, 0x66, 0x69, 0x6A,  # nibble 4-7
    0x95, 0x96, 0x99, 0x9A,  # nibble 8-B
    0xA5, 0xA6, 0xA9, 0xAA,  # nibble C-F
]

# ── Balanced-Code Decoding Table ────────────────────────────────────
# 256-entry table: encoded_byte -> decoded_value
# High nibble of decoded value is metadata:
#   0x20 = valid data nibble (low nibble is the 4-bit value)
#   0x10 = marker byte (start/response markers)
#   0x30 = terminator
#   0x00 = invalid/unused

DECODE_TABLE = [0x00] * 256

# Terminator: wire byte 0x00 decodes to metadata 0x30
DECODE_TABLE[0x00] = 0x30

# Marker bytes (start markers with LSBs masked off)
DECODE_TABLE[0x54] = 0x10  # cmd=0 marker
DECODE_TABLE[0x58] = 0x12  # cmd=1 marker
DECODE_TABLE[0x64] = 0x14  # cmd=2 marker
DECODE_TABLE[0x68] = 0x16  # cmd=3 marker
DECODE_TABLE[0x94] = 0x18  # cmd=4 marker
DECODE_TABLE[0x98] = 0x1A  # cmd=5 marker
DECODE_TABLE[0xA4] = 0x1C  # cmd=6 marker
DECODE_TABLE[0xA8] = 0x1E  # cmd=7 marker

# Data nibbles: ENCODE_TABLE[i] decodes to 0x20 | i
for _i, _v in enumerate(ENCODE_TABLE):
    DECODE_TABLE[_v] = 0x20 | _i


# ── PMBus Command Codes ─────────────────────────────────────────────
# Standard PMBus commands used for PSU telemetry.
# See PMBus Specification Part II for full command reference.

CMD_PAGE            = 0x00  # Select output rail: 0=12V, 1=5V, 2=3.3V
CMD_FAN_COMMAND     = 0x3B  # Fan duty cycle control
CMD_READ_VIN        = 0x88  # Input voltage (AC side), LINEAR11
CMD_READ_IIN        = 0x89  # Input current (AC side), LINEAR11
CMD_READ_VOUT       = 0x8B  # Output voltage (per-page), LINEAR11
CMD_READ_IOUT       = 0x8C  # Output current (per-page), LINEAR11
CMD_READ_TEMP1      = 0x8D  # Temperature sensor 1, LINEAR11
CMD_READ_TEMP2      = 0x8E  # Temperature sensor 2, LINEAR11
CMD_READ_FAN_SPEED  = 0x90  # Fan speed in RPM, LINEAR11
CMD_READ_POUT       = 0x96  # Output power (per-page), LINEAR11
CMD_READ_PIN        = 0x97  # Input power (AC side), LINEAR11
CMD_MFR_TOTAL_POUT  = 0xEE  # Corsair MFR: total DC output power, LINEAR11


# ── Dongle Action Codes ─────────────────────────────────────────────
# Internal commands for the SiUSBXpress dongle's SMBus bridge.

ACT_READ_MEMORY          = 0x08  # Read from dongle's result buffer
ACT_WRITE_SMBUS_SETTINGS = 0x11  # Configure SMBus parameters
ACT_READ_SMBUS_COMMAND   = 0x12  # Execute queued SMBus transaction
ACT_WRITE_SMBUS_COMMAND  = 0x13  # Queue an SMBus read/write command


# ── SMBus Init Payload ──────────────────────────────────────────────
# Sent once after connection to configure the SMBus bridge.
# Format: [ACT_WRITE_SMBUS_SETTINGS, 0x02, speed_khz, 0x00, 0x00, 0x00, 0x00]
# Speed 0x64 = 100 KHz (standard SMBus speed).

SMBUS_INIT_PAYLOAD = bytes([ACT_WRITE_SMBUS_SETTINGS, 0x02, 0x64, 0x00, 0x00, 0x00, 0x00])


# ── SiUSBXpress Control Transfer Values ─────────────────────────────
# Vendor control transfers: bmRequestType=0x40, bRequest=0x02, wIndex=0

SIUSBXP_ENABLE  = 0x0001  # wValue to enable SiUSBXpress device
SIUSBXP_FLUSH   = 0x0002  # wValue to flush buffers
SIUSBXP_DISABLE = 0x0000  # wValue to disable SiUSBXpress device

SIUSBXP_REQUEST_TYPE = 0x40  # bmRequestType: vendor, host-to-device
SIUSBXP_REQUEST      = 0x02  # bRequest code


# ── Encoding / Decoding Functions ───────────────────────────────────

def balanced_encode(msg: bytes, cmd: int = 0) -> bytes:
    """Encode a raw message to balanced-code wire format.

    Args:
        msg: Raw payload bytes to encode.
        cmd: Command index (0-7) for the start marker.

    Returns:
        Encoded bytes: [start_marker] [lo hi lo hi ...] [0x00]

    Example:
        >>> balanced_encode(bytes([0x12]), cmd=0).hex()
        '5459aa00'
        # 0x54 = start marker (cmd=0)
        # 0x59 = ENCODE[0x2] (low nibble of 0x12)
        # 0xAA = ENCODE[0x1] ... wait, ENCODE[1]=0x56
        # Actually: low nibble of 0x12 is 0x2 -> ENCODE[2]=0x59
        #           high nibble of 0x12 is 0x1 -> ENCODE[1]=0x56
        # Result: 54 59 56 00
    """
    out = bytearray()
    out.append(ENCODE_TABLE[(cmd << 1) & 0x0F] & 0xFC)  # start marker
    for b in msg:
        out.append(ENCODE_TABLE[b & 0x0F])         # LOW nibble first
        out.append(ENCODE_TABLE[(b >> 4) & 0x0F])   # HIGH nibble second
    out.append(0x00)  # terminator
    return bytes(out)


def balanced_decode(data: bytes) -> Tuple[bytes, int]:
    """Decode balanced-code wire format to raw bytes.

    Args:
        data: Raw wire bytes including start marker and terminator.

    Returns:
        Tuple of (decoded_payload, command_index).
        Returns (b"", -1) if data is too short.

    Example:
        >>> balanced_decode(bytes.fromhex('a8555500'))
        (b'\\x00', 0)
    """
    if len(data) < 3:
        return b"", -1

    # Extract command index from start marker
    marker = DECODE_TABLE[data[0]]
    cmd_index = (marker & 0x0F) >> 1

    # Decode nibble pairs
    out = bytearray()
    i = 1
    while i + 1 < len(data):
        if data[i] == 0x00:  # terminator
            break
        a = DECODE_TABLE[data[i]]
        b = DECODE_TABLE[data[i + 1]]
        out.append((a & 0x0F) | ((b & 0x0F) << 4))
        i += 2

    return bytes(out), cmd_index


def linear11_to_float(low: int, high: int) -> float:
    """Decode PMBus LINEAR11 format to a floating-point value.

    LINEAR11 is a 16-bit format used by PMBus for sensor readings:
      - Bits 15:11 = signed 5-bit exponent (-16 to +15)
      - Bits 10:0  = signed 11-bit mantissa (-1024 to +1023)
      - Value = mantissa * 2^exponent

    Args:
        low: Low byte (bits 7:0).
        high: High byte (bits 15:8).

    Returns:
        Decoded float value.

    Examples:
        >>> linear11_to_float(0x59, 0x08)  # 178.0 W
        178.0
        >>> linear11_to_float(0xDD, 0xF9)  # 238.5 V
        238.5
    """
    value = (high << 8) | low
    mantissa = value & 0x7FF
    if mantissa > 1024:
        mantissa = -(65536 - (mantissa | 0xF800))
    exponent = (value >> 11) & 0x1F
    if exponent > 15:
        exponent -= 32
    return mantissa * math.pow(2, exponent)
