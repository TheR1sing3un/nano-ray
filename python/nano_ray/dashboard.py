"""Simple Web Dashboard for nano-ray.

Provides a lightweight HTTP server that serves:
- A single-page dashboard (HTML + JS) for visualizing cluster state
- A JSON API (/api/metrics) for real-time metrics polling

The dashboard shows:
- Task statistics (submitted, completed, failed)
- Worker status (connected count)
- Object store statistics
- Recent task timeline

SIMPLIFICATION: Ray's dashboard is a full React application with
multiple backend services. nano-ray uses a single-file HTML dashboard
served by Python's built-in http.server, keeping dependencies minimal.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# Metrics collector singleton
_metrics: DashboardMetrics | None = None


class DashboardMetrics:
    """Thread-safe metrics collector for dashboard display."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tasks_submitted = 0
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.tasks_running = 0
        self.objects_stored = 0
        self.worker_count = 0
        self.start_time = time.time()
        # Recent task completions: list of (timestamp, task_id, duration_ms)
        self._recent_completions: list[tuple[float, int, float]] = []
        # Task submission timestamps for latency calculation
        self._task_submit_times: dict[int, float] = {}

    def on_task_submitted(self, task_id: int) -> None:
        with self._lock:
            self.tasks_submitted += 1
            self.tasks_running += 1
            self._task_submit_times[task_id] = time.time()

    def on_task_completed(self, task_id: int) -> None:
        with self._lock:
            self.tasks_completed += 1
            self.tasks_running = max(0, self.tasks_running - 1)
            submit_time = self._task_submit_times.pop(task_id, None)
            if submit_time is not None:
                duration_ms = (time.time() - submit_time) * 1000
                self._recent_completions.append(
                    (time.time(), task_id, duration_ms)
                )
                # Keep only last 100 completions
                if len(self._recent_completions) > 100:
                    self._recent_completions = self._recent_completions[-100:]

    def on_task_failed(self, task_id: int) -> None:
        with self._lock:
            self.tasks_failed += 1
            self.tasks_running = max(0, self.tasks_running - 1)
            self._task_submit_times.pop(task_id, None)

    def set_worker_count(self, count: int) -> None:
        with self._lock:
            self.worker_count = count

    def set_objects_stored(self, count: int) -> None:
        with self._lock:
            self.objects_stored = count

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            # Compute latency histogram buckets
            latencies = [c[2] for c in self._recent_completions[-50:]]
            latency_histogram = _compute_histogram(latencies)

            return {
                "uptime_seconds": round(time.time() - self.start_time, 1),
                "tasks": {
                    "submitted": self.tasks_submitted,
                    "completed": self.tasks_completed,
                    "failed": self.tasks_failed,
                    "running": self.tasks_running,
                    "pending": max(
                        0,
                        self.tasks_submitted
                        - self.tasks_completed
                        - self.tasks_failed
                        - self.tasks_running,
                    ),
                },
                "objects_stored": self.objects_stored,
                "worker_count": self.worker_count,
                "latency_histogram": latency_histogram,
                "recent_completions": [
                    {"ts": round(c[0], 3), "task_id": c[1], "ms": round(c[2], 2)}
                    for c in self._recent_completions[-20:]
                ],
            }


