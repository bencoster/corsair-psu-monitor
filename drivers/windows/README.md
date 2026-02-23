# Windows Driver Setup

Corsair PSUs ship with a Silicon Labs SiUSBXpress driver that prevents direct USB
access from Python. You need to replace it with the generic WinUSB driver.

## Option A: Zadig (Recommended)

[Zadig](https://zadig.akeo.ie/) is a free utility that installs generic USB drivers.

1. **Download** Zadig from https://zadig.akeo.ie/
2. **Run** Zadig as Administrator
3. Select **Options > List All Devices**
4. Find **"USB API"** (VID: 1B1C, PID: 1C11 for AX1600i) in the dropdown
5. Set the target driver to **WinUSB**
6. Click **Replace Driver** (or Install Driver)
7. Wait for "Driver installed successfully"

## Option B: INF File

1. Open **Device Manager**
2. Find the PSU device (may appear as "USB API" or under "SiUSBXpress")
3. Right-click > **Update driver** > Browse my computer > Let me pick
4. Click **Have Disk** > Browse to `corsair-psu-winusb.inf`
5. Select the matching PSU model and click Install

## Reverting

To restore the original SiUSBXpress driver (for iCUE compatibility):
- Use Zadig to switch back to the original driver, or
- Reinstall Corsair iCUE (it will reinstall its driver)

## Important Notes

- **iCUE Incompatibility**: WinUSB and iCUE's SiUSBXpress driver are mutually
  exclusive. You cannot use both at the same time.
- **Corsair Services**: If iCUE is installed, stop Corsair services before
  switching drivers: `net stop CorsairService`
- **Admin Required**: Driver installation requires administrator privileges.
