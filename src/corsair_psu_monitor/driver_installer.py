"""WinUSB driver installer for Corsair PSU devices (Windows only).

Automates detection and installation of the WinUSB driver needed for
pyusb/libusb to communicate with Corsair AXi/HXi PSU dongles.

On Windows, the PSU dongle ships with a Silicon Labs SiUSBXpress driver
that blocks direct USB access.  This module uses wdi-simple.exe (from
the libwdi project, https://github.com/pbatard/libwdi) to replace it
with WinUSB, handling self-signed certificate generation and .cat file
creation automatically.

Usage::

    from corsair_psu_monitor.driver_installer import (
        check_driver_status, install_winusb_driver
    )

    status = check_driver_status()
    if status.needs_install:
        result = install_winusb_driver()
        print(result.message)
"""

import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .protocol import CORSAIR_VENDOR_ID, SUPPORTED_DEVICES

logger = logging.getLogger(__name__)

# wdi-simple.exe release info (libwdi v1.5.1)
_WDI_DOWNLOAD_URL = (
    "https://github.com/pbatard/libwdi/releases/download/"
    "v1.5.1/wdi-simple.exe"
)
_WDI_SHA256 = ""  # Will be populated after first verified download
_WDI_CACHE_DIR_NAME = ".corsair-psu"


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
    WDI_NOT_FOUND = "wdi_not_found"
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


# ── Admin detection ──────────────────────────────────────────────────


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


# ── wdi-simple.exe locator ───────────────────────────────────────────


def _find_wdi_simple() -> Optional[Path]:
    """Locate wdi-simple.exe.

    Search order:
      1. Package drivers directory (development layout)
      2. User cache directory (~/.corsair-psu/)
      3. System PATH
    """
    # 1. Relative to this package (development: ../../drivers/windows/)
    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir / "drivers" / "wdi-simple.exe",
        pkg_dir.parent.parent / "drivers" / "windows" / "wdi-simple.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c

    # 2. User cache
    cache = Path.home() / _WDI_CACHE_DIR_NAME / "wdi-simple.exe"
    if cache.is_file():
        return cache

    # 3. System PATH
    found = shutil.which("wdi-simple")
    if found:
        return Path(found)

    return None


def _get_wdi_cache_path() -> Path:
    """Return the path where wdi-simple.exe should be cached."""
    return Path.home() / _WDI_CACHE_DIR_NAME / "wdi-simple.exe"


