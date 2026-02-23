'use strict';
// ============================================================
//  PSU Telemetry Dashboard — app.js
// ============================================================

const API  = '';          // same origin
const WS_URL = `ws://${location.host}/ws`;
const PSU_MAX_W = 1600;

// ── state ────────────────────────────────────────────────────
let ws            = null;
let wsRetryTimer  = null;
let reconnectDelay = 2000;
let sessionStart  = Date.now();
let events        = [];
let activeHours   = 0.083;   // 5-minute default
let historyLoaded = false;

// Chart data buffers (timestamps + values)
const MAX_LIVE_PTS = 600;
const powerBuf     = { ts:[], input:[], output:[] };
const voltTempBuf  = { ts:[], v12:[], vin:[], t1:[], t2:[] };

// ── Chart.js global defaults ─────────────────────────────────
Chart.defaults.color           = '#8b95a8';
Chart.defaults.borderColor     = '#1e2d40';
Chart.defaults.font.family     = "'Inter', sans-serif";
Chart.defaults.font.size       = 11;
Chart.defaults.animation       = { duration: 0 };  // disable for performance

const CHART_OPTS = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
        legend: {
            position: 'top',
            labels: { usePointStyle: true, pointStyle: 'circle', padding: 16, boxWidth: 8 }
        },
        tooltip: {
            backgroundColor: '#1a2332',
            borderColor: '#2a3a4e',
            borderWidth: 1,
            padding: 10,
            callbacks: {}
        }
    },
    scales: {
        x: {
            type: 'time',
            time: { tooltipFormat: 'HH:mm:ss', displayFormats: { second:'HH:mm:ss', minute:'HH:mm', hour:'HH:mm' } },
            grid: { color: 'rgba(255,255,255,0.04)' },
            ticks: { maxRotation: 0, autoSkipPadding: 20 }
        }
    }
};

