'use strict';
// ============================================================
//  PSU Telemetry Dashboard — app.js
//  Pan (drag), zoom (wheel), auto-scrolling time window
// ============================================================

const API  = '';
const WS_URL = `ws://${location.host}/ws`;
const PSU_MAX_W = 1600;

// ── state ────────────────────────────────────────────────────
let ws            = null;
let wsRetryTimer  = null;
let reconnectDelay = 2000;
let sessionStart  = Date.now();
let events        = [];
let historyLoaded = false;

// Time window: how many minutes of data to show in the visible window
let windowMinutes = 1;        // default: 1 minute view
let autoScroll    = true;     // true = window follows live data; false = user is panning
const MAX_BUF     = 200000;   // keep up to ~28 hours of data at 0.5s intervals

// Chart data buffers — always growing, never trimmed (except at MAX_BUF)
const powerBuf     = { ts:[], input:[], output:[] };
const voltTempBuf  = { ts:[], v12:[], t1:[], t2:[] };

// ── Chart.js global defaults ─────────────────────────────────
Chart.defaults.color           = '#8b95a8';
Chart.defaults.borderColor     = '#1e2d40';
Chart.defaults.font.family     = "'Inter', sans-serif";
Chart.defaults.font.size       = 11;
Chart.defaults.animation       = { duration: 0 };

// Zoom/pan plugin config shared by both charts
const ZOOM_PAN_OPTS = {
    pan: {
        enabled: true,
        mode: 'x',
        onPanStart: () => { autoScroll = false; },
    },
    zoom: {
        wheel: { enabled: true, modifierKey: null },
        pinch: { enabled: true },
        mode: 'x',
        onZoomStart: () => { autoScroll = false; },
    },
    limits: {
        x: { minRange: 10 * 1000 },  // minimum 10 seconds visible
    }
};

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
        },
        zoom: ZOOM_PAN_OPTS,
    },
    scales: {
        x: {
            type: 'time',
            time: { tooltipFormat: 'HH:mm:ss', displayFormats: { second:'HH:mm:ss', minute:'HH:mm', hour:'HH:mm' } },
            grid: { color: 'rgba(255,255,255,0.04)' },
            ticks: { maxRotation: 0, autoSkipPadding: 20 },
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
            },
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

// ── 12V Rail + Temperature chart ──────────────────────────────
const vtCtx      = document.getElementById('voltTempChart').getContext('2d');
const voltTempChart = new Chart(vtCtx, {
    type: 'line',
    data: {
        datasets: [
            {
                label: '12V Rail (V)',
                data: [],
                borderColor: '#f59e0b',
                backgroundColor: 'rgba(245,158,11,0.08)',
                fill: true,
                tension: 0.3,
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
                yAxisID: 'y12v',
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
                        const u = ctx.datasetIndex === 0 ? 'V' : '°C';
                        const dec = ctx.datasetIndex === 0 ? 3 : 1;
                        return `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(dec)} ${u}`;
                    }
                }
            },
        },
        scales: {
            ...CHART_OPTS.scales,
            y12v: {
                position: 'left',
                title: { display: true, text: '12V Rail', color: '#f59e0b', font: { size: 11 } },
                suggestedMin: 11.8,
                suggestedMax: 12.3,
                grid: { color: 'rgba(245,158,11,0.08)' },
                ticks: { callback: v => `${v.toFixed(2)}V`, color: '#f59e0b' }
            },
            yTemp: {
                position: 'right',
                title: { display: true, text: 'Temperature', color: '#f97316', font: { size: 11 } },
                suggestedMin: 25,
                suggestedMax: 65,
                grid: { drawOnChartArea: false },
                ticks: { callback: v => `${v}°C`, color: '#f97316' }
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
        ws._pingTimer = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type:'ping' }));
        }, 20000);
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
    if (d.fan_rpm != null && d.fan_rpm === 0) {
        setText('kpiFan', 'Silent');
    } else {
        setKPI('kpiFan', d.fan_rpm, 0);
    }

    // Progress bars
    setPct('barInputPower',  d.input_power,  PSU_MAX_W);
    setPct('barOutputPower', d.output_power, PSU_MAX_W);
    setPct('barEfficiency',  d.efficiency,   100);

    // ── Rails ─────────────────────────────────────────────────
    setText('rail12vVoltage', fmt(d['12v_voltage'], 2) + ' V');
    setKPIColor('rail12vVoltage', d['12v_voltage'], 11.8, 12.3);
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
    setTempColor('temp1Val', d.temp1);
    setTempColor('temp2Val', d.temp2);

    // ── Append to buffers (always, regardless of view) ────────
    pushBuf(powerBuf.ts,     ts);
    pushBuf(powerBuf.input,  d.input_power);
    pushBuf(powerBuf.output, d.output_power);

    pushBuf(voltTempBuf.ts,  ts);
    pushBuf(voltTempBuf.v12, d['12v_voltage']);
    pushBuf(voltTempBuf.t1,  d.temp1);
    pushBuf(voltTempBuf.t2,  d.temp2);

    refreshCharts();

    // ── Events ────────────────────────────────────────────────
    if (newEvents.length > 0) {
        newEvents.forEach(addEvent);
        updateEventCount();
    }

    updateUptime();
}

