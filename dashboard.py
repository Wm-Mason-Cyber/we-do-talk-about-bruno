"""
Live classroom dashboard for the Roster API — meant to be projected during the lab.

Watches every request that reaches the API and pushes it to /dashboard over
Server-Sent Events, so students see their GET/POST/PUT/DELETE land (and the
status code they got back) within a second.

    /dashboard          projector page
    /dashboard/events   SSE stream the page listens to
    /dashboard/state    JSON snapshot (handy for curl / debugging)
    /dashboard/reset    POST with header X-Teacher-Key: <teacher key> → fresh roster
    /logs/access        every request, in the standard nginx/Apache "combined" format

Hooked into the API by app.py; roster_api.py knows nothing about it.
"""

import asyncio
import json
import secrets
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

FEED_LENGTH = 150        # recent requests kept for a freshly opened dashboard
HEARTBEAT_SECS = 15      # keeps the SSE connection alive through nginx
QUIET_PREFIXES = ("/dashboard", "/favicon.ico", "/openapi.json", "/docs/oauth2-redirect")
LOG_MAX_BYTES = 5_000_000  # access.log rolls over to access.log.1 past this


# ── State shared by the middleware and every open dashboard ───────────────────

class Hub:
    def __init__(self, get_roster: Callable[[], list]):
        self.get_roster = get_roster
        self.events: deque = deque(maxlen=FEED_LENGTH)
        self.methods: Counter = Counter()
        self.statuses: Counter = Counter()
        self.devices: set = set()
        self.subscribers: set = set()
        self.roster = self._read_roster()

    def _read_roster(self) -> List[dict]:
        return jsonable_encoder(self.get_roster())

    def _diff(self) -> List[dict]:
        """What changed in the roster since we last looked."""
        before = {s["id"]: s["name"] for s in self.roster}
        self.roster = self._read_roster()
        after = {s["id"]: s["name"] for s in self.roster}

        changes = []
        for sid, name in after.items():
            if sid not in before:
                changes.append({"op": "add", "id": sid, "name": name})
            elif before[sid] != name:
                changes.append({"op": "rename", "id": sid, "name": name, "old": before[sid]})
        for sid, name in before.items():
            if sid not in after:
                changes.append({"op": "remove", "id": sid, "name": name})
        return changes

    def record(self, scope: dict, status: int):
        ip = (scope.get("client") or ("?", 0))[0]
        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode("latin-1")
        event = {
            "t": int(time.time() * 1000),
            "ip": ip,
            "method": scope.get("method", "?"),
            "path": path + ("?" + query if query else ""),
            "status": status,
            "changes": self._diff(),
        }
        self.events.append(event)
        self.methods[event["method"]] += 1
        self.statuses[status] += 1
        self.devices.add(ip)
        self._broadcast("update", {"event": event, **self._summary()})

    def reset(self):
        self.events.clear()
        self.methods.clear()
        self.statuses.clear()
        self.devices.clear()
        self.roster = self._read_roster()
        self._broadcast("snapshot", self.snapshot())

    def _summary(self) -> dict:
        return {
            "roster": self.roster,
            "methods": dict(self.methods),
            "statuses": {str(k): v for k, v in self.statuses.items()},
            "devices": len(self.devices),
        }

    def snapshot(self) -> dict:
        return {"events": list(self.events), **self._summary()}

    def _broadcast(self, kind: str, data: dict):
        message = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A stalled browser tab; drop it — EventSource reconnects on its own
                self.subscribers.discard(queue)


# ── Access log in the standard "combined" format (what AP FRQs show) ─────────

class AccessLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    def write(self, scope: dict, status: int, size: int):
        headers = dict(scope.get("headers") or [])
        h = lambda name: headers.get(name, b"-").decode("latin-1").replace('"', "'") or "-"
        query = scope.get("query_string", b"").decode("latin-1")
        target = scope.get("path", "") + ("?" + query if query else "")
        line = '%s - - [%s] "%s %s HTTP/%s" %d %d "%s" "%s"\n' % (
            (scope.get("client") or ("-", 0))[0],
            datetime.now().astimezone().strftime("%d/%b/%Y:%H:%M:%S %z"),
            scope.get("method", "-"), target, scope.get("http_version", "1.1"),
            status, size, h(b"referer"), h(b"user-agent"),
        )
        try:
            if self.path.stat().st_size > LOG_MAX_BYTES:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
        except FileNotFoundError:
            pass
        with open(self.path, "a") as f:
            f.write(line)


# ── ASGI middleware: notice every request and the status code it got ─────────

