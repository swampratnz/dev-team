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
# The page. Self-contained: inline CSS/JS, no external requests, one plain
# Python string (not an f-string: ``${...}`` below is JavaScript, and every
# backslash the browser must see is written ``\\`` here). Status colors are
# the validated status palette (good/critical/warning) plus the categorical
# slot-1 blue for "active"; a text label always accompanies the color, so
# color never carries meaning alone. Report markdown is rendered client-side
# by an escape-first renderer: the raw report text is HTML-escaped in full
# before any markdown transform runs, so untrusted repository content can
# never reach ``innerHTML`` unescaped.
# --------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dev-team dashboard</title>
<style>
:root {
  --bg: #f6f6f4; --card: #ffffff; --inset: #f9f9f7;
  --line: #e5e4e0; --line-soft: #eeedea;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #8a8983;
  --accent: #2a78d6; --accent-soft: rgba(42, 120, 214, .12);
  --good: #0ca30c; --good-ink: #006300; --good-soft: rgba(12, 163, 12, .12);
  --critical: #d03b3b; --critical-soft: rgba(208, 59, 59, .12);
  --warning: #b97e00; --track: #ececea;
  --shadow: 0 1px 2px rgba(11, 11, 11, .04), 0 2px 8px rgba(11, 11, 11, .05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141413; --card: #212120; --inset: #1a1a19;
    --line: #383835; --line-soft: #2c2c2a;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #8a8983;
    --accent: #3987e5; --accent-soft: rgba(57, 135, 229, .18);
    --good-ink: #0ca30c;
    --warning: #fab219; --track: #33332f;
    --shadow: 0 1px 2px rgba(0, 0, 0, .35), 0 2px 10px rgba(0, 0, 0, .30);
  }
}
* { box-sizing: border-box; margin: 0; }
html { scroll-behavior: smooth; }
body {
  background: var(--bg); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 24px 20px 56px; max-width: 1200px; margin: 0 auto;
  overflow-x: hidden;
}
code { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; }
button { font: inherit; }

header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
header h1 { font-size: 20px; letter-spacing: -.01em; }
header h1 small { color: var(--ink-3); font-weight: 500; font-size: 13px; margin-left: 6px; }
.ws {
  background: var(--inset); border: 1px solid var(--line); border-radius: 8px;
  padding: 3px 10px; font-size: 12px; color: var(--ink-2);
  max-width: 46ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.beat { margin-left: auto; display: inline-flex; align-items: center; gap: 7px;
        color: var(--ink-3); font-size: 12px; font-variant-numeric: tabular-nums; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good); flex: none;
       animation: livepulse 2.2s ease-in-out infinite; }
@keyframes livepulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--good-soft); }
  50%      { box-shadow: 0 0 0 5px var(--good-soft); }
}
.beat.warn { color: var(--warning); }
.beat.warn .dot { background: var(--warning); animation: none; }
.beat.down { color: var(--critical); }
.beat.down .dot { background: var(--critical); animation: none; }

.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 12px 16px; box-shadow: var(--shadow); transition: opacity .3s ease; }
.tile .v { font-size: 24px; font-weight: 650; }
.tile .k { color: var(--ink-2); font-size: 12px; margin-top: 2px; }
.tile.dim { opacity: .5; box-shadow: none; }

h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-3);
     font-weight: 600; margin: 24px 0 10px; }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: 14px 16px; box-shadow: var(--shadow); }
.muted { color: var(--ink-3); font-size: 13px; }

.chip { display: inline-flex; align-items: center; gap: 4px; padding: 1px 9px;
        border-radius: 999px; font-size: 11px; font-weight: 600; line-height: 1.7;
        border: 1px solid var(--line); color: var(--ink-2); background: transparent;
        white-space: nowrap; }
