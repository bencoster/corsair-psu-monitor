"""WinUSB driver installer for Corsair PSU devices (Windows only).

Automates detection and installation of the WinUSB driver needed for
pyusb/libusb to communicate with Corsair AXi/HXi PSU dongles.

On Windows, the PSU dongle ships with a Silicon Labs SiUSBXpress driver
that blocks direct USB access.  This module installs the WinUSB driver
using the bundled .inf file via two methods:

  1. **pnputil** — Adds the driver package to the Windows driver store
     and binds it to the target device.
  2. **newdev.dll SetupAPI** — Calls ``UpdateDriverForPlugAndPlayDevicesW``
     via ctypes to force the driver onto the hardware ID.

Both are built into Windows — no external downloads needed.

Usage::

    from corsair_psu_monitor.driver_installer import (
        check_driver_status, install_winusb_driver
    )

    status = check_driver_status()
    if status.needs_install:
        result = install_winusb_driver()
        print(result.message)
"""

import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .protocol import CORSAIR_VENDOR_ID, SUPPORTED_DEVICES

logger = logging.getLogger(__name__)


# ── Types ────────────────────────────────────────────────────────────


class DriverStatus(Enum):
    """Current state of the WinUSB driver for the PSU device."""
    OK = "ok"
    NEEDS_INSTALL = "needs_install"
    NO_DEVICE = "no_device"
    DEVICE_ERROR = "device_error"
    NOT_WINDOWS = "not_windows"


class InstallResult(Enum):
    """Outcome of driver installation attempt."""
    SUCCESS = "success"
    ALREADY_INSTALLED = "already_installed"
    NEEDS_ADMIN = "needs_admin"
    DEVICE_NOT_FOUND = "device_not_found"
    INF_NOT_FOUND = "inf_not_found"
    INSTALL_FAILED = "install_failed"
    NOT_WINDOWS = "not_windows"


@dataclass
class DriverCheckResult:
    """Result of a driver status check."""
    status: DriverStatus
    device_name: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None
    current_driver: Optional[str] = None
    is_present: Optional[bool] = None
    needs_install: bool = False
    message: str = ""


@dataclass
class DriverInstallResult:
    """Result of a driver installation attempt."""
    result: InstallResult
    message: str
    return_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


# ── Admin helpers ────────────────────────────────────────────────────


