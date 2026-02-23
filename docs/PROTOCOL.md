# Corsair AXi/HXi PSU USB Protocol Reference

Complete interface documentation for communicating with Corsair digital power supplies
over USB. Covers the full stack from USB device identification through wire encoding
to PMBus register reads.

## Table of Contents

- [USB Device Identification](#usb-device-identification)
- [SiUSBXpress Control Transfers](#siusbxpress-control-transfers)
- [Balanced-Code Wire Encoding](#balanced-code-wire-encoding)
- [Dongle Action Codes](#dongle-action-codes)
- [SMBus Bridge Protocol](#smbus-bridge-protocol)
- [PMBus Register Map](#pmbus-register-map)
- [LINEAR11 Data Format](#linear11-data-format)
- [Timing Requirements](#timing-requirements)
- [Worked Examples](#worked-examples)

---

## USB Device Identification

All Corsair AXi/HXi PSUs use the same Corsair USB Vendor ID with model-specific
Product IDs:

| Model    | VID      | PID      | Wattage | Efficiency    |
|----------|----------|----------|---------|---------------|
| AX1600i  | `0x1B1C` | `0x1C11` | 1600W   | 80+ Titanium  |
| AX1300i  | `0x1B1C` | `0x1C10` | 1300W   | 80+ Titanium  |
| AX1000i  | `0x1B1C` | `0x1C0F` | 1000W   | 80+ Titanium  |
| AX850i   | `0x1B1C` | `0x1C0E` | 850W    | 80+ Platinum  |
| AX860i   | `0x1B1C` | `0x1C0C` | 860W    | 80+ Platinum  |
| AX760i   | `0x1B1C` | `0x1C0D` | 760W    | 80+ Platinum  |
| HX1200i  | `0x1B1C` | `0x1C08` | 1200W   | 80+ Platinum  |
| HX1000i  | `0x1B1C` | `0x1C07` | 1000W   | 80+ Platinum  |
| HX850i   | `0x1B1C` | `0x1C06` | 850W    | 80+ Platinum  |
| HX750i   | `0x1B1C` | `0x1C05` | 750W    | 80+ Platinum  |
| HX650i   | `0x1B1C` | `0x1C04` | 650W    | 80+ Platinum  |

**USB Descriptor Details (AX1600i):**
- Manufacturer: `SILABS`
- Product: `USB API`
- Interface: Vendor-class (0xFF)
- Endpoints: Bulk OUT (`0x02`), Bulk IN (`0x82`), max packet 64 bytes

---

## SiUSBXpress Control Transfers

The PSU USB dongle contains a Silicon Labs C8051F microcontroller running
SiUSBXpress firmware. Before any bulk data transfer, the device must be
enabled via vendor control transfers.

### Control Transfer Parameters

All SiUSBXpress control transfers use:
- **bmRequestType:** `0x40` (vendor, host-to-device)
- **bRequest:** `0x02`
- **wIndex:** `0x0000`

### Commands

| Operation | wValue   | Description                                    |
|-----------|----------|------------------------------------------------|
| Enable    | `0x0001` | Enable SiUSBXpress device for bulk transfers   |
| Flush     | `0x0002` | Flush internal RX/TX buffers                   |
| Disable   | `0x0000` | Disable device (call before disconnecting)     |

### Initialization Sequence

```
1. ctrl_transfer(0x40, 0x02, 0x0001, 0)   # Enable
2. sleep(50ms)
3. ctrl_transfer(0x40, 0x02, 0x0002, 0)   # Flush
4. sleep(50ms)
5. Drain stale bulk IN data (read until timeout)
6. Send SMBus init command (see below)
```

**Important:** Bulk transfers will timeout or fail if the Enable control
transfer has not been sent first.

---

## Balanced-Code Wire Encoding

All data on the USB bulk endpoints uses balanced-code encoding. This ensures
DC balance (equal 0s and 1s per byte) as required by the SiUSBXpress protocol.

### Encoding Table

Each 4-bit nibble maps to one 8-bit encoded byte:

| Nibble | Hex  | Encoded | Binary     |
|--------|------|---------|------------|
| 0x0    | 0    | `0x55`  | `01010101` |
| 0x1    | 1    | `0x56`  | `01010110` |
| 0x2    | 2    | `0x59`  | `01011001` |
| 0x3    | 3    | `0x5A`  | `01011010` |
| 0x4    | 4    | `0x65`  | `01100101` |
| 0x5    | 5    | `0x66`  | `01100110` |
| 0x6    | 6    | `0x69`  | `01101001` |
| 0x7    | 7    | `0x6A`  | `01101010` |
| 0x8    | 8    | `0x95`  | `10010101` |
| 0x9    | 9    | `0x96`  | `10010110` |
| 0xA    | A    | `0x99`  | `10011001` |
| 0xB    | B    | `0x9A`  | `10011010` |
| 0xC    | C    | `0xA5`  | `10100101` |
| 0xD    | D    | `0xA6`  | `10100110` |
| 0xE    | E    | `0xA9`  | `10101001` |
| 0xF    | F    | `0xAA`  | `10101010` |

Note: Every encoded byte has exactly 4 zero bits and 4 one bits.

### Message Format

```
[start_marker] [lo0 hi0] [lo1 hi1] ... [loN hiN] [0x00]
 ^               ^    ^                            ^
 |               |    |                            terminator
 |               |    high nibble of byte 0
 |               low nibble of byte 0
 command marker
```

- **Start marker:** `ENCODE[(cmd << 1) & 0x0F] & 0xFC`
  - For cmd=0: `ENCODE[0] & 0xFC = 0x55 & 0xFC = 0x54`
- **Data bytes:** Each raw byte becomes two encoded bytes, **low nibble first**
- **Terminator:** `0x00`

### Decoding Table

The decode table maps each possible wire byte to a value where the high
nibble indicates the byte type:

| High nibble | Meaning   | Low nibble contains    |
|-------------|-----------|------------------------|
| `0x2_`      | Data      | 4-bit nibble value     |
| `0x1_`      | Marker    | Command info           |
| `0x3_`      | Terminator| (padding)              |
| `0x0_`      | Invalid   | -                      |

**Marker byte decode values:**

| Wire byte | Decode | Cmd index |
|-----------|--------|-----------|
| `0x54`    | `0x10` | 0         |
| `0x58`    | `0x12` | 1         |
| `0x64`    | `0x14` | 2         |
| `0x68`    | `0x16` | 3         |
| `0x94`    | `0x18` | 4         |
| `0x98`    | `0x1A` | 5         |
| `0xA4`    | `0x1C` | 6         |
| `0xA8`    | `0x1E` | 7         |

Command index = `(decode_value & 0x0F) >> 1`

### Decoding Algorithm

```python
def decode(wire_data):
    cmd = (DECODE[wire_data[0]] & 0x0F) >> 1   # extract command
    result = bytearray()
    i = 1
    while i + 1 < len(wire_data):
        if wire_data[i] == 0x00:                # terminator
            break
        lo = DECODE[wire_data[i]] & 0x0F        # low nibble
        hi = DECODE[wire_data[i+1]] & 0x0F      # high nibble
        result.append(lo | (hi << 4))
        i += 2
    return bytes(result), cmd
```

---

## Dongle Action Codes

The first byte of every decoded message is an action code that tells the
dongle what operation to perform.

| Code   | Name                   | Direction | Description                         |
|--------|------------------------|-----------|-------------------------------------|
| `0x08` | ACT_READ_MEMORY        | Read      | Read from dongle's result buffer    |
| `0x11` | ACT_WRITE_SMBUS_SETTINGS | Write   | Configure SMBus bus parameters      |
| `0x12` | ACT_READ_SMBUS_COMMAND | Execute   | Execute queued SMBus transaction    |
| `0x13` | ACT_WRITE_SMBUS_COMMAND| Write     | Queue an SMBus read or write        |

### SMBus Settings (0x11)

Configures the dongle's I2C/SMBus bridge. Sent once during initialization.

**Message:** `[0x11, 0x02, speed, 0x00, 0x00, 0x00, 0x00]`

| Byte | Value  | Meaning                         |
|------|--------|---------------------------------|
| 0    | `0x11` | Action: Write SMBus Settings    |
| 1    | `0x02` | Sub-command: set speed          |
| 2    | `0x64` | Speed: 100 KHz (standard mode)  |
| 3-6  | `0x00` | Reserved                        |

### Queue Read Command (0x13, sub=0x03)

Queues a PMBus register read on the SMBus bus.

**Message:** `[0x13, 0x03, 0x06, 0x01, 0x07, length, register]`

| Byte | Value      | Meaning                           |
|------|------------|-----------------------------------|
| 0    | `0x13`     | Action: Write SMBus Command       |
| 1    | `0x03`     | Sub-command: queue read           |
| 2    | `0x06`     | Payload length (remaining bytes)  |
| 3    | `0x01`     | SMBus address mode                |
| 4    | `0x07`     | Result buffer offset              |
| 5    | `length`   | Bytes to read (usually 2)         |
| 6    | `register` | PMBus register address            |

### Queue Write Command (0x13, sub=0x01)

Queues a PMBus register write (e.g., page select).

**Message:** `[0x13, 0x01, 0x04, data_len+1, register, data...]`

| Byte | Value       | Meaning                          |
|------|-------------|----------------------------------|
| 0    | `0x13`      | Action: Write SMBus Command      |
| 1    | `0x01`      | Sub-command: queue write         |
| 2    | `0x04`      | Payload length marker            |
| 3    | `len+1`     | Total bytes (register + data)    |
| 4    | `register`  | PMBus register address           |
| 5+   | `data`      | Bytes to write                   |

### Execute Transaction (0x12)

Executes the previously queued SMBus command. Must be called after
every 0x13 command.

**Message:** `[0x12]`

### Read Result Buffer (0x08)

Reads the result of the last executed SMBus read transaction.

**Message:** `[0x08, 0x07, length]`

| Byte | Value    | Meaning                         |
|------|----------|---------------------------------|
| 0    | `0x08`   | Action: Read Memory             |
| 1    | `0x07`   | Buffer offset                   |
| 2    | `length` | Bytes to read (matches queued)  |

---

## SMBus Bridge Protocol

All PMBus register access goes through a three-step bridge sequence.

### Reading a Register (3 Steps)

```
Step 1: Queue Read
  TX: [0x13, 0x03, 0x06, 0x01, 0x07, 0x02, <register>]
  RX: (empty response = ACK)
  Wait: 3ms

Step 2: Execute Transaction
  TX: [0x12]
  RX: [0x00] = ACK_OK
  Wait: 3ms

Step 3: Read Result Buffer
  TX: [0x08, 0x07, 0x02]
  RX: [low_byte, high_byte]   <- the register value (little-endian)
  Wait: 3ms
```

### Writing a Register (2 Steps)

```
Step 1: Queue Write
  TX: [0x13, 0x01, 0x04, <len+1>, <register>, <data...>]
  RX: (empty response = ACK)
  Wait: 3ms

Step 2: Execute Transaction
  TX: [0x12]
  RX: [0x00] = ACK_OK
  Wait: 8ms (extra settle time for page switch)
```

### Page Selection (Per-Rail Reads)

The PSU has multiple output rails. To read per-rail registers (VOUT, IOUT,
POUT), first select the page:

| Page | Rail  | Nominal Voltage |
|------|-------|-----------------|
| 0    | 12V   | 12.0V           |
| 1    | 5V    | 5.0V            |
| 2    | 3.3V  | 3.3V            |

Write `CMD_PAGE` (0x00) with the page number:
```
TX: [0x13, 0x01, 0x04, 0x02, 0x00, <page>]
    (Execute with [0x12])
    Wait: 8ms + 5ms settle
```

---

## PMBus Register Map

### Global Registers (Not Page-Dependent)

| Address | Name             | Unit | Encoding | Description                    |
|---------|------------------|------|----------|--------------------------------|
| `0x88`  | READ_VIN         | V    | LINEAR11 | AC input voltage               |
| `0x89`  | READ_IIN         | A    | LINEAR11 | AC input current               |
| `0x8D`  | READ_TEMP1       | C    | LINEAR11 | Temperature sensor 1 (intake)  |
| `0x8E`  | READ_TEMP2       | C    | LINEAR11 | Temperature sensor 2 (internal)|
| `0x90`  | READ_FAN_SPEED   | RPM  | LINEAR11 | Fan speed (0 = fan-off mode)   |
| `0x97`  | READ_PIN         | W    | LINEAR11 | Total AC input power           |
| `0xEE`  | MFR_TOTAL_POUT   | W    | LINEAR11 | Total DC output power (MFR)    |

### Per-Page Registers (Require Page Selection)

| Address | Name             | Unit | Encoding | Description                    |
|---------|------------------|------|----------|--------------------------------|
| `0x8B`  | READ_VOUT        | V    | LINEAR11 | Output voltage for selected rail|
| `0x8C`  | READ_IOUT        | A    | LINEAR11 | Output current for selected rail|
| `0x96`  | READ_POUT        | W    | LINEAR11 | Output power for selected rail  |

### Control Registers

| Address | Name             | Type  | Description                    |
|---------|------------------|-------|--------------------------------|
| `0x00`  | CMD_PAGE         | Write | Select output rail (0/1/2)     |
| `0x3B`  | FAN_COMMAND      | Write | Fan duty cycle (0-100%)        |

---

## LINEAR11 Data Format

Most PMBus sensor readings use the LINEAR11 format, a 16-bit fixed-point
representation:

```
Bit:  15  14  13  12  11  10   9   8   7   6   5   4   3   2   1   0
     [  exponent (5-bit) ][       mantissa (11-bit signed)          ]
```

### Fields

- **Exponent** (bits 15:11): Signed 5-bit integer, range -16 to +15
- **Mantissa** (bits 10:0): Signed 11-bit integer, range -1024 to +1023

### Formula

```
value = mantissa * 2^exponent
```

### Decoding Algorithm

```python
def linear11_to_float(low_byte, high_byte):
    raw = (high_byte << 8) | low_byte    # little-endian
    mantissa = raw & 0x7FF               # bits 10:0
    if mantissa > 1024:                  # sign extension
        mantissa -= 2048
    exponent = (raw >> 11) & 0x1F        # bits 15:11
    if exponent > 15:                    # sign extension
        exponent -= 32
    return mantissa * (2 ** exponent)
```

### Example Values

| Raw (hex) | Raw (LE)    | Exponent | Mantissa | Value      | Meaning        |
|-----------|-------------|----------|----------|------------|----------------|
| `0x0859`  | `59 08`     | +1       | 89       | 178.0      | 178W power     |
| `0xF9DD`  | `DD F9`     | -1       | 477      | 238.5      | 238.5V voltage |
| `0xD81E`  | `1E D8`     | -5       | 30       | 0.9375     | 0.94A current  |
| `0xF0AF`  | `AF F0`     | -2       | 175      | 43.75      | 43.8C temp     |

---

## Timing Requirements

| Operation                        | Delay    | Notes                        |
|----------------------------------|----------|------------------------------|
| After SiUSBXpress Enable         | 50 ms    | Device initialization        |
| After SiUSBXpress Flush          | 50 ms    | Buffer clear                 |
| Between SMBus protocol steps     | 3 ms     | Min inter-step delay         |
| After page switch execute (0x12) | 8 ms     | Page register needs settling |
| After page switch before read    | 5 ms     | Additional settle time       |
| Between bulk IN read attempts    | 5 ms     | Response may span chunks     |
| USB bulk write timeout           | 2000 ms  | Generous for slow devices    |
| USB bulk read timeout            | 2000 ms  | Includes SMBus round-trip    |

---

## Worked Examples

### Example 1: Read Input Voltage (0x88)

**Step 1: Queue read**
```
Raw TX:     13 03 06 01 07 02 88
Encoded TX: 54 5A 56 5A 55 69 55 56 55 6A 59 55 95 95 00
            ^                                           ^
            start marker (cmd=0)                        terminator

Raw RX:     (empty)
Encoded RX: A8 00
            ^
            response marker (0xA8 -> cmd=0)
```

**Step 2: Execute**
```
Raw TX:     12
Encoded TX: 54 59 56 00

Raw RX:     00
Encoded RX: A8 55 55 00
               ^  ^
               low=0x0 high=0x0 -> byte 0x00 = ACK_OK
```

**Step 3: Read result**
```
Raw TX:     08 07 02
Encoded TX: 54 95 55 6A 55 59 55 00

Raw RX:     DD F9
Encoded RX: A8 A6 A6 96 AA 00
               ^  ^  ^  ^
               lo=D hi=D -> 0xDD
                     lo=9 hi=F -> 0xF9
```

**Decode LINEAR11:** `0xF9DD`
- mantissa = 0x1DD = 477
- exponent = 0x1F = -1 (signed)
- value = 477 * 2^(-1) = **238.5V**

### Example 2: Select Page 0 (12V Rail)

```
Raw TX:     13 01 04 02 00 00     (write register 0x00 with data 0x00)
Encoded TX: 54 5A 56 56 55 65 55 59 55 55 55 55 55 00

(Execute with [0x12], wait 8ms)
```

### Example 3: Full Telemetry Read Sequence

```
1. SiUSBXpress Enable + Flush
2. SMBus Init: [0x11, 0x02, 0x64, 0x00, 0x00, 0x00, 0x00]
3. Read 0x97 (input power)    -> 3-step bridge
4. Read 0xEE (output power)   -> 3-step bridge
5. Read 0x88 (input voltage)  -> 3-step bridge
6. Read 0x89 (input current)  -> 3-step bridge
7. Read 0x8D (temp 1)         -> 3-step bridge
8. Read 0x8E (temp 2)         -> 3-step bridge
9. Read 0x90 (fan speed)      -> 3-step bridge
10. Write page=0, read 0x8B/0x8C/0x96 (12V rail)
11. Write page=1, read 0x8B/0x8C/0x96 (5V rail)
12. Write page=2, read 0x8B/0x8C/0x96 (3.3V rail)
```

Total: ~30 USB round-trips, ~500ms at 3ms inter-step delays.

---

## References

- [Jon0/ax1600i](https://github.com/Jon0/ax1600i) - Rust implementation
- [ka87/cpsumon](https://github.com/ka87/cpsumon) - C implementation
- [EvanMulawski/FanControl.CorsairLink](https://github.com/EvanMulawski/FanControl.CorsairLink) - C# implementation
- [Silicon Labs AN169](https://www.silabs.com/documents/public/application-notes/AN169.pdf) - USBXpress Programmer's Guide
- [PMBus Specification Part II](https://pmbus.org/specifications) - PMBus Command Reference