class RequestRecorder:
    def __init__(self, app, hub: Hub, log: Optional[AccessLog]):
        self.app = app
        self.hub = hub
        self.log = log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # The dashboard's own traffic stays out of the feed (and the log), or it would flood both
        quiet = scope["path"].startswith(QUIET_PREFIXES)
        status, size = 500, 0

        async def send_and_record(message):
            nonlocal status, size
            if message["type"] == "http.response.start":
                status = message["status"]
                if not quiet:
                    # The route has finished by now, so the roster diff belongs to this request
                    self.hub.record(scope, status)
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_and_record)
        except Exception:
            if not quiet:
                self.hub.record(scope, 500)
            raise
        finally:
            if self.log and not quiet:
                self.log.write(scope, status, size)


# ── Routes ────────────────────────────────────────────────────────────────────

def attach(app: FastAPI, get_roster: Callable[[], list], reset_roster: Callable[[], None],
           teacher_key: str = "", access_log: Optional[Path] = None):
    hub = Hub(get_roster)
    log = AccessLog(access_log) if access_log else None
    router = APIRouter(prefix="/dashboard", include_in_schema=False)

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def page():
        return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})

    @router.get("/state")
    async def state():
        return hub.snapshot()

    @router.get("/events")
    async def events(request: Request):
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        hub.subscribers.add(queue)

        async def stream():
            try:
                yield f"event: snapshot\ndata: {json.dumps(hub.snapshot())}\n\n"
                while not await request.is_disconnected():
                    try:
                        yield await asyncio.wait_for(queue.get(), HEARTBEAT_SECS)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub.subscribers.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/reset")
    async def reset(x_teacher_key: str = Header(None)):
        if not teacher_key:
            raise HTTPException(503, "Reset is disabled: no teacher key is set on the server.")
        if not x_teacher_key or not secrets.compare_digest(x_teacher_key, teacher_key):
            raise HTTPException(403, "Wrong teacher key.")
        reset_roster()
        hub.reset()
        return {"message": "Roster reset."}

    app.include_router(router)

    if log:
        @app.get("/logs/access", include_in_schema=False)
        async def access_log_text():
            return PlainTextResponse(log.path.read_text(), headers={"Cache-Control": "no-store"})

    app.add_middleware(RequestRecorder, hub=hub, log=log)
    return hub


