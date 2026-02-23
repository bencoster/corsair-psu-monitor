"""Command-line interface for corsair-psu-monitor.

Usage:
    corsair-psu-monitor              # Single reading, formatted table
    corsair-psu-monitor watch        # Continuous monitoring (Ctrl+C to stop)
    corsair-psu-monitor watch -i 5   # Update every 5 seconds
    corsair-psu-monitor json         # Single reading, JSON output
    corsair-psu-monitor json --loop  # Continuous JSON (one object per line)
"""

import argparse
import json
import logging
import sys
import time
from typing import Dict


def _print_table(stats: Dict) -> None:
    """Print a formatted telemetry table."""
    pin = stats.get("input_power", 0)
    pout = stats.get("output_power", 0)
    eff = stats.get("efficiency", 0)

    print(f"{'=' * 52}")
    print(f"  Corsair {stats.get('model', '?')} PSU Telemetry")
    print(f"{'=' * 52}")
    print(f"  Input Power:    {pin:7.1f} W")
    print(f"  Output Power:   {pout:7.1f} W")
    print(f"  Efficiency:     {eff:7.1f} %")
    print(f"  Input Voltage:  {stats.get('input_voltage', 0):7.1f} V")
    print(f"  Input Current:  {stats.get('input_current', 0):7.1f} A")
    print(f"  Temp 1:         {stats.get('temp1', 0):7.1f} C")
    print(f"  Temp 2:         {stats.get('temp2', 0):7.1f} C")
    print(f"  Fan:            {stats.get('fan_rpm', 0):7.0f} RPM")
    print(f"  {'-' * 50}")
    print(f"   12V Rail:  {stats.get('12v_voltage', 0):6.2f} V  "
          f"{stats.get('12v_current', 0):6.2f} A  "
          f"{stats.get('12v_power', 0):6.1f} W")
    print(f"    5V Rail:  {stats.get('5v_voltage', 0):6.2f} V  "
          f"{stats.get('5v_current', 0):6.2f} A  "
          f"{stats.get('5v_power', 0):6.1f} W")
    print(f"  3.3V Rail:  {stats.get('3v3_voltage', 0):6.2f} V  "
          f"{stats.get('3v3_current', 0):6.2f} A  "
          f"{stats.get('3v3_power', 0):6.1f} W")
    print(f"  Rail Sum:   {stats.get('rail_power_sum', 0):6.1f} W")
    print(f"{'=' * 52}")


def _print_watch_line(stats: Dict) -> None:
    """Print a single-line status for watch mode."""
    pin = stats.get("input_power", 0)
    pout = stats.get("output_power", 0)
    eff = stats.get("efficiency", 0)
    v12 = stats.get("12v_voltage", 0)
    i12 = stats.get("12v_current", 0)
    t1 = stats.get("temp1", 0)
    fan = stats.get("fan_rpm", 0)

    ts = time.strftime("%H:%M:%S")
    line = (
        f"[{ts}] "
        f"{pin:5.0f}W in  {pout:5.0f}W out  {eff:4.0f}%  "
        f"12V:{v12:5.2f}V/{i12:5.1f}A  "
        f"{t1:4.1f}C  Fan:{fan:4.0f}rpm"
    )
    print(line, flush=True)


def cmd_read(args: argparse.Namespace) -> None:
    """Single telemetry reading."""
    from .psu import CorsairPSU

    try:
        with CorsairPSU() as psu:
            stats = psu.read_all()
            if "error" in stats:
                print(f"ERROR: {stats['error']}", file=sys.stderr)
                sys.exit(1)
            _print_table(stats)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_watch(args: argparse.Namespace) -> None:
    """Continuous monitoring at specified interval."""
    from .psu import CorsairPSU

    interval = args.interval

    try:
        with CorsairPSU() as psu:
            model = psu.model
            print(f"Monitoring Corsair {model} (Ctrl+C to stop, "
                  f"interval={interval}s)")
            print("-" * 70)
            while True:
                stats = psu.read_all()
                if "error" in stats:
                    print(f"  READ ERROR: {stats['error']}", file=sys.stderr)
                else:
                    _print_watch_line(stats)
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_json(args: argparse.Namespace) -> None:
    """JSON output (single or continuous)."""
    from .psu import CorsairPSU

    try:
        with CorsairPSU() as psu:
            if args.loop:
                interval = args.interval
                try:
                    while True:
                        stats = psu.read_all()
                        print(json.dumps(stats), flush=True)
                        time.sleep(interval)
                except KeyboardInterrupt:
                    pass
            else:
                stats = psu.read_all()
                print(json.dumps(stats, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="corsair-psu-monitor",
        description="Read telemetry from Corsair AXi/HXi power supplies over USB",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging")

    sub = parser.add_subparsers(dest="command")

    # Default command (no subcommand) = single read
    # Watch subcommand
    watch_parser = sub.add_parser("watch", help="Continuous monitoring")
    watch_parser.add_argument(
        "-i", "--interval", type=float, default=2.0,
        help="Update interval in seconds (default: 2)")

    # JSON subcommand
    json_parser = sub.add_parser("json", help="JSON output")
    json_parser.add_argument(
        "--loop", action="store_true",
        help="Continuous output (one JSON object per line)")
    json_parser.add_argument(
        "-i", "--interval", type=float, default=2.0,
        help="Update interval in seconds for --loop (default: 2)")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    if args.command == "watch":
        cmd_watch(args)
    elif args.command == "json":
        cmd_json(args)
    else:
        cmd_read(args)


if __name__ == "__main__":
    main()