def _download_wdi_simple(url: str = _WDI_DOWNLOAD_URL) -> Optional[Path]:
    """Download wdi-simple.exe from GitHub releases.

    Downloads to ~/.corsair-psu/wdi-simple.exe and verifies SHA256
    if a known hash is set.

    Returns the path on success, None on failure.
    """
    import urllib.request

    dest = _get_wdi_cache_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading wdi-simple.exe from %s ...", url)
    try:
        urllib.request.urlretrieve(url, str(dest))
    except Exception as e:
        logger.error("Download failed: %s", e)
        return None

    # Verify SHA256 if we have a known hash
    if _WDI_SHA256:
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        if sha != _WDI_SHA256:
            logger.error("SHA256 mismatch: got %s, expected %s", sha, _WDI_SHA256)
            dest.unlink(missing_ok=True)
            return None
        logger.info("SHA256 verified: %s", sha[:16])

    logger.info("Saved to %s", dest)
    return dest


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
    auto_download: bool = True,
) -> DriverInstallResult:
    """Install WinUSB driver for a Corsair PSU.

    Uses wdi-simple.exe (from libwdi) to prepare and install the driver.
    Falls back to pnputil with the bundled .inf file if wdi-simple is
    not available.

    Requires administrator privileges.  If not elevated, returns
    NEEDS_ADMIN with instructions.

    Args:
        vid: USB Vendor ID (default: Corsair 0x1B1C).
        pid: USB Product ID.  If None, auto-detects.
        force: Reinstall even if WinUSB is already present.
        auto_download: Download wdi-simple.exe if not found.

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

    # 3. Find or download wdi-simple.exe
    wdi_path = _find_wdi_simple()
    if wdi_path is None and auto_download:
        wdi_path = _download_wdi_simple()
    if wdi_path is None:
        return _fallback_pnputil_install(vid, target_pid)

    # 4. Run wdi-simple.exe
    model = SUPPORTED_DEVICES.get(target_pid, f"PID_{target_pid:04X}")
    device_name = f"Corsair {model} PSU"

    with tempfile.TemporaryDirectory(prefix="corsair_psu_drv_") as tmpdir:
        cmd = [
            str(wdi_path),
            "--vid", f"0x{vid:04X}",
            "--pid", f"0x{target_pid:04X}",
            "--type", "0",  # WDI_WINUSB = 0
            "--name", device_name,
            "--dest", tmpdir,
            "--progressbar",
            "--timeout", "120000",
        ]

        logger.info("Running: %s", " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return DriverInstallResult(
                InstallResult.INSTALL_FAILED,
                "Driver installation timed out after 180 seconds.")
        except Exception as e:
            return DriverInstallResult(
                InstallResult.INSTALL_FAILED,
                f"Failed to run wdi-simple.exe: {e}")

        if proc.returncode == 0:
            # 5. Verify
            verify = check_driver_status(vid, target_pid)
            if verify.status == DriverStatus.OK:
                return DriverInstallResult(
                    InstallResult.SUCCESS,
                    f"WinUSB driver installed successfully for {model}.",
                    proc.returncode, proc.stdout, proc.stderr)
            return DriverInstallResult(
                InstallResult.SUCCESS,
                f"wdi-simple reported success. {verify.message} "
                f"You may need to unplug and replug the USB cable.",
                proc.returncode, proc.stdout, proc.stderr)
        else:
            return DriverInstallResult(
                InstallResult.INSTALL_FAILED,
                f"wdi-simple failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()}",
                proc.returncode, proc.stdout, proc.stderr)


def _fallback_pnputil_install(vid: int, pid: int) -> DriverInstallResult:
    """Fallback: install using the bundled .inf file via pnputil.

    This requires the .inf to be properly signed or the system to
    accept unsigned drivers.  Less reliable than wdi-simple but
    doesn't need an external binary.
    """
    # Find the .inf file
    pkg_dir = Path(__file__).resolve().parent
    inf_candidates = [
        pkg_dir.parent.parent / "drivers" / "windows" / "corsair-psu-winusb.inf",
        pkg_dir / "drivers" / "corsair-psu-winusb.inf",
    ]
    inf_path = None
    for c in inf_candidates:
        if c.is_file():
            inf_path = c
            break

    if inf_path is None:
        return DriverInstallResult(
            InstallResult.WDI_NOT_FOUND,
            "Neither wdi-simple.exe nor the .inf driver file was found.\n"
            "Download wdi-simple.exe to ~/.corsair-psu/ or install via Zadig:\n"
            "  https://zadig.akeo.ie/")

    model = SUPPORTED_DEVICES.get(pid, f"PID_{pid:04X}")
    logger.info("Fallback: installing via pnputil with %s", inf_path)

    cmd = ["pnputil", "/add-driver", str(inf_path), "/install"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        return DriverInstallResult(
            InstallResult.INSTALL_FAILED,
            f"pnputil failed: {e}")

    if proc.returncode == 0:
        return DriverInstallResult(
            InstallResult.SUCCESS,
            f"Driver package added via pnputil for {model}. "
            f"You may need to unplug and replug the USB cable.",
            proc.returncode, proc.stdout, proc.stderr)
    else:
        return DriverInstallResult(
            InstallResult.INSTALL_FAILED,
            f"pnputil failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}\n"
            f"Try installing manually with Zadig: https://zadig.akeo.ie/",
            proc.returncode, proc.stdout, proc.stderr)
