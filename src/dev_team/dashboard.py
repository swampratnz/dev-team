"""A local web dashboard over a dev-team workspace.

``dev-team --dashboard --workspace DIR`` serves a single self-contained page
showing what the team is doing: every agent's last activity (from the event
journal ``.dev_team/events.jsonl``), recent runs, the persistent backlog,
cross-run memory (retrospectives and ADRs), captured conventions, and the
assessment reports on disk.

The server is deliberately boring: stdlib ``http.server``, no dependencies,
read-only over the workspace, bound to localhost by default. It reads shared
state from disk on every request, so it runs happily as a separate process
next to (or without) an active delivery — start it once and leave it open.
It serves live state, not secrets — but anything in the workspace is
readable through it, so keep the bind address local unless the host is
trusted.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from .backlog import BacklogStore
from .conventions import ConventionsStore
from .eventlog import read_events
from .execution import Workspace
from .memory import ProjectMemory
from .persona import DEFAULT_CAST

#: Engine-level event roles that are not agents (they feed the feed, not cards).
_ENGINE_ROLES = frozenset({"workflow", "delivery", "assessment", "chat"})

#: How many feed entries / runs the state payload carries.
_FEED_LIMIT = 60
_RUN_LIMIT = 10


def _agent_cards(events: List[Dict]) -> List[Dict]:
    """One card per role: the default cast plus any extra role seen."""

    last_by_role: Dict[str, Dict] = {}
    for event in events:
        role = str(event.get("role", ""))
        if role and role not in _ENGINE_ROLES:
            last_by_role[role] = event
    roles = list(DEFAULT_CAST) + [
        role for role in sorted(last_by_role) if role not in DEFAULT_CAST
    ]
    cards = []
    for role in roles:
        persona = DEFAULT_CAST.get(role)
        event = last_by_role.get(role)
        cards.append(
            {
                "role": role,
                "name": (event or {}).get("name") or (persona.name if persona else None),
                "last": event,
            }
        )
    return cards


def _run_summaries(events: List[Dict]) -> List[Dict]:
    """Newest-first summaries of the runs present in the journal."""

    runs: Dict[str, Dict] = {}
    for event in events:
        run_id = str(event.get("run", ""))
        summary = runs.setdefault(
            run_id,
            {"id": run_id, "started": event.get("ts"), "events": 0},
        )
        summary["events"] += 1
        summary["ended"] = event.get("ts")
        summary["last_message"] = event.get("message")
        summary["last_stage"] = event.get("stage")
    ordered = sorted(runs.values(), key=lambda r: r.get("ended") or 0)
    return list(reversed(ordered))[:_RUN_LIMIT]


def _backlog_state(workspace: Workspace) -> Dict:
    backlog = BacklogStore(workspace).load()
    counts = {"todo": 0, "in_progress": 0, "done": 0, "blocked": 0}
    by_epic: Dict[Optional[str], List[Dict]] = {}
    for story in backlog.stories:
        status = story.status.value
        counts[status] = counts.get(status, 0) + 1
        by_epic.setdefault(story.epic_id, []).append(
            {
                "id": story.id,
                "title": story.title,
                "status": status,
                "estimate": story.estimate,
            }
        )
    epics = []
    for epic in backlog.epics:
        stories = by_epic.pop(epic.id, [])
        total = sum(s["estimate"] for s in stories)
        done = sum(s["estimate"] for s in stories if s["status"] == "done")
        epics.append(
            {
                "id": epic.id,
                "title": epic.title,
                "description": epic.description,
                "stories": stories,
                "points_total": total,
                "points_done": done,
            }
        )
    orphans = [story for stories in by_epic.values() for story in stories]
    return {
        "present": bool(backlog.stories or backlog.epics),
        "counts": counts,
        "epics": epics,
        "orphan_stories": orphans,
    }


def _memory_state(workspace: Workspace) -> Dict:
    memory = ProjectMemory(workspace).load()
    if memory is None:
        return {"present": False, "runs": 0, "retrospectives": [], "decisions": []}
    entries = memory.get("entries") or {}
    retro = [str(note) for note in (entries.get("retrospective") or [])[-6:]]
    decisions = [
        {"id": str(d.get("id", "")), "title": str(d.get("title", ""))}
        for d in (memory.get("decisions") or [])[-6:]
        if isinstance(d, dict)
    ]
    return {
        "present": True,
        "runs": int(memory.get("runs", 0)),
        "retrospectives": list(reversed(retro)),
        "decisions": list(reversed(decisions)),
    }


def _conventions_state(workspace: Workspace) -> Dict:
    profile = ConventionsStore(workspace).load()
    if profile is None:
        return {"present": False, "summary": ""}
    return {"present": True, "summary": profile.summary}


def _report_paths(workspace: Workspace) -> List[str]:
    return sorted(
        path
        for path in workspace.list_files()
        if path.endswith(".md")
        and (path.startswith("audit/") or "/audit/" in path)
    )


def collect_state(
    workspace: Workspace, *, clock: Callable[[], float] = time.time
) -> Dict:
    """Everything the dashboard shows, freshly read from the workspace."""

    events = read_events(workspace)
    root = getattr(workspace, "root", None)
    return {
        "generated_at": clock(),
        "workspace": str(root) if root is not None else "(in-memory)",
        "agents": _agent_cards(events),
        "activity": list(reversed(events[-_FEED_LIMIT:])),
        "runs": _run_summaries(events),
        "backlog": _backlog_state(workspace),
        "memory": _memory_state(workspace),
        "conventions": _conventions_state(workspace),
        "reports": _report_paths(workspace),
    }


def _make_handler(workspace: Workspace) -> type:
    """A request handler class bound to ``workspace``."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            """Silence per-request stderr noise; the CLI prints the URL once."""

        def _send(self, status: int, content_type: str, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            parts = urlsplit(self.path)
            if parts.path == "/":
                self._send(200, "text/html", DASHBOARD_HTML)
            elif parts.path == "/api/state":
                self._send(200, "application/json", json.dumps(collect_state(workspace)))
            elif parts.path == "/api/report":
                self._report(parts.query)
            else:
                self._send(404, "text/plain", "not found")

        def _report(self, query: str) -> None:
            requested = parse_qs(query).get("path", [""])[0]
            # Membership in the workspace listing is the traversal guard:
            # only real, workspace-relative markdown files are served.
            if requested not in _report_paths(workspace):
                self._send(404, "text/plain", "unknown report")
                return
            self._send(200, "text/plain", workspace.read_text(requested))

    return Handler


class DashboardServer:
    """The dashboard HTTP server; read-only over one workspace."""

    def __init__(
        self, workspace: Workspace, *, host: str = "127.0.0.1", port: int = 8737
    ) -> None:
        self.workspace = workspace
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(workspace))

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------
# The page. Self-contained: inline CSS/JS, no external requests. Status
# colors are the validated status palette (good/critical) plus the
# categorical slot-1 blue for "active"; text always carries the label, so
# color never means anything alone.
# --------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dev-team dashboard</title>
<style>
:root {
  --surface: #fcfcfb; --card: #ffffff; --line: #e5e4e0;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #8a8983;
  --accent: #2a78d6; --good: #0ca30c; --critical: #d03b3b;
  --warning: #fab219; --track: #ececea;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #1a1a19; --card: #232322; --line: #3a3936;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #8a8983;
    --accent: #3987e5; --track: #33332f;
  }
}
* { box-sizing: border-box; margin: 0; }
body {
  background: var(--surface); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 20px; max-width: 1180px; margin: 0 auto;
}
header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
header h1 { font-size: 19px; }
header .ws { color: var(--ink-3); font-family: ui-monospace, monospace; font-size: 12px; }
header .beat { margin-left: auto; color: var(--ink-3); font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 18px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; }
.tile .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.tile .k { color: var(--ink-2); font-size: 12px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-2); margin: 18px 0 8px; }
.agents { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
.agent { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.agent .who { display: flex; gap: 8px; align-items: baseline; }
.agent .name { font-weight: 650; }
.agent .role { color: var(--ink-3); font-size: 12px; }
.agent .msg { margin-top: 6px; color: var(--ink-2); font-size: 13px; min-height: 2.6em; }
.agent .meta { margin-top: 6px; display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--ink-3); }
.chip { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
        border: 1px solid var(--line); color: var(--ink-2); background: transparent; }
.chip.active  { border-color: var(--accent);  color: var(--accent); }
.chip.done    { border-color: var(--good);    color: var(--good); }
.chip.blocked { border-color: var(--critical);color: var(--critical); }
.chip.idle    { border-style: dashed; }
.cols { display: grid; grid-template-columns: minmax(320px, 3fr) minmax(320px, 2fr); gap: 16px; align-items: start; }
@media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.feed { list-style: none; max-height: 480px; overflow-y: auto; }
.feed li { padding: 6px 0; border-top: 1px solid var(--line); display: grid;
           grid-template-columns: 66px 1fr auto; gap: 10px; font-size: 13px; }
.feed li:first-child { border-top: 0; }
.feed .who { color: var(--accent); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.feed .what { color: var(--ink-2); }
.feed .when { color: var(--ink-3); font-size: 12px; white-space: nowrap; }
.epic { margin-bottom: 14px; }
.epic .t { display: flex; justify-content: space-between; gap: 8px; font-weight: 650; }
.epic .pts { color: var(--ink-3); font-weight: 400; font-size: 12px; }
.bar { height: 6px; background: var(--track); border-radius: 3px; margin: 6px 0; overflow: hidden; }
.bar b { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
.story { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; font-size: 13px; }
.story .st { flex: 1; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.story .pts { color: var(--ink-3); font-size: 12px; }
.muted { color: var(--ink-3); font-size: 13px; }
.list { list-style: none; }
.list li { padding: 4px 0; border-top: 1px solid var(--line); font-size: 13px; color: var(--ink-2); }
.list li:first-child { border-top: 0; }
a.report { color: var(--accent); cursor: pointer; text-decoration: underline; font-size: 13px; }
#reportview { display: none; margin-top: 10px; }
#reportview pre { background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
                  padding: 12px; overflow-x: auto; font-size: 12px; max-height: 420px; overflow-y: auto;
                  white-space: pre-wrap; }
.section-gap { margin-top: 16px; }
</style>
</head>
<body>
<header>
  <h1>dev-team</h1>
  <span class="ws" id="ws"></span>
  <span class="beat" id="beat">connecting…</span>
</header>
<div class="tiles" id="tiles"></div>
<h2>The team</h2>
<div class="agents" id="agents"></div>
<div class="cols section-gap">
  <div>
    <h2>Activity</h2>
    <div class="panel"><ul class="feed" id="feed"><li class="muted">no events yet</li></ul></div>
    <h2>Runs</h2>
    <div class="panel"><ul class="list" id="runs"><li class="muted">no runs recorded</li></ul></div>
  </div>
  <div>
    <h2>Backlog</h2>
    <div class="panel" id="backlog"><span class="muted">no backlog yet</span></div>
    <h2>Memory</h2>
    <div class="panel" id="memory"><span class="muted">no cross-run memory yet</span></div>
    <h2>Reports</h2>
    <div class="panel" id="reports"><span class="muted">no assessment reports</span></div>
    <div id="reportview"><pre id="reportbody"></pre></div>
  </div>
</div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = ts => {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
};
const chip = (label, cls) => `<span class="chip ${cls}">${esc(label)}</span>`;
const storyChip = st => ({
  done: chip("\\u2713 done", "done"),
  in_progress: chip("\\u25B6 in progress", "active"),
  blocked: chip("\\u2715 blocked", "blocked"),
  todo: chip("todo", ""),
}[st] || chip(st, ""));

function tiles(s) {
  const c = s.backlog.counts;
  const open = c.todo + c.in_progress + c.blocked;
  const latest = s.activity.length ? s.activity[0].ts : null;
  document.getElementById("tiles").innerHTML = [
    ["recent runs", s.runs.length],
    ["stories open", open],
    ["stories done", c.done],
    ["stories blocked", c.blocked],
    ["last activity", latest ? ago(latest) : "\\u2014"],
  ].map(([k, v]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
}

function agents(s) {
  document.getElementById("agents").innerHTML = s.agents.map(a => {
    const last = a.last;
    const status = last ? chip(esc(last.stage), "active") : chip("idle", "idle");
    const msg = last ? esc(last.message) + (last.detail ? ` <span class="muted">(${esc(last.detail)})</span>` : "") : "no recorded activity";
    return `<div class="agent">
      <div class="who"><span class="name">${esc(a.name || a.role)}</span><span class="role">${esc(a.role)}</span></div>
      <div class="msg">${msg}</div>
      <div class="meta">${status}<span>${last ? esc(ago(last.ts)) : ""}</span></div>
    </div>`;
  }).join("");
}

function feed(s) {
  if (!s.activity.length) return;
  document.getElementById("feed").innerHTML = s.activity.map(e => `<li>
    <span class="who" title="${esc(e.role)}">${esc(e.name || e.role)}</span>
    <span class="what"><b>${esc(e.stage)}</b> \\u00b7 ${esc(e.message)}${e.detail ? ` <span class="muted">(${esc(e.detail)})</span>` : ""}</span>
    <span class="when">${esc(ago(e.ts))}</span></li>`).join("");
}

function runs(s) {
  if (!s.runs.length) return;
  document.getElementById("runs").innerHTML = s.runs.map(r => `<li>
    <b>${esc(r.id)}</b> \\u2014 ${esc(r.last_message || "")}
    <span class="muted">(${esc(r.events)} events, ${esc(ago(r.ended))})</span></li>`).join("");
}

function backlog(s) {
  if (!s.backlog.present) return;
  const epic = e => {
    const pct = e.points_total ? Math.round(100 * e.points_done / e.points_total) : 0;
    return `<div class="epic">
      <div class="t"><span>${esc(e.title)}</span><span class="pts">${e.points_done}/${e.points_total} pts</span></div>
      <div class="bar"><b style="width:${pct}%"></b></div>
      ${e.stories.map(st => `<div class="story">${storyChip(st.status)}<span class="st">${esc(st.title)}</span><span class="pts">${st.estimate}pt</span></div>`).join("")}
    </div>`;
  };
  const orphans = s.backlog.orphan_stories;
  document.getElementById("backlog").innerHTML =
    s.backlog.epics.map(epic).join("") +
    (orphans.length ? `<div class="epic"><div class="t"><span>Unassigned</span></div>` +
      orphans.map(st => `<div class="story">${storyChip(st.status)}<span class="st">${esc(st.title)}</span><span class="pts">${st.estimate}pt</span></div>`).join("") + `</div>` : "");
}

function memory(s) {
  if (!s.memory.present && !s.conventions.present) return;
  let html = "";
  if (s.memory.retrospectives.length)
    html += `<div class="muted">retrospectives</div><ul class="list">` +
      s.memory.retrospectives.map(r => `<li>${esc(r)}</li>`).join("") + `</ul>`;
  if (s.memory.decisions.length)
    html += `<div class="muted section-gap">decisions</div><ul class="list">` +
      s.memory.decisions.map(d => `<li><b>${esc(d.id)}</b> ${esc(d.title)}</li>`).join("") + `</ul>`;
  if (s.conventions.present)
    html += `<div class="muted section-gap">house conventions</div><div style="font-size:13px">${esc(s.conventions.summary)}</div>`;
  document.getElementById("memory").innerHTML = html || `<span class="muted">no cross-run memory yet</span>`;
}

async function openReport(path) {
  const res = await fetch("/api/report?path=" + encodeURIComponent(path));
  document.getElementById("reportbody").textContent = await res.text();
  document.getElementById("reportview").style.display = "block";
}

function reports(s) {
  if (!s.reports.length) return;
  document.getElementById("reports").innerHTML = s.reports.map(p =>
    `<div><a class="report" onclick="openReport('${esc(p)}')">${esc(p)}</a></div>`).join("");
}

async function refresh() {
  try {
    const res = await fetch("/api/state");
    const s = await res.json();
    document.getElementById("ws").textContent = s.workspace;
    document.getElementById("beat").textContent = "live \\u00b7 refreshed " + new Date().toLocaleTimeString();
    tiles(s); agents(s); feed(s); runs(s); backlog(s); memory(s); reports(s);
  } catch (err) {
    document.getElementById("beat").textContent = "disconnected \\u2014 retrying";
  }
}
refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""