.chip.active  { border-color: transparent; color: var(--accent);   background: var(--accent-soft); }
.chip.done    { border-color: transparent; color: var(--good-ink); background: var(--good-soft); }
.chip.blocked { border-color: transparent; color: var(--critical); background: var(--critical-soft); }
.chip.idle    { border-style: dashed; color: var(--ink-3); }

.agents { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
.agent { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: 12px 14px; box-shadow: var(--shadow); }
.agent .who { display: flex; align-items: center; gap: 10px; min-width: 0; }
.avatar { width: 34px; height: 34px; border-radius: 50%; flex: none;
          display: flex; align-items: center; justify-content: center;
          background: var(--accent-soft); color: var(--accent);
          font-weight: 700; font-size: 15px; }
.agent .name { font-weight: 650; line-height: 1.25; }
.agent .role { color: var(--ink-3); font-size: 12px; }
.agent .msg { margin-top: 8px; color: var(--ink-2); font-size: 13px; min-height: 2.7em;
              display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
              overflow: hidden; }
.agent .meta { margin-top: 8px; display: flex; gap: 6px; align-items: center;
               flex-wrap: wrap; font-size: 12px; color: var(--ink-3); }
.agent .meta .when { margin-left: auto; white-space: nowrap; }
.agent.active { border-color: var(--accent); animation: cardpulse 2.6s ease-in-out infinite; }
@keyframes cardpulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--accent-soft), var(--shadow); }
  50%      { box-shadow: 0 0 0 5px var(--accent-soft), var(--shadow); }
}
.agent.idle { opacity: .55; border-style: dashed; box-shadow: none; }
.agent.idle .avatar { background: var(--track); color: var(--ink-3); }

.cols { display: grid; grid-template-columns: minmax(340px, 3fr) minmax(300px, 2fr);
        gap: 0 16px; align-items: start; }
@media (max-width: 900px) {
  .cols { grid-template-columns: 1fr; }
  body { padding: 16px 12px 40px; }
  .ws { max-width: 100%; }
}

.filters { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
           padding-bottom: 10px; border-bottom: 1px solid var(--line-soft); }
.filters select {
  background: var(--inset); color: var(--ink); border: 1px solid var(--line);
  border-radius: 8px; padding: 4px 8px; font: inherit; font-size: 12px; max-width: 100%;
}
.filters .ghost { background: none; border: none; color: var(--accent);
                  font-size: 12px; cursor: pointer; padding: 4px 6px; border-radius: 6px; }
.filters .ghost:hover { background: var(--accent-soft); }

.feed { list-style: none; max-height: 520px; overflow-y: auto; }
.feed li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px;
           padding: 8px 0; border-top: 1px solid var(--line-soft); font-size: 13px; }
.feed li:first-child { border-top: 0; }
.evdot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent);
         flex: none; align-self: center; }
.feed .who { color: var(--ink); font-weight: 600; }
.feed .what { flex: 1 1 240px; color: var(--ink-2); overflow-wrap: anywhere; }
.feed .what b { color: var(--ink); font-weight: 600; }
.feed .when { margin-left: auto; color: var(--ink-3); font-size: 12px; white-space: nowrap; }
.feed li.engine .evdot { background: var(--line); }
.feed li.engine .who { color: var(--ink-3); font-style: italic; font-weight: 500; }