def _compute_histogram(values: list[float]) -> dict[str, int]:
    """Compute a simple histogram of latency values."""
    if not values:
        return {}
    buckets = {"<1ms": 0, "1-5ms": 0, "5-20ms": 0, "20-100ms": 0, ">100ms": 0}
    for v in values:
        if v < 1:
            buckets["<1ms"] += 1
        elif v < 5:
            buckets["1-5ms"] += 1
        elif v < 20:
            buckets["5-20ms"] += 1
        elif v < 100:
            buckets["20-100ms"] += 1
        else:
            buckets[">100ms"] += 1
    return buckets


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>nano-ray Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0; padding: 24px;
  }
  h1 { font-size: 24px; margin-bottom: 8px; color: #38bdf8; }
  .subtitle { color: #64748b; margin-bottom: 24px; font-size: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card {
    background: #1e293b; border-radius: 12px; padding: 20px;
    border: 1px solid #334155;
  }
  .card-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }
  .card-value { font-size: 32px; font-weight: 700; margin-top: 4px; }
  .card-value.green { color: #4ade80; }
  .card-value.blue { color: #38bdf8; }
  .card-value.red { color: #f87171; }
  .card-value.yellow { color: #fbbf24; }
  .card-value.purple { color: #a78bfa; }
  .section { background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; margin-bottom: 24px; }
  .section h2 { font-size: 16px; color: #94a3b8; margin-bottom: 16px; }
  .bar-chart { display: flex; align-items: flex-end; gap: 8px; height: 120px; }
  .bar-group { display: flex; flex-direction: column; align-items: center; flex: 1; }
  .bar {
    background: #38bdf8; border-radius: 4px 4px 0 0; width: 100%;
    min-height: 2px; transition: height 0.3s;
  }
  .bar-label { font-size: 11px; color: #64748b; margin-top: 6px; white-space: nowrap; }
  .bar-count { font-size: 11px; color: #94a3b8; margin-bottom: 4px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px; font-size: 12px; color: #64748b; border-bottom: 1px solid #334155; }
  td { padding: 8px; font-size: 13px; border-bottom: 1px solid #1e293b; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .status-dot.ok { background: #4ade80; }
  .footer { text-align: center; color: #475569; font-size: 12px; margin-top: 32px; }
</style>
</head>
<body>
  <h1>nano-ray Dashboard</h1>
  <p class="subtitle">Cluster monitoring &mdash; auto-refreshes every 1s</p>

  <div class="grid">
    <div class="card">
      <div class="card-label">Tasks Submitted</div>
      <div class="card-value blue" id="submitted">0</div>
    </div>
    <div class="card">
      <div class="card-label">Tasks Completed</div>
      <div class="card-value green" id="completed">0</div>
    </div>
    <div class="card">
      <div class="card-label">Tasks Failed</div>
      <div class="card-value red" id="failed">0</div>
    </div>
    <div class="card">
      <div class="card-label">Tasks Running</div>
      <div class="card-value yellow" id="running">0</div>
    </div>
    <div class="card">
      <div class="card-label">Workers</div>
      <div class="card-value purple" id="workers">0</div>
    </div>
    <div class="card">
      <div class="card-label">Objects Stored</div>
      <div class="card-value blue" id="objects">0</div>
    </div>
    <div class="card">
      <div class="card-label">Uptime</div>
      <div class="card-value" id="uptime" style="font-size:24px">0s</div>
    </div>
  </div>

  <div class="section">
    <h2>Task Latency Distribution</h2>
    <div class="bar-chart" id="histogram"></div>
  </div>

  <div class="section">
    <h2>Recent Completions</h2>
    <table>
      <thead><tr><th>Task ID</th><th>Latency</th><th>Time</th></tr></thead>
      <tbody id="recent"></tbody>
    </table>
  </div>

  <div class="footer">nano-ray &mdash; educational distributed computing framework</div>

<script>
function formatUptime(s) {
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + Math.round(s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}

function update() {
  fetch('/api/metrics')
    .then(r => r.json())
    .then(d => {
      document.getElementById('submitted').textContent = d.tasks.submitted;
      document.getElementById('completed').textContent = d.tasks.completed;
      document.getElementById('failed').textContent = d.tasks.failed;
      document.getElementById('running').textContent = d.tasks.running;
      document.getElementById('workers').textContent = d.worker_count;
      document.getElementById('objects').textContent = d.objects_stored;
      document.getElementById('uptime').textContent = formatUptime(d.uptime_seconds);

      // Histogram
      const hist = d.latency_histogram || {};
      const buckets = ['<1ms','1-5ms','5-20ms','20-100ms','>100ms'];
      const maxVal = Math.max(1, ...buckets.map(b => hist[b] || 0));
      const chartEl = document.getElementById('histogram');
      chartEl.innerHTML = buckets.map(b => {
        const v = hist[b] || 0;
        const h = Math.max(2, (v / maxVal) * 100);
        return '<div class="bar-group">'
          + '<div class="bar-count">' + v + '</div>'
          + '<div class="bar" style="height:' + h + 'px"></div>'
          + '<div class="bar-label">' + b + '</div></div>';
      }).join('');

      // Recent completions
      const tbody = document.getElementById('recent');
      tbody.innerHTML = (d.recent_completions || []).reverse().map(c => {
        const t = new Date(c.ts * 1000).toLocaleTimeString();
        return '<tr><td>' + c.task_id + '</td><td>' + c.ms.toFixed(1) + ' ms</td><td>' + t + '</td></tr>';
      }).join('');
    })
    .catch(() => {});
}

update();
setInterval(update, 1000);
</script>
</body>
</html>
"""


class _DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard."""

    def do_GET(self) -> None:
        if self.path == "/api/metrics":
            self._serve_metrics()
        elif self.path == "/" or self.path == "/dashboard":
            self._serve_html()
        else:
            self.send_error(404)

    def _serve_html(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DASHBOARD_HTML.encode("utf-8"))

    def _serve_metrics(self) -> None:
        global _metrics
        data = _metrics.to_dict() if _metrics else {}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress HTTP access logs


def start_dashboard(
    metrics: DashboardMetrics,
    host: str = "127.0.0.1",
    port: int = 8265,
) -> HTTPServer:
    """Start the dashboard HTTP server in a background thread.

    Args:
        metrics: Shared metrics collector instance.
        host: Host to bind to.
        port: Port to bind to (default 8265, same as Ray's dashboard).

    Returns:
        The HTTPServer instance (for shutdown).
    """
    global _metrics
    _metrics = metrics

    server = HTTPServer((host, port), _DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
