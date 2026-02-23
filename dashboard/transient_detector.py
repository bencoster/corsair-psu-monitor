"""Power transient detection engine.

Identifies rapid power changes, voltage droops, overcurrent events,
and patterns that precede PSU OCP/OPP shutdowns.
"""

import time
from collections import deque
from dataclasses import dataclass, field

# Thresholds tuned for AX1600i with 4x RTX 3090 @ 275W
THRESHOLDS = {
    # Power spike detection (watts change per reading interval)
    "power_spike_w": 150,           # >150W jump in one interval = spike
    "power_spike_critical_w": 300,  # >300W jump = critical spike
    "power_sustained_high_w": 1400, # Sustained draw above this = danger zone

    # Voltage sag detection
    "voltage_sag_v": 5,             # >5V drop from nominal = sag
    "voltage_sag_critical_v": 10,   # >10V drop = critical sag
    "voltage_nominal_v": 240,       # Expected input voltage (adjust for region)

    # 12V rail monitoring
    "12v_overcurrent_a": 120,       # 12V rail OCP warning threshold
    "12v_voltage_low_v": 11.8,      # 12V undervoltage
    "12v_voltage_high_v": 12.3,     # 12V overvoltage

    # Temperature alerts
    "temp_warning_c": 50,
    "temp_critical_c": 60,

    # Rate-of-change (per second)
    "power_ramp_rate_w_s": 200,     # >200W/s ramp rate

    # Efficiency
    "efficiency_low_pct": 80,       # Efficiency below this is anomalous

    # PSU capacity (AX1600i rated)
    "psu_max_w": 1600,
    "psu_warning_pct": 85,          # Warn at 85% capacity
}


@dataclass
class TransientEvent:
    timestamp: float
    type: str           # spike, sag, overcurrent, thermal, efficiency, ramp, sustained
    metric: str
    value: float
    previous_value: float
    delta: float
    severity: str       # info, warning, critical
    description: str


