# corsair-psu-monitor

Read real-time telemetry from Corsair AXi and HXi digital power supplies over USB.

Get input/output power, voltage, current, temperature, fan speed, and per-rail
breakdowns directly from your PSU — no iCUE required.

## Supported Models

| Model    | PID      | Wattage | Rating        |
|----------|----------|---------|---------------|
| AX1600i  | `0x1C11` | 1600W   | 80+ Titanium  |
| AX1300i  | `0x1C10` | 1300W   | 80+ Titanium  |
| AX1000i  | `0x1C0F` | 1000W   | 80+ Titanium  |
| AX850i   | `0x1C0E` | 850W    | 80+ Platinum  |
| AX860i   | `0x1C0C` | 860W    | 80+ Platinum  |
| AX760i   | `0x1C0D` | 760W    | 80+ Platinum  |
| HX1200i  | `0x1C08` | 1200W   | 80+ Platinum  |
| HX1000i  | `0x1C07` | 1000W   | 80+ Platinum  |
| HX850i   | `0x1C06` | 850W    | 80+ Platinum  |
| HX750i   | `0x1C05` | 750W    | 80+ Platinum  |
| HX650i   | `0x1C04` | 650W    | 80+ Platinum  |

## Installation

```bash
pip install corsair-psu-monitor
```

### Driver Setup (Required)

<details>
<summary><b>Windows</b> — Install WinUSB via Zadig</summary>

1. Download [Zadig](https://zadig.akeo.ie/)
2. Run Zadig as Administrator
3. Select **Options > List All Devices**
4. Find **"USB API"** (VID: 1B1C) in the dropdown
5. Set target driver to **WinUSB**
6. Click **Replace Driver**

Alternatively, use the `.inf` file in [`drivers/windows/`](drivers/windows/).

> **Note:** This replaces Corsair's SiUSBXpress driver. iCUE will no longer be
> able to communicate with the PSU. Use Zadig to switch back if needed.
</details>

<details>
<summary><b>Linux</b> — Install udev rules</summary>

```bash
sudo cp drivers/linux/99-corsair-psu.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
# Log out and back in
```

No driver change needed — Linux uses the built-in `usbfs` interface.
</details>

<details>
<summary><b>macOS</b> — Install libusb</summary>

```bash
brew install libusb
```

No driver change needed — macOS uses IOKit natively.
</details>

## Quick Start

### Python API

```python
from corsair_psu_monitor import CorsairPSU

with CorsairPSU() as psu:
    stats = psu.read_all()
    print(f"Input:  {stats['input_power']:.0f}W")
    print(f"Output: {stats['output_power']:.0f}W")
    print(f"Efficiency: {stats['efficiency']:.0f}%")
    print(f"12V Rail: {stats['12v_voltage']:.2f}V / {stats['12v_current']:.1f}A")
```

### Command Line

```bash
# Single reading
corsair-psu-monitor

# Continuous monitoring (Ctrl+C to stop)
corsair-psu-monitor watch

# Update every 5 seconds
corsair-psu-monitor watch -i 5

# JSON output (for scripting)
corsair-psu-monitor json

# Continuous JSON stream
corsair-psu-monitor json --loop -i 2
```

### Example Output

```
====================================================
  Corsair AX1600i PSU Telemetry
====================================================
  Input Power:      245.0 W
  Output Power:     218.0 W
  Efficiency:        89.0 %
  Input Voltage:    239.5 V
  Input Current:      1.0 A
  Temp 1:            43.5 C
  Temp 2:            39.8 C
  Fan:                  0 RPM
  --------------------------------------------------
   12V Rail:  12.05 V   15.38 A   172.0 W
    5V Rail:   4.94 V    0.00 A    11.5 W
  3.3V Rail:   3.28 V    0.00 A     0.0 W
  Rail Sum:   183.5 W
====================================================
```

## API Reference

### `CorsairPSU`

```python
psu = CorsairPSU(vid=0x1B1C, pid=None)  # auto-detect model
psu.open()                                # open USB connection
psu.close()                               # close connection

# Properties
psu.model      # str: "AX1600i"
psu.connected  # bool: True if open

# Individual readings
psu.read_input_power()     # float: AC input watts
psu.read_output_power()    # float: DC output watts
psu.read_apparent_power()  # float: V*I in VA
psu.read_input_voltage()   # float: AC volts
psu.read_input_current()   # float: AC amps
psu.read_temp1()           # float: Celsius
psu.read_temp2()           # float: Celsius
psu.read_fan_rpm()         # float: RPM

# Per-rail readings
psu.read_rail(0)           # dict: {voltage, current, power} for 12V
psu.read_rail(1)           # dict: 5V rail
psu.read_rail(2)           # dict: 3.3V rail
psu.read_12v_rail()        # shortcut for read_rail(0)

# Aggregate
stats = psu.read_all()     # dict with ALL readings
psu.format_status(stats)   # one-line summary string

# Low-level
psu.read_register(0x88, 2) # raw PMBus register bytes
psu.write_register(0x00, bytes([0]))  # write PMBus register
psu.read_linear11(0x88)    # read + decode LINEAR11
```

## How It Works

```
 Python App                pyusb/libusb              USB Cable                PSU Dongle
 ──────────                ────────────              ─────────                ──────────
                                                                          ┌──────────────┐
 CorsairPSU.read_all() ─> balanced_encode() ──> Bulk OUT EP 0x02 ──────> │ SiLabs C8051F│
                                                                          │ MCU running   │
                          balanced_decode() <── Bulk IN  EP 0x82 <─────── │ SiUSBXpress   │
                                                                          │               │
                                                                          │   I2C/SMBus   │
                                                                          │      │        │
                                                                          │  ┌───┴──────┐ │
                                                                          │  │  PMBus   │ │
                                                                          │  │Controller│ │
                                                                          │  │          │ │
                                                                          │  │ Sensors: │ │
                                                                          │  │ V,I,T,Fan│ │
                                                                          │  └──────────┘ │
                                                                          └──────────────┘
```

The PSU dongle uses **balanced-code encoding** (each byte becomes 2 wire bytes with
DC-balanced bit patterns) and a **three-step SMBus bridge** protocol (queue command,
execute, read result). See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the full
wire-level specification and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for
detailed architecture diagrams.

## Project Structure

```
corsair-psu-monitor/
  src/corsair_psu_monitor/
    __init__.py          # Package exports
    protocol.py          # Balanced-code encoding, LINEAR11, constants
    psu.py               # CorsairPSU class
    cli.py               # Command-line interface
  drivers/
    windows/             # WinUSB .inf driver file
    linux/               # udev rules for non-root access
  docs/
    PROTOCOL.md          # Full protocol specification
    ARCHITECTURE.md      # System architecture diagrams
  tests/                 # Unit tests (no hardware needed)
  examples/              # Usage examples
```

## Development

```bash
git clone https://github.com/bencoster/corsair-psu-monitor.git
cd corsair-psu-monitor
pip install -e ".[dev]"
pytest
```

## Credits

Protocol reverse-engineered with reference to:
- [Jon0/ax1600i](https://github.com/Jon0/ax1600i) (Rust)
- [ka87/cpsumon](https://github.com/ka87/cpsumon) (C)
- [EvanMulawski/FanControl.CorsairLink](https://github.com/EvanMulawski/FanControl.CorsairLink) (C#)

## License

MIT License. See [LICENSE](LICENSE).
