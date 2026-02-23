"""FastAPI backend for PSU telemetry dashboard."""

import asyncio
import json
import time
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

import database as db
from transient_detector import TransientDetector, THRESHOLDS

# Global state
collector_thread = None
connected_clients: set[WebSocket] = set()
detector = TransientDetector()
latest_reading: dict = {}
psu_connected = False
USE_MOCK = False


_ema_efficiency = None  # exponential moving average for efficiency smoothing
EMA_ALPHA = 0.25       # lower = smoother (0.25 ≈ ~2s effective window at 0.5s poll)


def collect_loop():
    """Background thread: read PSU every ~0.5s, store, detect transients, broadcast."""
    global latest_reading, psu_connected, USE_MOCK, _ema_efficiency

    # Try real PSU first
    psu = None
    try:
        from corsair_psu_monitor import CorsairPSU
        psu = CorsairPSU()
        psu.open()
        psu_connected = True
        print(f"[PSU] Connected to {psu.model}")
    except Exception as e:
        print(f"[PSU] Could not connect to real PSU: {e}")
        print("[PSU] Running in DEMO mode with simulated data")
        USE_MOCK = True
        psu_connected = False

    while True:
        try:
            if USE_MOCK:
                reading = _mock_reading()
            else:
                reading = psu.read_all()
                reading["timestamp"] = time.time()
                # Smooth efficiency with EMA (register timing skew causes jitter)
                raw_eff = reading.get("efficiency")
                if raw_eff is not None:
                    raw_eff = min(100.0, max(0.0, raw_eff))
                    if _ema_efficiency is None:
                        _ema_efficiency = raw_eff
                    else:
                        _ema_efficiency = EMA_ALPHA * raw_eff + (1 - EMA_ALPHA) * _ema_efficiency
                    reading["efficiency"] = round(_ema_efficiency, 1)

            latest_reading = reading

            # Store in database
            db.insert_reading(reading)

            # Detect transients
            events = detector.analyze(reading)
            for event in events:
                db.insert_transient({
                    "timestamp": event.timestamp,
                    "type": event.type,
                    "metric": event.metric,
                    "value": event.value,
                    "previous_value": event.previous_value,
                    "delta": event.delta,
                    "severity": event.severity,
                    "description": event.description,
                })

            # Broadcast to WebSocket clients
            payload = json.dumps({
                "type": "reading",
                "data": _sanitize(reading),
                "events": [
                    {
                        "timestamp": e.timestamp,
                        "type": e.type,
                        "metric": e.metric,
                        "value": e.value,
                        "delta": e.delta,
                        "severity": e.severity,
                        "description": e.description,
                    }
                    for e in events
                ],
            })
            _broadcast(payload)

        except Exception as e:
            print(f"[PSU] Read error: {e}")
            error_payload = json.dumps({
                "type": "error",
                "message": str(e),
            })
            _broadcast(error_payload)

        time.sleep(0.5)


# Mock data generator for demo/testing
_mock_state = {
    "base_power": 245,
    "tick": 0,
}


def _mock_reading() -> dict:
    import math
    import random
    s = _mock_state
    s["tick"] += 1
    t = s["tick"]

    # Simulate realistic power fluctuations with occasional spikes
    noise = random.gauss(0, 5)
    slow_wave = 30 * math.sin(t * 0.02)  # slow load cycle
    spike = 0
    if random.random() < 0.02:  # 2% chance of a transient spike
        spike = random.choice([150, 200, -100, 250])

    input_power = max(50, s["base_power"] + slow_wave + noise + spike)
    efficiency = 89 + random.gauss(0, 0.5) - max(0, (input_power - 800) * 0.005)
    output_power = input_power * efficiency / 100

    v12_power = output_power * 0.85
    v5_power = output_power * 0.08
    v3_power = output_power * 0.02

    return {
        "timestamp": time.time(),
        "model": "AX1600i",
        "input_power": round(input_power, 1),
        "output_power": round(output_power, 1),
        "efficiency": round(efficiency, 1),
        "input_voltage": round(239.5 + random.gauss(0, 0.3), 1),
        "input_current": round(input_power / 239.5, 2),
        "temp1": round(43.5 + 3 * math.sin(t * 0.005) + random.gauss(0, 0.2), 1),
        "temp2": round(39.8 + 2 * math.sin(t * 0.004) + random.gauss(0, 0.2), 1),
        "fan_rpm": round(max(0, 800 * max(0, (input_power - 400) / 600) + random.gauss(0, 10))),
        "12v_voltage": round(12.05 + random.gauss(0, 0.01), 2),
        "12v_current": round(v12_power / 12.05, 2),
        "12v_power": round(v12_power, 1),
        "5v_voltage": round(4.94 + random.gauss(0, 0.005), 2),
        "5v_current": round(v5_power / 4.94, 2),
        "5v_power": round(v5_power, 1),
        "3v3_voltage": round(3.28 + random.gauss(0, 0.005), 2),
        "3v3_current": round(v3_power / 3.28, 2),
        "3v3_power": round(v3_power, 1),
        "rail_power_sum": round(v12_power + v5_power + v3_power, 1),
    }


def _sanitize(d: dict) -> dict:
    """Ensure all values are JSON-serializable."""
    return {k: (v if v is not None else 0) for k, v in d.items()}


def _broadcast(message: str):
    """Send message to all connected WebSocket clients."""
    disconnected = set()
    for ws in connected_clients:
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(message), loop)
        except Exception:
            disconnected.add(ws)
    connected_clients.difference_update(disconnected)


loop = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector_thread, loop
    loop = asyncio.get_event_loop()
    db.init_db()
    collector_thread = threading.Thread(target=collect_loop, daemon=True)
    collector_thread.start()
    yield


app = FastAPI(title="PSU Telemetry Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)

    # Send current state immediately
    if latest_reading:
        await ws.send_text(json.dumps({
            "type": "reading",
            "data": _sanitize(latest_reading),
            "events": [],
        }))

    try:
        while True:
            # Keep connection alive, handle client messages
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        connected_clients.discard(ws)


@app.get("/api/history")
async def api_history(hours: float = 1, points: int = 500):
    readings = db.get_downsampled_readings(hours=hours, points=points)
    return JSONResponse(readings)


@app.get("/api/transients")
async def api_transients(hours: float = 24, limit: int = 100):
    since = time.time() - (hours * 3600)
    return JSONResponse(db.get_transients(since=since, limit=limit))


@app.get("/api/stats")
async def api_stats(hours: float = 24):
    return JSONResponse(db.get_stats(hours=hours))


@app.get("/api/thresholds")
async def api_thresholds():
    return JSONResponse(THRESHOLDS)


@app.get("/api/status")
async def api_status():
    return JSONResponse({
        "psu_connected": psu_connected,
        "demo_mode": USE_MOCK,
        "uptime": time.time() - (latest_reading.get("timestamp", time.time()) if latest_reading else time.time()),
        "model": latest_reading.get("model", "Unknown"),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8099)