function onError(msg) {
    console.warn('[PSU error]', msg);
}

// ── Chart helpers ─────────────────────────────────────────────
function pushBuf(arr, val) {
    arr.push(val);
    if (arr.length > MAX_BUF) arr.shift();
}

function buildXY(tsArr, valArr) {
    return tsArr.map((t, i) => ({ x: t, y: valArr[i] }));
}

function refreshCharts() {
    // Update data
    powerChart.data.datasets[0].data = buildXY(powerBuf.ts, powerBuf.input);
    powerChart.data.datasets[1].data = buildXY(powerBuf.ts, powerBuf.output);

    voltTempChart.data.datasets[0].data = buildXY(voltTempBuf.ts, voltTempBuf.v12);
    voltTempChart.data.datasets[1].data = buildXY(voltTempBuf.ts, voltTempBuf.t1);
    voltTempChart.data.datasets[2].data = buildXY(voltTempBuf.ts, voltTempBuf.t2);

    // Auto-scroll: move the visible window to follow the latest data
    if (autoScroll) {
        const now = Date.now();
        const windowMs = windowMinutes * 60 * 1000;
        const xMin = new Date(now - windowMs);
        const xMax = new Date(now);

        powerChart.options.scales.x.min = xMin;
        powerChart.options.scales.x.max = xMax;
        voltTempChart.options.scales.x.min = xMin;
        voltTempChart.options.scales.x.max = xMax;
    }
    // If !autoScroll, the user has panned/zoomed — leave the axes alone

    powerChart.update('none');
    voltTempChart.update('none');
}

// ── History load ──────────────────────────────────────────────
async function loadHistory() {
    // Load enough data to fill the buffer for panning back
    const hours = Math.max(1, windowMinutes / 60);
    try {
        const res  = await fetch(`${API}/api/history?hours=${hours}&points=2000`);
        const rows = await res.json();

        // Prepend historical data (oldest first)
        if (rows.length > 0) {
            const histPower = { ts:[], input:[], output:[] };
            const histVT    = { ts:[], v12:[], t1:[], t2:[] };

            rows.forEach(r => {
                const ts = new Date(r.timestamp * 1000);
                histPower.ts.push(ts);
                histPower.input.push(r.input_power);
                histPower.output.push(r.output_power);
                histVT.ts.push(ts);
                histVT.v12.push(r.v12_voltage);
                histVT.t1.push(r.temp1);
                histVT.t2.push(r.temp2);
            });

            // Merge: history first, then any live data already collected
            powerBuf.ts.unshift(...histPower.ts);
            powerBuf.input.unshift(...histPower.input);
            powerBuf.output.unshift(...histPower.output);
            voltTempBuf.ts.unshift(...histVT.ts);
            voltTempBuf.v12.unshift(...histVT.v12);
            voltTempBuf.t1.unshift(...histVT.t1);
            voltTempBuf.t2.unshift(...histVT.t2);
        }

        refreshCharts();
    } catch (e) {
        console.warn('History fetch failed:', e);
    }

    // Load transient history
    try {
        const res2 = await fetch(`${API}/api/transients?hours=${hours}`);
        const ev   = await res2.json();
        ev.slice(-50).reverse().forEach(addEvent);
        updateEventCount();
    } catch (e) {}

    loadStats();
}

