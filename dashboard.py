"""Read-only closed-loop RAN dashboard.

Serves local status from the latest audit JSONL and config.yaml.
Does not modify the loop, simulator, or diagnosis code.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.yaml"

KNOWN_FAULTS = [
    "ul_interference", "overshoot", "congestion", "combined",
    "sleeping_cell", "pim", "multiple", "unknown",
]

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
if not DASHBOARD_PASSWORD:
    raise RuntimeError(
        "DASHBOARD_PASSWORD is not set. Add it to .env "
        "(see .env.example) and ensure the systemd unit loads "
        "EnvironmentFile=/home/milebase/closed-loop-ran/.env"
    )

security = HTTPBasic()


def require_password(credentials: HTTPBasicCredentials = Depends(security)):
    """Username is ignored; only the password must match DASHBOARD_PASSWORD."""
    try:
        ok = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


app = FastAPI(title="Closed-Loop RAN Dashboard", docs_url=None, redoc_url=None)


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def expected_cells(cfg):
    sites = int(cfg.get("simulator", {}).get("sites", 8))
    return [f"SITE{s:03d}_{sec}" for s in range(1, sites + 1) for sec in "ABC"]


def cells_for_run(events, cfg):
    """Prefer the run's meta cell inventory (real PM labels) over the simulator template."""
    for ev in events:
        if ev.get("stage") == "meta" and ev.get("cells"):
            return list(ev["cells"])
    return expected_cells(cfg)


def list_run_logs():
    if not LOG_DIR.is_dir():
        return []
    return sorted(LOG_DIR.glob("run_*.jsonl"))


