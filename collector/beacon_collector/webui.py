"""Read-mostly device console: `beacon serve`.

A SEPARATE process from the collector, deliberately. It shares only the SQLite
registry, so it cannot wedge a stream, cannot hold the event loop, and can be
restarted at will while collection continues. Everything it writes goes to
`device_overrides`, which the collector re-reads on each discovery sweep — so
an edit lands without a restart.

stdlib http.server only: the collector's dependencies are requests/pyyaml/
pyarrow and a status page is not worth adding a web framework to a box whose
RAM budget is already spoken for.

NOT hardened. No auth, no CSRF token, no TLS. Bind it to localhost or a
tailnet address; it is an operator console for a lab bench, not a public
service.
"""
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>beacon · devices</title>
<style>
  :root {
    --bg:#0f1216; --panel:#161b22; --line:#232a34; --ink:#d7dee8;
    --dim:#7d8896; --accent:#4db6a0; --warn:#d99b3f; --bad:#d4614e;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f5f3; --panel:#fff; --line:#dcdfd9; --ink:#1d2329;
            --dim:#68727e; --accent:#1f7a68; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--mono);
         font-size:13px; line-height:1.5; }
  header { padding:18px 22px 14px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  h1 { margin:0; font-size:15px; font-weight:600; letter-spacing:.14em;
       text-transform:uppercase; }
  h1 span { color:var(--accent); }
  .meta { color:var(--dim); font-size:12px; }
  main { padding:18px 22px 40px; }
  table { width:100%; border-collapse:collapse; }
  th { text-align:left; font-weight:500; color:var(--dim); font-size:11px;
       letter-spacing:.1em; text-transform:uppercase; padding:0 10px 8px;
       border-bottom:1px solid var(--line); white-space:nowrap; }
  td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:middle; }
  tr:hover td { background:var(--panel); }
  .serial { font-weight:600; }
  .addr, .age { color:var(--dim); }
  .dot { display:inline-block; width:7px; height:7px; border-radius:50%;
         margin-right:7px; vertical-align:1px; }
  .up   { background:var(--accent); }
  .stale{ background:var(--warn); }
  .down { background:var(--bad); }
  .skip { opacity:.45; }
  input[type=text] { width:100%; min-width:90px; background:transparent; color:inherit;
    border:1px solid transparent; border-radius:3px; padding:3px 6px;
    font:inherit; }
  input[type=text]:hover { border-color:var(--line); }
  input[type=text]:focus { border-color:var(--accent); outline:none;
                           background:var(--bg); }
  input::placeholder { color:var(--dim); opacity:.6; }
  button { font:inherit; color:var(--dim); background:transparent;
    border:1px solid var(--line); border-radius:3px; padding:3px 9px;
    cursor:pointer; }
  button:hover { color:var(--ink); border-color:var(--accent); }
  .empty { color:var(--dim); padding:40px 10px; }
  footer { color:var(--dim); font-size:11px; padding:0 22px 30px; max-width:70ch; }
  code { color:var(--accent); }
  .flash { position:fixed; right:18px; bottom:16px; background:var(--panel);
    border:1px solid var(--accent); border-radius:3px; padding:7px 13px;
    opacity:0; transition:opacity .18s; }
  .flash.on { opacity:1; }
</style>
<header>
  <h1>beacon<span>/</span>devices</h1>
  <div class="meta" id="meta">loading…</div>
</header>
<main>
  <table>
    <thead><tr>
      <th>status</th><th>serial</th><th>name</th><th>address</th>
      <th>app package</th><th>last seen</th><th>skip</th><th></th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" hidden>
    No devices yet. Discovery adopts anything on the subnet with adb exposed —
    give it one sweep interval, or start the collector if it is not running.
  </div>
</main>
<footer>
  Edits write to <code>device_overrides</code> in the registry and are picked up
  on the collector's next discovery sweep — no restart. Blank name falls back to
  the serial. Blank app package inherits the global; type <code>-</code> to
  clear an override, or set it empty to tail no app log at all.
</footer>
<div class="flash" id="flash"></div>
<script>
const $ = s => document.querySelector(s);
const fmtAge = s => s == null ? "never"
  : s < 60 ? Math.round(s) + "s ago"
  : s < 3600 ? Math.round(s/60) + "m ago"
  : s < 86400 ? (s/3600).toFixed(1) + "h ago"
  : (s/86400).toFixed(1) + "d ago";

function cls(age, skip) {
  if (skip) return "skip";
  if (age == null) return "down";
  return age < 120 ? "up" : age < 900 ? "stale" : "down";
}