.runs { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
.run { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
       padding: 10px 12px; box-shadow: var(--shadow); cursor: pointer;
       transition: border-color .15s ease; }
.run:hover { border-color: var(--accent); }
.run.selected { border-color: var(--accent);
                box-shadow: 0 0 0 2px var(--accent-soft), var(--shadow); }
.run .top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.run .top code { font-size: 12px; font-weight: 600; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
.run .meta { margin-top: 6px; color: var(--ink-3); font-size: 12px;
             display: flex; gap: 10px; flex-wrap: wrap; }
.run .msg { margin-top: 6px; color: var(--ink-2); font-size: 12px;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
            overflow: hidden; }

.epic { padding: 10px 0; border-top: 1px solid var(--line-soft); }
.epic:first-child { border-top: 0; padding-top: 0; }
.epic .t { display: flex; justify-content: space-between; gap: 8px;
           font-weight: 650; align-items: baseline; }
.epic .pts { color: var(--ink-3); font-weight: 400; font-size: 12px; white-space: nowrap; }
.bar { height: 8px; background: var(--track); border-radius: 999px; margin: 8px 0;
       overflow: hidden; }
.bar b { display: block; height: 100%; background: var(--accent); border-radius: 999px;
         transition: width .6s ease; }
.story { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; font-size: 13px; }
.story .st { flex: 1; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis;
             white-space: nowrap; }
.story.done .st { color: var(--ink-3); }
.story .pts { color: var(--ink-3); font-size: 12px; }

details { border-top: 1px solid var(--line-soft); padding: 8px 0; }
details:first-of-type { border-top: 0; }
summary { cursor: pointer; font-weight: 600; font-size: 13px; color: var(--ink); }
summary .preview { font-weight: 400; color: var(--ink-3); font-size: 12px; margin-top: 2px;
                   display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
                   overflow: hidden; }
details[open] summary .preview { display: none; }
.det-body { padding: 8px 0 2px; font-size: 13px; color: var(--ink-2); white-space: pre-wrap; }
.list { list-style: none; padding: 4px 0 0; }
.list li { padding: 5px 0; border-top: 1px solid var(--line-soft); font-size: 13px;
           color: var(--ink-2); }
.list li:first-child { border-top: 0; }

.report { display: block; width: 100%; text-align: left; background: none; border: none;
          border-top: 1px solid var(--line-soft); border-radius: 4px; padding: 7px 4px;
          cursor: pointer; color: var(--accent); overflow-wrap: anywhere;
          font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12px; }
.report:first-child { border-top: 0; }
.report:hover { background: var(--accent-soft); }

.overlay { position: fixed; inset: 0; z-index: 40; background: rgba(11, 11, 11, .45);
           display: flex; align-items: center; justify-content: center; padding: 24px; }
.overlay[hidden] { display: none; }
.modal { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
         width: min(860px, 100%); max-height: min(86vh, 940px);
         display: flex; flex-direction: column; box-shadow: 0 12px 40px rgba(0, 0, 0, .30); }
.modal-head { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
              border-bottom: 1px solid var(--line); }
.modal-head code { flex: 1; font-size: 13px; font-weight: 600; overflow: hidden;
                   text-overflow: ellipsis; white-space: nowrap; }
.modal-head button { background: none; border: 1px solid var(--line); border-radius: 8px;
                     color: var(--ink-2); font-size: 13px; line-height: 1;
                     padding: 6px 9px; cursor: pointer; }
.modal-head button:hover { color: var(--ink); border-color: var(--ink-3); }
.modal-body { overflow-y: auto; padding: 16px 20px 24px; }

.md { font-size: 14px; color: var(--ink-2); }
.md h1, .md h2, .md h3, .md h4 { color: var(--ink); margin: 18px 0 8px;
                                 text-transform: none; letter-spacing: 0; }
.md h1:first-child, .md h2:first-child, .md h3:first-child, .md h4:first-child { margin-top: 0; }
.md h1 { font-size: 20px; } .md h2 { font-size: 17px; }
.md h3 { font-size: 15px; } .md h4 { font-size: 14px; }
.md p { margin: 8px 0; }
.md ul, .md ol { margin: 8px 0; padding-left: 24px; }
.md li { margin: 3px 0; }
.md code { background: var(--inset); border: 1px solid var(--line-soft); border-radius: 5px;
           padding: 1px 5px; font-size: 12px; }
.md pre { background: var(--inset); border: 1px solid var(--line-soft); border-radius: 8px;
          padding: 12px 14px; overflow-x: auto; margin: 10px 0; }
.md pre code { background: none; border: 0; padding: 0; font-size: 12px; }
.md a { color: var(--accent); }
.md hr { border: 0; border-top: 1px solid var(--line); margin: 16px 0; }
.md blockquote { border-left: 3px solid var(--line); padding-left: 12px; margin: 8px 0;
                 color: var(--ink-3); }
.md .tablewrap { overflow-x: auto; margin: 10px 0; }
.md table { border-collapse: collapse; font-size: 13px; }
.md th, .md td { border: 1px solid var(--line); padding: 5px 10px; text-align: left; }
.md th { background: var(--inset); color: var(--ink); }

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
  html { scroll-behavior: auto; }
}
</style>
</head>
<body>
<header>
  <h1>dev-team<small>dashboard</small></h1>
  <code class="ws" id="ws"></code>
  <span class="beat" id="beat"><span class="dot" aria-hidden="true"></span><span id="beat-text">connecting&hellip;</span></span>
</header>
<div class="tiles" id="tiles"></div>
<h2>The team</h2>
<div class="agents" id="agents"></div>
<div class="cols">
  <div>
    <h2 id="activity-title">Activity</h2>
    <div class="panel" id="activity-panel">
      <div class="filters">
        <select id="f-agent" data-filter="agent" data-all="all agents" aria-label="filter by agent"><option value="">all agents</option></select>
        <select id="f-run" data-filter="run" data-all="all runs" aria-label="filter by run"><option value="">all runs</option></select>
        <button id="f-clear" class="ghost" hidden>clear filters</button>
      </div>
      <ul class="feed" id="feed"><li class="muted">no events yet</li></ul>
    </div>
    <h2>Runs</h2>
    <div class="runs" id="runs"><div class="panel muted">no runs recorded</div></div>
  </div>
  <div>
    <h2>Backlog</h2>
    <div class="panel" id="backlog"><span class="muted">no backlog yet</span></div>
    <h2>Memory &amp; conventions</h2>
    <div class="panel" id="memory"><span class="muted">no cross-run memory yet</span></div>
    <h2>Reports</h2>
    <div class="panel" id="reports"><span class="muted">no assessment reports</span></div>
  </div>
</div>
<div class="overlay" id="overlay" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="modal-head">
      <code id="modal-title"></code>
      <button id="modal-close" aria-label="close report">&#x2715;</button>
    </div>
    <div class="modal-body md" id="modal-body"></div>
  </div>
</div>
<script>
const ENGINE = new Set(["workflow", "delivery", "assessment", "chat"]);
const STAGE_GOOD = new Set(["done", "complete", "completed", "finished", "report", "merged"]);
const STAGE_BAD = new Set(["halted", "failed", "error", "blocked"]);
const ACTIVE_WINDOW = 600; // seconds since last event before a card stops pulsing

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const now = () => Date.now() / 1000;
const ago = ts => {
  if (!ts) return "";
  const s = Math.max(0, now() - ts);
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
};
const absTime = ts => ts ? new Date(ts * 1000).toLocaleString() : "";
const fmtDur = s => {
  if (!isFinite(s) || s < 0) return "";
  if (s < 1) return "<1s";
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + Math.floor(s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
};
const parseCost = msg => {
  const m = /\\(\\$(\\d[\\d,]*(?:\\.\\d+)?)\\)/.exec(String(msg ?? ""));
  return m ? "$" + m[1] : null;
};
const parseTurns = msg => {
  const m = /(\\d+)\\s*turns?\\b/i.exec(String(msg ?? ""));
  return m ? m[1] + (m[1] === "1" ? " turn" : " turns") : null;
};
const chip = (label, cls) => `<span class="chip ${cls}">${esc(label)}</span>`;
const storyChip = st => ({
  done: chip("\\u2713 done", "done"),
  in_progress: chip("\\u25B6 in progress", "active"),
  blocked: chip("\\u2715 blocked", "blocked"),
  todo: chip("todo", ""),
}[st] || chip(st, ""));

// Re-render a container only when its content actually changed: preserves
// scroll position, CSS transitions and <details> state between polls.
const lastHtml = new Map();
function put(el, html) {
  if (lastHtml.get(el) === html) return false;
  lastHtml.set(el, html);
  el.innerHTML = html;
  return true;
}

// ---- markdown, escape-first (reports are untrusted repository content) ----
function renderMarkdown(src) {
  // SECURITY: HTML-escape the ENTIRE text before anything else. Every
  // transform below operates on already-escaped text and only introduces
  // markup built here, so no byte of the report reaches innerHTML raw.
  const lines = esc(src).split(/\\r?\\n/);
  const inline = t => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")
    .replace(/\\*([^*]+)\\*/g, "<em>$1</em>")
    .replace(/\\[([^\\]]+)\\]\\(([^()\\s]+)\\)/g, (all, label, url) =>
      /^https?:\\/\\//i.test(url)
        ? '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>"
        : label);
  const blockStart = l => /^```/.test(l) || /^#{1,4}\\s/.test(l) || /^\\s*[-*]\\s+/.test(l)
    || /^\\s*\\d+[.)]\\s+/.test(l) || /^\\s*\\|/.test(l) || /^&gt;/.test(l)
    || /^\\s*(?:-{3,}|\\*{3,}|_{3,})\\s*$/.test(l);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    if (/^```/.test(line)) {
      i += 1;
      const buf = [];
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i += 1; // swallow the closing fence
      out.push("<pre><code>" + buf.join("\\n") + "</code></pre>");
      continue;
    }
    const h = /^(#{1,4})\\s+(.*)$/.exec(line);
    if (h) {
      out.push("<h" + h[1].length + ">" + inline(h[2]) + "</h" + h[1].length + ">");
      i += 1;
      continue;
    }
    if (/^\\s*(?:-{3,}|\\*{3,}|_{3,})\\s*$/.test(line)) { out.push("<hr>"); i += 1; continue; }
    if (/^&gt;/.test(line)) {
      const buf = [];
      while (i < lines.length && /^&gt;/.test(lines[i]))
        buf.push(lines[i++].replace(/^&gt;\\s?/, ""));
      out.push("<blockquote>" + inline(buf.join(" ")) + "</blockquote>");
      continue;
    }
    if (/^\\s*[-*]\\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\\s*[-*]\\s+/.test(lines[i]))
        items.push(inline(lines[i++].replace(/^\\s*[-*]\\s+/, "")));
      out.push("<ul>" + items.map(x => "<li>" + x + "</li>").join("") + "</ul>");
      continue;
    }
    if (/^\\s*\\d+[.)]\\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\\s*\\d+[.)]\\s+/.test(lines[i]))
        items.push(inline(lines[i++].replace(/^\\s*\\d+[.)]\\s+/, "")));
      out.push("<ol>" + items.map(x => "<li>" + x + "</li>").join("") + "</ol>");
      continue;
    }
    if (/^\\s*\\|/.test(line) && i + 1 < lines.length
        && /^[\\s|:-]+$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const rows = [];
      while (i < lines.length && /^\\s*\\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = r => r.trim().replace(/^\\|/, "").replace(/\\|$/, "")
        .split("|").map(c => inline(c.trim()));
      const head = cells(rows[0]).map(c => "<th>" + c + "</th>").join("");
      const body = rows.slice(2).map(r =>
        "<tr>" + cells(r).map(c => "<td>" + c + "</td>").join("") + "</tr>").join("");
      out.push('<div class="tablewrap"><table><thead><tr>' + head
        + "</tr></thead><tbody>" + body + "</tbody></table></div>");
      continue;
    }
    const buf = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !blockStart(lines[i])) buf.push(lines[i++]);
    out.push("<p>" + inline(buf.join(" ")) + "</p>");
  }
  return out.join("\\n");
}

