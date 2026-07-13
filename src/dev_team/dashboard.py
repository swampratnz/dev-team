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
readable through it (transcripts carry raw assessed-repo content), so keep
the bind address local unless the host is trusted.

**Auth (stopgap).** Passing ``token=`` to :class:`DashboardServer` puts every
route behind that token: ``Authorization: Bearer <token>`` for API callers,
or a ``devteam_dash`` session cookie that a browser obtains by submitting
the token to ``POST /login`` (and drops via ``POST /logout``). Comparisons
are constant-time (:func:`hmac.compare_digest`), the token is never logged,
reflected in a response, or accepted from a URL, and the cookie is
``HttpOnly; SameSite=Strict``. The cookie value is the token itself —
rotation is "change the env var and restart". This is a deliberate stopgap
until an IdP (Auth0) lands; the seam a real integration replaces is
``Handler._authorised`` plus the /login//logout flow. With no token the
dashboard is exactly as open as before (localhost dev).
"""

from __future__ import annotations

import hmac
import json
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from .backlog import BacklogStore
from .conventions import ConventionsStore
from .eventlog import read_events
from .execution import Workspace
from .memory import ProjectMemory
from .persona import DEFAULT_CAST
from .transcripts import list_transcripts, read_transcript

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
    counts = {"todo": 0, "in_progress": 0, "done": 0, "blocked": 0, "declined": 0}
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
                "description": story.description,
                "source_job": story.source_job,
                "finding_id": story.finding_id,
                "depends_on": list(story.depends_on),
                "updated_at": story.updated_at,
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


#: How many timeline entries a single agent's history carries.
_HISTORY_LIMIT = 100


def agent_history(workspace: Workspace, role: str) -> List[Dict]:
    """One role's event history: chronological (oldest-first), last 100 entries.

    Reads the same journal as the feed but keeps only events for ``role`` and
    only the per-event fields the timeline shows, so it is cheap and unit
    testable without the socket. An unknown/absent ``role`` yields ``[]``.
    """

    history = [
        {
            "ts": event.get("ts"),
            "run": event.get("run"),
            "stage": event.get("stage"),
            "message": event.get("message"),
            "detail": event.get("detail"),
        }
        for event in read_events(workspace)
        if event.get("role") == role
    ]
    return history[-_HISTORY_LIMIT:]


#: The session cookie a browser holds after ``POST /login``.
_COOKIE_NAME = "devteam_dash"

#: Upper bound on a ``/login`` form body; anything larger is rejected unread.
_MAX_LOGIN_BODY = 4096

#: The one (and only) path prefix whose writes are proxied to the dispatch
#: service's ``/backlog`` mutation API. Deliberately narrow: the dashboard
#: holds a dispatch bearer token, and a general passthrough would let any
#: dashboard session drive the whole dispatch surface (submit jobs, run
#: agents) with it. Board edits only.
_BACKLOG_PROXY_PREFIX = "/api/backlog/"

#: How long a proxied board write may take end to end. The dispatch cores are
#: pure disk transforms, so anything slower means the service is wedged.
_PROXY_TIMEOUT = 30.0


def _tokens_match(provided: str, expected: str) -> bool:
    """Constant-time equality over the full values (as UTF-8 bytes).

    Bytes rather than str because :func:`hmac.compare_digest` refuses
    non-ASCII text, and ``provided`` is attacker-controlled — a stray byte
    must read as "no match", never become an exception.
    """

    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _make_handler(
    workspace: Workspace,
    token: Optional[str] = None,
    dispatch_url: Optional[str] = None,
    dispatch_token: Optional[str] = None,
) -> type:
    """A request handler class bound to ``workspace``.

    With ``token`` set, every route requires it — as a bearer header or as
    the session cookie minted by ``POST /login``. ``None`` keeps every route
    open (the pre-auth localhost-dev behaviour). ``_authorised`` is the seam
    a later Auth0/IdP integration replaces.

    With BOTH ``dispatch_url`` and ``dispatch_token`` set, authorised writes
    under ``/api/backlog/`` are forwarded to the dispatch service's
    ``/backlog`` mutation API (the board's write path); with either unset the
    board is read-only and those writes answer ``501``. The dispatch token is
    only ever sent to ``dispatch_url`` — never logged or reflected.
    """

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

        # -- auth (stopgap; see the module docstring) -----------------------

        def _authorised(self) -> bool:
            """Whether this request may proceed: bearer header or cookie.

            Mirrors the dispatch service's discipline: constant-time
            comparison over the full value, and the token is never logged,
            echoed into a response, or read from a URL.
            """

            if token is None:
                return True
            if _tokens_match(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return True
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(_COOKIE_NAME)
            return morsel is not None and _tokens_match(morsel.value, token)

        def _reject(self, path: str) -> None:
            """401 an unauthenticated request: JSON for the API, the login
            page for anything a browser would render. Same body either way
            the credentials are wrong — no detail leaks."""

            if path.startswith("/api/"):
                self._send(401, "application/json", json.dumps({"error": "unauthorized"}))
            else:
                self._send(401, "text/html", LOGIN_HTML)

        def _redirect(self, cookie: Optional[str]) -> None:
            """303 back to ``/``, optionally (re)setting the session cookie."""

            self.send_response(303)
            self.send_header("Location", "/")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _login(self) -> None:
            """Exchange a form-posted token for the session cookie.

            The body read is Content-Length-bounded (an oversized or absent
            body is rejected unread) and the submitted value is compared in
            constant time. Every failure yields the same 401 form with the
            same generic note.
            """

            if token is None:
                self._redirect(None)  # nothing to log in to; the page is open
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= _MAX_LOGIN_BODY:
                self._send(401, "text/html", LOGIN_FAILED_HTML)
                return
            body = self.rfile.read(length).decode("utf-8", "replace")
            submitted = parse_qs(body).get("token", [""])[0]
            if not _tokens_match(submitted, token):
                self._send(401, "text/html", LOGIN_FAILED_HTML)
                return
            self._redirect(
                f"{_COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/"
            )

        def _logout(self) -> None:
            """Drop the session cookie; ``/`` then shows the login form."""

            self._redirect(
                f"{_COOKIE_NAME}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"
            )

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            path = urlsplit(self.path).path
            if path == "/login":
                self._login()
            elif path == "/logout":
                self._logout()
            elif not self._authorised():
                self._reject(path)
            elif path.startswith(_BACKLOG_PROXY_PREFIX):
                self._proxy_backlog("POST", path)
            else:
                self._send(404, "text/plain", "not found")

        def do_PATCH(self) -> None:  # noqa: N802 (http.server API)
            self._write_route("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802 (http.server API)
            self._write_route("DELETE")

        def _write_route(self, method: str) -> None:
            """Auth-gate a PATCH/DELETE: only ``/api/backlog/*`` exists."""

            path = urlsplit(self.path).path
            if not self._authorised():
                self._reject(path)
            elif path.startswith(_BACKLOG_PROXY_PREFIX):
                self._proxy_backlog(method, path)
            else:
                self._send(404, "text/plain", "not found")

        # -- board writes: a narrow proxy to the dispatch service -----------
        # The dashboard itself never mutates the workspace (it stays a
        # read-only viewer); board edits are forwarded — same method, same
        # JSON body — to the dispatch service's /backlog API, authenticated
        # with the dispatch bearer token this process (not the browser)
        # holds. Scope is strictly /api/backlog/*: no other dispatch route
        # is reachable through the dashboard, and the dispatch token is
        # never logged, echoed, or handed to the client.

        def _proxy_backlog(self, method: str, path: str) -> None:
            if not (dispatch_url and dispatch_token):
                self._send(
                    501,
                    "application/json",
                    json.dumps({"error": "board editing not configured"}),
                )
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else None
            rest = path[len(_BACKLOG_PROXY_PREFIX):]
            request = urllib.request.Request(
                f"{dispatch_url.rstrip('/')}/backlog/{rest}",
                data=body,
                method=method,
                headers={
                    "Authorization": f"Bearer {dispatch_token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=_PROXY_TIMEOUT) as res:
                    self._send(
                        res.status, "application/json", res.read().decode("utf-8")
                    )
            except urllib.error.HTTPError as exc:
                # Dispatch rejections (400/401/404/409) are real answers —
                # relay status and JSON body verbatim.
                payload = exc.read().decode("utf-8")
                exc.close()
                self._send(exc.code, "application/json", payload)
            except urllib.error.URLError:
                # Fail securely: no stack traces, no target details beyond
                # "the write path is down".
                self._send(
                    502,
                    "application/json",
                    json.dumps({"error": "dispatch service unreachable"}),
                )

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            parts = urlsplit(self.path)
            if not self._authorised():
                self._reject(parts.path)
            elif parts.path == "/":
                self._send(200, "text/html", DASHBOARD_HTML)
            elif parts.path == "/api/state":
                self._send(200, "application/json", json.dumps(collect_state(workspace)))
            elif parts.path == "/api/report":
                self._report(parts.query)
            elif parts.path == "/api/agent":
                self._agent(parts.query)
            elif parts.path == "/api/transcripts":
                self._transcripts(parts.query)
            elif parts.path == "/api/transcript":
                self._transcript(parts.query)
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

        def _agent(self, query: str) -> None:
            # A lean on-click route: filter the journal to one role on demand
            # so the 2.5s /api/state poll stays small. An unknown or absent
            # role is not an error — the UI shows an empty timeline.
            role = parse_qs(query).get("role", [""])[0]
            persona = DEFAULT_CAST.get(role)
            payload = {
                "role": role,
                "name": persona.name if persona else role,
                "history": agent_history(workspace, role),
            }
            self._send(200, "application/json", json.dumps(payload))

        # -- transcripts: a SENSITIVE surface -------------------------------
        # These two routes serve the raw system-prompt/prompt/response of each
        # agent call, which can include repository content (and any secrets in
        # it). Run with a token whenever transcripts are enabled or the bind
        # is non-local: _authorised gates these routes like everything else
        # (an IdP replaces that seam later). The read helpers sanitise every
        # query param and gate on workspace membership (the traversal guard),
        # mirroring /api/report.

        def _transcripts(self, query: str) -> None:
            # A list is never an error: an empty list means "none recorded /
            # recording disabled", which the UI renders as a muted hint.
            params = parse_qs(query)
            run = params.get("run", [""])[0]
            role = params.get("role", [""])[0]
            payload = {
                "run": run,
                "role": role,
                "transcripts": list_transcripts(workspace, run, role),
            }
            self._send(200, "application/json", json.dumps(payload))

        def _transcript(self, query: str) -> None:
            params = parse_qs(query)
            run = params.get("run", [""])[0]
            role = params.get("role", [""])[0]
            seq = params.get("seq", [""])[0]
            record = read_transcript(workspace, run, role, seq)
            if record is None:
                self._send(404, "text/plain", "unknown transcript")
                return
            self._send(200, "application/json", json.dumps(record))

    return Handler


class DashboardServer:
    """The dashboard HTTP server; read-only over one workspace.

    ``token`` (optional) puts every route behind bearer/cookie auth — see
    the module docstring. Pick a URL/cookie-safe token (e.g. from
    ``secrets.token_urlsafe``): the cookie value is the token verbatim.

    ``dispatch_url`` + ``dispatch_token`` (optional, both required together)
    enable the board's write path: authorised ``/api/backlog/*`` writes are
    proxied to that dispatch service's ``/backlog`` mutation API. Without
    them the dashboard stays fully read-only (board writes answer ``501``).
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        host: str = "127.0.0.1",
        port: int = 8737,
        token: Optional[str] = None,
        dispatch_url: Optional[str] = None,
        dispatch_token: Optional[str] = None,
    ) -> None:
        self.workspace = workspace
        self.httpd = ThreadingHTTPServer(
            (host, port),
            _make_handler(workspace, token, dispatch_url, dispatch_token),
        )

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
.agent { position: relative; background: var(--card); border: 1px solid var(--line);
         border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow); cursor: pointer;
         transition: transform .15s ease, border-color .15s ease; }
.agent:hover { transform: translateY(-2px); border-color: var(--accent); }
.agent:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.agent .hint { position: absolute; top: 10px; right: 12px; font-size: 11px;
               color: var(--ink-3); white-space: nowrap; }
.agent:hover .hint, .agent:focus-visible .hint { color: var(--accent); }
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
.story { display: flex; gap: 8px; align-items: baseline; padding: 3px 4px; font-size: 13px; }
.story .st { flex: 1; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis;
             white-space: nowrap; }
.story.done .st { color: var(--ink-3); }
.story .pts { color: var(--ink-3); font-size: 12px; }
.story[data-story] { cursor: pointer; border-radius: 6px; }
.story[data-story]:hover { background: var(--accent-soft); }
.story[data-story]:hover .st { color: var(--accent); }
.story[data-story]:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

.board { display: flex; gap: 8px; overflow-x: auto; align-items: flex-start; margin: 4px 0 0; padding-bottom: 4px; }
.col { flex: 1 0 118px; min-width: 118px; background: var(--inset);
       border: 1px solid var(--line-soft); border-radius: 8px; padding: 6px; }
.ch { display: flex; justify-content: space-between; gap: 6px; font-size: 11px;
      text-transform: uppercase; letter-spacing: .05em; color: var(--ink-3); font-weight: 600; }
.ch .cn { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
        padding: 6px 8px; margin-top: 6px; cursor: pointer; font-size: 12px;
        box-shadow: var(--shadow); }
.card:hover { border-color: var(--accent); }
.card:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.card .ct { color: var(--ink); font-weight: 600; overflow-wrap: anywhere;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
            overflow: hidden; }
.card .cmeta { margin-top: 4px; display: flex; gap: 6px; align-items: center;
               flex-wrap: wrap; color: var(--ink-3); font-size: 11px; }
.dep { white-space: nowrap; }
.depwarn { color: var(--warning); font-weight: 600; overflow-wrap: anywhere; }
.cmove { width: 100%; margin-top: 6px; font-size: 11px; background: var(--inset);
         color: var(--ink); border: 1px solid var(--line); border-radius: 6px;
         padding: 2px 4px; font-family: inherit; }
.declined-row { margin-top: 8px; opacity: .6; }
.declined-row .card { display: inline-block; margin-right: 6px; max-width: 220px;
                      vertical-align: top; }
.declined-row .card .ct { text-decoration: line-through; font-weight: 500; }
.addcard { margin-top: 8px; width: 100%; background: none; border: 1px dashed var(--line);
           border-radius: 8px; color: var(--ink-3); font-size: 12px; padding: 5px 10px;
           cursor: pointer; }
.addcard:hover { color: var(--accent); border-color: var(--accent); }
.board-err, .err { color: var(--critical); font-size: 12px; margin: 6px 0;
                   overflow-wrap: anywhere; }
.chip.declined { border-style: dashed; color: var(--ink-3); text-decoration: line-through; }
.story-form input, .story-form textarea {
  width: 100%; padding: 6px 8px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--inset); color: var(--ink); font: inherit; font-size: 13px;
  margin: 2px 0 8px; }
.story-form textarea { min-height: 64px; resize: vertical; }
.story-form button, .modal-actions button {
  background: none; border: 1px solid var(--line); border-radius: 6px;
  color: var(--ink-2); font-size: 12px; padding: 5px 10px; cursor: pointer; }
.story-form button:hover, .modal-actions button:hover { color: var(--ink); border-color: var(--ink-3); }
.modal-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.modal-actions .cmove { width: auto; margin: 0; }
.depopt { display: block; font-size: 13px; color: var(--ink-2); padding: 2px 0; }

.story-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
              margin-bottom: 12px; font-size: 12px; color: var(--ink-3); }
.verify { margin-top: 14px; padding: 10px 12px; background: var(--inset);
          border: 1px solid var(--line-soft); border-radius: 8px; }
.verify .cmd { display: flex; gap: 8px; align-items: center; margin-top: 6px; }
.verify .cmd code { flex: 1; background: var(--card); border: 1px solid var(--line-soft);
                    border-radius: 6px; padding: 6px 8px; font-size: 12px;
                    overflow-x: auto; white-space: nowrap; }
.verify button { background: none; border: 1px solid var(--line); border-radius: 6px;
                 color: var(--ink-2); font-size: 12px; padding: 5px 10px; cursor: pointer;
                 white-space: nowrap; }
.verify button:hover { color: var(--ink); border-color: var(--ink-3); }
.verify .note { margin-top: 6px; font-size: 12px; color: var(--ink-3); }

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

.tl-run { padding: 16px 0 4px; font-size: 11px; text-transform: uppercase;
          letter-spacing: .06em; color: var(--ink-3); font-weight: 600; }
.tl-run:first-child { padding-top: 0; }
.tl-run code { font-size: 11px; text-transform: none; letter-spacing: 0; }
.tl { position: relative; margin-left: 5px; padding: 8px 0 8px 18px;
      border-left: 2px solid var(--line); }
.tl::before { content: ""; position: absolute; left: -5px; top: 13px; width: 8px; height: 8px;
              border-radius: 50%; background: var(--accent); }
.tl.good::before { background: var(--good); }
.tl.bad::before { background: var(--critical); }
.tl .head { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.tl .when { margin-left: auto; color: var(--ink-3); font-size: 12px; white-space: nowrap; }
.tl .msg { margin-top: 5px; color: var(--ink); overflow-wrap: anywhere; }
.tl .detail { margin-top: 3px; color: var(--ink-3); font-size: 12px; overflow-wrap: anywhere; }

details.tx summary { font-weight: 500; font-variant-numeric: tabular-nums; }
.tx-body { padding: 4px 0 6px; }
.tx-field { margin: 8px 0; }
.tx-label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
            color: var(--ink-3); font-weight: 600; margin-bottom: 4px; }
.tx-pre { background: var(--inset); border: 1px solid var(--line-soft); border-radius: 8px;
          padding: 10px 12px; max-height: 300px; overflow: auto; white-space: pre-wrap;
          overflow-wrap: anywhere; font-size: 12px; color: var(--ink-2);
          font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; }

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
<h2>Backlog</h2>
<div class="board-err" id="board-error" role="alert" hidden></div>
<div class="panel" id="backlog"><span class="muted">no backlog yet</span></div>
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
<div class="overlay" id="agent-overlay" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="agent-title">
    <div class="modal-head">
      <code id="agent-title"></code>
      <button id="agent-close" aria-label="close agent history">&#x2715;</button>
    </div>
    <div class="modal-body" id="agent-body"></div>
  </div>
</div>
<div class="overlay" id="story-overlay" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="story-title">
    <div class="modal-head">
      <code id="story-title"></code>
      <button id="story-close" aria-label="close story detail">&#x2715;</button>
    </div>
    <div class="modal-body" id="story-body"></div>
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
  declined: chip("\\u2298 declined", "declined"),
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
    ["stories declined", c.declined, true],
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
    return `<div class="agent ${v.cls}" role="button" tabindex="0" data-role="${esc(a.role)}" aria-label="${esc(who)} \\u2014 open event history" title="open ${esc(who)}'s event history">
      <span class="hint" aria-hidden="true">history \\u203a</span>
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

// Story detail lookup for the modal, rebuilt from every /api/state payload:
// keyed by story id, carrying the owning epic's title (the repo context)
// and id (the add-card / dependency-editor grouping key).
let storyIndex = new Map();

// The Kanban columns, in board order; declined renders as its own
// de-emphasised row under the board rather than a fifth column.
const COLUMNS = [
  ["todo", "To do"], ["in_progress", "In progress"],
  ["blocked", "Blocked"], ["done", "Done"],
];
const MOVE_TARGETS = [
  ["todo", "todo"], ["in_progress", "in progress"], ["done", "done"],
  ["blocked", "blocked"], ["declined", "declined"],
];

// The card's dependency indicator. SECURITY: dependency titles are resolved
// from the same repo-derived state as everything else, so they flow through
// esc() before innerHTML like every other card field.
function depBadge(st) {
  const deps = st.depends_on || [];
  if (!deps.length) return "";
  const titles = deps.map(id => (storyIndex.get(id) || { title: id }).title);
  let html = `<span class="dep" title="depends on: ${esc(titles.join(", "))}">\\u26D3 ${deps.length}</span>`;
  const unfinished = deps.map(id => storyIndex.get(id)).filter(d =>
    d && d.status !== "done" && d.status !== "declined");
  if (unfinished.length)
    html += `<span class="depwarn">blocked by unfinished ${esc(unfinished[0].title)}</span>`;
  return html;
}

// The reliable move control: a <select> posting to .../status on change.
function moveSelect(st) {
  return `<select class="cmove" data-move="${esc(st.id)}" aria-label="move ${esc(st.title)}">`
    + MOVE_TARGETS.map(([v, label]) =>
      `<option value="${v}"${st.status === v ? " selected" : ""}>${label}</option>`).join("")
    + "</select>";
}

// One board card. SECURITY: title/estimate are repo-derived — esc() before
// innerHTML, always (the title is also clamped to two lines in CSS).
function card(st) {
  return `<div class="card" role="button" tabindex="0" data-story="${esc(st.id)}" title="${esc(st.title)} \\u2014 click for detail">
    <div class="ct">${esc(st.title)}</div>
    <div class="cmeta"><span class="pts">${esc(st.estimate)}pt</span>${depBadge(st)}</div>
    ${moveSelect(st)}
  </div>`;
}

// One epic's board: the four columns plus the muted declined row.
function board(stories) {
  const cols = COLUMNS.map(([key, label]) => {
    const items = stories.filter(st => st.status === key);
    return `<div class="col"><div class="ch"><span>${label}</span><span class="cn">${items.length}</span></div>${items.map(card).join("")}</div>`;
  }).join("");
  const declined = stories.filter(st => st.status === "declined");
  return `<div class="board">${cols}</div>` + (declined.length
    ? `<div class="declined-row"><div class="ch"><span>Declined (${declined.length})</span></div>${declined.map(card).join("")}</div>`
    : "");
}

function backlog(s) {
  if (!s.backlog.present) { put($("backlog"), '<span class="muted">no backlog yet</span>'); return; }
  storyIndex = new Map();
  for (const e of s.backlog.epics)
    for (const st of e.stories) storyIndex.set(st.id, { ...st, epic: e.title, epicId: e.id });
  for (const st of s.backlog.orphan_stories) storyIndex.set(st.id, { ...st, epic: "", epicId: "" });
  const epic = (e, stories) => {
    const pct = e.points_total ? Math.round(100 * e.points_done / e.points_total) : 0;
    return `<div class="epic">
      <div class="t"><span title="${esc(e.description)}">${esc(e.title)}</span><span class="pts">${esc(e.points_done)}/${esc(e.points_total)} pts \\u00b7 ${pct}%</span></div>
      <div class="bar"><b style="width:${pct}%"></b></div>
      ${board(stories)}
      <button class="addcard" data-add="${esc(e.id)}" data-title="${esc(e.title)}">\\uFF0B Add card</button>
    </div>`;
  };
  const orphans = s.backlog.orphan_stories;
  put($("backlog"), s.backlog.epics.map(e => epic(e, e.stories)).join("")
    + (orphans.length ? epic({
        id: "", title: "Unassigned", description: "",
        points_done: orphans.filter(st => st.status === "done").reduce((n, st) => n + st.estimate, 0),
        points_total: orphans.reduce((n, st) => n + st.estimate, 0),
      }, orphans) : ""));
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

// ---- agent history modal ----
// A per-entry stage class, reusing the feed's good/bad stage vocabulary.
function stageClass(stage) {
  const s = String(stage || "").toLowerCase();
  if (STAGE_BAD.has(s)) return "bad";
  if (STAGE_GOOD.has(s)) return "good";
  return "";
}

// Render one agent's history as a vertical timeline, oldest -> newest so it
// reads top-to-bottom as a story, grouped by run. SECURITY: every field is
// service/agent-derived and possibly repo-influenced, so all of it goes
// through esc() (chip() escapes its own label) before touching innerHTML.
function renderHistory(data) {
  const items = (data && data.history) || [];
  if (!items.length) return '<p class="muted">No recorded activity yet.</p>';
  const out = [];
  let lastRun;
  for (const e of items) {
    if (e.run !== lastRun) {
      lastRun = e.run;
      out.push(`<div class="tl-run">run <code>${esc(e.run)}</code></div>`);
    }
    const kind = stageClass(e.stage);
    out.push(`<div class="tl${kind ? " " + kind : ""}">
      <div class="head">${chip(e.stage, kind === "good" ? "done" : kind === "bad" ? "blocked" : "")}<span class="when" title="${esc(absTime(e.ts))}">${esc(ago(e.ts))}</span></div>
      <div class="msg">${esc(e.message)}</div>
      ${e.detail ? `<div class="detail">${esc(e.detail)}</div>` : ""}
    </div>`);
  }
  return out.join("");
}

// The distinct run ids this agent appears in, oldest-first (transcripts are
// keyed by run+role, so we fetch one group per run present in the history).
function distinctRuns(data) {
  const items = (data && data.history) || [];
  const seen = [];
  for (const e of items) if (e.run && !seen.includes(e.run)) seen.push(e.run);
  return seen;
}

// One labelled, scrollable, monospace <pre>. SECURITY: transcript text is raw
// repo-derived content, so every field is escaped through esc() before it
// touches innerHTML — a prompt/response containing <script> or </pre> renders
// inert as literal text. null fields (e.g. no system prompt) are omitted.
function txField(label, text) {
  if (text == null) return "";
  return `<div class="tx-field"><div class="tx-label">${esc(label)}</div>`
    + `<pre class="tx-pre">${esc(text)}</pre></div>`;
}

// Render the "Transcripts (N)" subsection from per-run metadata groups. Each
// call is a collapsible showing seq/cost/time; the body loads on expand.
function renderTranscripts(role, groups) {
  let total = 0;
  for (const g of groups) total += ((g && g.transcripts) || []).length;
  const out = [`<div class="tl-run">Transcripts (${total})</div>`];
  if (!total) {
    out.push('<p class="muted">No transcripts recorded for this run '
      + '(enable with --record-transcripts / DEV_TEAM_RECORD_TRANSCRIPTS).</p>');
    return out.join("");
  }
  for (const g of groups) {
    const list = (g && g.transcripts) || [];
    if (!list.length) continue;
    if (groups.length > 1) out.push(`<div class="tl-run">run <code>${esc(g.run)}</code></div>`);
    for (const t of list) {
      const cost = (typeof t.cost_usd === "number") ? "$" + t.cost_usd.toFixed(4) : "";
      const bits = ["#" + esc(t.seq), cost && esc(cost), t.is_error ? "error" : "",
                    esc(absTime(t.ts))].filter(Boolean).join(" \\u00b7 ");
      out.push(`<details class="tx" data-run="${esc(g.run)}" data-role="${esc(role)}" data-seq="${esc(t.seq)}">`
        + `<summary>${bits}<span class="preview">${esc(t.prompt_preview)}</span></summary>`
        + '<div class="tx-body"><p class="muted">loading\\u2026</p></div></details>');
    }
  }
  return out.join("");
}

// Fetch and render one transcript's full I/O, once, when its <details> opens.
async function fillTranscript(d) {
  if (d.dataset.loaded) return;
  d.dataset.loaded = "1";
  const body = d.querySelector(".tx-body");
  try {
    const res = await fetch("/api/transcript?run=" + encodeURIComponent(d.dataset.run)
      + "&role=" + encodeURIComponent(d.dataset.role)
      + "&seq=" + encodeURIComponent(d.dataset.seq));
    if (!res.ok) throw new Error(String(res.status));
    const t = await res.json();
    body.innerHTML = txField("System prompt", t.system_prompt)
      + txField("Prompt", t.prompt) + txField("Response", t.response);
  } catch (err) {
    d.dataset.loaded = "";
    body.innerHTML = '<p class="muted">failed to load transcript</p>';
  }
}

async function loadTranscripts(role, runs) {
  const box = $("agent-transcripts");
  if (!box) return;
  try {
    const groups = await Promise.all(runs.map(run =>
      fetch("/api/transcripts?run=" + encodeURIComponent(run)
            + "&role=" + encodeURIComponent(role))
        .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })));
    box.innerHTML = renderTranscripts(role, groups);
    box.querySelectorAll("details.tx").forEach(d =>
      d.addEventListener("toggle", () => { if (d.open) fillTranscript(d); }));
  } catch (err) {
    box.innerHTML = '<div class="tl-run">Transcripts</div>'
      + '<p class="muted">failed to load transcripts</p>';
  }
}

async function openAgent(role) {
  $("agent-title").textContent = role;
  $("agent-body").innerHTML = '<p class="muted">loading\\u2026</p>';
  $("agent-overlay").hidden = false;
  $("agent-close").focus();
  try {
    const res = await fetch("/api/agent?role=" + encodeURIComponent(role));
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    const label = (data.name && data.name !== data.role)
      ? data.name + " (" + data.role + ")" : (data.role || role);
    $("agent-title").textContent = label;
    $("agent-title").title = label;
    $("agent-body").innerHTML = renderHistory(data)
      + '<div id="agent-transcripts"><p class="muted">loading transcripts\\u2026</p></div>';
    loadTranscripts(data.role || role, distinctRuns(data));
  } catch (err) {
    $("agent-body").innerHTML = '<p class="muted">failed to load history</p>';
  }
}

function closeAgent() { $("agent-overlay").hidden = true; }

// ---- story detail modal ----
// SECURITY: every story field is repo-derived (assessment findings quote
// repository content), so ALL of it goes through esc() before touching
// innerHTML — a <script> in a story description renders inert as text. The
// title goes via textContent, which never parses HTML at all.
function openStory(id) {
  const st = storyIndex.get(id);
  if (!st) return;
  $("story-title").textContent = st.id + " \\u00b7 " + st.title;
  const bits = [
    `<div class="story-meta">${storyChip(st.status)}<span>${esc(st.estimate)}pt</span>${st.epic ? `<span>\\u00b7 ${esc(st.epic)}</span>` : ""}${st.source_job ? `<span>\\u00b7 job <code>${esc(st.source_job)}</code></span>` : ""}</div>`,
    '<div class="tx-label">Description</div>',
    `<pre class="tx-pre">${st.description ? esc(st.description) : "(no description)"}</pre>`,
  ];
  if (st.finding_id && st.source_job) {
    const cmd = "dev_team_verify " + st.source_job + " " + st.finding_id;
    bits.push(`<div class="verify"><div class="tx-label">Re-verify this finding</div>
      <div class="cmd"><code>${esc(cmd)}</code><button data-copy="${esc(cmd)}">copy</button></div>
      <div class="note">Re-checks this claim (finding <code>${esc(st.finding_id)}</code>) with a fresh, skeptical agent against a clean clone \\u2014 independent of the auditor that wrote it.</div></div>`);
  } else if (st.finding_id) {
    bits.push(`<div class="verify"><div class="tx-label">Re-verify this finding</div>
      <div class="note">LLM finding <code>${esc(st.finding_id)}</code> \\u2014 assessed outside the dispatch service, so there is no source job to re-verify against.</div></div>`);
  } else {
    bits.push('<p class="muted" style="margin-top:14px">Deterministic finding (dependency/dead-code scan) \\u2014 re-run the assessment to refresh; not agent-verifiable.</p>');
  }
  // Board controls: move/decline/delete plus the edit and dependency forms.
  // SECURITY: input values round-trip through the server, so they are still
  // escaped on every render (value="${esc(...)}" / escaped textarea body);
  // the dependency list offers only OTHER cards in the same epic (no self).
  const deps = st.depends_on || [];
  const others = [...storyIndex.values()].filter(o => o.epicId === st.epicId && o.id !== st.id);
  const depBoxes = others.map(o =>
    `<label class="depopt"><input type="checkbox" name="dep" value="${esc(o.id)}"${deps.includes(o.id) ? " checked" : ""}> ${esc(o.title)} <span class="muted">${esc(o.id)}</span></label>`).join("");
  bits.push(`<div class="verify">
    <div class="tx-label">Board actions</div>
    <div class="modal-actions">${moveSelect(st)}<button data-decline="${esc(st.id)}">decline</button><button data-del="${esc(st.id)}">delete</button></div>
    <div class="tx-label" style="margin-top:12px">Edit card</div>
    <form class="story-form" id="story-edit" data-id="${esc(st.id)}">
      <input name="title" value="${esc(st.title)}" required aria-label="title">
      <textarea name="description" aria-label="description">${esc(st.description ?? "")}</textarea>
      <input name="estimate" type="number" min="1" value="${esc(st.estimate)}" aria-label="estimate (points)">
      <button type="submit">save</button>
    </form>
    <div class="tx-label" style="margin-top:12px">Depends on (cards in the same epic)</div>
    <form class="story-form" id="story-deps" data-id="${esc(st.id)}">
      ${depBoxes || '<span class="muted">no other cards in this epic</span>'}
      <button type="submit">save dependencies</button>
    </form>
    <div class="err" id="story-error" role="alert" hidden></div>
  </div>`);
  $("story-body").innerHTML = bits.join("");
  $("story-overlay").hidden = false;
  $("story-close").focus();
}

// ---- board writes (every mutation via the PR-A /api/backlog/* proxy) ----
// Same-origin fetch: the dashboard session cookie rides along; the dispatch
// bearer token stays in the dashboard process, never in this page.
async function backlogWrite(method, path, payload) {
  const res = await fetch("/api/backlog/" + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: payload === undefined ? null : JSON.stringify(payload),
  });
  if (!res.ok) {
    // Surface the dispatch rejection ("story S1 depends on itself", ...).
    // Callers put this message in the DOM via textContent ONLY: it can
    // quote repo-derived story titles straight from the service.
    let msg = "write failed (HTTP " + res.status + ")";
    try {
      const data = await res.json();
      if (data && data.error) msg = String(data.error);
    } catch (e) { /* non-JSON error body: keep the generic message */ }
    throw new Error(msg);
  }
  return res;
}