async function load() {
  const r = await fetch("api/devices");
  const d = await r.json();
  $("#meta").textContent =
    `${d.nuc_id} · ${d.devices.length} device(s) · discovery ` +
    (d.discovery.enabled ? `${d.discovery.subnet} every ${d.discovery.interval}s` : "off");
  const rows = d.devices.map(x => {
    const age = x.last_seen == null ? null : d.now - x.last_seen;
    return `<tr data-serial="${x.serial}" class="${x.skip ? 'skip' : ''}">
      <td><span class="dot ${cls(age, x.skip)}"></span>${x.skip ? "skipped" : (age != null && age < 120 ? "live" : "idle")}</td>
      <td class="serial">${x.serial}</td>
      <td><input type="text" data-f="friendly_name" value="${x.friendly_name === x.serial ? '' : (x.friendly_name ?? '')}" placeholder="${x.serial}"></td>
      <td class="addr">${x.address ?? "—"}</td>
      <td><input type="text" data-f="app_package" value="${x.app_package ?? ''}" placeholder="${d.app_package}"></td>
      <td class="age">${fmtAge(age)}</td>
      <td><input type="checkbox" data-f="skip" ${x.skip ? "checked" : ""}></td>
      <td><button data-act="save">save</button></td>
    </tr>`;
  }).join("");
  $("#rows").innerHTML = rows;
  $("#empty").hidden = d.devices.length > 0;
}

function flash(msg) {
  const f = $("#flash"); f.textContent = msg; f.classList.add("on");
  setTimeout(() => f.classList.remove("on"), 1400);
}

document.addEventListener("click", async e => {
  if (e.target.dataset.act !== "save") return;
  const tr = e.target.closest("tr");
  const body = { serial: tr.dataset.serial };
  tr.querySelectorAll("[data-f]").forEach(i => {
    body[i.dataset.f] = i.type === "checkbox" ? i.checked : i.value;
  });
  const r = await fetch("api/devices", {
    method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify(body) });
  flash(r.ok ? "saved · applies next sweep" : "save failed");
  if (r.ok) load();
});

load();
setInterval(load, 10000);
</script>
"""


class _Handler(BaseHTTPRequestHandler):
    registry = None
    cfg = None

    def log_message(self, fmt, *args):        # quiet: stdout is the run log
        log.debug("webui %s", fmt % args)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            return self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/devices":
            return self._json(200, self._snapshot())
        if path == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path != "/api/devices":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})

        serial = (body.get("serial") or "").strip()
        if not serial:
            return self._json(400, {"error": "serial is required"})

        self.registry.set_override(
            serial,
            friendly_name=body.get("friendly_name"),
            app_package=body.get("app_package"),
            skip=body.get("skip"),
        )
        log.info("webui: override set for %s: %s", serial,
                 {k: v for k, v in body.items() if k != "serial"})
        return self._json(200, {"ok": True, "serial": serial})

    # -- data ----------------------------------------------------------------

    def _snapshot(self) -> dict:
        """Union of what the config declares and what the registry has seen.

        A declared-but-never-connected device still shows up (last_seen null),
        which is the whole point: that is how you notice a stick that never
        arrived, rather than it being absent from every view.
        """
        rows = {d["serial"]: d for d in self.registry.list_devices()}
        ov = self.registry.overrides()
        for d in self.cfg.my_devices():
            r = rows.setdefault(d.serial, {
                "serial": d.serial, "address": d.address, "nuc": d.nuc,
                "friendly_name": d.friendly_name or d.serial,
                "app_package": d.app_package, "skip": False,
                "first_seen": None, "last_seen": None, "overridden": False,
            })
            r["declared"] = True
        for serial, o in ov.items():
            r = rows.setdefault(serial, {
                "serial": serial, "address": None, "nuc": self.cfg.nuc_id,
                "friendly_name": serial, "app_package": None, "skip": False,
                "first_seen": None, "last_seen": None, "overridden": True,
            })
            r["skip"] = o["skip"]
            if o["friendly_name"]:
                r["friendly_name"] = o["friendly_name"]
            if o["app_package"] is not None:
                r["app_package"] = o["app_package"]
        return {
            "now": time.time(),
            "nuc_id": self.cfg.nuc_id,
            "app_package": self.cfg.app_package,
            "discovery": {
                "enabled": self.cfg.discovery.enabled,
                "subnet": self.cfg.discovery.subnet,
                "interval": self.cfg.discovery.interval,
            },
            "devices": sorted(rows.values(),
                              key=lambda r: (r["last_seen"] is None,
                                             -(r["last_seen"] or 0))),
        }


def serve(cfg, registry, host: str = "127.0.0.1", port: int = 8088):
    _Handler.registry = registry
    _Handler.cfg = cfg
    # Single-threaded ON PURPOSE. sqlite3 connections are bound to the thread
    # that created them, and ThreadingHTTPServer hands each request to a new
    # one — which raises ProgrammingError on the first write. Plain HTTPServer
    # handles every request in this thread, the same one that opened the
    # registry. One operator polling every 10 s does not need concurrency; if
    # that ever changes, give each request its own Registry rather than
    # reaching for the threading mixin again.
    srv = HTTPServer((host, port), _Handler)
    log.info("device console on http://%s:%d/", host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
