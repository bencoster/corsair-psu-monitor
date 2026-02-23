"""Continuous monitoring example with formatted console output.

Reads PSU telemetry every 2 seconds and prints a summary line.
Press Ctrl+C to stop.
"""

import time
from corsair_psu_monitor import CorsairPSU

INTERVAL = 2.0  # seconds between readings

with CorsairPSU() as psu:
    print(f"Monitoring Corsair {psu.model} (Ctrl+C to stop)")
    print("-" * 70)

    try:
        while True:
            stats = psu.read_all()
            if "error" in stats:
                print(f"  ERROR: {stats['error']}")
            else:
                ts = time.strftime("%H:%M:%S")
                pin = stats["input_power"]
                pout = stats["output_power"]
                eff = stats["efficiency"]
                v12 = stats["12v_voltage"]
                i12 = stats["12v_current"]
                t1 = stats["temp1"]
                fan = stats["fan_rpm"]

                print(
                    f"[{ts}] "
                    f"{pin:5.0f}W in  {pout:5.0f}W out  {eff:4.0f}%  "
                    f"12V:{v12:5.2f}V/{i12:5.1f}A  "
                    f"{t1:4.1f}C  Fan:{fan:4.0f}rpm",
                    flush=True,
                )
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\nStopped.")