def _is_admin() -> bool:
    """Check if running with administrator privileges."""
    if platform.system() != "Windows":
        return os.getuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _request_elevation() -> bool:
    """Re-launch the current Python command with admin privileges (UAC).

    Returns True if elevation was requested (a new elevated process
    will run; the caller should exit).  Returns False on failure.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        params = " ".join(sys.argv)
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
        return ret > 32  # > 32 means success
    except Exception as e:
        logger.error("Failed to request elevation: %s", e)
        return False


# ── .inf file locator ────────────────────────────────────────────────


def _find_inf() -> Optional[Path]:
    """Locate the bundled corsair-psu-winusb.inf driver file.

    Search order:
      1. Development layout: ../../drivers/windows/
      2. Installed package: ./drivers/
    """
    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir.parent.parent / "drivers" / "windows" / "corsair-psu-winusb.inf",
        pkg_dir / "drivers" / "corsair-psu-winusb.inf",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


# ── Device detection via PowerShell ──────────────────────────────────


def _query_device_info(
    vid: int = CORSAIR_VENDOR_ID,
    pid: Optional[int] = None,
) -> Optional[dict]:
    """Query Windows Device Manager for a Corsair PSU device.

    Returns a dict with keys: pid, model, driver_service, driver_provider,
    is_present, friendly_name, instance_id.  Returns None if not found.
    """
    if platform.system() != "Windows":
        return None

    pids = [pid] if pid else list(SUPPORTED_DEVICES.keys())

    for check_pid in pids:
        instance_pattern = f"USB\\VID_{vid:04X}&PID_{check_pid:04X}*"
        cmd = [
            "powershell.exe", "-NoProfile", "-Command",
            f'Get-PnpDevice -InstanceId "{instance_pattern}" '
            f'-ErrorAction SilentlyContinue | '
            f'Select-Object -First 1 | '
            f'Format-List Status,FriendlyName,InstanceId,Service',
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10)
            output = proc.stdout.strip()
        except Exception as e:
            logger.debug("PowerShell query failed: %s", e)
            continue

        if not output or "InstanceId" not in output:
            continue

        # Parse Format-List output
        info = {}
        for line in output.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                info[key.strip()] = val.strip()

        # Query additional properties (IsPresent, DriverProvider)
        instance_id = info.get("InstanceId", "")
        if instance_id:
            props = _query_device_properties(instance_id)
            info.update(props)

        model = SUPPORTED_DEVICES.get(check_pid, f"PID_{check_pid:04X}")
        return {
            "pid": check_pid,
            "model": model,
            "driver_service": info.get("Service", ""),
            "driver_provider": info.get("DriverProvider", ""),
            "is_present": info.get("IsPresent", "").lower() == "true",
            "friendly_name": info.get("FriendlyName", ""),
            "instance_id": instance_id,
            "status": info.get("Status", ""),
        }

    return None


def _query_device_properties(instance_id: str) -> dict:
    """Query extended PnP device properties."""
    result = {}
    cmd = [
        "powershell.exe", "-NoProfile", "-Command",
        f'$props = Get-PnpDeviceProperty -InstanceId "{instance_id}" '
        f'-ErrorAction SilentlyContinue; '
        f'$present = ($props | Where-Object {{ $_.KeyName -eq '
        f'"DEVPKEY_Device_IsPresent" }}).Data; '
        f'$provider = ($props | Where-Object {{ $_.KeyName -eq '
        f'"DEVPKEY_Device_DriverProvider" }}).Data; '
        f'Write-Output "IsPresent=$present"; '
        f'Write-Output "DriverProvider=$provider"',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10)
        for line in proc.stdout.strip().splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip()
    except Exception:
        pass
    return result


def _check_libusb_visible(
    vid: int = CORSAIR_VENDOR_ID,
    pid: Optional[int] = None,
) -> Optional[dict]:
    """Check if libusb can see the device (independent of Windows driver).

    Returns dict with bus, address, num_configurations or None.
    """
    try:
        import libusb_package
        import usb.core

        backend = libusb_package.get_libusb1_backend()
        if pid:
            dev = usb.core.find(
                idVendor=vid, idProduct=pid, backend=backend)
            if dev:
                return {
                    "pid": pid,
                    "bus": dev.bus,
                    "address": dev.address,
                    "num_configs": dev.bNumConfigurations,
                }
        else:
            for check_pid in SUPPORTED_DEVICES:
                dev = usb.core.find(
                    idVendor=vid, idProduct=check_pid, backend=backend)
                if dev:
                    return {
                        "pid": check_pid,
                        "bus": dev.bus,
                        "address": dev.address,
                        "num_configs": dev.bNumConfigurations,
                    }
    except Exception as e:
        logger.debug("libusb check failed: %s", e)

    return None


# ── Main API ─────────────────────────────────────────────────────────


def check_driver_status(
    vid: int = CORSAIR_VENDOR_ID,
    pid: Optional[int] = None,
) -> DriverCheckResult:
    """Check WinUSB driver status for a Corsair PSU.

    Queries both Windows Device Manager and libusb to determine the
    full picture: driver binding, device presence, and USB health.

    Returns:
        DriverCheckResult with status, device info, and actionable message.
    """
    if platform.system() != "Windows":
        return DriverCheckResult(
            status=DriverStatus.NOT_WINDOWS,
            message="No driver installation needed on this platform.")

    # Query Windows Device Manager
    dev_info = _query_device_info(vid, pid)

    # Also check libusb visibility
    libusb_info = _check_libusb_visible(vid, pid)

    # Case 1: Not registered in Windows at all
    if dev_info is None:
        if libusb_info:
            return DriverCheckResult(
                status=DriverStatus.NEEDS_INSTALL,
                device_name=SUPPORTED_DEVICES.get(
                    libusb_info["pid"], "Unknown"),
                vid=vid,
                pid=libusb_info["pid"],
                is_present=True,
                needs_install=True,
                message=(
                    f"Corsair PSU found on USB bus {libusb_info['bus']} "
                    f"but no Windows driver installed."))
        return DriverCheckResult(
            status=DriverStatus.NO_DEVICE,
            message="No Corsair PSU found. Is the USB cable connected?")

    # Device is registered in Windows
    model = dev_info["model"]
    service = dev_info["driver_service"]
    is_present = dev_info["is_present"]
    found_pid = dev_info["pid"]

    # Case 2: WinUSB driver is bound
    if service.lower() == "winusb":
        if is_present:
            # Check USB health via bNumConfigurations
            if libusb_info and libusb_info.get("num_configs", 1) == 0:
                return DriverCheckResult(
                    status=DriverStatus.DEVICE_ERROR,
                    device_name=model,
                    vid=vid, pid=found_pid,
                    current_driver="WinUSB",
                    is_present=True,
                    needs_install=False,
                    message=(
                        f"{model}: WinUSB installed, device present, "
                        f"but USB descriptor reports 0 configurations. "
                        f"The dongle may need a physical reconnect "
                        f"(unplug and replug the USB cable)."))

            return DriverCheckResult(
                status=DriverStatus.OK,
                device_name=model,
                vid=vid, pid=found_pid,
                current_driver="WinUSB",
                is_present=True,
                needs_install=False,
                message=f"{model}: WinUSB driver installed and working.")

        # WinUSB bound but device not present
        return DriverCheckResult(
            status=DriverStatus.DEVICE_ERROR,
            device_name=model,
            vid=vid, pid=found_pid,
            current_driver="WinUSB",
            is_present=False,
            needs_install=False,
            message=(
                f"{model}: WinUSB driver installed but device not "
                f"present. Check the USB cable connection."))

    # Case 3: Different driver bound (SiUSBXp, etc.)
    return DriverCheckResult(
        status=DriverStatus.NEEDS_INSTALL,
        device_name=model,
        vid=vid, pid=found_pid,
        current_driver=service or dev_info.get("friendly_name"),
        is_present=is_present,
        needs_install=True,
        message=(
            f"{model} is using driver '{service or 'unknown'}'. "
            f"WinUSB driver is required for direct USB access."))


def install_winusb_driver(
    vid: int = CORSAIR_VENDOR_ID,
    pid: Optional[int] = None,
    force: bool = False,
) -> DriverInstallResult:
    """Install WinUSB driver for a Corsair PSU.

    Uses two Windows built-in mechanisms (no external downloads):

      1. ``pnputil /add-driver`` to add the .inf to the driver store.
      2. ``UpdateDriverForPlugAndPlayDevicesW`` (newdev.dll) to bind the
         driver to the specific hardware ID.
      3. Device disable/enable cycle to force rebind if needed.

    Requires administrator privileges.

    Args:
        vid: USB Vendor ID (default: Corsair 0x1B1C).
        pid: USB Product ID.  If None, auto-detects.
        force: Reinstall even if WinUSB is already present.

    Returns:
        DriverInstallResult with outcome, message, and process output.
    """
    if platform.system() != "Windows":
        return DriverInstallResult(
            InstallResult.NOT_WINDOWS,
            "Driver installation only supported on Windows.")

    # 1. Check current status
    status = check_driver_status(vid, pid)
    if status.status == DriverStatus.OK and not force:
        return DriverInstallResult(
            InstallResult.ALREADY_INSTALLED, status.message)
    if status.status == DriverStatus.NO_DEVICE:
        return DriverInstallResult(
            InstallResult.DEVICE_NOT_FOUND, status.message)

    target_pid = status.pid or pid
    if target_pid is None:
        return DriverInstallResult(
            InstallResult.DEVICE_NOT_FOUND,
            "Could not determine PSU product ID.")

    # 2. Check admin
    if not _is_admin():
        return DriverInstallResult(
            InstallResult.NEEDS_ADMIN,
            "Administrator privileges required.\n"
            "Run: corsair-psu-monitor install-driver --elevate")

    # 3. Find .inf
    inf_path = _find_inf()
    if inf_path is None:
        return DriverInstallResult(
            InstallResult.INF_NOT_FOUND,
            "Driver .inf file not found. Reinstall corsair-psu-monitor or\n"
            "install WinUSB manually with Zadig: https://zadig.akeo.ie/")

    model = SUPPORTED_DEVICES.get(target_pid, f"PID_{target_pid:04X}")
    hardware_id = f"USB\\VID_{vid:04X}&PID_{target_pid:04X}"
    messages = []

    # 4. Add driver to Windows store via pnputil
    pnp_result = _pnputil_add_driver(inf_path)
    messages.append(f"pnputil: {pnp_result}")

    # 5. Force-bind via UpdateDriverForPlugAndPlayDevicesW
    api_result = _setupapi_update_driver(hardware_id, inf_path)
    messages.append(f"SetupAPI: {api_result}")

    # 6. Device disable/enable to rebind
    dev_info = _query_device_info(vid, target_pid)
    if dev_info and dev_info.get("instance_id"):
        rebind_result = _device_rebind(dev_info["instance_id"])
        messages.append(f"Rebind: {rebind_result}")

    # 7. Verify
    verify = check_driver_status(vid, target_pid)
    detail = " | ".join(messages)

    if verify.status == DriverStatus.OK:
        return DriverInstallResult(
            InstallResult.SUCCESS,
            f"WinUSB driver installed successfully for {model}.",
            stdout=detail)

    if verify.current_driver and verify.current_driver.lower() == "winusb":
        return DriverInstallResult(
            InstallResult.SUCCESS,
            f"WinUSB driver bound to {model}. {verify.message}",
            stdout=detail)

    return DriverInstallResult(
        InstallResult.INSTALL_FAILED,
        f"Driver installation attempted but verification shows: "
        f"{verify.message}\n{detail}\n"
        f"Try Zadig as a fallback: https://zadig.akeo.ie/",
        stdout=detail)


# ── Installation methods ─────────────────────────────────────────────


def _pnputil_add_driver(inf_path: Path) -> str:
    """Add driver .inf to Windows driver store via pnputil."""
    cmd = ["pnputil", "/add-driver", str(inf_path), "/install"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60)
        output = (proc.stdout or "").strip()
        if proc.returncode == 0:
            return f"OK ({output[:80]})"
        return f"exit {proc.returncode}: {output[:120]}"
    except Exception as e:
        return f"error: {e}"


def _setupapi_update_driver(hardware_id: str, inf_path: Path) -> str:
    """Use Windows SetupAPI to force-bind a driver to a hardware ID.

    Calls ``UpdateDriverForPlugAndPlayDevicesW`` from ``newdev.dll``
    which is the same API that Zadig/libwdi ultimately uses.
    """
    try:
        import ctypes
        from ctypes import wintypes

        newdev = ctypes.WinDLL("newdev.dll", use_last_error=True)

        # BOOL UpdateDriverForPlugAndPlayDevicesW(
        #   HWND hwndParent,         // NULL
        #   LPCWSTR HardwareId,
        #   LPCWSTR FullInfPath,
        #   DWORD InstallFlags,      // INSTALLFLAG_FORCE = 1
        #   PBOOL bRebootRequired
        # )
        UpdateDriver = newdev.UpdateDriverForPlugAndPlayDevicesW
        UpdateDriver.argtypes = [
            wintypes.HWND,     # hwndParent
            wintypes.LPCWSTR,  # HardwareId
            wintypes.LPCWSTR,  # FullInfPath
            wintypes.DWORD,    # InstallFlags
            ctypes.POINTER(wintypes.BOOL),  # bRebootRequired
        ]
        UpdateDriver.restype = wintypes.BOOL

        INSTALLFLAG_FORCE = 0x00000001
        reboot_required = wintypes.BOOL(False)

        full_inf = str(inf_path.resolve())
        logger.info("UpdateDriverForPlugAndPlayDevices(%s, %s)",
                     hardware_id, full_inf)

        success = UpdateDriver(
            None,                # no parent window
            hardware_id,
            full_inf,
            INSTALLFLAG_FORCE,
            ctypes.byref(reboot_required),
        )

        if success:
            reboot_msg = " (reboot required)" if reboot_required.value else ""
            return f"OK{reboot_msg}"
        else:
            err = ctypes.get_last_error()
            # Common errors:
            # ERROR_NO_SUCH_DEVINST = 0xE000020B (device not present)
            # ERROR_NO_MORE_ITEMS = 259
            return f"failed (Win32 error {err:#010x})"

    except OSError as e:
        return f"newdev.dll not available: {e}"
    except Exception as e:
        return f"error: {e}"


def _device_rebind(instance_id: str) -> str:
    """Disable and re-enable a USB device to force driver rebind."""
    cmd = [
        "powershell.exe", "-NoProfile", "-Command",
        f'Disable-PnpDevice -InstanceId "{instance_id}" '
        f'-Confirm:$false -ErrorAction Stop; '
        f'Start-Sleep 2; '
        f'Enable-PnpDevice -InstanceId "{instance_id}" '
        f'-Confirm:$false -ErrorAction Stop',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return "OK (device recycled)"
        err = (proc.stderr or proc.stdout or "").strip()[:120]
        return f"exit {proc.returncode}: {err}"
    except Exception as e:
        return f"error: {e}"