// ---- views ----
let state = null;
const filters = { agent: "", run: "" };

function tiles(s) {
  const c = s.backlog.counts;
  const open = c.todo + c.in_progress + c.blocked;
  const latest = s.activity.length ? s.activity[0].ts : null;
  const cells = [
    ["recent runs", s.runs.length, false],
    ["stories open", open, true],
    ["stories done", c.done, true],
    ["stories blocked", c.blocked, true],
    ["last activity", latest ? ago(latest) : "\\u2014", false],
  ];
  put($("tiles"), cells.map(([k, v, dimzero]) =>
    `<div class="tile${dimzero && !v ? " dim" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`
  ).join(""));
}

function agentView(a) {
  const last = a.last;
  if (!last) return { cls: "idle", chips: chip("idle", "idle"), when: "" };
  const stage = String(last.stage || "").toLowerCase();
  let cls = "", st;
  if (STAGE_BAD.has(stage)) st = chip("\\u2715 " + last.stage, "blocked");
  else if (STAGE_GOOD.has(stage)) st = chip("\\u2713 " + last.stage, "done");
  else if (now() - (last.ts || 0) < ACTIVE_WINDOW) {
    cls = "active";
    st = chip("\\u25B6 " + last.stage, "active");
  } else st = chip(last.stage, "");
  const turns = parseTurns(last.message);
  return { cls, chips: st + (turns ? " " + chip(turns, "") : ""), when: ago(last.ts) };
}