def parse_run_timestamp(path: Path):
    """Parse run_YYYYMMDD_HHMMSS.jsonl filename into aware datetime."""
    stem = path.stem  # run_20260929_155003
    try:
        return datetime.strptime(stem[4:], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def read_events(path: Path):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def fault_of(event):
    """Normalize fault_type (act/review/escalate) and fault (verify) to one field."""
    return event.get("fault_type") or event.get("fault") or "unknown"


def build_cell_status(events, cells):
    """Last event per cell across the whole file (file order = cycle order).

    Stages written by main.py: review, act, verify, escalate (no DETECT).
    """
    last = {}  # cell -> event
    for ev in events:
        stage = ev.get("stage")
        cell = ev.get("cell")
        if cell and stage in ("review", "act", "verify", "escalate"):
            last[cell] = ev

    ordered = []
    for c in cells:
        ev = last.get(c)
        if ev is None:
            ordered.append({"cell": c, "status": "healthy", "fault_type": None, "detail": "no events"})
            continue
        stage = ev.get("stage")
        ft = fault_of(ev)
        if stage == "escalate":
            ordered.append({"cell": c, "status": "escalated", "fault_type": ft,
                            "detail": "escalated"})
        elif stage == "verify" and ev.get("resolved"):
            ordered.append({"cell": c, "status": "healthy", "fault_type": None,
                            "detail": "resolved"})
        elif stage == "verify" and not ev.get("resolved"):
            ordered.append({"cell": c, "status": "unresolved", "fault_type": ft,
                            "detail": "not resolved, retrying"})
        elif stage == "act":
            ordered.append({"cell": c, "status": "awaiting_verify", "fault_type": ft,
                            "detail": "action taken, awaiting verify"})
        elif stage == "review":
            ordered.append({"cell": c, "status": "pending_review", "fault_type": ft,
                            "detail": "pending human review"})
        else:
            ordered.append({"cell": c, "status": "faulted", "fault_type": ft,
                            "detail": stage or "unknown"})
    # Any cells that appeared in the log but are outside the expected 24
    for c, ev in last.items():
        if c in cells:
            continue
        ordered.append({"cell": c, "status": "faulted", "fault_type": fault_of(ev),
                        "detail": ev.get("stage")})
    return ordered


def build_recent_actions(events, limit=20):
    """act / verify / escalate / review, newest first by ts then file order."""
    rows = []
    for idx, ev in enumerate(events):
        stage = ev.get("stage")
        if stage not in ("act", "verify", "escalate", "review"):
            continue
        fault = fault_of(ev)
        if stage == "act":
            action = ev.get("cmd") or "action proposed"
            outcome = "pending" if ev.get("approved") else "rejected"
        elif stage == "verify":
            action = f"verify {ev.get('kpi', '')}".strip()
            outcome = "resolved" if ev.get("resolved") else "unresolved"
        elif stage == "escalate":
            action = "escalate to engineer"
            outcome = "escalated"
        else:  # review
            action = "queued for engineer review"
            outcome = "pending_review"
        rows.append({
            "ts": ev.get("ts"),
            "cell": ev.get("cell"),
            "fault_type": fault,
            "action": action,
            "outcome": outcome,
            "stage": stage,
            "_ord": idx,
        })
    # Sort by simulated ts descending; stable within same ts via file order
    rows.sort(key=lambda r: (r["ts"] or "", r["_ord"]), reverse=True)
    out = []
    for r in rows[:limit]:
        r.pop("_ord", None)
        out.append(r)
    return out


def build_status():
    cfg = load_config()
    auto = list(cfg.get("loop", {}).get("auto_approve_faults", []) or [])
    logs = list_run_logs()
    if not logs:
        cells = expected_cells(cfg)
        return {
            "cells": [{"cell": c, "status": "healthy", "fault_type": None} for c in cells],
            "recent_actions": [],
            "auto_approve_faults": auto,
            "level3_faults": [f for f in KNOWN_FAULTS if f not in auto],
            "uptime_seconds": 0,
            "uptime": "no runs yet",
            "last_run": None,
            "log_file": None,
        }

    latest = logs[-1]
    earliest = logs[0]
    events = read_events(latest)
    cells = cells_for_run(events, cfg)
    now = datetime.now(timezone.utc)
    first_ts = parse_run_timestamp(earliest)
    last_ts = parse_run_timestamp(latest)
    uptime_s = max(0, int((now - first_ts).total_seconds()))

    def fmt_uptime(seconds):
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        return " ".join(parts)

    return {
        "cells": build_cell_status(events, cells),
        "recent_actions": build_recent_actions(events),
        "auto_approve_faults": auto,
        "level3_faults": [f for f in KNOWN_FAULTS if f not in auto],
        "uptime_seconds": uptime_s,
        "uptime": fmt_uptime(uptime_s),
        "last_run": last_ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "log_file": latest.name,
    }


@app.get("/api/status")
def api_status(_: bool = Depends(require_password)):
    return build_status()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Closed-Loop RAN Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --ink: #e6edf3;
    --muted: #8b949e;
    --line: #30363d;
    --green: #3fb950;
    --green-bg: #12261a;
    --green-border: #238636;
    --yellow: #d29922;
    --yellow-bg: #2a2111;
    --yellow-border: #9e6a03;
    --red: #f85149;
    --red-bg: #2d1214;
    --red-border: #da3633;
    --row-alt: #1c2128;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "IBM Plex Sans", "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--ink);
    line-height: 1.4;
  }
  header {
    padding: 1.25rem 1.5rem 1rem;
    border-bottom: 1px solid var(--line);
    background: var(--panel);
  }
  header h1 {
    margin: 0 0 0.35rem;
    font-size: 1.35rem;
    font-weight: 650;
    letter-spacing: -0.02em;
    color: var(--ink);
  }
  header .meta {
    color: var(--muted);
    font-size: 0.92rem;
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
  }
  header .meta strong { color: var(--ink); font-weight: 600; }
  main {
    padding: 1.25rem 1.5rem 2rem;
    display: grid;
    gap: 1.25rem;
  }
  section {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 1rem 1.1rem 1.15rem;
  }
  section h2 {
    margin: 0 0 0.75rem;
    font-size: 1rem;
    font-weight: 600;
    color: var(--ink);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(7.5rem, 1fr));
    gap: 0.5rem;
  }
  .cell {
    border-radius: 4px;
    padding: 0.55rem 0.5rem;
    border: 1px solid transparent;
    min-height: 3.6rem;
  }
  .cell .name { font-weight: 600; font-size: 0.82rem; }
  .cell .fault { font-size: 0.72rem; margin-top: 0.2rem; word-break: break-word; opacity: 0.92; }
  .cell.healthy { background: var(--green-bg); color: var(--green); border-color: var(--green-border); }
  .cell.faulted, .cell.pending_review, .cell.awaiting_verify, .cell.unresolved {
    background: var(--yellow-bg); color: var(--yellow); border-color: var(--yellow-border);
  }
  .cell.escalated { background: var(--red-bg); color: var(--red); border-color: var(--red-border); }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.88rem;
    background: var(--panel);
  }
  th, td {
    text-align: left;
    padding: 0.45rem 0.5rem;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
  }
  th { color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }
  tbody tr:nth-child(even) { background: var(--row-alt); }
  td.action { max-width: 28rem; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem; color: var(--muted); }
  code { color: var(--ink); }
  .pill {
    display: inline-block;
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .pill.resolved, .pill.healthy { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
  .pill.pending, .pill.unresolved, .pill.faulted, .pill.pending_review, .pill.awaiting_verify, .pill.rejected {
    background: var(--yellow-bg); color: var(--yellow); border: 1px solid var(--yellow-border);
  }
  .pill.escalated { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }
  .policy {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }
  @media (max-width: 700px) { .policy { grid-template-columns: 1fr; } }
  .policy h3 { color: var(--muted); }
  .policy ul { margin: 0; padding-left: 1.1rem; }
  .policy li { margin: 0.2rem 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.85rem; color: var(--ink); }
  .err { color: var(--red); padding: 1rem; }
  .note { color: var(--muted); font-size: 0.82rem; margin: 0 0 0.75rem; }
</style>
</head>
<body>
<header>
  <h1>Closed-Loop RAN</h1>
  <div class="meta">
    <span>Uptime: <strong id="uptime">—</strong></span>
    <span>Last run: <strong id="last-run">—</strong></span>
    <span>Log: <strong id="log-file">—</strong></span>
    <span>Refresh: 15s</span>
  </div>
</header>
<main>
  <section>
    <h2>Cells</h2>
    <p class="note">Green = healthy · Yellow = pending review / awaiting verify / not resolved · Red = escalated</p>
    <div class="grid" id="cell-grid"></div>
  </section>
  <section>
    <h2>Recent actions</h2>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr><th>Time</th><th>Cell</th><th>Fault</th><th>Action</th><th>Outcome</th></tr>
        </thead>
        <tbody id="actions-body"></tbody>
      </table>
    </div>
  </section>
  <section>
    <h2>Approval policy</h2>
    <div class="policy">
      <div>
        <h3 style="margin:0 0 0.4rem;font-size:0.9rem">Level 4 (auto-approve)</h3>
        <ul id="level4"></ul>
      </div>
      <div>
        <h3 style="margin:0 0 0.4rem;font-size:0.9rem">Level 3 (human / review)</h3>
        <ul id="level3"></ul>
      </div>
    </div>
  </section>
</main>
<script>
async function refresh() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    document.getElementById('uptime').textContent = data.uptime || '—';
    document.getElementById('last-run').textContent = data.last_run || '—';
    document.getElementById('log-file').textContent = data.log_file || '—';

    const grid = document.getElementById('cell-grid');
    grid.innerHTML = '';
    (data.cells || []).forEach(c => {
      const el = document.createElement('div');
      el.className = 'cell ' + (c.status || 'healthy');
      const label = c.fault_type
        ? (c.fault_type + (c.detail ? ' · ' + c.detail : ''))
        : (c.detail || c.status || 'healthy');
      el.innerHTML = '<div class="name">' + c.cell + '</div><div class="fault">' + label + '</div>';
      grid.appendChild(el);
    });

    const tbody = document.getElementById('actions-body');
    tbody.innerHTML = '';
    const actions = data.recent_actions || [];
    if (!actions.length) {
      tbody.innerHTML = '<tr><td colspan="5">No act/verify/escalate events in the latest log.</td></tr>';
    } else {
      actions.forEach(a => {
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td>' + (a.ts || '') + '</td>' +
          '<td>' + (a.cell || '') + '</td>' +
          '<td><code>' + (a.fault_type || '') + '</code></td>' +
          '<td class="action">' + (a.action || '') + '</td>' +
          '<td><span class="pill ' + (a.outcome || '') + '">' + (a.outcome || '') + '</span></td>';
        tbody.appendChild(tr);
      });
    }

    const l4 = document.getElementById('level4');
    const l3 = document.getElementById('level3');
    l4.innerHTML = '';
    l3.innerHTML = '';
    (data.auto_approve_faults || []).forEach(f => {
      const li = document.createElement('li'); li.textContent = f; l4.appendChild(li);
    });
    if (!(data.auto_approve_faults || []).length) {
      l4.innerHTML = '<li>(none)</li>';
    }
    (data.level3_faults || []).forEach(f => {
      const li = document.createElement('li'); li.textContent = f; l3.appendChild(li);
    });
  } catch (e) {
    document.getElementById('cell-grid').innerHTML =
      '<div class="err">Failed to load /api/status: ' + e.message + '</div>';
  }
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index(_: bool = Depends(require_password)):
    return DASHBOARD_HTML