// ── Power chart ───────────────────────────────────────────────
const powerCtx   = document.getElementById('powerChart').getContext('2d');
const powerChart = new Chart(powerCtx, {
    type: 'line',
    data: {
        datasets: [
            {
                label: 'Input Power (W)',
                data: [],
                borderColor: '#3b82f6',
                backgroundColor: 'rgba(59,130,246,0.08)',
                fill: true,
                tension: 0.3,
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
            },
            {
                label: 'Output Power (W)',
                data: [],
                borderColor: '#10b981',
                backgroundColor: 'rgba(16,185,129,0.06)',
                fill: true,
                tension: 0.3,
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
            },
        ]
    },
    options: {
        ...CHART_OPTS,
        plugins: {
            ...CHART_OPTS.plugins,
            tooltip: {
                ...CHART_OPTS.plugins.tooltip,
                callbacks: {
                    label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} W`
                }
            }
        },
        scales: {
            ...CHART_OPTS.scales,
            y: {
                min: 0,
                grid: { color: 'rgba(255,255,255,0.04)' },
                ticks: { callback: v => `${v}W` }
            }
        }
    }
});

// ── Voltage + Temperature chart ───────────────────────────────
const vtCtx      = document.getElementById('voltTempChart').getContext('2d');
const voltTempChart = new Chart(vtCtx, {
    type: 'line',
    data: {
        datasets: [
            {
                label: '12V Rail (V)',
                data: [],
                borderColor: '#f59e0b',
                backgroundColor: 'transparent',
                tension: 0.3,
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
                yAxisID: 'yVolt',
            },
            {
                label: 'Input Voltage (V)',
                data: [],
                borderColor: '#8b5cf6',
                backgroundColor: 'transparent',
                tension: 0.3,
                borderWidth: 1.5,
                borderDash: [4, 3],
                pointRadius: 0,
                pointHoverRadius: 4,
                yAxisID: 'yVolt',
            },
            {
                label: 'Temp 1 (°C)',
                data: [],
                borderColor: '#f97316',
                backgroundColor: 'rgba(249,115,22,0.06)',
                fill: true,
                tension: 0.3,
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
                yAxisID: 'yTemp',
            },
            {
                label: 'Temp 2 (°C)',
                data: [],
                borderColor: '#ef4444',
                backgroundColor: 'transparent',
                tension: 0.3,
                borderWidth: 1.5,
                borderDash: [3, 3],
                pointRadius: 0,
                pointHoverRadius: 4,
                yAxisID: 'yTemp',
            },
        ]
    },
    options: {
        ...CHART_OPTS,
        plugins: {
            ...CHART_OPTS.plugins,
            tooltip: {
                ...CHART_OPTS.plugins.tooltip,
                callbacks: {
                    label: ctx => {
                        const u = ctx.datasetIndex < 2 ? 'V' : '°C';
                        return `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)} ${u}`;
                    }
                }
            }
        },
        scales: {
            ...CHART_OPTS.scales,
            yVolt: {
                position: 'left',
                grid: { color: 'rgba(255,255,255,0.04)' },
                ticks: { callback: v => `${v}V` }
            },
            yTemp: {
                position: 'right',
                grid: { drawOnChartArea: false },
                ticks: { callback: v => `${v}°C` }
            }
        }
    }
});

// ── WebSocket ─────────────────────────────────────────────────
function connect() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        reconnectDelay = 2000;
        clearTimeout(wsRetryTimer);
        setConnectionBadge('connected');
        // Ping keepalive every 20s
        ws._pingTimer = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type:'ping' }));
        }, 20000);
        // Load history once
        if (!historyLoaded) { loadHistory(); historyLoaded = true; }
    };

    ws.onmessage = e => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'reading') onReading(msg.data, msg.events || []);
        else if (msg.type === 'error') onError(msg.message);
    };

    ws.onclose = () => {
        clearInterval(ws._pingTimer);
        setConnectionBadge('connecting');
        wsRetryTimer = setTimeout(() => {
            reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
            connect();
        }, reconnectDelay);
    };

    ws.onerror = () => ws.close();
}

// ── Incoming reading handler ──────────────────────────────────
function onReading(d, newEvents) {
    const ts = new Date(d.timestamp * 1000);

    // ── KPI Cards ────────────────────────────────────────────
    setKPI('kpiInputPower',   d.input_power,   0);
    setKPI('kpiOutputPower',  d.output_power,  0);
    setKPI('kpiEfficiency',   d.efficiency,    1);
    setKPI('kpiInputVoltage', d.input_voltage, 1);
    setKPI('kpiTemp',         Math.max(d.temp1 || 0, d.temp2 || 0), 1);
    setKPI('kpiFan',          d.fan_rpm,       0);

    // Progress bars
    setPct('barInputPower',  d.input_power,  PSU_MAX_W);
    setPct('barOutputPower', d.output_power, PSU_MAX_W);
    setPct('barEfficiency',  d.efficiency,   100);

    // ── Rails ─────────────────────────────────────────────────
    setText('rail12vVoltage', fmt(d['12v_voltage'], 2) + ' V');
    setKPIColor('rail12vVoltage', d['12v_voltage'], 11.8, 12.3, 'good');
    setText('rail12vCurrent', fmt(d['12v_current'], 2));
    setText('rail12vPower',   fmt(d['12v_power'],   1));
    setPct('bar12v', d['12v_power'], PSU_MAX_W * 0.85);

    setText('rail5vVoltage', fmt(d['5v_voltage'], 2) + ' V');
    setText('rail5vCurrent', fmt(d['5v_current'], 2));
    setText('rail5vPower',   fmt(d['5v_power'],   1));
    setPct('bar5v', d['5v_power'], 100);

    setText('rail3v3Voltage', fmt(d['3v3_voltage'], 2) + ' V');
    setText('rail3v3Current', fmt(d['3v3_current'], 2));
    setText('rail3v3Power',   fmt(d['3v3_power'],   1));
    setPct('bar3v3', d['3v3_power'], 50);

    // ── Input stats ────────────────────────────────────────────
    setText('inputCurrent', fmt(d.input_current, 2));
    setText('temp1Val',     fmt(d.temp1, 1));
    setText('temp2Val',     fmt(d.temp2, 1));
    setText('railSum',      fmt(d.rail_power_sum, 1));

    // Temp colour
    setTempColor('temp1Val', d.temp1);
    setTempColor('temp2Val', d.temp2);

    // ── Live chart buffers ─────────────────────────────────────
    pushBuf(powerBuf.ts,     ts);
    pushBuf(powerBuf.input,  d.input_power);
    pushBuf(powerBuf.output, d.output_power);

    pushBuf(voltTempBuf.ts,  ts);
    pushBuf(voltTempBuf.v12, d['12v_voltage']);
    pushBuf(voltTempBuf.vin, d.input_voltage);
    pushBuf(voltTempBuf.t1,  d.temp1);
    pushBuf(voltTempBuf.t2,  d.temp2);

    refreshCharts();

    // ── Events ────────────────────────────────────────────────
    if (newEvents.length > 0) {
        newEvents.forEach(addEvent);
        updateEventCount();
    }

    // ── Uptime ────────────────────────────────────────────────
    updateUptime();
}

function onError(msg) {
    console.warn('[PSU error]', msg);
}

// ── Chart helpers ─────────────────────────────────────────────
function pushBuf(arr, val) {
    arr.push(val);
    if (arr.length > MAX_LIVE_PTS) arr.shift();
}

function buildXY(tsArr, valArr) {
    return tsArr.map((t, i) => ({ x: t, y: valArr[i] }));
}

function refreshCharts() {
    powerChart.data.datasets[0].data = buildXY(powerBuf.ts, powerBuf.input);
    powerChart.data.datasets[1].data = buildXY(powerBuf.ts, powerBuf.output);
    powerChart.update('none');   // 'none' = no animation

    voltTempChart.data.datasets[0].data = buildXY(voltTempBuf.ts, voltTempBuf.v12);
    voltTempChart.data.datasets[1].data = buildXY(voltTempBuf.ts, voltTempBuf.vin);
    voltTempChart.data.datasets[2].data = buildXY(voltTempBuf.ts, voltTempBuf.t1);
    voltTempChart.data.datasets[3].data = buildXY(voltTempBuf.ts, voltTempBuf.t2);
    voltTempChart.update('none');
}

// ── History load (on connect and time-range change) ───────────
async function loadHistory() {
    try {
        const res  = await fetch(`${API}/api/history?hours=${activeHours}&points=600`);
        const rows = await res.json();

        // Clear live buffers and repopulate from history
        [powerBuf.ts, powerBuf.input, powerBuf.output,
         voltTempBuf.ts, voltTempBuf.v12, voltTempBuf.vin, voltTempBuf.t1, voltTempBuf.t2
        ].forEach(a => a.length = 0);

        rows.forEach(r => {
            const ts = new Date(r.timestamp * 1000);
            powerBuf.ts.push(ts);
            powerBuf.input.push(r.input_power);
            powerBuf.output.push(r.output_power);
            voltTempBuf.ts.push(ts);
            voltTempBuf.v12.push(r.v12_voltage);
            voltTempBuf.vin.push(r.input_voltage);
            voltTempBuf.t1.push(r.temp1);
            voltTempBuf.t2.push(r.temp2);
        });

        refreshCharts();
    } catch (e) {
        console.warn('History fetch failed:', e);
    }

    // Also load transient history
    try {
        const res2 = await fetch(`${API}/api/transients?hours=${activeHours}`);
        const ev   = await res2.json();
        ev.slice(-50).reverse().forEach(addEvent);
        updateEventCount();
    } catch (e) {}

    // Load stats
    loadStats();
}

async function loadStats() {
    try {
        const res  = await fetch(`${API}/api/stats?hours=24`);
        const s    = await res.json();

        setText('statPeakInput',  s.max_input_power   != null ? `${s.max_input_power.toFixed(0)} W`   : '--');
        setText('statPeakOutput', s.max_output_power  != null ? `${s.max_output_power.toFixed(0)} W`  : '--');
        setText('statAvgEff',     s.avg_efficiency     != null ? `${s.avg_efficiency.toFixed(1)} %`    : '--');
        setText('statMaxTemp',    s.max_temp1          != null ? `${s.max_temp1.toFixed(1)} °C`        : '--');
        setText('statVoltRange',  (s.min_input_voltage != null && s.max_input_voltage != null)
            ? `${s.min_input_voltage.toFixed(1)}–${s.max_input_voltage.toFixed(1)} V` : '--');
        setText('statReadings',   s.total_readings     != null ? s.total_readings.toLocaleString()      : '--');
        setText('statTransients', s.transient_count    != null ? s.transient_count.toString()           : '--');
        setText('statMax12vA',    s.max_12v_current    != null ? `${s.max_12v_current.toFixed(1)} A`   : '--');
    } catch (e) {}
}

// ── Time range buttons ────────────────────────────────────────
document.querySelectorAll('.time-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeHours = parseFloat(btn.dataset.hours);
        historyLoaded = false;
        loadHistory();
        historyLoaded = true;
    });
});

// ── Transient events ──────────────────────────────────────────
function addEvent(ev) {
    events.unshift(ev);
    if (events.length > 200) events.pop();

    const list = document.getElementById('eventsList');
    const empty = list.querySelector('.event-empty');
    if (empty) empty.remove();

    const item  = document.createElement('div');
    item.className = 'event-item';

    const t = new Date(ev.timestamp * 1000);
    const timeStr = t.toLocaleTimeString('en-GB', { hour12: false });

    item.innerHTML = `
        <div class="event-severity ${ev.severity}"></div>
        <span class="event-time">${timeStr}</span>
        <span class="event-type ${ev.type}">${ev.type}</span>
        <span class="event-desc">${escHtml(ev.description)}</span>
    `;
    list.insertBefore(item, list.firstChild);

    // Keep list to 100 visible items
    while (list.children.length > 100) list.removeChild(list.lastChild);

    // Flash header border on critical
    if (ev.severity === 'critical') flashAlert();
}

function updateEventCount() {
    setText('eventCount', `${events.length} event${events.length !== 1 ? 's' : ''}`);
}

document.getElementById('clearEvents').addEventListener('click', () => {
    events = [];
    document.getElementById('eventsList').innerHTML = '<div class="event-empty">No transient events detected. Monitoring...</div>';
    updateEventCount();
});

// ── Visual alert flash ────────────────────────────────────────
function flashAlert() {
    const header = document.querySelector('.events-panel .panel-header');
    header.style.transition = 'background 0.15s';
    header.style.background = 'rgba(239,68,68,0.15)';
    setTimeout(() => { header.style.background = ''; }, 800);
}

// ── Connection badge ──────────────────────────────────────────
function setConnectionBadge(state) {
    const el = document.getElementById('connectionBadge');
    el.className = `badge badge-${state}`;
    el.textContent = state === 'connected' ? 'Live' : state === 'connecting' ? 'Connecting...' : 'Disconnected';

    // Check demo mode from server status
    fetch(`${API}/api/status`).then(r => r.json()).then(s => {
        const demo = document.getElementById('demoBadge');
        demo.style.display = s.demo_mode ? 'inline-block' : 'none';
    }).catch(() => {});
}

// ── Uptime clock ──────────────────────────────────────────────
function updateUptime() {
    const diff = Math.floor((Date.now() - sessionStart) / 1000);
    const h = String(Math.floor(diff / 3600)).padStart(2, '0');
    const m = String(Math.floor((diff % 3600) / 60)).padStart(2, '0');
    const s = String(diff % 60).padStart(2, '0');
    setText('uptimeClock', `${h}:${m}:${s}`);
}

// ── DOM helpers ───────────────────────────────────────────────
function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function fmt(v, dec = 1) {
    return (v != null && !isNaN(v)) ? Number(v).toFixed(dec) : '--';
}

function setKPI(id, v, dec) {
    setText(id, fmt(v, dec));
}

function setPct(id, val, max) {
    const el = document.getElementById(id);
    if (el) el.style.width = `${Math.min(100, Math.max(0, (val / max) * 100)).toFixed(1)}%`;
}

function setKPIColor(id, val, low, high, direction) {
    const el = document.getElementById(id);
    if (!el) return;
    // For voltage: ok is between low and high
    if (val < low)        el.style.color = 'var(--accent-red)';
    else if (val > high)  el.style.color = 'var(--accent-orange)';
    else                  el.style.color = '';
}

function setTempColor(id, temp) {
    const el = document.getElementById(id);
    if (!el || temp == null) return;
    if      (temp >= 60) el.style.color = 'var(--accent-red)';
    else if (temp >= 50) el.style.color = 'var(--accent-yellow)';
    else                 el.style.color = '';
}

function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Stats auto-refresh ────────────────────────────────────────
setInterval(loadStats, 60000);

// ── Boot ─────────────────────────────────────────────────────
setConnectionBadge('connecting');
connect();
