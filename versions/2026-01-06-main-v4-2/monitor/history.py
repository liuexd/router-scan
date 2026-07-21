import atexit
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import config
from .fetcher import load_state
from .utils import parse_rssi_value, ts


TARGET_HISTORY_LOCK = threading.Lock()
TARGET_HISTORY: List[Dict[str, Any]] = []
PERSISTED_HISTORY: List[Dict[str, Any]] = []
HISTORY_SERVER: Optional[ThreadingHTTPServer] = None
HISTORY_SERVER_THREAD: Optional[threading.Thread] = None
LAST_SAVE_TS: float = 0.0
UNSAVED_CHANGES = False


def load_history() -> List[Dict[str, Any]]:
    if not config.TARGET_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(config.TARGET_HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for entry in data:
                if "rssi_value" not in entry:
                    entry["rssi_value"] = parse_rssi_value(entry.get("rssi", ""))
                if "arping_ok" not in entry:
                    entry["arping_ok"] = None
            return data
    except Exception as exc:
        print(f"[{ts()}] 加载历史记录失败：{exc}")
    return []


def _parse_timestamp(value: str) -> Optional["datetime"]:
    from datetime import datetime
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _recent_cutoff() -> Optional["datetime"]:
    from datetime import datetime, timedelta
    try:
        days = int(getattr(config, "HISTORY_RECENT_DAYS", 1))
    except Exception:
        days = 1
    if days <= 0:
        return None
    return datetime.now() - timedelta(days=days)


def prune_history(history: List[Dict[str, Any]]) -> None:
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=config.TARGET_HISTORY_RETENTION_DAYS)
    history[:] = [entry for entry in history if (entry_ts := _parse_timestamp(entry.get("timestamp", ""))) and entry_ts >= cutoff]


def prune_recent_history(history: List[Dict[str, Any]]) -> None:
    cutoff = _recent_cutoff()
    if cutoff is None:
        return
    history[:] = [entry for entry in history if (entry_ts := _parse_timestamp(entry.get("timestamp", ""))) and entry_ts >= cutoff]


def save_history(history: List[Dict[str, Any]]) -> None:
    try:
        config.TARGET_HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[{ts()}] 写入历史记录失败：{exc}")


def _save_history_locked(force: bool = False) -> None:
    global LAST_SAVE_TS, UNSAVED_CHANGES
    interval = getattr(config, "HISTORY_SAVE_INTERVAL_SECONDS", 0)
    now = time.monotonic()
    if not force:
        if not UNSAVED_CHANGES:
            return
        if interval > 0 and now - LAST_SAVE_TS < interval:
            return
    save_history(PERSISTED_HISTORY)
    LAST_SAVE_TS = now
    UNSAVED_CHANGES = False


def record_history(
    label: str,
    online: bool,
    device: Optional[Dict[str, str]],
    *,
    arping_ok: Optional[bool] = None,
) -> None:
    global UNSAVED_CHANGES
    rssi_value = parse_rssi_value((device or {}).get("rssi", ""))
    entry = {
        "timestamp": ts(),
        "label": label,
        "online": online,
        "ip": (device or {}).get("ip", ""),
        "mac": (device or {}).get("mac", ""),
        "rssi": (device or {}).get("rssi", ""),
        "rssi_value": rssi_value,
        "arping_ok": arping_ok,
    }
    with TARGET_HISTORY_LOCK:
        TARGET_HISTORY.append(entry)
        prune_recent_history(TARGET_HISTORY)
        PERSISTED_HISTORY.append(entry)
        prune_history(PERSISTED_HISTORY)
        UNSAVED_CHANGES = True
        _save_history_locked()


def snapshot(limit: int = config.HISTORY_MAX_SNAPSHOT) -> List[Dict[str, Any]]:
    with TARGET_HISTORY_LOCK:
        data = list(TARGET_HISTORY)
    if limit and limit > 0:
        return data[-limit:]
    return data


def filter_history_by_date(date_str: str) -> List[Dict[str, Any]]:
    from datetime import datetime, timedelta
    try:
        start = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return []
    end = start + timedelta(days=1)
    with TARGET_HISTORY_LOCK:
        source = PERSISTED_HISTORY if PERSISTED_HISTORY else TARGET_HISTORY
        return [entry for entry in source if (entry_ts := _parse_timestamp(entry.get("timestamp", ""))) and start <= entry_ts < end]


