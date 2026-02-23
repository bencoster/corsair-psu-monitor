"""Basic example: read all PSU telemetry once and print it."""

from corsair_psu_monitor import CorsairPSU

with CorsairPSU() as psu:
    stats = psu.read_all()

    print(f"Model:        {stats['model']}")
    print(f"Input Power:  {stats['input_power']:.1f} W")
    print(f"Output Power: {stats['output_power']:.1f} W")
    print(f"Efficiency:   {stats['efficiency']:.1f} %")
    print(f"Voltage:      {stats['input_voltage']:.1f} V")
    print(f"Temperature:  {stats['temp1']:.1f} C")
    print(f"Fan:          {stats['fan_rpm']:.0f} RPM")
    print(f"12V Rail:     {stats['12v_voltage']:.2f} V / {stats['12v_current']:.1f} A")
