# Linux Setup

On Linux, no special driver is needed (the kernel's built-in `usbfs` works with
libusb). However, you need udev rules to allow non-root access to the USB device.

## Install udev Rules

```bash
sudo cp 99-corsair-psu.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Add User to plugdev Group

```bash
sudo usermod -aG plugdev $USER
```

Log out and back in for the group change to take effect.

## Verify

```bash
# Check device is detected
lsusb | grep 1b1c

# Check permissions
ls -la /dev/bus/usb/$(lsusb | grep 1b1c | awk '{print $2"/"$4}' | tr -d ':')
```

## Troubleshooting

- **Permission denied**: Make sure you're in the `plugdev` group and have logged out/in
- **Device not found**: Check USB cable connection; try `lsusb -d 1b1c:`
- **libusb not found**: Install system libusb: `sudo apt install libusb-1.0-0-dev` (Debian/Ubuntu) or `sudo dnf install libusb1-devel` (Fedora)