class _HistoryRequestHandler(BaseHTTPRequestHandler):
    def _set_headers(self, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()

    def _json_response(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._set_headers(status=status, content_type="application/json; charset=utf-8")
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path.startswith("/data"):
            date_param = params.get("date", [None])[0]
            entries = filter_history_by_date(date_param) if date_param else snapshot()
            self._json_response({"timestamp": ts(), "entries": entries, "threshold": config.RSSI_THRESHOLD, "default_threshold": config.DEFAULT_RSSI_THRESHOLD, "settings": config.get_monitor_settings()})
            return
        if path.startswith("/settings"):
            self._json_response({"settings": config.get_monitor_settings()})
            return
        if path.startswith("/threshold"):
            self._json_response({"threshold": config.RSSI_THRESHOLD, "default_threshold": config.DEFAULT_RSSI_THRESHOLD})
            return
        if path.startswith("/devices"):
            devices = []
            state = load_state()
            for mac, info in state.items():
                devices.append(
                    {
                        "name": info.get("name", ""),
                        "ip": info.get("ip", ""),
                        "mac": mac,
                        "online": bool(info.get("online")),
                    }
                )
            devices.sort(key=lambda item: (not item["online"], item["name"] or item["ip"] or item["mac"]))
            self._json_response({"devices": devices})
            return
        if path.startswith("/table"):
            self._set_headers(); self.wfile.write(self._render_table_page().encode("utf-8")); return
        if path.startswith("/history"):
            self._set_headers(); self.wfile.write(self._render_history_page().encode("utf-8")); return
        self._set_headers(); self.wfile.write(self._render_monitor_page().encode("utf-8"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/threshold"):
            payload = self._read_json()
            value = payload.get("threshold")
            if value is None:
                self._json_response({"error": "missing threshold"}, status=400)
                return
            try:
                new_val = config.set_rssi_threshold(int(value))
            except Exception as exc:
                self._json_response({"error": str(exc)}, status=400)
                return
            self._json_response({"threshold": new_val, "default_threshold": config.DEFAULT_RSSI_THRESHOLD})
            return
        if path.startswith("/settings"):
            payload = self._read_json()
            settings = config.set_monitor_settings(
                status_notify_enabled=payload.get("status_notify_enabled"),
                exit_notify_enabled=payload.get("exit_notify_enabled"),
                push_notify_enabled=payload.get("push_notify_enabled"),
                beep_on_event=payload.get("beep_on_event"),
                chart_max_points=None,
                chart_y_range_db=payload.get("chart_y_range_db"),
                chart_y_min_db=payload.get("chart_y_min_db"),
                arping_warn_threshold=payload.get("arping_warn_threshold"),
                arping_emergency_threshold=payload.get("arping_emergency_threshold"),
                router_base_url=payload.get("router_base_url"),
                router_fetch_path=payload.get("router_fetch_path"),
                arping_before_fetch=payload.get("arping_before_fetch"),
                arping_mode=payload.get("arping_mode"),
                arping_manual_ip=payload.get("arping_manual_ip"),
                target_mac=payload.get("target_mac"),
                target_name_contains=payload.get("target_name_contains"),
            )
            self._json_response({"settings": settings})
            return
        self._json_response({"error": "unsupported"}, status=404)

    def _render_table_page(self) -> str:
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>事件表格</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #6b7280;
      --primary: #2563eb;
      --shadow: 0 18px 48px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 22px;
      font-family: "Fira Sans", "Segoe UI", "Microsoft YaHei", sans-serif;
      background: radial-gradient(circle at 15% 20%, rgba(37,99,235,0.08), transparent 25%),
                  radial-gradient(circle at 80% 0%, rgba(79,70,229,0.05), transparent 20%),
                  var(--bg);
      color: var(--text);
    }
    .page { max-width: 1220px; margin: 0 auto; }
    .header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
    h1 { margin: 0; font-size: 26px; }
    .muted { color: var(--muted); font-size: 14px; }
    .link {
      color: var(--primary);
      text-decoration: none;
      font-weight: 600;
    }
    .card {
      background: var(--card);
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: var(--shadow);
      border: 1px solid #e5e7eb;
    }
    table { border-collapse: collapse; width: 100%; font-size: 14px; }
    th, td { border: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; }
    th { background: #f8fafc; }
    tr.online { background: #ecfdf3; }
    tr.offline { background: #fff1f2; }
    .table-wrap { overflow-x: auto; }
  </style>
  <script>
    function formatArpingStatus(value) {
      if (value === true) return 'up';
      if (value === false) return 'down';
      return 'unknown';
    }

    async function fetchData() {
      try {
        const resp = await fetch('/data?_=' + Date.now());
        const data = await resp.json();
        document.getElementById('lastUpdated').textContent = '最后更新：' + data.timestamp;
        const tbody = document.getElementById('historyBody');
        tbody.innerHTML = '';
        (data.entries || []).slice().reverse().forEach(entry => {
          const arpingStatus = formatArpingStatus(entry.arping_ok);
          const tr = document.createElement('tr');
          tr.className = entry.online ? 'online' : 'offline';
          tr.innerHTML = '<td>' + entry.timestamp + '</td>' +
                         '<td>' + (entry.label || '') + '</td>' +
                         '<td>' + (entry.online ? '在线' : '离线') + '</td>' +
                         '<td>' + (entry.ip || '') + '</td>' +
                         '<td>' + (entry.mac || '') + '</td>' +
                         '<td>' + (entry.rssi || '') + '</td>' +
                         '<td>' + arpingStatus + '</td>';
          tbody.appendChild(tr);
        });
      } catch (err) {
        console.error('加载表格失败', err);
      }
    }
    window.onload = () => {
      fetchData();
      setInterval(fetchData, 5000);
    };
  </script>
</head>
<body>
  <div class="page">
    <div class="header">
      <div>
        <h1>事件表格</h1>
        <div class="muted" id="lastUpdated">正在加载...</div>
      </div>
      <div style="display:flex; gap:12px; flex-wrap:wrap;">
        <a class="link" href="/">返回监控 →</a>
        <a class="link" href="/history">历史天视图 →</a>
      </div>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>标签</th>
              <th>状态</th>
              <th>IP</th>
              <th>MAC</th>
              <th>RSSI</th>
              <th>ARPing</th>
            </tr>
          </thead>
          <tbody id="historyBody"></tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>"""

    def _render_monitor_page(self) -> str:
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>监控概览</title>
  <style>
    :root {
      --bg: #e8ecff;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #64748b;
      --primary: #2563eb;
      --accent: #fb7185;
      --success: #16a34a;
      --danger: #ef4444;
      --shadow: 0 22px 60px rgba(15, 23, 42, 0.08);
      --chart-bg: linear-gradient(180deg, rgba(37, 99, 235, 0.06), rgba(37, 99, 235, 0.01));
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 22px;
      font-family: "Fira Sans", "Segoe UI", "Microsoft YaHei", sans-serif;
      background: radial-gradient(circle at 12% 18%, rgba(37, 99, 235, 0.12), transparent 28%),
                  radial-gradient(circle at 82% 8%, rgba(251, 113, 133, 0.15), transparent 25%),
                  linear-gradient(120deg, #f8fafc 0%, #eef2ff 60%, #ffffff 100%);
      color: var(--text);
    }
    .page { max-width: 1280px; margin: 0 auto; }
    .eyebrow { letter-spacing: 0.12em; text-transform: uppercase; font-size: 12px; color: var(--muted); margin: 0 0 8px 0; }
    h1 { margin: 0; font-size: 28px; font-weight: 800; }
    p { margin: 0; }
    .header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 16px; }
    .card { background: var(--card); border: 1px solid #e5e7eb; border-radius: 16px; padding: 16px 18px; box-shadow: var(--shadow); }
    .card + .card { margin-top: 16px; }
    .card-title { font-weight: 700; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
    .card-title::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: linear-gradient(135deg, var(--primary), var(--accent)); }
    .threshold-card { min-width: 320px; }
    .threshold-row { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; justify-content: space-between; }
    .value { font-size: 28px; font-weight: 700; color: var(--primary); }
    .control label { font-size: 13px; color: var(--muted); }
    .control-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
    input[type="number"] { padding: 8px 10px; border-radius: 10px; border: 1px solid #d1d5db; width: 110px; font-size: 14px; }
    button { padding: 9px 14px; border-radius: 12px; border: none; background: linear-gradient(135deg, var(--primary), #1d4ed8); color: #fff; font-weight: 700; cursor: pointer; box-shadow: 0 10px 30px rgba(37, 99, 235, 0.3); transition: transform 0.08s ease, box-shadow 0.12s ease; }
    button:hover { transform: translateY(-1px); box-shadow: 0 16px 40px rgba(37, 99, 235, 0.32); }
    button:active { transform: translateY(0); }
    .hint { font-size: 12px; color: var(--muted); margin-top: 4px; }
    #chart { width: 100%; height: 420px; display: block; background: var(--chart-bg); border-radius: 14px; border: 1px solid #e5e7eb; }
    .chart-wrap { width: 100%; height: 420px; overflow: auto; padding-bottom: 8px; position: relative; cursor: grab; }
    .chart-wrap.dragging { cursor: grabbing; }
    .legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 10px; color: var(--muted); font-size: 13px; }
    .legend span { display: inline-flex; align-items: center; gap: 6px; }
    .legend .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
    .range-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
    .range-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
    .range-btn { border: 1px solid #d1d5db; background: #fff; color: #0f172a; border-radius: 999px; padding: 8px 14px; cursor: pointer; font-weight: 700; transition: all 0.12s ease; }
    .range-btn.active { background: #2563eb; color: #fff; border-color: #1d4ed8; box-shadow: 0 10px 28px rgba(37,99,235,0.3); }
    .link { color: #2563eb; text-decoration: none; font-weight: 700; }
    .last-updated { color: var(--muted); font-size: 14px; margin-top: 6px; }
    .switch { display: inline-flex; align-items: center; gap: 8px; padding: 8px 10px; background: #f8fafc; border-radius: 12px; border: 1px solid #e5e7eb; font-weight: 600; cursor: pointer; user-select: none; min-width: 240px; }
    .switch input { display: none; }
    .switch span.knob { width: 36px; height: 20px; border-radius: 999px; background: #d1d5db; position: relative; transition: all 0.15s ease; }
    .switch span.knob::after { content: ""; position: absolute; width: 16px; height: 16px; border-radius: 50%; background: #fff; top: 2px; left: 2px; transition: all 0.15s ease; box-shadow: 0 2px 6px rgba(0,0,0,0.2); }
    .switch input:checked + span.knob { background: linear-gradient(135deg, var(--primary), var(--accent)); }
    .switch input:checked + span.knob::after { transform: translateX(16px); }
    .switches { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; }
    .dashboard-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 16px; }
    .field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
    .field { display: flex; flex-direction: column; gap: 6px; }
    .field label { font-size: 13px; color: var(--muted); }
    select { padding: 8px 10px; border-radius: 10px; border: 1px solid #d1d5db; font-size: 14px; width: 100%; min-width: 0; }
    .select-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; }
    @media (max-width: 720px) { .select-row { grid-template-columns: 1fr; } }
    .zoom-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
    input[type="range"] { accent-color: var(--primary); }
    .tooltip {
      position: absolute;
      background: rgba(15, 23, 42, 0.85);
      color: #fff;
      padding: 6px 8px;
      border-radius: 8px;
      font-size: 12px;
      pointer-events: none;
      transform: translate(-50%, -110%);
      white-space: nowrap;
      box-shadow: 0 10px 30px rgba(0,0,0,0.25);
      z-index: 5;
      display: none;
    }
    @media (max-width: 720px) { body { padding: 14px; } .threshold-row { align-items: flex-start; } input[type="number"] { width: 90px; } .card { padding: 14px; } #chart { height: 320px; } .chart-wrap { height: 320px; } }
  </style>
  <script>
    const state = { entries: [], threshold: 0, defaultThreshold: 0, range: '10m', settings: {}, yRange: 90, yBase: 0, zoom: 1, points: [], stableSegments: [], baseWidth: 0, baseHeight: 0, scale: null, zoomSession: null };
    let currentTargetMac = '';
    const palette = ["#0ea5e9", "#fb7185", "#22c55e", "#8b5cf6", "#ef4444", "#f59e0b", "#06b6d4"];
    const ranges = { '1d': 24 * 3600 * 1000, '4h': 4 * 3600 * 1000, '1h': 1 * 3600 * 1000, '10m': 10 * 60 * 1000 };
    const MAX_ZOOM = 8;
    const STALE_MINUTES = 1;
    const STALE_VALUE_EPS = 0;
    const ARPING_FAIL_COLOR = '#7c3aed';
    const ZOOM_ANCHOR_TTL = 2000;
    const ZOOM_ANCHOR_MOVE_PX = 10;

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function formatArpingStatus(value) {
      if (value === true) return 'arping: up';
      if (value === false) return 'arping: down';
      return 'arping: n/a';
    }

    function ensureBaseSize(canvas) {
      if (!state.baseWidth || !state.baseHeight) {
        const rect = canvas.getBoundingClientRect();
        state.baseWidth = rect.width || canvas.clientWidth || canvas.width;
        state.baseHeight = rect.height || canvas.clientHeight || canvas.height;
      }
    }

    function getAnchorFromPoint(x, y) {
      const scale = state.scale;
      if (!scale) return null;
      const rx = (x - scale.margin.left) / (scale.width || 1);
      const ry = (y - scale.margin.top) / (scale.height || 1);
      const t = clamp(rx, 0, 1);
      const v = clamp(ry, 0, 1);
      const ts = scale.minTs + t * (scale.maxTs - scale.minTs || 1);
      const value = scale.yMax - v * (scale.yMax - scale.yMin || 1);
      return { ts, value };
    }

    function getPointFromAnchor(anchor) {
      const scale = state.scale;
      if (!scale || !anchor) return null;
      const tsSpan = scale.maxTs - scale.minTs || 1;
      const valSpan = scale.yMax - scale.yMin || 1;
      const x = scale.margin.left + (scale.width * (anchor.ts - scale.minTs)) / tsSpan;
      const y = scale.margin.top + scale.height - (scale.height * (anchor.value - scale.yMin)) / valSpan;
      return { x, y };
    }

    function findStableSegments(points, minDurationMs, valueEps) {
      const segments = [];
      if (!points || points.length < 2) return segments;
      let startIdx = null;
      for (let i = 0; i < points.length; i++) {
        const curr = points[i];
        if (!curr.online) {
          if (startIdx !== null) {
            const end = points[i - 1];
            const start = points[startIdx];
            const durationMs = end.ts - start.ts;
            if (durationMs >= minDurationMs) {
              segments.push({ startTs: start.ts, endTs: end.ts, value: start.value, durationMs });
            }
            startIdx = null;
          }
          continue;
        }
        if (startIdx === null) {
          startIdx = i;
          continue;
        }
        const prev = points[i - 1];
        if (!prev.online) {
          startIdx = i;
          continue;
        }
        if (Math.abs(curr.value - prev.value) <= valueEps) {
          continue;
        }
        const start = points[startIdx];
        const end = points[i - 1];
        const durationMs = end.ts - start.ts;
        if (durationMs >= minDurationMs) {
          segments.push({ startTs: start.ts, endTs: end.ts, value: start.value, durationMs });
        }
        startIdx = i;
      }
      if (startIdx !== null) {
        const start = points[startIdx];
        const end = points[points.length - 1];
        if (end.online) {
          const durationMs = end.ts - start.ts;
          if (durationMs >= minDurationMs) {
            segments.push({ startTs: start.ts, endTs: end.ts, value: start.value, durationMs });
          }
        }
      }
      return segments;
    }

    function updateZoomSession(origin, cursor, wrapper) {
      const session = state.zoomSession || {};
      const moved = session.lastCursorX !== undefined && (Math.abs(cursor.x - session.lastCursorX) > ZOOM_ANCHOR_MOVE_PX || Math.abs(cursor.y - session.lastCursorY) > ZOOM_ANCHOR_MOVE_PX);
      if (!session.anchor || moved) {
        session.anchor = getAnchorFromPoint(origin.x, origin.y);
        session.mouseX = cursor.x;
        session.mouseY = cursor.y;
      }
      session.lastCursorX = cursor.x;
      session.lastCursorY = cursor.y;
      if (session.timer) {
        clearTimeout(session.timer);
      }
      session.timer = setTimeout(() => { state.zoomSession = null; }, ZOOM_ANCHOR_TTL);
      state.zoomSession = session;
      return session;
    }

    function enablePanning(wrapper, tooltip) {
      let dragging = false;
      let startX = 0;
      let startY = 0;
      let startScrollLeft = 0;
      let startScrollTop = 0;
      wrapper.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        dragging = true;
        state.zoomSession = null;
        startX = e.clientX;
        startY = e.clientY;
        startScrollLeft = wrapper.scrollLeft;
        startScrollTop = wrapper.scrollTop;
        wrapper.classList.add('dragging');
        if (tooltip) tooltip.style.display = 'none';
      });
      window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        wrapper.scrollLeft = startScrollLeft - dx;
        wrapper.scrollTop = startScrollTop - dy;
      });
      window.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        wrapper.classList.remove('dragging');
      });
      wrapper.addEventListener('mouseleave', () => {
        if (!dragging) return;
        dragging = false;
        wrapper.classList.remove('dragging');
      });
    }

    function applySettings(settings) {
      state.settings = settings || {};
      document.getElementById('alertStatus').checked = !!settings.status_notify_enabled;
      document.getElementById('alertExit').checked = !!settings.exit_notify_enabled;
      document.getElementById('alertPush').checked = settings.push_notify_enabled !== false;
      document.getElementById('alertBeep').checked = !!settings.beep_on_event;
      document.getElementById('arpingWarnInput').value = settings.arping_warn_threshold ?? 10;
      document.getElementById('arpingEmergencyInput').value = settings.arping_emergency_threshold ?? 20;
      document.getElementById('arpingToggle').checked = settings.arping_before_fetch !== false;
      const modeSelect = document.getElementById('arpingModeSelect');
      if (modeSelect) {
        modeSelect.value = settings.arping_mode || settings.arping_mode_default || 'auto';
      }
      const manualIpInput = document.getElementById('arpingManualIpInput');
      if (manualIpInput) {
        manualIpInput.value = settings.arping_manual_ip || '';
      }
      document.getElementById('routerBaseInput').value = settings.router_base_url || settings.router_base_url_default || '';
      document.getElementById('routerPathInput').value = settings.router_fetch_path || settings.router_fetch_path_default || '';
      document.getElementById('targetNameInput').value = settings.target_name_contains || '';
      document.getElementById('targetMacInput').value = settings.target_mac || '';
      currentTargetMac = settings.target_mac || '';
      const targetSelect = document.getElementById('targetSelect');
      if (targetSelect && currentTargetMac) {
        targetSelect.value = currentTargetMac;
      }
      state.yRange = Number(settings.chart_y_range_db) || 90;
      state.yBase = Math.max(0, Number(settings.chart_y_min_db) || 0);
      document.getElementById('yRangeInput').value = state.yRange;
      document.getElementById('yBaseInput').value = state.yBase;
      updateArpingModeUI();
    }

    function updateArpingModeUI() {
      const modeSelect = document.getElementById('arpingModeSelect');
      const manualIpInput = document.getElementById('arpingManualIpInput');
      const toggle = document.getElementById('arpingToggle');
      const mode = modeSelect ? modeSelect.value : 'auto';
      const enabled = toggle ? toggle.checked : true;
      if (manualIpInput) {
        manualIpInput.disabled = mode !== 'manual' || !enabled;
        manualIpInput.style.opacity = manualIpInput.disabled ? '0.6' : '1';
      }
    }

    function getFilteredEntries() {
      const transformed = (state.entries || [])
        .map(e => { const t = new Date((e.timestamp || '').replace(' ', 'T')).getTime(); return { ...e, ts: t, value: Number(e.rssi_value) }; })
        .filter(e => Number.isFinite(e.value) && Number.isFinite(e.ts));
      if (!transformed.length) return [];
      const latestTs = Math.max(...transformed.map(e => e.ts));
      const windowMs = ranges[state.range] || ranges['10m'];
      const cutoff = latestTs - windowMs;
      return transformed.filter(e => e.ts >= cutoff);
    }

    function renderLegend(series) {
      const legend = document.getElementById('legend'); legend.innerHTML = '';
      series.forEach(s => { const span = document.createElement('span'); span.innerHTML = '<span class="dot" style="background:' + s.color + '"></span>' + s.label; legend.appendChild(span); });
      const stableSpan = document.createElement('span');
      stableSpan.innerHTML = '<span class="dot" style="background: #ef4444; border-radius: 2px; width: 14px; height: 2px;"></span>长时间无变化';
      legend.appendChild(stableSpan);
      const arpingSpan = document.createElement('span');
      arpingSpan.innerHTML = '<span class="dot" style="background:' + ARPING_FAIL_COLOR + ';"></span>ARP 预热失败';
      legend.appendChild(arpingSpan);
      const thresholdSpan = document.createElement('span'); thresholdSpan.innerHTML = '<span class="dot" style="background: #d946ef; border-radius: 2px; height: 2px;"></span>阈值线'; legend.appendChild(thresholdSpan);
    }

    function renderChart() {
      const canvas = document.getElementById('chart');
      const rect = canvas.getBoundingClientRect();
      const cssWidth = canvas.clientWidth || rect.width || canvas.width;
      const cssHeight = canvas.clientHeight || rect.height || canvas.height;
      const ratio = window.devicePixelRatio || 1;
      const ctx = canvas.getContext('2d');
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      canvas.width = cssWidth * ratio;
      canvas.height = cssHeight * ratio;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, cssWidth, cssHeight);

      const entries = getFilteredEntries();
      state.points = [];
      state.stableSegments = [];
      if (!entries.length) {
        state.scale = null;
        ctx.fillStyle = '#94a3b8';
        ctx.font = '14px "Fira Sans", "Segoe UI", sans-serif';
        ctx.fillText('暂无选定范围的 RSSI 数据', 20, 30);
        return;
      }
      entries.sort((a, b) => a.ts - b.ts);
      const labels = Array.from(new Set(entries.map(e => e.label || '未命名')));
      const series = labels.map((label, idx) => ({ label, color: palette[idx % palette.length], points: entries.filter(e => (e.label || '未命名') === label) }));

      const margin = { top: 28, right: 28, bottom: 56, left: 70 };
      const width = cssWidth - margin.left - margin.right;
      const height = cssHeight - margin.top - margin.bottom;

      const values = entries.map(e => e.value).concat([state.threshold]);
      const maxVal = Math.max(...values);
      const span = state.yRange || 90;
      let yMin = Math.max(0, state.yBase || 0);
      let yMax = yMin + span;
      if (maxVal > yMax) { yMax = maxVal; yMin = Math.max(0, yMax - span); }

      const minTs = entries[0].ts;
      const maxTs = entries[entries.length - 1].ts || (minTs + 1);
      const xScale = (ts) => margin.left + (width * (ts - minTs)) / (maxTs - minTs || 1);
      const yScale = (val) => margin.top + height - ((val - yMin) / (yMax - yMin || 1)) * height;
      const pointScale = Math.max(0.35, 1 / Math.sqrt(state.zoom || 1));

      const stableSegments = [];
      const minStableMs = STALE_MINUTES * 60 * 1000;
      series.forEach(s => {
        const segs = findStableSegments(s.points, minStableMs, STALE_VALUE_EPS);
        segs.forEach(seg => {
          stableSegments.push({ ...seg });
        });
      });
      state.stableSegments = stableSegments;

      ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
      const gridLines = 4;
      for (let i = 0; i <= gridLines; i++) { const y = margin.top + (height / gridLines) * i; ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(margin.left + width, y); ctx.stroke(); const value = Math.round(yMax - ((y - margin.top) / height) * (yMax - yMin)); ctx.fillStyle = '#94a3b8'; ctx.font = '12px "Fira Sans", "Segoe UI", sans-serif'; ctx.fillText(value + ' dBm', 10, y + 4); }
      ctx.setLineDash([]);

      const thresholdY = yScale(state.threshold);
      ctx.strokeStyle = '#d946ef'; ctx.lineWidth = 1.6; ctx.setLineDash([8, 6]); ctx.beginPath(); ctx.moveTo(margin.left, thresholdY); ctx.lineTo(margin.left + width, thresholdY); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = '#d946ef'; ctx.font = '12px "Fira Sans", "Segoe UI", sans-serif'; ctx.fillText('阈值 ' + state.threshold + ' dBm', margin.left + 6, thresholdY - 8);

      series.forEach(s => {
        const scaled = s.points.map(p => ({ x: xScale(p.ts), y: yScale(p.value), online: p.online, arping_ok: p.arping_ok }));
        if (!scaled.length) return;
        scaled.forEach((pt, idx) => { state.points.push({ ...pt, label: s.label, value: s.points[idx].value, ts: s.points[idx].ts, arping_ok: s.points[idx].arping_ok }); });
        ctx.strokeStyle = s.color; ctx.lineWidth = 2.2; ctx.beginPath(); ctx.moveTo(scaled[0].x, scaled[0].y);
        if (scaled.length === 1) { ctx.lineTo(scaled[0].x, scaled[0].y); }
        else {
          for (let i = 1; i < scaled.length - 1; i++) { const xc = (scaled[i].x + scaled[i + 1].x) / 2; const yc = (scaled[i].y + scaled[i + 1].y) / 2; ctx.quadraticCurveTo(scaled[i].x, scaled[i].y, xc, yc); }
          const last = scaled[scaled.length - 1]; const prev = scaled[scaled.length - 2]; ctx.quadraticCurveTo(prev.x, prev.y, last.x, last.y);
        }
        ctx.stroke();
        scaled.forEach((pt, idx) => {
          const radius = (idx === scaled.length - 1 ? 6 : 4) * pointScale;
          ctx.fillStyle = pt.arping_ok === false ? ARPING_FAIL_COLOR : (pt.online ? '#16a34a' : '#ef4444');
          ctx.strokeStyle = 'transparent'; ctx.lineWidth = 0;
          ctx.beginPath(); ctx.arc(pt.x, pt.y, radius, 0, Math.PI * 2); ctx.fill();
          if (idx === scaled.length - 1) { ctx.beginPath(); ctx.arc(pt.x, pt.y, radius + 3 * pointScale, 0, Math.PI * 2); ctx.strokeStyle = s.color + '80'; ctx.lineWidth = Math.max(1, 2 * pointScale); ctx.stroke(); }
        });
      });

      if (stableSegments.length) {
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = Math.max(1.2, 2 * pointScale);
        stableSegments.forEach(seg => {
          const x1 = xScale(seg.startTs);
          const x2 = xScale(seg.endTs);
          if (x2 - x1 < 6) return;
          const y = yScale(seg.value);
          ctx.beginPath();
          ctx.moveTo(x1, y);
          ctx.lineTo(x2, y);
          ctx.stroke();
        });
      }

      const labelTs = entries.length === 1 ? [entries[0].ts] : entries.length === 2 ? [entries[0].ts, entries[1].ts] : [entries[0].ts, entries[Math.floor(entries.length / 2)].ts, entries[entries.length - 1].ts];
      ctx.fillStyle = '#94a3b8'; ctx.font = '12px "Fira Sans", "Segoe UI", sans-serif';
      labelTs.forEach(tsVal => { const x = xScale(tsVal); const date = new Date(tsVal); const label = (date.getMonth() + 1) + '-' + date.getDate() + ' ' + String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0'); ctx.fillText(label, x - 30, cssHeight - margin.bottom + 30); ctx.strokeStyle = '#e5e7eb'; ctx.beginPath(); ctx.moveTo(x, margin.top + height); ctx.lineTo(x, margin.top + height + 6); ctx.stroke(); });

      state.scale = { minTs, maxTs, yMin, yMax, margin, width, height };
      renderLegend(series);
    }

    async function loadDeviceOptions() {
      const select = document.getElementById('targetSelect');
      if (!select) return;
      select.innerHTML = '<option value="">选择设备...</option>';
      try {
        const resp = await fetch('/devices?_=' + Date.now());
        const data = await resp.json();
        const devices = data.devices || [];
        devices.forEach(device => {
          const opt = document.createElement('option');
          opt.value = device.mac || '';
          opt.dataset.ip = device.ip || '';
          opt.dataset.name = device.name || '';
          const nameText = device.name || '未命名';
          const ipText = device.ip || '-';
          const macText = device.mac || '-';
          const onlineText = device.online ? '在线' : '离线';
          opt.textContent = `${nameText} (${ipText}) ${macText} ${onlineText}`;
          select.appendChild(opt);
        });
        if (currentTargetMac) {
          select.value = currentTargetMac;
        }
      } catch (err) {
        console.error('加载设备列表失败', err);
      }
    }

    async function fetchSettings() { try { const resp = await fetch('/settings?_=' + Date.now()); const data = await resp.json(); applySettings(data.settings || {}); } catch (err) { console.error('加载设置失败', err); } }

    async function fetchData() {
      try {
        const resp = await fetch('/data?_=' + Date.now());
        const data = await resp.json();
        state.entries = data.entries || [];
        state.threshold = Number(data.threshold) || 0;
        state.defaultThreshold = Number(data.default_threshold) || 0;
        document.getElementById('lastUpdated').textContent = '最后更新：' + data.timestamp;
        document.getElementById('thresholdDisplay').textContent = state.threshold;
        document.getElementById('thresholdInput').value = state.threshold;
        document.getElementById('thresholdDefault').textContent = state.defaultThreshold;
        renderChart();
      } catch (err) { console.error('加载历史失败', err); }
    }

    async function submitThreshold() {
      const raw = Number(document.getElementById('thresholdInput').value);
      if (!Number.isFinite(raw) || raw <= 0) { alert('请输入大于 0 的数字阈值'); return; }
      try { const resp = await fetch('/threshold', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ threshold: raw }) }); const data = await resp.json(); state.threshold = Number(data.threshold) || raw; document.getElementById('thresholdDisplay').textContent = state.threshold; document.getElementById('thresholdInput').value = state.threshold; renderChart(); }
      catch (err) { alert('阈值更新失败，请稍后重试'); console.error(err); }
    }

    async function submitSettings() {
      const payload = {
        status_notify_enabled: document.getElementById('alertStatus').checked,
        exit_notify_enabled: document.getElementById('alertExit').checked,
        push_notify_enabled: document.getElementById('alertPush').checked,
        beep_on_event: document.getElementById('alertBeep').checked,
        chart_y_range_db: Number(document.getElementById('yRangeInput').value) || 90,
        chart_y_min_db: Number(document.getElementById('yBaseInput').value) || 0,
        arping_warn_threshold: Number(document.getElementById('arpingWarnInput').value) || 1,
        arping_emergency_threshold: Number(document.getElementById('arpingEmergencyInput').value) || 1,
        router_base_url: document.getElementById('routerBaseInput').value.trim(),
        router_fetch_path: document.getElementById('routerPathInput').value.trim(),
        arping_before_fetch: document.getElementById('arpingToggle').checked,
        arping_mode: document.getElementById('arpingModeSelect').value,
        arping_manual_ip: document.getElementById('arpingManualIpInput').value.trim(),
        target_name_contains: document.getElementById('targetNameInput').value.trim(),
        target_mac: document.getElementById('targetMacInput').value.trim(),
      };
      try { const resp = await fetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); const data = await resp.json(); applySettings(data.settings || {}); renderChart(); }
      catch (err) { alert('保存设置失败'); console.error(err); }
    }

    function setRange(rangeKey) { state.range = rangeKey; document.querySelectorAll('.range-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.range === rangeKey)); renderChart(); }

    function setZoom(value, wrapper, anchorOverride, mouseXOverride, mouseYOverride) {
      const canvas = document.getElementById('chart');
      const newZoom = Math.min(MAX_ZOOM, Math.max(1, value));
      ensureBaseSize(canvas);
      let anchor = anchorOverride || null;
      let mouseX = mouseXOverride ?? (canvas.clientWidth / 2);
      let mouseY = mouseYOverride ?? (canvas.clientHeight / 2);
      if (wrapper && !anchor) {
        const scrollLeft = wrapper.scrollLeft;
        const scrollTop = wrapper.scrollTop;
        mouseX = wrapper.clientWidth / 2;
        mouseY = wrapper.clientHeight / 2;
        anchor = getAnchorFromPoint(scrollLeft + mouseX, scrollTop + mouseY);
      }
      state.zoom = newZoom;
      if (newZoom <= 1.001) {
        canvas.style.width = '';
        canvas.style.height = '';
      } else {
        canvas.style.width = (state.baseWidth * newZoom) + 'px';
        canvas.style.height = (state.baseHeight * newZoom) + 'px';
      }
      canvas.style.transform = 'none';
      canvas.style.transformOrigin = '0 0';
      renderChart();
      if (wrapper && anchor && state.scale) {
        const nextPoint = getPointFromAnchor(anchor);
        if (nextPoint) {
          const maxScrollLeft = Math.max(0, wrapper.scrollWidth - wrapper.clientWidth);
          const maxScrollTop = Math.max(0, wrapper.scrollHeight - wrapper.clientHeight);
          wrapper.scrollLeft = clamp(nextPoint.x - mouseX, 0, maxScrollLeft);
          wrapper.scrollTop = clamp(nextPoint.y - mouseY, 0, maxScrollTop);
        }
      }
      document.getElementById('chartZoom').value = state.zoom.toFixed(1);
      document.getElementById('zoomValue').textContent = state.zoom.toFixed(1) + 'x';
    }

    window.onload = () => {
      fetchSettings().then(() => {
        fetchData();
        loadDeviceOptions();
      });
      setInterval(fetchData, 5000);
      document.getElementById('thresholdSave').addEventListener('click', submitThreshold);
      document.getElementById('thresholdInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitThreshold(); });
      document.getElementById('settingsSave').addEventListener('click', submitSettings);
      document.getElementById('targetSave').addEventListener('click', submitSettings);
      document.getElementById('refreshDevices').addEventListener('click', (e) => { e.preventDefault(); loadDeviceOptions(); });
      document.getElementById('targetSelect').addEventListener('change', (e) => {
        const option = e.target.selectedOptions[0];
        if (!option || !option.value) return;
        document.getElementById('targetMacInput').value = option.value;
        document.getElementById('targetNameInput').value = option.dataset.name || '';
      });
      document.getElementById('arpingModeSelect').addEventListener('change', updateArpingModeUI);
      document.getElementById('arpingToggle').addEventListener('change', updateArpingModeUI);
      document.querySelectorAll('.range-btn').forEach(btn => { btn.addEventListener('click', () => setRange(btn.dataset.range)); });
      const wrap = document.getElementById('chartWrap');
      const chartCanvas = document.getElementById('chart');
      state.baseWidth = chartCanvas.clientWidth || chartCanvas.getBoundingClientRect().width;
      state.baseHeight = chartCanvas.clientHeight || chartCanvas.getBoundingClientRect().height;
      document.getElementById('chartZoom').addEventListener('input', (e) => setZoom(Math.max(1, Number(e.target.value)), wrap));
      document.getElementById('yRangeInput').addEventListener('change', (e) => { state.yRange = Number(e.target.value) || 90; renderChart(); });
      document.getElementById('yBaseInput').addEventListener('change', (e) => { state.yBase = Math.max(0, Number(e.target.value) || 0); renderChart(); });
      wrap.addEventListener('wheel', (e) => {
        e.preventDefault();
        const rect = wrap.getBoundingClientRect();
        const cursor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
        const origin = { x: cursor.x + wrap.scrollLeft, y: cursor.y + wrap.scrollTop };
        const session = updateZoomSession(origin, cursor, wrap);
        const delta = e.deltaY > 0 ? -0.1 : 0.1;
        const next = Math.min(MAX_ZOOM, Math.max(1, Number(document.getElementById('chartZoom').value) + delta));
        setZoom(next, wrap, session.anchor, session.mouseX, session.mouseY);
      }, { passive: false });
      wrap.addEventListener('dblclick', (e) => {
        e.preventDefault();
        const rect = wrap.getBoundingClientRect();
        const cursor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
        const origin = { x: cursor.x + wrap.scrollLeft, y: cursor.y + wrap.scrollTop };
        const session = updateZoomSession(origin, cursor, wrap);
        setZoom(MAX_ZOOM, wrap, session.anchor, session.mouseX, session.mouseY);
      });
      const tooltip = document.getElementById('chartTooltip');
      enablePanning(wrap, tooltip);
      wrap.addEventListener('mousemove', (e) => {
        const rect = wrap.getBoundingClientRect();
        const x = e.clientX - rect.left + wrap.scrollLeft;
        const y = e.clientY - rect.top + wrap.scrollTop;
        if (!state.points.length) { tooltip.style.display = 'none'; return; }
        let best = null;
        let bestDist = Infinity;
        state.points.forEach(pt => {
          const dx = pt.x - x;
          const dy = pt.y - y;
          const d2 = dx * dx + dy * dy;
          if (d2 < bestDist) { bestDist = d2; best = pt; }
        });
        if (best && bestDist <= 400) {
          const date = new Date(best.ts);
          const timeStr = (date.getMonth() + 1) + '-' + date.getDate() + ' ' + String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0') + ':' + String(date.getSeconds()).padStart(2, '0');
          const arpingText = formatArpingStatus(best.arping_ok);
          tooltip.innerHTML = `${best.label || '未命名'}<br/>${timeStr}<br/>RSSI: ${best.value} dBm<br/>${arpingText}`;
          tooltip.style.left = best.x + 'px';
          tooltip.style.top = best.y + 'px';
          tooltip.style.display = 'block';
        } else {
          tooltip.style.display = 'none';
        }
      });
      wrap.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
      setRange('10m');
      setZoom(1, wrap);
    };
  </script>
</head>
<body>
  <div class="page">
    <div class="header">
      <div>
        <p class="eyebrow">Signal Monitor</p>
        <h1>监控概览</h1>
        <p class="last-updated" id="lastUpdated">正在加载...</p>
      </div>
      <div style="display:flex; gap:12px; flex-wrap:wrap;">
        <a class="link" href="/history">历史天视图 →</a>
        <a class="link" href="/table">事件表格 →</a>
      </div>
    </div>

    <div class="dashboard-grid">
      <div class="card threshold-card">
        <div class="card-title">RSSI 阈值</div>
        <div class="threshold-row">
          <div>
            <div class="control label">当前值</div>
            <div class="value"><span id="thresholdDisplay">--</span> dBm</div>
          </div>
          <div class="control">
            <label for="thresholdInput">调整阈值</label>
            <div class="control-row">
              <input id="thresholdInput" type="number" min="1" step="1" />
              <button id="thresholdSave" type="button">保存</button>
            </div>
            <div class="hint">默认值 <span id="thresholdDefault">--</span> dBm</div>
          </div>
        </div>
      </div>

      <div class="card target-card">
        <div class="card-title">目标设备设置</div>
        <div class="field-grid" style="margin-bottom: 10px;">
          <div class="field">
            <label for="routerBaseInput">路由器地址</label>
            <input id="routerBaseInput" type="text" placeholder="http://192.168.5.1" />
          </div>
          <div class="field">
            <label for="routerPathInput">数据接口路径</label>
            <input id="routerPathInput" type="text" placeholder="/?_type=vueData&_tag=vue_topo_data&Action=GetALLClients" />
          </div>
        </div>
        <div class="hint" style="margin: -4px 0 10px 0;">自动拼接成完整抓取地址</div>
        <div class="field-grid">
          <div class="field">
            <label for="targetNameInput">名称包含</label>
            <input id="targetNameInput" type="text" placeholder="如 设备名称" />
          </div>
          <div class="field">
            <label for="targetMacInput">MAC</label>
            <input id="targetMacInput" type="text" placeholder="AA:BB:CC:DD:EE:FF" />
          </div>
        </div>
        <div class="field" style="margin-top: 10px;">
          <label for="targetSelect">从已发现设备选择</label>
          <div class="select-row">
            <select id="targetSelect">
              <option value="">选择设备...</option>
            </select>
            <button id="refreshDevices" type="button">刷新列表</button>
          </div>
        </div>
        <div class="control-row" style="margin-top: 10px;">
          <button id="targetSave" type="button">保存目标</button>
          <span class="hint">支持名称包含 / MAC 匹配，IP 自动从设备列表获取。</span>
        </div>
      </div>

      <div class="card">
        <div class="card-title">告警开关</div>
        <div class="switches">
          <label class="switch"><input type="checkbox" id="alertPush" /><span class="knob"></span> 推送总开关</label>
          <label class="switch"><input type="checkbox" id="alertStatus" /><span class="knob"></span> 上下线提醒</label>
          <label class="switch"><input type="checkbox" id="alertExit" /><span class="knob"></span> 程序退出提醒</label>
          <label class="switch"><input type="checkbox" id="alertBeep" /><span class="knob"></span> 控制台蜂鸣</label>
          <label class="switch"><input type="checkbox" id="arpingToggle" /><span class="knob"></span> ARPing 预热</label>
        </div>
        <div class="field-grid" style="margin-top: 10px;">
          <div class="field">
            <label for="arpingModeSelect">ARPing 模式</label>
            <select id="arpingModeSelect">
              <option value="auto">自动获取目标 IP</option>
              <option value="manual">手动填写 IP</option>
            </select>
          </div>
          <div class="field">
            <label for="arpingManualIpInput">手动目标 IP</label>
            <input id="arpingManualIpInput" type="text" placeholder="如 192.168.5.100" />
          </div>
        </div>
        <div class="field-grid" style="margin-top: 10px;">
          <div class="field">
            <label for="arpingWarnInput">ARPing 连续 down 警告阈值(次)</label>
            <input id="arpingWarnInput" type="number" min="1" step="1" />
          </div>
          <div class="field">
            <label for="arpingEmergencyInput">ARPing 连续 down 紧急阈值(次)</label>
            <input id="arpingEmergencyInput" type="number" min="1" step="1" />
          </div>
        </div>
        <div class="control-row" style="margin-top: 10px;">
          <button id="settingsSave" type="button">应用设置</button>
          <span class="hint">推送总开关会控制企业微信与 PushPlus。</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">实时曲线</div>
      <div class="range-bar">
        <div class="range-buttons">
          <button class="range-btn" data-range="1d">1 天</button>
          <button class="range-btn" data-range="4h">4 小时</button>
          <button class="range-btn" data-range="1h">1 小时</button>
          <button class="range-btn" data-range="10m">默认检测 (10 分钟)</button>
        </div>
      </div>
      <div class="chart-wrap" id="chartWrap">
        <canvas id="chart" height="420"></canvas>
        <div class="tooltip" id="chartTooltip"></div>
      </div>
      <div class="legend" id="legend"></div>
      <div class="zoom-row">
        <label class="control" for="yRangeInput">纵坐标范围 (dBm)</label>
        <input id="yRangeInput" type="number" min="5" step="1" value="90" />
        <label class="control" for="yBaseInput" style="margin-left:12px;">纵轴起点</label>
        <input id="yBaseInput" type="number" min="0" step="1" value="0" />
        <label class="control" for="chartZoom" style="margin-left:12px;">缩放</label>
        <input id="chartZoom" type="range" min="1" max="8" step="0.1" value="1" />
        <span class="hint" id="zoomValue">1.0x</span>
        <span class="hint">滚轮在曲线上可放大/缩小并以鼠标位置为中心</span>
      </div>
    </div>

  </div>
</body>
</html>"""

    def _render_history_page(self) -> str:
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>历史天视图</title>
  <style>
    :root {
      --bg: #eef2ff;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #64748b;
      --primary: #2563eb;
      --accent: #fb7185;
      --chart-bg: linear-gradient(180deg, rgba(37, 99, 235, 0.06), rgba(37, 99, 235, 0.01));
    }
    * { box-sizing: border-box; }
    body { margin: 0; padding: 22px; font-family: "Fira Sans", "Segoe UI", "Microsoft YaHei", sans-serif; background: linear-gradient(120deg, #f8fafc 0%, #eef2ff 60%, #ffffff 100%); color: var(--text); }
    .page { max-width: 1280px; margin: 0 auto; }
    .header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; }
    h1 { margin: 0; font-size: 26px; }
    .card { background: var(--card); border: 1px solid #e5e7eb; border-radius: 16px; padding: 16px 18px; box-shadow: 0 18px 48px rgba(15,23,42,0.08); }
    .card + .card { margin-top: 14px; }
    .card-title { font-weight: 700; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
    .card-title::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: linear-gradient(135deg, var(--primary), var(--accent)); }
    input[type="date"], input[type="number"], input[type="time"] { padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 10px; }
    button { padding: 9px 14px; border-radius: 12px; border: none; background: linear-gradient(135deg, var(--primary), #1d4ed8); color: #fff; font-weight: 700; cursor: pointer; box-shadow: 0 10px 30px rgba(37, 99, 235, 0.3); transition: transform 0.08s ease, box-shadow 0.12s ease; }
    button:hover { transform: translateY(-1px); box-shadow: 0 16px 40px rgba(37, 99, 235, 0.32); }
    #historyChart { width: 100%; height: 440px; display: block; background: var(--chart-bg); border-radius: 14px; border: 1px solid #e5e7eb; }
    .chart-wrap { width: 100%; height: 440px; overflow: auto; padding-bottom: 8px; position: relative; cursor: grab; }
    .chart-wrap.dragging { cursor: grabbing; }
    .chart-wrap.dragging { cursor: grabbing; }
    .legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 10px; color: var(--muted); font-size: 13px; }
    .legend .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
    .controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .hint { color: var(--muted); font-size: 12px; }
    input[type="range"] { accent-color: var(--primary); }
    .link { color: #2563eb; text-decoration: none; font-weight: 700; }
    @media (max-width: 720px) { body { padding: 14px; } #historyChart { height: 320px; } .chart-wrap { height: 320px; } }
  </style>
  <script>
    const dayState = { entries: [], threshold: 0, yRange: 90, yBase: 0, zoom: 1, points: [], stableSegments: [], baseWidth: 0, baseHeight: 0, scale: null, zoomSession: null };
    const palette = ["#0ea5e9", "#fb7185", "#22c55e", "#8b5cf6", "#ef4444", "#f59e0b", "#06b6d4"];
    const STALE_MINUTES = 1;
    const STALE_VALUE_EPS = 0;
    const MAX_ZOOM = 8;
    const ARPING_FAIL_COLOR = '#7c3aed';
    const ZOOM_ANCHOR_TTL = 2000;
    const ZOOM_ANCHOR_MOVE_PX = 10;

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function formatArpingStatus(value) {
      if (value === true) return 'arping: up';
      if (value === false) return 'arping: down';
      return 'arping: n/a';
    }

    function formatDuration(ms) {
      const totalMinutes = Math.max(0, Math.round(ms / 60000));
      const hours = Math.floor(totalMinutes / 60);
      const minutes = totalMinutes % 60;
      if (hours <= 0) return `${minutes}分钟`;
      if (minutes === 0) return `${hours}小时`;
      return `${hours}小时${minutes}分钟`;
    }

    function formatTime(ts) {
      const date = new Date(ts);
      const hh = String(date.getHours()).padStart(2, '0');
      const mm = String(date.getMinutes()).padStart(2, '0');
      return `${hh}:${mm}`;
    }

    function parseTimeMinutes(value) {
      if (!value) return null;
      const parts = value.split(':');
      if (parts.length < 2) return null;
      const hours = Number(parts[0]);
      const minutes = Number(parts[1]);
      if (!Number.isFinite(hours) || !Number.isFinite(minutes)) return null;
      return hours * 60 + minutes;
    }

    function filterTimeRange(entries, startStr, endStr) {
      const startMin = parseTimeMinutes(startStr);
      const endMin = parseTimeMinutes(endStr);
      if (startMin === null && endMin === null) return entries;
      const start = startMin ?? 0;
      const end = endMin ?? 24 * 60;
      if (end <= start) return entries;
      return entries.filter(e => {
        const date = new Date(e.ts);
        const minutes = date.getHours() * 60 + date.getMinutes();
        return minutes >= start && minutes <= end;
      });
    }

    function ensureBaseSize() {
      const canvas = document.getElementById('historyChart');
      if (!dayState.baseWidth || !dayState.baseHeight) {
        const rect = canvas.getBoundingClientRect();
        dayState.baseWidth = rect.width || canvas.clientWidth || canvas.width;
        dayState.baseHeight = rect.height || canvas.clientHeight || canvas.height;
      }
    }

    function getAnchorFromPoint(x, y) {
      const scale = dayState.scale;
      if (!scale) return null;
      const rx = (x - scale.margin.left) / (scale.width || 1);
      const ry = (y - scale.margin.top) / (scale.height || 1);
      const t = clamp(rx, 0, 1);
      const v = clamp(ry, 0, 1);
      const ts = scale.minTs + t * (scale.maxTs - scale.minTs || 1);
      const value = scale.yMax - v * (scale.yMax - scale.yMin || 1);
      return { ts, value };
    }

    function getPointFromAnchor(anchor) {
      const scale = dayState.scale;
      if (!scale || !anchor) return null;
      const tsSpan = scale.maxTs - scale.minTs || 1;
      const valSpan = scale.yMax - scale.yMin || 1;
      const x = scale.margin.left + (scale.width * (anchor.ts - scale.minTs)) / tsSpan;
      const y = scale.margin.top + scale.height - (scale.height * (anchor.value - scale.yMin)) / valSpan;
      return { x, y };
    }

    function updateDayZoomSession(origin, cursor, wrapper) {
      const session = dayState.zoomSession || {};
      const moved = session.lastCursorX !== undefined && (Math.abs(cursor.x - session.lastCursorX) > ZOOM_ANCHOR_MOVE_PX || Math.abs(cursor.y - session.lastCursorY) > ZOOM_ANCHOR_MOVE_PX);
      if (!session.anchor || moved) {
        session.anchor = getAnchorFromPoint(origin.x, origin.y);
        session.mouseX = cursor.x;
        session.mouseY = cursor.y;
      }
      session.lastCursorX = cursor.x;
      session.lastCursorY = cursor.y;
      if (session.timer) {
        clearTimeout(session.timer);
      }
      session.timer = setTimeout(() => { dayState.zoomSession = null; }, ZOOM_ANCHOR_TTL);
      dayState.zoomSession = session;
      return session;
    }

    function enablePanning(wrapper, tooltip) {
      let dragging = false;
      let startX = 0;
      let startY = 0;
      let startScrollLeft = 0;
      let startScrollTop = 0;
      wrapper.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        dragging = true;
        dayState.zoomSession = null;
        startX = e.clientX;
        startY = e.clientY;
        startScrollLeft = wrapper.scrollLeft;
        startScrollTop = wrapper.scrollTop;
        wrapper.classList.add('dragging');
        if (tooltip) tooltip.style.display = 'none';
      });
      window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        wrapper.scrollLeft = startScrollLeft - dx;
        wrapper.scrollTop = startScrollTop - dy;
      });
      window.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        wrapper.classList.remove('dragging');
      });
      wrapper.addEventListener('mouseleave', () => {
        if (!dragging) return;
        dragging = false;
        wrapper.classList.remove('dragging');
      });
    }

    function findStableSegments(points, minDurationMs, valueEps) {
      const segments = [];
      if (!points || points.length < 2) return segments;
      let startIdx = null;
      for (let i = 0; i < points.length; i++) {
        const curr = points[i];
        if (!curr.online) {
          if (startIdx !== null) {
            const end = points[i - 1];
            const start = points[startIdx];
            const durationMs = end.ts - start.ts;
            if (durationMs >= minDurationMs) {
              segments.push({ startTs: start.ts, endTs: end.ts, value: start.value, durationMs });
            }
            startIdx = null;
          }
          continue;
        }
        if (startIdx === null) {
          startIdx = i;
          continue;
        }
        const prev = points[i - 1];
        if (!prev.online) {
          startIdx = i;
          continue;
        }
        if (Math.abs(curr.value - prev.value) <= valueEps) {
          continue;
        }
        const start = points[startIdx];
        const end = points[i - 1];
        const durationMs = end.ts - start.ts;
        if (durationMs >= minDurationMs) {
          segments.push({ startTs: start.ts, endTs: end.ts, value: start.value, durationMs });
        }
        startIdx = i;
      }
      if (startIdx !== null) {
        const start = points[startIdx];
        const end = points[points.length - 1];
        if (end.online) {
          const durationMs = end.ts - start.ts;
          if (durationMs >= minDurationMs) {
            segments.push({ startTs: start.ts, endTs: end.ts, value: start.value, durationMs });
          }
        }
      }
      return segments;
    }

    function filterDay(entries, dateStr) {
      if (!dateStr) return [];
      const start = new Date(dateStr + 'T00:00:00');
      const end = new Date(start.getTime() + 24 * 3600 * 1000);
      return entries
        .map(e => ({ ...e, ts: new Date((e.timestamp || '').replace(' ', 'T')).getTime(), value: Number(e.rssi_value) }))
        .filter(e => Number.isFinite(e.ts) && Number.isFinite(e.value) && e.ts >= start.getTime() && e.ts < end.getTime());
    }

    function renderLegendDay(series) {
      const legend = document.getElementById('historyLegend');
      legend.innerHTML = '';
      series.forEach(s => { const span = document.createElement('span'); span.innerHTML = '<span class="dot" style="background:' + s.color + '"></span>' + s.label; legend.appendChild(span); });
      const stableSpan = document.createElement('span');
      stableSpan.innerHTML = '<span class="dot" style="background: #ef4444; border-radius: 2px; width: 14px; height: 2px;"></span>长时间无变化(≥' + STALE_MINUTES + '分钟)';
      legend.appendChild(stableSpan);
      const arpingSpan = document.createElement('span');
      arpingSpan.innerHTML = '<span class="dot" style="background:' + ARPING_FAIL_COLOR + ';"></span>ARP 预热失败';
      legend.appendChild(arpingSpan);
      const thresholdSpan = document.createElement('span'); thresholdSpan.innerHTML = '<span class="dot" style="background: #d946ef; border-radius: 2px; height: 2px;"></span>阈值线'; legend.appendChild(thresholdSpan);
    }

    function renderHistoryChart() {
      const canvas = document.getElementById('historyChart');
      const rect = canvas.getBoundingClientRect();
      const cssWidth = canvas.clientWidth || rect.width || canvas.width;
      const cssHeight = canvas.clientHeight || rect.height || canvas.height;
      const ratio = window.devicePixelRatio || 1;
      const ctx = canvas.getContext('2d');
      ctx.setTransform(1,0,0,1,0,0);
      canvas.width = cssWidth * ratio;
      canvas.height = cssHeight * ratio;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0,0,cssWidth,cssHeight);

      const entries = dayState.entries;
      dayState.points = [];
      dayState.stableSegments = [];
      if (!entries.length) {
        dayState.scale = null;
        ctx.fillStyle='#94a3b8';
        ctx.font='14px \"Fira Sans\", \"Segoe UI\", sans-serif';
        ctx.fillText('所选日期暂无数据', 20, 30);
        return;
      }
      entries.sort((a,b)=>a.ts-b.ts);
      const labels = Array.from(new Set(entries.map(e => e.label || '未命名')));
      const series = labels.map((label, idx) => ({ label, color: palette[idx % palette.length], points: entries.filter(e => (e.label || '未命名') === label) }));

      const margin = { top: 28, right: 28, bottom: 56, left: 70 };
      const width = cssWidth - margin.left - margin.right;
      const height = cssHeight - margin.top - margin.bottom;

      const values = entries.map(e => e.value).concat([dayState.threshold]);
      const maxVal = Math.max(...values);
      const span = dayState.yRange || 90;
      let yMin = Math.max(0, dayState.yBase || 0);
      let yMax = yMin + span;
      if (maxVal > yMax) { yMax = maxVal; yMin = Math.max(0, yMax - span); }

      const minTs = entries[0].ts;
      const maxTs = entries[entries.length - 1].ts || (minTs + 1);
      const xScale = (ts) => margin.left + (width * (ts - minTs)) / (maxTs - minTs || 1);
      const yScale = (val) => margin.top + height - ((val - yMin) / (yMax - yMin || 1)) * height;
      const pointScale = Math.max(0.35, 1 / Math.sqrt(dayState.zoom || 1));

      const stableSegments = [];
      const minStableMs = STALE_MINUTES * 60 * 1000;
      series.forEach(s => {
        const segs = findStableSegments(s.points, minStableMs, STALE_VALUE_EPS);
        segs.forEach(seg => {
          stableSegments.push({
            startTs: seg.startTs,
            endTs: seg.endTs,
            value: seg.value,
            durationMs: seg.durationMs,
            label: s.label,
            color: s.color
          });
        });
      });
      dayState.stableSegments = stableSegments;

      ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
      const gridLines = 4;
      for (let i=0;i<=gridLines;i++){ const y=margin.top+(height/gridLines)*i; ctx.beginPath(); ctx.moveTo(margin.left,y); ctx.lineTo(margin.left+width,y); ctx.stroke(); const value=Math.round(yMax-((y-margin.top)/height)*(yMax-yMin)); ctx.fillStyle='#94a3b8'; ctx.font='12px \"Fira Sans\", \"Segoe UI\", sans-serif'; ctx.fillText(value+' dBm',10,y+4); }
      ctx.setLineDash([]);

      const thresholdY = yScale(dayState.threshold);
      ctx.strokeStyle='#d946ef'; ctx.lineWidth=1.6; ctx.setLineDash([8,6]); ctx.beginPath(); ctx.moveTo(margin.left,thresholdY); ctx.lineTo(margin.left+width,thresholdY); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle='#d946ef'; ctx.font='12px \"Fira Sans\", \"Segoe UI\", sans-serif'; ctx.fillText('阈值 '+dayState.threshold+' dBm', margin.left+6, thresholdY-8);

      if (stableSegments.length) {
        const minSegWidth = 10;
        const labelMinWidth = 160;
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = Math.max(1.2, 2 * pointScale);
        stableSegments.forEach(seg => {
          const x1 = xScale(seg.startTs);
          const x2 = xScale(seg.endTs);
          if (x2 - x1 < minSegWidth) return;
          const y = yScale(seg.value);
          ctx.beginPath();
          ctx.moveTo(x1, y);
          ctx.lineTo(x2, y);
          ctx.stroke();
          if (x2 - x1 >= labelMinWidth) {
            ctx.fillStyle = '#ef4444';
            ctx.font = `11px \"Fira Sans\", \"Segoe UI\", sans-serif`;
            const labelY = Math.max(margin.top + 12, y - 6);
            const timeLabel = `${formatTime(seg.startTs)}~${formatTime(seg.endTs)}`;
            ctx.fillText(`${timeLabel} 稳定 ${formatDuration(seg.durationMs)}`, x1 + 6, labelY);
          }
        });
      }

      series.forEach(s=>{
        const scaled = s.points.map(p=>({x:xScale(p.ts), y:yScale(p.value), online:p.online, arping_ok: p.arping_ok}));
        if(!scaled.length)return;
        scaled.forEach((pt, idx)=>{ dayState.points.push({ ...pt, label: s.label, value: s.points[idx].value, ts: s.points[idx].ts, arping_ok: s.points[idx].arping_ok }); });
        ctx.strokeStyle=s.color; ctx.lineWidth=2.2; ctx.beginPath(); ctx.moveTo(scaled[0].x, scaled[0].y);
        if (scaled.length === 1) { ctx.lineTo(scaled[0].x, scaled[0].y); }
        else {
          for(let i=1;i<scaled.length-1;i++){ const xc=(scaled[i].x+scaled[i+1].x)/2; const yc=(scaled[i].y+scaled[i+1].y)/2; ctx.quadraticCurveTo(scaled[i].x, scaled[i].y, xc, yc); }
          const last=scaled[scaled.length-1]; const prev=scaled[scaled.length-2]; ctx.quadraticCurveTo(prev.x, prev.y, last.x, last.y);
        }
        ctx.stroke();
        scaled.forEach((pt, idx)=>{ const radius = (idx === scaled.length -1 ? 6 : 4) * pointScale; ctx.fillStyle = pt.arping_ok === false ? ARPING_FAIL_COLOR : (pt.online ? '#16a34a' : '#ef4444'); ctx.strokeStyle='transparent'; ctx.lineWidth=0; ctx.beginPath(); ctx.arc(pt.x, pt.y, radius, 0, Math.PI*2); ctx.fill(); if(idx===scaled.length-1){ ctx.beginPath(); ctx.arc(pt.x, pt.y, radius + 3 * pointScale,0,Math.PI*2); ctx.strokeStyle=s.color+'80'; ctx.lineWidth=Math.max(1, 2 * pointScale); ctx.stroke(); } });
      });

      const labelTs = [entries[0].ts, entries[Math.floor(entries.length/2)].ts, entries[entries.length-1].ts];
      ctx.fillStyle='#94a3b8'; ctx.font='12px \"Fira Sans\", \"Segoe UI\", sans-serif';
      labelTs.forEach(tsVal=>{ const x=xScale(tsVal); const date=new Date(tsVal); const label=String(date.getHours()).padStart(2,'0')+':'+String(date.getMinutes()).padStart(2,'0'); ctx.fillText(label, x-16, cssHeight - margin.bottom + 30); ctx.strokeStyle='#e5e7eb'; ctx.beginPath(); ctx.moveTo(x, margin.top+height); ctx.lineTo(x, margin.top+height+6); ctx.stroke(); });
      dayState.scale = { minTs, maxTs, yMin, yMax, margin, width, height };
      renderLegendDay(series);
    }

    async function loadDay() {
      const dateStr = document.getElementById('dateInput').value;
      if (!dateStr) return;
      try {
        const resp = await fetch('/data?date=' + dateStr + '&_=' + Date.now());
        const data = await resp.json();
        const timeStart = document.getElementById('timeStart').value;
        const timeEnd = document.getElementById('timeEnd').value;
        dayState.entries = filterTimeRange(filterDay(data.entries || [], dateStr), timeStart, timeEnd);
        dayState.threshold = Number(data.threshold) || 0;
        renderHistoryChart();
      } catch (err) { alert('加载历史数据失败'); console.error(err); }
    }

    function setDayZoom(value, wrapper, anchorOverride, mouseXOverride, mouseYOverride) {
      const canvas = document.getElementById('historyChart');
      const newZoom = Math.min(MAX_ZOOM, Math.max(1, value));
      ensureBaseSize();
      let anchor = anchorOverride || null;
      let mouseX = mouseXOverride ?? (canvas.clientWidth / 2);
      let mouseY = mouseYOverride ?? (canvas.clientHeight / 2);
      if (wrapper && !anchor) {
        const scrollLeft = wrapper.scrollLeft;
        const scrollTop = wrapper.scrollTop;
        mouseX = wrapper.clientWidth / 2;
        mouseY = wrapper.clientHeight / 2;
        anchor = getAnchorFromPoint(scrollLeft + mouseX, scrollTop + mouseY);
      }
      dayState.zoom = newZoom;
      if (newZoom <= 1.001) {
        canvas.style.width = '';
        canvas.style.height = '';
      } else {
        canvas.style.width = (dayState.baseWidth * newZoom) + 'px';
        canvas.style.height = (dayState.baseHeight * newZoom) + 'px';
      }
      canvas.style.transform = 'none';
      canvas.style.transformOrigin = '0 0';
      renderHistoryChart();
      if (wrapper && anchor && dayState.scale) {
        const nextPoint = getPointFromAnchor(anchor);
        if (nextPoint) {
          const maxScrollLeft = Math.max(0, wrapper.scrollWidth - wrapper.clientWidth);
          const maxScrollTop = Math.max(0, wrapper.scrollHeight - wrapper.clientHeight);
          wrapper.scrollLeft = clamp(nextPoint.x - mouseX, 0, maxScrollLeft);
          wrapper.scrollTop = clamp(nextPoint.y - mouseY, 0, maxScrollTop);
        }
      }
      document.getElementById('historyZoom').value = dayState.zoom.toFixed(1);
      document.getElementById('historyZoomVal').textContent = dayState.zoom.toFixed(1) + 'x';
    }

    window.onload = () => {
      const today = new Date(); const dateStr = today.toISOString().slice(0,10); document.getElementById('dateInput').value = dateStr;
      document.getElementById('loadDay').addEventListener('click', loadDay);
      document.getElementById('dateInput').addEventListener('change', loadDay);
      const timeStart = document.getElementById('timeStart');
      const timeEnd = document.getElementById('timeEnd');
      document.getElementById('btnFullDay').addEventListener('click', ()=>{
        timeStart.value = '';
        timeEnd.value = '';
        loadDay();
      });
      timeStart.addEventListener('change', loadDay);
      timeEnd.addEventListener('change', loadDay);
      document.getElementById('btnToday').addEventListener('click', ()=>{ document.getElementById('dateInput').value = new Date().toISOString().slice(0,10); loadDay(); });
      document.getElementById('btnYesterday').addEventListener('click', ()=>{ const d=new Date(); d.setDate(d.getDate()-1); document.getElementById('dateInput').value = d.toISOString().slice(0,10); loadDay(); });
      const historyWrap = document.getElementById('historyChartWrap');
      const historyCanvas = document.getElementById('historyChart');
      dayState.baseWidth = historyCanvas.clientWidth || historyCanvas.getBoundingClientRect().width;
      dayState.baseHeight = historyCanvas.clientHeight || historyCanvas.getBoundingClientRect().height;
      document.getElementById('historyZoom').addEventListener('input', (e)=> setDayZoom(Math.max(1, Number(e.target.value)), historyWrap));
      historyWrap.addEventListener('wheel', (e)=>{
        e.preventDefault();
        const rect = historyWrap.getBoundingClientRect();
        const cursor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
        const origin = { x: cursor.x + historyWrap.scrollLeft, y: cursor.y + historyWrap.scrollTop };
        const session = updateDayZoomSession(origin, cursor, historyWrap);
        const delta = e.deltaY > 0 ? -0.1 : 0.1;
        const next = Math.min(MAX_ZOOM, Math.max(1, Number(document.getElementById('historyZoom').value) + delta));
        setDayZoom(next, historyWrap, session.anchor, session.mouseX, session.mouseY);
      }, { passive: false });
      historyWrap.addEventListener('dblclick', (e) => {
        e.preventDefault();
        const rect = historyWrap.getBoundingClientRect();
        const cursor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
        const origin = { x: cursor.x + historyWrap.scrollLeft, y: cursor.y + historyWrap.scrollTop };
        const session = updateDayZoomSession(origin, cursor, historyWrap);
        setDayZoom(MAX_ZOOM, historyWrap, session.anchor, session.mouseX, session.mouseY);
      });
      document.getElementById('dayRange').addEventListener('change', (e)=>{ dayState.yRange = Number(e.target.value)||90; renderHistoryChart(); });
      document.getElementById('dayBase').addEventListener('change', (e)=>{ dayState.yBase = Math.max(0, Number(e.target.value)||0); renderHistoryChart(); });
      const dayTip = document.getElementById('historyTooltip');
      enablePanning(historyWrap, dayTip);
      historyWrap.addEventListener('mousemove', (e)=>{
        const rect = historyWrap.getBoundingClientRect();
        const x = e.clientX - rect.left + historyWrap.scrollLeft;
        const y = e.clientY - rect.top + historyWrap.scrollTop;
        if (!dayState.points.length) { dayTip.style.display = 'none'; return; }
        let best=null, bestDist=Infinity;
        dayState.points.forEach(pt=>{
          const d2=(pt.x-x)*(pt.x-x)+(pt.y-y)*(pt.y-y);
          if(d2<bestDist){bestDist=d2; best=pt;}
        });
        const hitRadius = 20;
        const hitDist2 = hitRadius * hitRadius;
        if (best && bestDist <= hitDist2) {
          const date=new Date(best.ts);
          const timeStr=String(date.getHours()).padStart(2,'0')+':'+String(date.getMinutes()).padStart(2,'0')+':'+String(date.getSeconds()).padStart(2,'0');
          const stableHit = dayState.stableSegments.find(seg => best.ts >= seg.startTs && best.ts <= seg.endTs && Math.abs(seg.value - best.value) <= STALE_VALUE_EPS);
          const stableText = stableHit ? `<br/>稳定: ${formatTime(stableHit.startTs)}~${formatTime(stableHit.endTs)} (${formatDuration(stableHit.durationMs)})` : '';
          const arpingText = `<br/>${formatArpingStatus(best.arping_ok)}`;
          const coordText = `X: ${timeStr}<br/>Y: ${best.value} dBm`;
          dayTip.innerHTML = `${best.label || '未命名'}<br/>${coordText}${stableText}${arpingText}`;
          dayTip.style.left = best.x + 'px';
          dayTip.style.top = best.y + 'px';
          dayTip.style.display = 'block';
        } else { dayTip.style.display = 'none'; }
      });
      historyWrap.addEventListener('mouseleave', ()=>{ dayTip.style.display = 'none'; });
      loadDay();
      setDayZoom(1, historyWrap);
    };
  </script>
</head>
<body>
  <div class="page">
    <div class="header">
      <h1>历史天视图</h1>
      <div style="display:flex; gap:12px; flex-wrap:wrap;">
        <a class="link" href="/">返回监控 →</a>
        <a class="link" href="/table">事件表格 →</a>
      </div>
    </div>

    <div class="card">
      <div class="card-title">选择日期</div>
      <div class="controls">
        <input id="dateInput" type="date" />
        <button id="btnToday" type="button">今天</button>
        <button id="btnYesterday" type="button">昨天</button>
        <button id="loadDay" type="button">加载</button>
        <span class="hint">可查看当日 24 小时曲线，滚轮缩放聚焦细节。</span>
      </div>
      <div class="controls" style="margin-top:6px;">
        <span class="hint">时间范围</span>
        <input id="timeStart" type="time" />
        <span class="hint">~</span>
        <input id="timeEnd" type="time" />
        <button id="btnFullDay" type="button">全日</button>
        <span class="hint">缩短横轴，便于观察细节。</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">全天曲线</div>
      <div class="chart-wrap" id="historyChartWrap">
        <canvas id="historyChart" height="440"></canvas>
        <div class="tooltip" id="historyTooltip"></div>
      </div>
      <div class="legend" id="historyLegend"></div>
      <div class="controls" style="margin-top:8px;">
        <label for="dayRange">纵坐标范围 (dBm)</label>
        <input id="dayRange" type="number" min="5" step="1" value="90" />
        <label for="dayBase">纵轴起点</label>
        <input id="dayBase" type="number" min="0" step="1" value="0" />
        <label for="historyZoom">缩放</label>
        <input id="historyZoom" type="range" min="1" max="8" step="0.1" value="1" />
        <span class="hint" id="historyZoomVal">1.0x</span>
      </div>
    </div>
  </div>
</body>
</html>"""


def start_server() -> None:
    global HISTORY_SERVER, HISTORY_SERVER_THREAD
    if not config.ENABLE_HISTORY_WEB or HISTORY_SERVER is not None:
        return
    try:
        server = ThreadingHTTPServer(("0.0.0.0", config.HISTORY_WEB_PORT), _HistoryRequestHandler)
    except Exception as exc:
        print(f"[{ts()}] ???????????{exc}")
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    HISTORY_SERVER = server
    HISTORY_SERVER_THREAD = thread
    print(f"[INFO] ??????????http://localhost:{config.HISTORY_WEB_PORT}/")


def stop_server() -> None:
    global HISTORY_SERVER, HISTORY_SERVER_THREAD
    server = HISTORY_SERVER
    thread = HISTORY_SERVER_THREAD
    HISTORY_SERVER = None
    HISTORY_SERVER_THREAD = None
    if server:
        server.shutdown()
        server.server_close()
    if thread:
        thread.join(timeout=2)


def _initialize_history() -> None:
    global LAST_SAVE_TS, UNSAVED_CHANGES
    with TARGET_HISTORY_LOCK:
        PERSISTED_HISTORY[:] = load_history()
        prune_history(PERSISTED_HISTORY)
        save_history(PERSISTED_HISTORY)
        TARGET_HISTORY[:] = list(PERSISTED_HISTORY)
        prune_recent_history(TARGET_HISTORY)
        LAST_SAVE_TS = time.monotonic()
        UNSAVED_CHANGES = False


def _flush_history_on_exit() -> None:
    with TARGET_HISTORY_LOCK:
        _save_history_locked(force=True)


_initialize_history()
atexit.register(_flush_history_on_exit)