function agents(s) {
  put($("agents"), s.agents.map(a => {
    const v = agentView(a);
    const who = a.name || a.role;
    const last = a.last;
    const full = last ? (last.message || "") + (last.detail ? " (" + last.detail + ")" : "") : "";
    const msg = last
      ? esc(last.message) + (last.detail ? ` <span class="muted">(${esc(last.detail)})</span>` : "")
      : '<span class="muted">no recorded activity</span>';
    return `<div class="agent ${v.cls}">
      <div class="who">
        <span class="avatar" aria-hidden="true">${esc(who.charAt(0).toUpperCase())}</span>
        <div><div class="name">${esc(who)}</div><div class="role">${esc(a.role)}</div></div>
      </div>
      <div class="msg" title="${esc(full)}">${msg}</div>
      <div class="meta">${v.chips}<span class="when" title="${esc(absTime(last && last.ts))}">${esc(v.when)}</span></div>
    </div>`;
  }).join(""));
}

function setOptions(sel, values, labelFor) {
  const sig = JSON.stringify(values);
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  const current = filters[sel.dataset.filter];
  sel.innerHTML = `<option value="">${esc(sel.dataset.all)}</option>`
    + values.map(v => `<option value="${esc(v)}">${esc(labelFor(v))}</option>`).join("");
  sel.value = values.includes(current) ? current : "";
  filters[sel.dataset.filter] = sel.value;
}