// Load more history when user pans into the past
async function loadMoreHistory(olderThanTs) {
    const sinceHours = (Date.now() - olderThanTs) / 3600000 + 1; // 1 hour before oldest
    try {
        const res = await fetch(`${API}/api/history?hours=${sinceHours}&points=2000`);
        const rows = await res.json();
        if (rows.length === 0) return;

        const oldestExisting = powerBuf.ts.length > 0 ? powerBuf.ts[0].getTime() : Infinity;
        const newRows = rows.filter(r => r.timestamp * 1000 < oldestExisting);
        if (newRows.length === 0) return;

        const prepend = { ts:[], input:[], output:[], v12:[], t1:[], t2:[] };
        newRows.forEach(r => {
            const ts = new Date(r.timestamp * 1000);
            prepend.ts.push(ts);
            prepend.input.push(r.input_power);
            prepend.output.push(r.output_power);
            prepend.v12.push(r.v12_voltage);
            prepend.t1.push(r.temp1);
            prepend.t2.push(r.temp2);
        });

        powerBuf.ts.unshift(...prepend.ts);
        powerBuf.input.unshift(...prepend.input);
        powerBuf.output.unshift(...prepend.output);
        voltTempBuf.ts.unshift(...prepend.ts);
        voltTempBuf.v12.unshift(...prepend.v12);
        voltTempBuf.t1.unshift(...prepend.t1);
        voltTempBuf.t2.unshift(...prepend.t2);
    } catch (e) {}
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

        const minutes = parseInt(btn.dataset.minutes, 10);
        windowMinutes = minutes;
        autoScroll = true;  // resume auto-scroll when clicking a time button

        // Reset zoom state on both charts
        powerChart.resetZoom();
        voltTempChart.resetZoom();

        // For longer time ranges, load history from DB
        if (minutes > 5) {
            loadHistoryForRange(minutes);
        }

        refreshCharts();
    });
});

async function loadHistoryForRange(minutes) {
    const hours = minutes / 60;
    try {
        const res = await fetch(`${API}/api/history?hours=${hours}&points=2000`);
        const rows = await res.json();

        // Clear and reload
        [powerBuf.ts, powerBuf.input, powerBuf.output,
         voltTempBuf.ts, voltTempBuf.v12, voltTempBuf.t1, voltTempBuf.t2
        ].forEach(a => a.length = 0);

        rows.forEach(r => {
            const ts = new Date(r.timestamp * 1000);
            powerBuf.ts.push(ts);
            powerBuf.input.push(r.input_power);
            powerBuf.output.push(r.output_power);
            voltTempBuf.ts.push(ts);
            voltTempBuf.v12.push(r.v12_voltage);
            voltTempBuf.t1.push(r.temp1);
            voltTempBuf.t2.push(r.temp2);
        });

        refreshCharts();
    } catch (e) {}
}

// Double-click to resume auto-scroll (reset view to live)
document.getElementById('powerChart').addEventListener('dblclick', () => {
    autoScroll = true;
    powerChart.resetZoom();
    voltTempChart.resetZoom();
    refreshCharts();
});
document.getElementById('voltTempChart').addEventListener('dblclick', () => {
    autoScroll = true;
    powerChart.resetZoom();
    voltTempChart.resetZoom();
    refreshCharts();
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

    while (list.children.length > 100) list.removeChild(list.lastChild);
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

function setKPIColor(id, val, low, high) {
    const el = document.getElementById(id);
    if (!el) return;
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