# ── The projector page ────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Roster API Live</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --yellow: #d29922; --red: #f85149; --blue: #58a6ff; --purple: #bc8cff;
    --green-bg: rgba(63,185,80,.16); --yellow-bg: rgba(210,153,34,.18); --red-bg: rgba(248,81,73,.16);
  }
  :root[data-theme="light"] {
    --bg: #ffffff; --surface: #f6f8fa; --border: #d0d7de;
    --text: #1f2328; --muted: #59636e;
    --green: #1a7f37; --yellow: #9a6700; --red: #cf222e; --blue: #0969da; --purple: #8250df;
    --green-bg: #dafbe1; --yellow-bg: #fff8c5; --red-bg: #ffebe9;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'SF Mono', 'Consolas', 'Liberation Mono', monospace;
    font-size: 15px; padding: 20px 28px; min-height: 100vh;
  }
  header {
    display: flex; flex-wrap: wrap; gap: 12px 24px; justify-content: space-between; align-items: center;
    padding-bottom: 14px; margin-bottom: 18px; border-bottom: 1px solid var(--border);
  }
  h1 { font-size: 20px; color: var(--blue); letter-spacing: .02em; }
  #url { font-size: 22px; font-weight: bold; }
  #url .verb { color: var(--muted); font-weight: normal; }
  .controls { display: flex; gap: 8px; align-items: center; color: var(--muted); font-size: 13px; }
  button {
    font: inherit; font-size: 13px; color: var(--text); background: var(--surface);
    border: 1px solid var(--border); border-radius: 6px; padding: 5px 10px; cursor: pointer;
  }
  button:hover { border-color: var(--muted); }
  button.danger { color: var(--red); }
  .live-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; background: var(--muted); margin-right: 6px; }
  .live .live-dot { background: var(--green); box-shadow: 0 0 6px var(--green); }

  #stats { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
  .stat { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; min-width: 92px; }
  .stat .n { font-size: 26px; font-weight: bold; font-variant-numeric: tabular-nums; }
  .stat .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
  .stat.sep { margin-left: 14px; }

  main { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 24px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  h2 { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 10px; }

  #roster { display: flex; flex-wrap: wrap; gap: 8px; align-content: flex-start; }
  .card {
    display: flex; align-items: baseline; gap: 8px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; max-width: 100%;
    transition: background 1.5s, border-color 1.5s, opacity .8s;
  }
  .card .id { color: var(--muted); font-size: 12px; }
  .card .name { font-size: 17px; overflow-wrap: anywhere; }
  .card .old { color: var(--muted); text-decoration: line-through; font-size: 13px; }
  .card .x { display: none; padding: 0 6px; font-size: 12px; }
  .teacher .card .x { display: inline-block; }
  .card.add    { background: var(--green-bg);  border-color: var(--green); }
  .card.rename { background: var(--yellow-bg); border-color: var(--yellow); }
  .card.remove { background: var(--red-bg);    border-color: var(--red); opacity: .55; }
  .card.remove .name { text-decoration: line-through; }

  #feed { width: 100%; border-collapse: collapse; font-size: 14px; }
  #feed td { padding: 6px 12px 6px 0; border-top: 1px solid var(--border); vertical-align: top; }
  #feed tr.fresh td { animation: flash 2s ease-out; }
  @keyframes flash { from { background: var(--surface); } to { background: transparent; } }
  #feed .time, #feed .ip { color: var(--muted); white-space: nowrap; }
  #feed .path { overflow-wrap: anywhere; }
  #feed .note { color: var(--muted); font-size: 13px; }
  .m { font-weight: bold; }
  .m-GET { color: var(--green); } .m-POST { color: var(--yellow); } .m-PUT { color: var(--blue); }
  .m-DELETE { color: var(--red); } .m-PATCH, .m-HEAD, .m-OPTIONS { color: var(--purple); }
  .code { white-space: nowrap; font-weight: bold; }
  .c2 { color: var(--green); } .c3 { color: var(--blue); } .c4 { color: var(--yellow); } .c5 { color: var(--red); }
  .empty { color: var(--muted); padding: 10px 0; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Class Roster API &mdash; Live</h1>
    <div id="url"><span class="verb">GET / POST / PUT / DELETE</span> <span id="base"></span>/students</div>
  </div>
  <div class="controls">
    <span id="conn"><span class="live-dot"></span><span id="conn-text">connecting&hellip;</span></span>
    <button id="theme" title="Toggle light/dark">&#9680;</button>
    <button id="teacher" title="Teacher mode">Teacher</button>
    <button id="reset" class="danger" hidden>New period (reset)</button>
  </div>
</header>

<section id="stats"></section>

<main>
  <section>
    <h2>Roster &mdash; <span id="count">0</span> students</h2>
    <div id="roster"></div>
  </section>
  <section>
    <h2>Live request feed</h2>
    <table id="feed"><tbody></tbody></table>
    <div id="feed-empty" class="empty">Waiting for requests&hellip;</div>
  </section>
</main>

<script>
const REASONS = {
  200: 'OK', 201: 'Created', 204: 'No Content', 304: 'Not Modified', 307: 'Temporary Redirect',
  400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found',
  405: 'Method Not Allowed', 418: "I'm a teapot 🫖", 422: 'Unprocessable Content', 429: 'Too Many Requests',
  500: 'Internal Server Error', 502: 'Bad Gateway', 503: 'Service Unavailable', 507: 'Insufficient Storage',
};
const METHODS = ['GET', 'POST', 'PUT', 'DELETE'];
const FEED_MAX = 60;

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;   // textContent only: student names are untrusted
  return n;
};

$('base').textContent = location.protocol + '//' + location.host;

// ── Theme + teacher mode (per-browser conveniences) ──────────────────────────
const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch {} },
};
function applyTheme(t) { document.documentElement.dataset.theme = t; }
applyTheme(store.get('theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
$('theme').onclick = () => {
  const t = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  applyTheme(t); store.set('theme', t);
};

let teacherKey = store.get('teacherKey');
function applyTeacher() {
  document.body.classList.toggle('teacher', !!teacherKey);
  $('reset').hidden = !teacherKey;
  $('teacher').textContent = teacherKey ? 'Exit teacher' : 'Teacher';
}
$('teacher').onclick = () => {
  teacherKey = teacherKey ? null : (prompt('Teacher key:') || null);
  store.set('teacherKey', teacherKey); applyTeacher();
};
$('reset').onclick = async () => {
  if (!confirm('Reset the roster back to Ada + Grace and clear the feed?')) return;
  const r = await fetch('/dashboard/reset', { method: 'POST', headers: { 'X-Teacher-Key': teacherKey } });
  if (!r.ok) { alert('Reset failed: ' + r.status + ' ' + (REASONS[r.status] || '')); if (r.status === 403) { teacherKey = null; store.set('teacherKey', null); applyTeacher(); } }
};
applyTeacher();

// ── Rendering ────────────────────────────────────────────────────────────────
const cards = new Map();   // id → card element

function renderStats(s) {
  const box = $('stats'); box.replaceChildren();
  const add = (n, label, cls) => {
    const d = el('div', 'stat' + (cls ? ' ' + cls : ''));
    d.append(el('div', 'n', n), el('div', 'label', label)); box.append(d);
    return d;
  };
  for (const m of METHODS) add(s.methods[m] || 0, m).querySelector('.n').classList.add('m-' + m);
  const byClass = { 2: 0, 3: 0, 4: 0, 5: 0 };
  for (const [code, n] of Object.entries(s.statuses)) byClass[code[0]] = (byClass[code[0]] || 0) + n;
  add(byClass[2], '2xx success', 'sep').querySelector('.n').classList.add('c2');
  add(byClass[4], '4xx client err').querySelector('.n').classList.add('c4');
  add(byClass[5], '5xx server err').querySelector('.n').classList.add('c5');
  add(s.devices, 'devices', 'sep');
}

function makeCard(s) {
  const c = el('div', 'card');
  const x = el('button', 'x', '×');
  x.title = 'Remove (sends DELETE /students/' + s.id + ')';
  x.onclick = () => fetch('/students/' + s.id, { method: 'DELETE' });
  c.append(el('span', 'id', '#' + s.id), el('span', 'name', s.name), x);
  return c;
}

function renderRoster(roster, changes = []) {
  const box = $('roster');
  const live = new Set(roster.map(s => s.id));
  for (const s of roster) {
    let c = cards.get(s.id);
    if (!c) { c = makeCard(s); cards.set(s.id, c); box.append(c); }
    c.querySelector('.name').textContent = s.name;
  }
  // Cards that vanished without a "remove" change (e.g. a reset) go immediately
  const removing = new Set(changes.filter(ch => ch.op === 'remove').map(ch => ch.id));
  for (const [id, c] of cards) if (!live.has(id) && !removing.has(id)) { c.remove(); cards.delete(id); }

  for (const ch of changes) {
    const c = cards.get(ch.id);
    if (!c) continue;
    c.querySelector('.old')?.remove();
    c.classList.remove('add', 'rename', 'remove');
    void c.offsetWidth;
    c.classList.add(ch.op);
    if (ch.op === 'rename') c.querySelector('.name').before(el('span', 'old', ch.old));
    if (ch.op === 'remove') {
      setTimeout(() => { c.remove(); if (cards.get(ch.id) === c) cards.delete(ch.id); }, 4000);
    } else {
      setTimeout(() => { c.classList.remove(ch.op); c.querySelector('.old')?.remove(); }, 6000);
    }
  }
  $('count').textContent = roster.length;
}

function feedRow(e, fresh) {
  const tr = el('tr', fresh ? 'fresh' : '');
  const code = el('td', 'code c' + String(e.status)[0], e.status + ' ' + (REASONS[e.status] || ''));
  const what = el('td', 'path');
  what.append(el('span', 'm m-' + e.method, e.method + ' '), document.createTextNode(e.path));
  for (const ch of e.changes) {
    const txt = ch.op === 'add' ? `+ added #${ch.id} “${ch.name}”`
              : ch.op === 'rename' ? `✎ #${ch.id} “${ch.old}” → “${ch.name}”`
              : `− removed #${ch.id} “${ch.name}”`;
    what.append(el('div', 'note', txt));
  }
  tr.append(el('td', 'time', new Date(e.t).toLocaleTimeString()), el('td', 'ip', e.ip), what, code);
  return tr;
}

function addFeed(e, fresh) {
  const body = $('feed').tBodies[0];
  body.prepend(feedRow(e, fresh));
  while (body.rows.length > FEED_MAX) body.lastChild.remove();
  $('feed-empty').hidden = true;
}

// ── Live connection ──────────────────────────────────────────────────────────
function connect() {
  const src = new EventSource('/dashboard/events');
  src.addEventListener('open', () => { $('conn').className = 'live'; $('conn-text').textContent = 'live'; });
  src.addEventListener('error', () => { $('conn').className = ''; $('conn-text').textContent = 'reconnecting…'; });
  src.addEventListener('snapshot', m => {
    const s = JSON.parse(m.data);
    $('feed').tBodies[0].replaceChildren();
    $('feed-empty').hidden = s.events.length > 0;
    for (const e of s.events) addFeed(e, false);
    cards.forEach(c => c.remove()); cards.clear();
    renderRoster(s.roster); renderStats(s);
  });
  src.addEventListener('update', m => {
    const s = JSON.parse(m.data);
    addFeed(s.event, true);
    renderRoster(s.roster, s.event.changes); renderStats(s);
  });
}
connect();
</script>
</body>
</html>
"""