function syncFilters(s) {
  const nameByRole = {};
  for (const e of s.activity)
    if (e.role && !(e.role in nameByRole)) nameByRole[e.role] = e.name || "";
  const roles = [...new Set(s.activity.map(e => e.role).filter(Boolean))].sort();
  const runIds = [...new Set(
    [...s.runs.map(r => r.id), ...s.activity.map(e => e.run)].filter(Boolean))];
  setOptions($("f-agent"), roles, r => nameByRole[r] ? nameByRole[r] + " \\u00b7 " + r : r);
  setOptions($("f-run"), runIds, r => r);
}

function syncClear() {
  $("f-clear").hidden = !(filters.agent || filters.run);
}

function feed(s) {
  if (!s.activity.length) { put($("feed"), '<li class="muted">no events yet</li>'); return; }
  const items = s.activity.filter(e =>
    (!filters.agent || e.role === filters.agent) && (!filters.run || e.run === filters.run));
  if (!items.length) {
    put($("feed"), '<li class="muted">no events match the current filters</li>');
    return;
  }
  put($("feed"), items.map(e => `<li${ENGINE.has(e.role) ? ' class="engine"' : ""}>
    <span class="evdot" aria-hidden="true"></span>
    <span class="who" title="${esc(e.role)}">${esc(e.name || e.role)}</span>
    <span class="what"><b>${esc(e.stage)}</b> \\u00b7 ${esc(e.message)}${e.detail ? ` <span class="muted">(${esc(e.detail)})</span>` : ""}</span>
    <span class="when" title="${esc(absTime(e.ts))}">${esc(ago(e.ts))}</span></li>`).join(""));
}

