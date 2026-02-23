# Architecture: Python to Power Supply

How `corsair-psu-monitor` reads telemetry from your Corsair PSU.

## System Overview

```
 ┌─────────────────────────────────────────────────────────────┐
 │                    YOUR APPLICATION                         │
 │                                                             │
 │   from corsair_psu_monitor import CorsairPSU               │
 │   with CorsairPSU() as psu:                                │
 │       stats = psu.read_all()                                │
 │       print(f"Power: {stats['input_power']}W")              │
 │                                                             │
 └───────────────────────────┬─────────────────────────────────┘
                             │
              Python API     │  read_all(), read_input_power(), etc.
                             │
 ┌───────────────────────────┼─────────────────────────────────┐
 │                   psu.py  │  CorsairPSU class               │
 │                           │                                 │
 │  ┌─────────────┐   ┌─────┴──────┐   ┌──────────────────┐   │
 │  │ read_all()  │──>│ read_reg() │──>│  _send_recv()    │   │
 │  │ read_rail() │   │ write_reg()│   │  encode + write   │   │
 │  │ format()    │   │ linear11() │   │  read + decode    │   │
 │  └─────────────┘   └────────────┘   └────────┬─────────┘   │
 │                                               │             │
 └───────────────────────────────────────────────┼─────────────┘
                                                 │
              Balanced-Code                      │  protocol.py
              Encoding                           │
 ┌───────────────────────────────────────────────┼─────────────┐
 │                protocol.py                    │             │
 │                                               │             │
 │  Raw bytes ──> balanced_encode() ──> Wire bytes             │
 │  Wire bytes ──> balanced_decode() ──> Raw bytes             │
 │  Register data ──> linear11_to_float() ──> Float            │
 │                                               │             │
 │  Each raw byte becomes 2 wire bytes:          │             │
 │  0x12 ──> [0x59, 0x56]  (low nibble first)   │             │
 │                                               │             │
 └───────────────────────────────────────────────┼─────────────┘
                                                 │
              USB Bulk I/O                       │  pyusb + libusb
                                                 │
 ┌───────────────────────────────────────────────┼─────────────┐
 │            pyusb / libusb-package             │             │
 │                                               │             │
 │  ep_out.write(encoded_bytes)    EP 0x02 OUT ──┤             │
 │  ep_in.read(64)                 EP 0x82 IN  ──┤             │
 │  ctrl_transfer(0x40, 0x02, ..)  Control EP ───┤             │
 │                                               │             │
 └───────────────────────────────────────────────┼─────────────┘
                                                 │
              Kernel Driver                      │  WinUSB / libusb
                                                 │
 ┌───────────────────────────────────────────────┼─────────────┐
 │  Windows: WinUSB.sys (installed via Zadig)    │             │
 │  Linux:   usbfs (kernel built-in)             │             │
 │  macOS:   IOKit (kernel built-in)             │             │
 └───────────────────────────────────────────────┼─────────────┘
                                                 │
 ═══════════════════ USB Cable ══════════════════╪═════════════
                                                 │
 ┌───────────────────────────────────────────────┼─────────────┐
 │          PSU USB Dongle (inside PSU)          │             │
 │                                               │             │
 │  ┌─────────────────────────────────────────┐  │             │
 │  │  Silicon Labs C8051F Microcontroller    │  │             │
 │  │  Running SiUSBXpress Firmware           │  │             │
 │  │                                         │  │             │
 │  │  USB Bulk ──> Decode balanced-code      │  │             │
 │  │           ──> Execute action code       │  │             │
 │  │           ──> I2C/SMBus transaction     │  │             │
 │  │           ──> Encode response           │  │             │
 │  │           ──> USB Bulk response         │  │             │
 │  └──────────────────┬──────────────────────┘  │             │
 │                     │                         │             │
 │              I2C/SMBus Bus                     │             │
 │                     │                         │             │
 │  ┌──────────────────┴──────────────────────┐  │             │
 │  │  PMBus Controller                       │  │             │
 │  │  (Microchip PIC + Flextronics custom)   │  │             │
 │  │                                         │  │             │
 │  │  Handles PMBus commands:                │  │             │
 │  │    PAGE select, READ_VIN, READ_IIN,     │  │             │
 │  │    READ_VOUT, READ_IOUT, READ_POUT,     │  │             │
 │  │    READ_TEMP, READ_FAN, READ_PIN,       │  │             │
 │  │    MFR_TOTAL_POUT                       │  │             │
 │  │                                         │  │             │
 │  │  Returns LINEAR11 encoded sensor data   │  │             │
 │  └──────────────────┬──────────────────────┘  │             │
 │                     │                         │             │
 │              Analog Sensors                   │             │
 │                     │                         │             │
 │  ┌──────────────────┴──────────────────────┐  │             │
 │  │  Power Stage Hardware                   │  │             │
 │  │                                         │  │             │
 │  │  ┌─────────┐ ┌─────────┐ ┌──────────┐  │  │             │
 │  │  │ Voltage │ │ Current │ │   Temp   │  │  │             │
 │  │  │ Dividers│ │ Shunts  │ │Thermistor│  │  │             │
 │  │  └─────────┘ └─────────┘ └──────────┘  │  │             │
 │  │                                         │  │             │
 │  │  ┌──────┐  ┌──────┐  ┌────────────┐    │  │             │
 │  │  │ 12V  │  │  5V  │  │   3.3V     │    │  │             │
 │  │  │ Rail │  │ Rail │  │   Rail     │    │  │             │
 │  │  └──────┘  └──────┘  └────────────┘    │  │             │
 │  └─────────────────────────────────────────┘  │             │
 │                                               │             │
 └───────────────────────────────────────────────┴─────────────┘
        AC Input ──> PFC ──> DC-DC ──> Output Rails
        (wall)                         (to PC components)
```