let boardErrTimer = null;
function showBoardError(msg) {
  const el = $("board-error");
  el.textContent = msg; // SECURITY: textContent, never innerHTML
  el.hidden = false;
  clearTimeout(boardErrTimer);
  boardErrTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

function storyError(msg) {
  const el = document.getElementById("story-error"); // inside the open modal
  if (!el) { showBoardError(msg); return; }
  el.textContent = msg; // SECURITY: textContent, never innerHTML
  el.hidden = false;
}

async function moveStory(id, status, report) {
  try {
    await backlogWrite("POST", "story/" + encodeURIComponent(id) + "/status", { status });
    await refresh();
    return true;
  } catch (err) {
    report("move failed: " + err.message);
    // snap the <select> back to the story's real status
    lastHtml.delete($("backlog"));
    if (state) backlog(state);
    return false;
  }
}

async function declineStory(id) {
  try {
    await backlogWrite("POST", "story/" + encodeURIComponent(id) + "/decline");
    await refresh();
    openStory(id);
  } catch (err) { storyError("decline failed: " + err.message); }
}

async function deleteStory(id) {
  try {
    await backlogWrite("DELETE", "story/" + encodeURIComponent(id));
    closeStory();
    await refresh();
  } catch (err) { storyError("delete failed: " + err.message); }
}

async function saveStoryEdit(form) {
  try {
    await backlogWrite("PATCH", "story/" + encodeURIComponent(form.dataset.id), {
      title: form.elements.title.value,
      description: form.elements.description.value,
      estimate: Number(form.elements.estimate.value) || 1,
    });
    await refresh();
    openStory(form.dataset.id);
  } catch (err) { storyError("save failed: " + err.message); }
}

async function saveStoryDeps(form) {
  const chosen = [...form.querySelectorAll('input[name="dep"]:checked')].map(b => b.value);
  try {
    await backlogWrite("POST", "story/" + encodeURIComponent(form.dataset.id) + "/deps",
                       { depends_on: chosen });
    await refresh();
    openStory(form.dataset.id);
  } catch (err) { storyError("dependencies rejected: " + err.message); }
}

async function addCard(form) {
  const payload = { title: form.elements.title.value };
  if (form.elements.description.value) payload.description = form.elements.description.value;
  const estimate = Number(form.elements.estimate.value);
  if (estimate >= 1) payload.estimate = estimate;
  if (form.dataset.epic) payload.epic_id = form.dataset.epic;
  try {
    await backlogWrite("POST", "story", payload);
    closeStory();
    await refresh();
  } catch (err) { storyError("add failed: " + err.message); }
}

// The "＋ Add card" form, in the story modal (immune to the 2.5s re-render).
// The title bypasses innerHTML (textContent); the epic id round-trips via an
// escaped data attribute and the server re-validates it anyway.
function openAddCard(epicId, epicTitle) {
  $("story-title").textContent = "add card \\u00b7 " + (epicTitle || "Unassigned");
  $("story-body").innerHTML = `<form class="story-form" id="story-add" data-epic="${esc(epicId)}">
    <div class="tx-label">Title</div>
    <input name="title" required aria-label="title">
    <div class="tx-label">Description (optional)</div>
    <textarea name="description" aria-label="description"></textarea>
    <div class="tx-label">Estimate in points (optional)</div>
    <input name="estimate" type="number" min="1" placeholder="1" aria-label="estimate (points)">
    <button type="submit">add card</button>
    <div class="err" id="story-error" role="alert" hidden></div>
  </form>`;
  $("story-overlay").hidden = false;
  $("story-close").focus();
}

function closeStory() { $("story-overlay").hidden = true; }

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
$("agents").addEventListener("click", e => {
  const card = e.target.closest("[data-role]");
  if (card) openAgent(card.dataset.role);
});
$("agents").addEventListener("keydown", e => {
  const card = e.target.closest("[data-role]");
  if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openAgent(card.dataset.role); }
});
$("backlog").addEventListener("click", e => {
  const add = e.target.closest("[data-add]");
  if (add) { openAddCard(add.dataset.add, add.dataset.title); return; }
  if (e.target.closest("select")) return; // the move control, not the card
  const row = e.target.closest("[data-story]");
  if (row) openStory(row.dataset.story);
});
$("backlog").addEventListener("keydown", e => {
  if (e.target.closest("select")) return;
  const row = e.target.closest("[data-story]");
  if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openStory(row.dataset.story); }
});
$("backlog").addEventListener("change", e => {
  const sel = e.target.closest("select[data-move]");
  if (sel) moveStory(sel.dataset.move, sel.value, showBoardError);
});
$("story-body").addEventListener("click", e => {
  const dec = e.target.closest("[data-decline]");
  if (dec) { declineStory(dec.dataset.decline); return; }
  const del = e.target.closest("[data-del]");
  if (del) {
    // two-step confirm: the first click arms the button, the second deletes
    if (del.dataset.armed) deleteStory(del.dataset.del);
    else { del.dataset.armed = "1"; del.textContent = "confirm delete?"; }
    return;
  }
  const btn = e.target.closest("[data-copy]");
  if (!btn || !navigator.clipboard) return;
  navigator.clipboard.writeText(btn.dataset.copy).then(
    () => { btn.textContent = "copied"; setTimeout(() => { btn.textContent = "copy"; }, 1200); },
    () => {});
});
$("story-body").addEventListener("change", e => {
  const sel = e.target.closest("select[data-move]");
  if (sel) moveStory(sel.dataset.move, sel.value, storyError)
    .then(ok => { if (ok) openStory(sel.dataset.move); });
});
$("story-body").addEventListener("submit", e => {
  e.preventDefault();
  const form = e.target;
  if (form.id === "story-edit") saveStoryEdit(form);
  else if (form.id === "story-deps") saveStoryDeps(form);
  else if (form.id === "story-add") addCard(form);
});
$("modal-close").addEventListener("click", closeModal);
$("overlay").addEventListener("click", e => { if (e.target === $("overlay")) closeModal(); });
$("agent-close").addEventListener("click", closeAgent);
$("agent-overlay").addEventListener("click", e => { if (e.target === $("agent-overlay")) closeAgent(); });
$("story-close").addEventListener("click", closeStory);
$("story-overlay").addEventListener("click", e => { if (e.target === $("story-overlay")) closeStory(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); closeAgent(); closeStory(); } });

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