function runChip(stage) {
  const s = String(stage || "").toLowerCase();
  if (STAGE_BAD.has(s)) return chip("\\u2715 " + stage, "blocked");
  if (STAGE_GOOD.has(s)) return chip("\\u2713 finished", "done");
  return chip("\\u25B6 running", "active");
}

function runsPanel(s) {
  if (!s.runs.length) { put($("runs"), '<div class="panel muted">no runs recorded</div>'); return; }
  put($("runs"), s.runs.map(r => {
    const cost = parseCost(r.last_message);
    const dur = (r.started != null && r.ended != null) ? fmtDur(r.ended - r.started) : "";
    const meta = [
      dur && `<span title="started ${esc(absTime(r.started))}">${esc(dur)}</span>`,
      `<span>${esc(r.events)} event${r.events === 1 ? "" : "s"}</span>`,
      cost && `<span>${esc(cost)}</span>`,
      `<span title="${esc(absTime(r.ended))}">${esc(ago(r.ended))}</span>`,
    ].filter(Boolean).join("");
    return `<div class="run${filters.run === r.id ? " selected" : ""}" data-run="${esc(r.id)}" role="button" tabindex="0" title="filter the activity feed to ${esc(r.id)}">
      <div class="top"><code>${esc(r.id)}</code>${runChip(r.last_stage)}</div>
      <div class="meta">${meta}</div>
      ${r.last_message ? `<div class="msg" title="${esc(r.last_message)}">${esc(r.last_message)}</div>` : ""}
    </div>`;
  }).join(""));
}

function backlog(s) {
  if (!s.backlog.present) { put($("backlog"), '<span class="muted">no backlog yet</span>'); return; }
  const story = st => `<div class="story${st.status === "done" ? " done" : ""}">${storyChip(st.status)}<span class="st" title="${esc(st.title)}">${esc(st.title)}</span><span class="pts">${esc(st.estimate)}pt</span></div>`;
  const epic = e => {
    const pct = e.points_total ? Math.round(100 * e.points_done / e.points_total) : 0;
    return `<div class="epic">
      <div class="t"><span>${esc(e.title)}</span><span class="pts">${esc(e.points_done)}/${esc(e.points_total)} pts \\u00b7 ${pct}%</span></div>
      <div class="bar"><b style="width:${pct}%"></b></div>
      ${e.stories.map(story).join("")}
    </div>`;
  };
  const orphans = s.backlog.orphan_stories;
  put($("backlog"), s.backlog.epics.map(epic).join("")
    + (orphans.length ? `<div class="epic"><div class="t"><span>Unassigned</span></div>${orphans.map(story).join("")}</div>` : ""));
}

function details(key, title, body, open) {
  return `<details data-key="${key}"${open ? " open" : ""}><summary>${title}</summary>${body}</details>`;
}