## Data Flow: Reading Input Voltage

```
 Time ──>

 Python          pyusb/libusb        USB Wire              PSU Dongle           PMBus Controller
 ──────          ────────────        ────────              ──────────           ────────────────

 read_linear11
 (CMD_READ_VIN)
      │
      ├─ Step 1: Queue Read
      │   send_recv([0x13, 0x03, 0x06, 0x01, 0x07, 0x02, 0x88])
      │          │
      │          ├─ balanced_encode()
      │          │   [0x13,0x03,...] ──> [0x54, 0x5A, 0x56, ...]
      │          │
      │          ├─ ep_out.write() ─────> Bulk OUT ──────> Receive
      │          │                                         Decode balanced
      │          │                                         Queue: "read 2 bytes
      │          │                                                 from reg 0x88"
      │          ├─ ep_in.read()  <────── Bulk IN <─────── Encode ACK
      │          │                        [0xA8, 0x00]
      │          │
      │   3ms delay
      │
      ├─ Step 2: Execute
      │   send_recv([0x12])
      │          │
      │          ├─ ep_out.write() ─────> Bulk OUT ──────> Receive
      │          │                                         Execute SMBus read
      │          │                                              │
      │          │                                              ├──> I2C START
      │          │                                              ├──> Address + R
      │          │                                              ├──> Cmd 0x88
      │          │                                              ├──> Read 2 bytes
      │          │                                              └──> I2C STOP
      │          │                                                      │
      │          │                                         Store [0xDD, 0xF9]
      │          │                                         in buffer at offset 7
      │          ├─ ep_in.read()  <────── Bulk IN <─────── Encode [0x00] ACK
      │          │
      │   3ms delay
      │
      ├─ Step 3: Read Buffer
      │   send_recv([0x08, 0x07, 0x02])
      │          │
      │          ├─ ep_out.write() ─────> Bulk OUT ──────> Receive
      │          │                                         Read buffer[7..8]
      │          │                                         Get [0xDD, 0xF9]
      │          ├─ ep_in.read()  <────── Bulk IN <─────── Encode [0xDD, 0xF9]
      │          │                        [0xA8, 0xA6, 0xA6, 0x96, 0xAA, 0x00]
      │          │
      │          ├─ balanced_decode()
      │          │   [0xA8, 0xA6, ...] ──> [0xDD, 0xF9]
      │
      ├─ linear11_to_float(0xDD, 0xF9)
      │   raw = 0xF9DD
      │   mantissa = 477, exponent = -1
      │   value = 477 * 2^(-1) = 238.5
      │
      └─ return 238.5  ──>  "Input Voltage: 238.5V"
```

## Module Dependency Graph

```
  corsair_psu_monitor/
  ├── __init__.py          imports from: psu.py, protocol.py
  ├── protocol.py          no internal dependencies (standalone)
  ├── psu.py               imports from: protocol.py
  └── cli.py               imports from: psu.py

  External:
  ├── pyusb                used by: psu.py (lazy import in open())
  └── libusb-package       used by: psu.py (lazy import in open())
```

## Platform-Specific Stack

```
                        ┌──────────────────┐
                        │  corsair-psu-mon  │
                        │  (Python package) │
                        └────────┬─────────┘
                                 │
                        ┌────────┴─────────┐
                        │ pyusb + libusb   │
                        │ (pip packages)   │
                        └────────┬─────────┘
                                 │
            ┌────────────────────┼────────────────────┐
            │                    │                    │
   ┌────────┴────────┐ ┌────────┴────────┐ ┌────────┴────────┐
   │    Windows      │ │     Linux       │ │     macOS       │
   ├─────────────────┤ ├─────────────────┤ ├─────────────────┤
   │ WinUSB.sys      │ │ usbfs (kernel)  │ │ IOKit (kernel)  │
   │ (via Zadig)     │ │ + udev rules    │ │ + libusb        │
   │                 │ │ for permissions  │ │ (via Homebrew)  │
   │ Replaces the    │ │                 │ │                 │
   │ SiUSBXpress     │ │ No driver       │ │ No driver       │
   │ driver from     │ │ change needed   │ │ change needed   │
   │ Corsair iCUE    │ │                 │ │                 │
   └─────────────────┘ └─────────────────┘ └─────────────────┘
```

## Wire Encoding Visualization

```
Raw byte: 0x3F

Binary:  0011 1111
         ──── ────
         high  low
         nib   nib

Encoding (low nibble first):
  Low nibble  0xF ──> ENCODE[0xF] = 0xAA  (10101010)
  High nibble 0x3 ──> ENCODE[0x3] = 0x5A  (01011010)

Wire: [0xAA, 0x5A]

         ┌────────────────────────────────┐
Raw:     │ 0  0  1  1  1  1  1  1        │  = 0x3F
         └────────────────────────────────┘
                    │ split
         ┌─────────┴──────────┐
         │                    │
    High: 0x3            Low: 0xF
         │                    │
    ENCODE[3]            ENCODE[F]
         │                    │
    ┌────┴─────┐        ┌────┴─────┐
    │ 01011010 │        │ 10101010 │  = 0x5A, 0xAA
    └──────────┘        └──────────┘
         │                    │
         │  Wire order: LOW first, HIGH second
         │                    │
    Wire: [0xAA, 0x5A]
           ────  ────
           low   high
```