# --------------------------------------------------------------------------
# The login page: what an unauthenticated browser gets instead of the
# dashboard. Self-contained like the main page (inline CSS, no external
# assets, light/dark via the same palette), one password field POSTed
# form-encoded to /login. Built with .replace() on a __NOTE__ marker
# (not .format(): the CSS braces would need escaping). The failure note is
# deliberately generic — nothing about the expected token leaks.
# --------------------------------------------------------------------------

_LOGIN_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dev-team login</title>
<style>
:root {
  --bg: #f6f6f4; --card: #ffffff; --line: #e5e4e0;
  --ink: #0b0b0b; --ink-2: #52514e; --accent: #2a78d6; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141413; --card: #212120; --line: #383835;
    --ink: #ffffff; --ink-2: #c3c2b7; --accent: #3987e5;
  }
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--bg); color: var(--ink);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
       min-height: 100vh; display: flex; align-items: center;
       justify-content: center; padding: 24px; }
form { background: var(--card); border: 1px solid var(--line);
       border-radius: 12px; padding: 28px; width: min(360px, 100%); }
h1 { font-size: 18px; letter-spacing: -.01em; margin-bottom: 4px; }
h1 small { color: var(--ink-2); font-weight: 500; font-size: 13px; margin-left: 6px; }
p { color: var(--ink-2); font-size: 13px; margin-bottom: 14px; }
.err { color: var(--critical); font-weight: 600; }
input { width: 100%; padding: 8px 10px; border: 1px solid var(--line);
        border-radius: 8px; background: var(--bg); color: var(--ink);
        font: inherit; margin-bottom: 12px; }
button { width: 100%; padding: 8px 10px; border: 0; border-radius: 8px;
         background: var(--accent); color: #fff; font: inherit;
         font-weight: 600; cursor: pointer; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>dev-team<small>dashboard</small></h1>
  <p>This dashboard requires an access token.</p>
  __NOTE__
  <input type="password" name="token" placeholder="access token"
         autofocus autocomplete="current-password" aria-label="access token">
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""

#: The page an unauthenticated browser gets (401 body).
LOGIN_HTML = _LOGIN_TEMPLATE.replace("__NOTE__", "")

#: The same page after a failed ``POST /login`` — generic note, no detail.
LOGIN_FAILED_HTML = _LOGIN_TEMPLATE.replace(
    "__NOTE__", '<p class="err" role="alert">Invalid token.</p>'
)