function memory(s) {
  const el = $("memory");
  const parts = [];
  if (s.memory.present) {
    parts.push(`<div class="muted" style="margin-bottom:6px">${esc(s.memory.runs)} run${s.memory.runs === 1 ? "" : "s"} in memory</div>`);
    const retros = s.memory.retrospectives;
    if (retros.length) parts.push(details("retros", `Retrospectives (${retros.length})`,
      `<ul class="list">${retros.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`,
      retros.length <= 3));
    const decs = s.memory.decisions;
    if (decs.length) parts.push(details("decisions", `Decisions (${decs.length})`,
      `<ul class="list">${decs.map(d => `<li><b>${esc(d.id)}</b> ${esc(d.title)}</li>`).join("")}</ul>`,
      decs.length <= 3));
  }
  if (s.conventions.present) {
    parts.push(details("conventions",
      `House conventions<span class="preview">${esc(s.conventions.summary)}</span>`,
      `<div class="det-body">${esc(s.conventions.summary)}</div>`, false));
  }
  const html = parts.join("") || '<span class="muted">no cross-run memory yet</span>';
  const open = {};
  el.querySelectorAll("details[data-key]").forEach(d => { open[d.dataset.key] = d.open; });
  if (put(el, html))
    el.querySelectorAll("details[data-key]").forEach(d => {
      if (d.dataset.key in open) d.open = open[d.dataset.key];
    });
}

function reports(s) {
  if (!s.reports.length) { put($("reports"), '<span class="muted">no assessment reports</span>'); return; }
  put($("reports"), s.reports.map(p =>
    `<button class="report" data-path="${esc(p)}" title="open ${esc(p)}">${esc(p)}</button>`).join(""));
}

// ---- report modal ----
function openModal(path) {
  $("modal-title").textContent = path;
  $("modal-body").innerHTML = '<p class="muted">loading\\u2026</p>';
  $("overlay").hidden = false;
  $("modal-close").focus();
  fetch("/api/report?path=" + encodeURIComponent(path))
    .then(res => { if (!res.ok) throw new Error(String(res.status)); return res.text(); })
    .then(text => { $("modal-body").innerHTML = renderMarkdown(text); })
    .catch(() => { $("modal-body").innerHTML = '<p class="muted">failed to load report</p>'; });
}

function closeModal() { $("overlay").hidden = true; }

// ---- run -> feed cross-filter ----
function toggleRun(id) {
  filters.run = filters.run === id ? "" : id;
  $("f-run").value = filters.run;
  if (state) { feed(state); runsPanel(state); }
  syncClear();
  if (filters.run) $("activity-title").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---- wiring ----
$("runs").addEventListener("click", e => {
  const card = e.target.closest("[data-run]");
  if (card) toggleRun(card.dataset.run);
});
$("runs").addEventListener("keydown", e => {
  const card = e.target.closest("[data-run]");
  if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleRun(card.dataset.run); }
});
$("f-agent").addEventListener("change", e => {
  filters.agent = e.target.value;
  if (state) feed(state);
  syncClear();
});
$("f-run").addEventListener("change", e => {
  filters.run = e.target.value;
  if (state) { feed(state); runsPanel(state); }
  syncClear();
});
$("f-clear").addEventListener("click", () => {
  filters.agent = filters.run = "";
  $("f-agent").value = "";
  $("f-run").value = "";
  if (state) { feed(state); runsPanel(state); }
  syncClear();
});
$("reports").addEventListener("click", e => {
  const btn = e.target.closest("[data-path]");
  if (btn) openModal(btn.dataset.path);
});
$("modal-close").addEventListener("click", closeModal);
$("overlay").addEventListener("click", e => { if (e.target === $("overlay")) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

// ---- poll loop ----
let fails = 0;
function beat(ok) {
  if (ok) {
    fails = 0;
    $("beat").className = "beat";
    $("beat-text").textContent = "live \\u00b7 refreshed " + new Date().toLocaleTimeString();
  } else {
    fails += 1;
    $("beat").className = fails > 1 ? "beat down" : "beat warn";
    $("beat-text").textContent = "disconnected \\u2014 retrying";
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(String(res.status));
    const s = await res.json();
    state = s;
    $("ws").textContent = s.workspace;
    $("ws").title = s.workspace;
    syncFilters(s);
    syncClear();
    tiles(s); agents(s); feed(s); runsPanel(s); backlog(s); memory(s); reports(s);
    beat(true);
  } catch (err) {
    beat(false);
  }
}
refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""
