"""SQLite database for PSU telemetry storage and transient detection."""

import sqlite3
import time
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "psu_telemetry.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            input_power REAL,
            output_power REAL,
            efficiency REAL,
            input_voltage REAL,
            input_current REAL,
            temp1 REAL,
            temp2 REAL,
            fan_rpm REAL,
            v12_voltage REAL,
            v12_current REAL,
            v12_power REAL,
            v5_voltage REAL,
            v5_current REAL,
            v5_power REAL,
            v3_voltage REAL,
            v3_current REAL,
            v3_power REAL,
            rail_power_sum REAL
        );

        CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp);

        CREATE TABLE IF NOT EXISTS transients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            type TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL,
            previous_value REAL,
            delta REAL,
            severity TEXT NOT NULL DEFAULT 'warning',
            description TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_transients_timestamp ON transients(timestamp);
        CREATE INDEX IF NOT EXISTS idx_transients_severity ON transients(severity);

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time REAL NOT NULL,
            end_time REAL,
            peak_input_power REAL DEFAULT 0,
            peak_output_power REAL DEFAULT 0,
            avg_efficiency REAL DEFAULT 0,
            total_readings INTEGER DEFAULT 0,
            transient_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


def insert_reading(data: dict):
    conn = get_connection()
    conn.execute("""
        INSERT INTO readings (
            timestamp, input_power, output_power, efficiency,
            input_voltage, input_current, temp1, temp2, fan_rpm,
            v12_voltage, v12_current, v12_power,
            v5_voltage, v5_current, v5_power,
            v3_voltage, v3_current, v3_power, rail_power_sum
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("timestamp", time.time()),
        data.get("input_power"),
        data.get("output_power"),
        data.get("efficiency"),
        data.get("input_voltage"),
        data.get("input_current"),
        data.get("temp1"),
        data.get("temp2"),
        data.get("fan_rpm"),
        data.get("12v_voltage"),
        data.get("12v_current"),
        data.get("12v_power"),
        data.get("5v_voltage"),
        data.get("5v_current"),
        data.get("5v_power"),
        data.get("3v3_voltage"),
        data.get("3v3_current"),
        data.get("3v3_power"),
        data.get("rail_power_sum"),
    ))
    conn.commit()
    conn.close()


def insert_transient(data: dict):
    conn = get_connection()
    conn.execute("""
        INSERT INTO transients (timestamp, type, metric, value, previous_value, delta, severity, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["timestamp"],
        data["type"],
        data["metric"],
        data.get("value"),
        data.get("previous_value"),
        data.get("delta"),
        data.get("severity", "warning"),
        data.get("description"),
    ))
    conn.commit()
    conn.close()


def get_readings(since: float = None, limit: int = 1000):
    conn = get_connection()
    if since:
        rows = conn.execute(
            "SELECT * FROM readings WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (since, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM readings ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_transients(since: float = None, limit: int = 100):
    conn = get_connection()
    if since:
        rows = conn.execute(
            "SELECT * FROM transients WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (since, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM transients ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats(hours: float = 24):
    conn = get_connection()
    since = time.time() - (hours * 3600)
    row = conn.execute("""
        SELECT
            COUNT(*) as total_readings,
            MIN(input_power) as min_input_power,
            MAX(input_power) as max_input_power,
            AVG(input_power) as avg_input_power,
            MIN(output_power) as min_output_power,
            MAX(output_power) as max_output_power,
            AVG(output_power) as avg_output_power,
            AVG(efficiency) as avg_efficiency,
            MAX(temp1) as max_temp1,
            MAX(temp2) as max_temp2,
            MAX(v12_current) as max_12v_current,
            MIN(input_voltage) as min_input_voltage,
            MAX(input_voltage) as max_input_voltage
        FROM readings WHERE timestamp > ?
    """, (since,)).fetchone()

    transient_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM transients WHERE timestamp > ?",
        (since,)
    ).fetchone()["cnt"]

    conn.close()
    result = dict(row) if row else {}
    result["transient_count"] = transient_count
    result["hours"] = hours
    return result


def get_downsampled_readings(hours: float = 24, points: int = 500):
    """Get readings downsampled to ~points data points for efficient charting."""
    conn = get_connection()
    since = time.time() - (hours * 3600)

    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM readings WHERE timestamp > ?",
        (since,)
    ).fetchone()["cnt"]

    if total <= points:
        rows = conn.execute(
            "SELECT * FROM readings WHERE timestamp > ? ORDER BY timestamp ASC",
            (since,)
        ).fetchall()
    else:
        # Use NTILE to bucket rows and take the average per bucket
        nth = max(1, total // points)
        rows = conn.execute("""
            SELECT * FROM readings WHERE timestamp > ?
            AND (id % ?) = 0
            ORDER BY timestamp ASC
        """, (since, nth)).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def cleanup_old_data(days: int = 30):
    """Remove readings older than N days to keep DB small."""
    conn = get_connection()
    cutoff = time.time() - (days * 86400)
    conn.execute("DELETE FROM readings WHERE timestamp < ?", (cutoff,))
    conn.execute("DELETE FROM transients WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()
