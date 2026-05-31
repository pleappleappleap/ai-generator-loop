#!/usr/bin/env python3
"""Strategic UI — thin review interface for strategic sessions.

Serves a web page that lets the operator:
  - See the current strategic session state (polling the pipeline REST API)
  - Trigger a strategic review run
  - Write a north star directly
  - Resume the loop once the strategic session is done

Delegates all intelligence to the pipeline REST API at PIPELINE_URL (default http://localhost:12000).
"""

import os
import sys
import json
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

PIPELINE_URL = os.environ.get("PIPELINE_URL", "http://localhost:12000")
PORT = int(os.environ.get("STRATEGIC_UI_PORT", "12000"))

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strategic Review</title>
<style>
  body{font-family:monospace;background:#111;color:#ddd;max-width:900px;margin:40px auto;padding:0 20px}
  h1{color:#aef;border-bottom:1px solid #333;padding-bottom:8px}
  .card{background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:16px;margin:16px 0}
  .card h2{margin:0 0 10px 0;font-size:1em;color:#888;text-transform:uppercase;letter-spacing:.05em}
  .state{display:inline-block;padding:3px 10px;border-radius:3px;font-weight:bold}
  .state.idle{background:#333;color:#999}
  .state.running{background:#2a3a1a;color:#8f8}
  .state.complete{background:#1a2a3a;color:#88f}
  .state.failed{background:#3a1a1a;color:#f88}
  pre{white-space:pre-wrap;word-break:break-word;font-size:.85em;color:#aaa;margin:0}
  button{background:#223;color:#aef;border:1px solid #446;border-radius:4px;padding:8px 18px;
         cursor:pointer;font-family:monospace;font-size:.9em;margin:4px 2px}
  button:hover{background:#334}
  button:disabled{opacity:.4;cursor:default}
  textarea{width:100%;box-sizing:border-box;background:#111;color:#ddd;border:1px solid #333;
           border-radius:4px;padding:8px;font-family:monospace;font-size:.85em;resize:vertical}
  .err{color:#f88}
  #refresh{color:#555;font-size:.8em;float:right}
</style>
</head>
<body>
<h1>Strategic Review <span id="refresh"></span></h1>

<div class="card">
  <h2>Session State</h2>
  <p><span id="state-badge" class="state idle">idle</span></p>
  <p id="reasoning-line" style="display:none"><strong>Reasoning:</strong> <span id="reasoning"></span></p>
  <p id="error-line" style="display:none" class="err">Error: <span id="errmsg"></span></p>
  <button id="btn-run" onclick="runSession()">Run Strategic Review</button>
  <button onclick="refreshStatus()">Refresh</button>
</div>

<div class="card" id="north-star-card" style="display:none">
  <h2>Proposed North Star</h2>
  <pre id="north-star-text"></pre>
</div>

<div class="card" id="session-dir-card" style="display:none">
  <h2>Proposed Session Direction</h2>
  <pre id="session-dir-text"></pre>
</div>

<div class="card" id="analysis-card" style="display:none">
  <h2>Analysis</h2>
  <pre id="analysis-text"></pre>
</div>

<div class="card">
  <h2>Write North Star Directly</h2>
  <p style="color:#777;font-size:.85em">Bypasses the strategic LLM — writes immediately to the database.</p>
  <textarea id="ns-input" rows="4" placeholder="Enter north star text..."></textarea><br>
  <button onclick="writeNorthStar()">Write North Star</button>
  <span id="ns-status" style="color:#8f8;margin-left:10px"></span>
</div>

<script>
async function api(method, path, body) {
  const opts = {method, headers:{'Content-Type':'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch('/proxy?url=' + encodeURIComponent(path), opts);
  return r.json();
}

async function refreshStatus() {
  try {
    const d = await api('GET', '/strategic/status');
    const badge = document.getElementById('state-badge');
    badge.textContent = d.state || 'unknown';
    badge.className = 'state ' + (d.state || 'idle');

    const runBtn = document.getElementById('btn-run');
    runBtn.disabled = d.state === 'running';

    const setBlock = (id, cardId, val) => {
      if (val) {
        document.getElementById(id).textContent = val;
        document.getElementById(cardId).style.display = '';
      } else {
        document.getElementById(cardId).style.display = 'none';
      }
    };
    setBlock('north-star-text', 'north-star-card', d.north_star);
    setBlock('session-dir-text', 'session-dir-card', d.session_direction);
    setBlock('analysis-text', 'analysis-card', d.analysis);

    if (d.reasoning) {
      document.getElementById('reasoning').textContent = d.reasoning;
      document.getElementById('reasoning-line').style.display = '';
    } else {
      document.getElementById('reasoning-line').style.display = 'none';
    }
    if (d.error) {
      document.getElementById('errmsg').textContent = d.error;
      document.getElementById('error-line').style.display = '';
    } else {
      document.getElementById('error-line').style.display = 'none';
    }
    document.getElementById('refresh').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('state-badge').textContent = 'pipeline unreachable';
    document.getElementById('state-badge').className = 'state failed';
  }
}

async function runSession() {
  document.getElementById('btn-run').disabled = true;
  try {
    await api('POST', '/strategic/run');
    refreshStatus();
    const poll = setInterval(async () => {
      await refreshStatus();
      const s = document.getElementById('state-badge').textContent;
      if (s !== 'running') clearInterval(poll);
    }, 3000);
  } catch(e) {
    document.getElementById('btn-run').disabled = false;
  }
}

async function writeNorthStar() {
  const content = document.getElementById('ns-input').value.trim();
  if (!content) return;
  try {
    await api('POST', '/north-star', {content});
    document.getElementById('ns-status').textContent = 'saved ✓';
    document.getElementById('ns-input').value = '';
    setTimeout(() => document.getElementById('ns-status').textContent = '', 3000);
  } catch(e) {
    document.getElementById('ns-status').textContent = 'error: ' + e.message;
  }
}

refreshStatus();
setInterval(refreshStatus, 10000);
</script>
</body>
</html>
"""


def _proxy(method, path, body=None):
    url = PIPELINE_URL.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 502, json.dumps({"error": str(e)}).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default access log
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/proxy?url="):
            target = urllib.parse.unquote(self.path[len("/proxy?url="):])
            status, data = _proxy("GET", target)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/proxy?url="):
            target = urllib.parse.unquote(self.path[len("/proxy?url="):])
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            status, data = _proxy("POST", target, body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


import urllib.parse


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Strategic UI listening on http://0.0.0.0:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