class TransientDetector:
    def __init__(self, window_size: int = 60):
        self.history = deque(maxlen=window_size)
        self.events: list[TransientEvent] = []
        self.last_reading: dict = None
        self.last_timestamp: float = 0
        self.sustained_high_start: float = 0
        self.peak_power_1s: float = 0
        self.peak_power_5s: float = 0

    def analyze(self, reading: dict) -> list[TransientEvent]:
        """Analyze a new reading and return any detected transients."""
        now = reading.get("timestamp", time.time())
        events = []

        if self.last_reading is not None:
            dt = now - self.last_timestamp
            if dt > 0:
                events.extend(self._check_power_spikes(reading, dt))
                events.extend(self._check_voltage_sag(reading))
                events.extend(self._check_12v_rail(reading))
                events.extend(self._check_thermal(reading))
                events.extend(self._check_efficiency(reading))
                events.extend(self._check_sustained_high(reading, now))
                events.extend(self._check_capacity(reading))

        self.history.append(reading)
        self.last_reading = reading
        self.last_timestamp = now
        self.events.extend(events)
        return events

    def _check_power_spikes(self, reading, dt) -> list[TransientEvent]:
        events = []
        prev_power = self.last_reading.get("input_power", 0) or 0
        curr_power = reading.get("input_power", 0) or 0
        delta = curr_power - prev_power
        abs_delta = abs(delta)
        rate = abs_delta / dt if dt > 0 else 0

        # Absolute spike detection
        if abs_delta >= THRESHOLDS["power_spike_critical_w"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="spike",
                metric="input_power",
                value=curr_power,
                previous_value=prev_power,
                delta=delta,
                severity="critical",
                description=f"CRITICAL power {'surge' if delta > 0 else 'drop'}: {prev_power:.0f}W -> {curr_power:.0f}W ({delta:+.0f}W)"
            ))
        elif abs_delta >= THRESHOLDS["power_spike_w"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="spike",
                metric="input_power",
                value=curr_power,
                previous_value=prev_power,
                delta=delta,
                severity="warning",
                description=f"Power {'surge' if delta > 0 else 'drop'}: {prev_power:.0f}W -> {curr_power:.0f}W ({delta:+.0f}W)"
            ))

        # Rate-of-change detection
        if rate >= THRESHOLDS["power_ramp_rate_w_s"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="ramp",
                metric="input_power",
                value=curr_power,
                previous_value=prev_power,
                delta=rate,
                severity="warning",
                description=f"High power ramp rate: {rate:.0f} W/s (threshold: {THRESHOLDS['power_ramp_rate_w_s']} W/s)"
            ))

        return events

    def _check_voltage_sag(self, reading) -> list[TransientEvent]:
        events = []
        voltage = reading.get("input_voltage", 0) or 0
        prev_voltage = self.last_reading.get("input_voltage", 0) or 0
        drop = prev_voltage - voltage

        if drop >= THRESHOLDS["voltage_sag_critical_v"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="sag",
                metric="input_voltage",
                value=voltage,
                previous_value=prev_voltage,
                delta=-drop,
                severity="critical",
                description=f"CRITICAL voltage sag: {prev_voltage:.1f}V -> {voltage:.1f}V ({-drop:+.1f}V)"
            ))
        elif drop >= THRESHOLDS["voltage_sag_v"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="sag",
                metric="input_voltage",
                value=voltage,
                previous_value=prev_voltage,
                delta=-drop,
                severity="warning",
                description=f"Voltage sag: {prev_voltage:.1f}V -> {voltage:.1f}V ({-drop:+.1f}V)"
            ))

        return events

    def _check_12v_rail(self, reading) -> list[TransientEvent]:
        events = []
        v = reading.get("12v_voltage", 0) or 0
        a = reading.get("12v_current", 0) or 0

        if a >= THRESHOLDS["12v_overcurrent_a"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="overcurrent",
                metric="12v_current",
                value=a,
                previous_value=self.last_reading.get("12v_current", 0) or 0,
                delta=a - (self.last_reading.get("12v_current", 0) or 0),
                severity="critical",
                description=f"12V rail overcurrent: {a:.1f}A (limit: {THRESHOLDS['12v_overcurrent_a']}A)"
            ))

        if v > 0 and v < THRESHOLDS["12v_voltage_low_v"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="sag",
                metric="12v_voltage",
                value=v,
                previous_value=self.last_reading.get("12v_voltage", 0) or 0,
                delta=v - 12.0,
                severity="warning",
                description=f"12V rail undervoltage: {v:.2f}V (min: {THRESHOLDS['12v_voltage_low_v']}V)"
            ))

        return events

    def _check_thermal(self, reading) -> list[TransientEvent]:
        events = []
        for sensor in ["temp1", "temp2"]:
            temp = reading.get(sensor, 0) or 0
            if temp >= THRESHOLDS["temp_critical_c"]:
                events.append(TransientEvent(
                    timestamp=reading.get("timestamp", time.time()),
                    type="thermal",
                    metric=sensor,
                    value=temp,
                    previous_value=self.last_reading.get(sensor, 0) or 0,
                    delta=temp - (self.last_reading.get(sensor, 0) or 0),
                    severity="critical",
                    description=f"CRITICAL: {sensor} at {temp:.1f}C (limit: {THRESHOLDS['temp_critical_c']}C)"
                ))
            elif temp >= THRESHOLDS["temp_warning_c"]:
                events.append(TransientEvent(
                    timestamp=reading.get("timestamp", time.time()),
                    type="thermal",
                    metric=sensor,
                    value=temp,
                    previous_value=self.last_reading.get(sensor, 0) or 0,
                    delta=temp - (self.last_reading.get(sensor, 0) or 0),
                    severity="warning",
                    description=f"Temperature warning: {sensor} at {temp:.1f}C"
                ))

        return events

    def _check_efficiency(self, reading) -> list[TransientEvent]:
        events = []
        eff = reading.get("efficiency", 0) or 0
        inp = reading.get("input_power", 0) or 0

        # Only check efficiency when there's meaningful load
        if inp > 50 and eff > 0 and eff < THRESHOLDS["efficiency_low_pct"]:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="efficiency",
                metric="efficiency",
                value=eff,
                previous_value=self.last_reading.get("efficiency", 0) or 0,
                delta=eff - (self.last_reading.get("efficiency", 0) or 0),
                severity="warning",
                description=f"Low efficiency: {eff:.1f}% at {inp:.0f}W input"
            ))

        return events

    def _check_sustained_high(self, reading, now) -> list[TransientEvent]:
        events = []
        power = reading.get("input_power", 0) or 0

        if power >= THRESHOLDS["power_sustained_high_w"]:
            if self.sustained_high_start == 0:
                self.sustained_high_start = now
            elif now - self.sustained_high_start >= 5:
                duration = now - self.sustained_high_start
                events.append(TransientEvent(
                    timestamp=now,
                    type="sustained",
                    metric="input_power",
                    value=power,
                    previous_value=THRESHOLDS["power_sustained_high_w"],
                    delta=duration,
                    severity="warning",
                    description=f"Sustained high power: {power:.0f}W for {duration:.0f}s (>{THRESHOLDS['power_sustained_high_w']}W)"
                ))
        else:
            self.sustained_high_start = 0

        return events

    def _check_capacity(self, reading) -> list[TransientEvent]:
        events = []
        power = reading.get("input_power", 0) or 0
        psu_max = THRESHOLDS["psu_max_w"]
        warn_pct = THRESHOLDS["psu_warning_pct"]

        if power >= psu_max:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="overcurrent",
                metric="input_power",
                value=power,
                previous_value=psu_max,
                delta=power - psu_max,
                severity="critical",
                description=f"EXCEEDING PSU CAPACITY: {power:.0f}W / {psu_max}W ({power/psu_max*100:.0f}%)"
            ))
        elif power >= psu_max * warn_pct / 100:
            events.append(TransientEvent(
                timestamp=reading.get("timestamp", time.time()),
                type="sustained",
                metric="input_power",
                value=power,
                previous_value=psu_max,
                delta=power - psu_max,
                severity="warning",
                description=f"High PSU load: {power:.0f}W / {psu_max}W ({power/psu_max*100:.0f}%)"
            ))

        return events

    def get_recent_events(self, count: int = 50) -> list[dict]:
        return [
            {
                "timestamp": e.timestamp,
                "type": e.type,
                "metric": e.metric,
                "value": e.value,
                "previous_value": e.previous_value,
                "delta": e.delta,
                "severity": e.severity,
                "description": e.description,
            }
            for e in self.events[-count:]
        ]
