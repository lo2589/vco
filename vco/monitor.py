"""Local monitor HTTP service: watch run event streams in a browser."""

from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .events import emit

MAX_BODY_BYTES = 64 * 1024

_KIND_LABELS = {
    "step_start": "在干",
    "observation": "看到",
    "action_result": "得到",
    "page_error": "页面报错",
    "user_click": "用户点击",
    "annotation": "用户批注",
    "run_end": "结束",
}


def resolve_run_path(cache_root: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``cache_root``, refusing traversal outside it."""

    root = cache_root.resolve()
    candidate = (root / unquote(rel).lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def list_runs(cache_root: Path) -> list[dict]:
    """Find run directories (one nesting level deep) that have events.jsonl."""

    root = cache_root.resolve()
    runs = []
    if not root.is_dir():
        return runs
    candidates = [p / "events.jsonl" for p in root.iterdir() if p.is_dir()]
    for child in root.iterdir():
        if child.is_dir():
            candidates.extend(
                p / "events.jsonl" for p in child.iterdir() if p.is_dir()
            )
    seen = set()
    for events_file in candidates:
        if events_file in seen or not events_file.is_file():
            continue
        seen.add(events_file)
        first_summary = ""
        try:
            with open(events_file, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        first_summary = json.loads(line).get("summary") or ""
                    except json.JSONDecodeError:
                        continue
                    if first_summary:
                        break
        except OSError:
            continue
        run_dir = events_file.parent
        runs.append(
            {
                "path": run_dir.relative_to(root).as_posix(),
                "mtime": events_file.stat().st_mtime,
                "first_summary": first_summary,
            }
        )
    runs.sort(key=lambda item: item["mtime"], reverse=True)
    return runs


def read_events(run_dir: Path, after: int = 0) -> dict:
    """Return events from line ``after`` on plus the next cursor."""

    events = []
    total = 0
    try:
        with open(run_dir / "events.jsonl", encoding="utf-8") as fh:
            for total, line in enumerate(fh, start=1):
                if total <= after:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return {"events": events, "next": max(after, total)}


def latest_marks(run_dir: Path) -> Path | None:
    """Newest marks-*.json in a run directory, produced by the debug pipeline."""

    marks = sorted(
        run_dir.glob("marks-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return marks[0] if marks else None


def annotate_run(run_dir: Path, mark_id: object, text: str) -> dict:
    """Persist a user annotation and mirror it into the run's event stream."""

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": mark_id,
        "text": text,
    }
    with open(run_dir / "annotations.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    emit(
        run_dir,
        "annotation",
        summary=f"#{mark_id}: {text}",
        data={"id": mark_id, "text": text},
    )
    return record


def render_index_html() -> str:
    labels_js = json.dumps(_KIND_LABELS, ensure_ascii=False)
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>VCO 监工</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 -apple-system, "PingFang SC", sans-serif;
         display: flex; height: 100vh; color: #222; }
  #runs { width: 260px; border-right: 1px solid #ddd; overflow-y: auto;
          padding: 8px; background: #fafafa; }
  #runs h2 { font-size: 13px; margin: 4px 0 8px; color: #888; }
  .run { padding: 6px 8px; border-radius: 6px; cursor: pointer;
         word-break: break-all; }
  .run:hover { background: #eee; }
  .run.active { background: #dbeafe; }
  .run .summary { color: #666; font-size: 12px; }
  #main { flex: 1; overflow-y: auto; padding: 12px 16px; }
  #marks { display: none; margin-bottom: 12px; }
  #marks img { max-width: 480px; border: 1px solid #ccc; border-radius: 4px;
               cursor: crosshair; }
  .event { display: flex; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0; }
  .chip { flex: none; min-width: 64px; text-align: center; border-radius: 4px;
          padding: 0 6px; height: 22px; font-size: 12px; background: #e5e7eb; }
  .kind-observation { background: #dcfce7; }
  .kind-action_result { background: #dbeafe; }
  .kind-page_error { background: #fee2e2; }
  .kind-annotation, .kind-user_click { background: #fef9c3; }
  .kind-run_end { background: #e9d5ff; }
  .meta { color: #999; font-size: 12px; margin-right: 6px; }
  .event img { display: block; max-width: 320px; margin-top: 4px;
               border: 1px solid #ddd; border-radius: 4px; cursor: zoom-in; }
  #annotate { display: none; gap: 6px; margin-bottom: 10px; }
  #annotate input { padding: 4px 6px; border: 1px solid #ccc; border-radius: 4px; }
</style>
</head>
<body>
<div id="runs"><h2>Runs</h2><div id="run-list"></div></div>
<div id="main">
  <div id="marks">最新标注图(点击图上元素写批注):<br><img id="marks-img" alt=""></div>
  <div id="annotate">
    <input id="ann-id" placeholder="编号" size="6">
    <input id="ann-text" placeholder="批注内容" size="40">
    <button id="ann-send">提交批注</button>
  </div>
  <div id="timeline"></div>
</div>
<script>
const KIND_LABELS = __LABELS__;
let currentRun = null, after = 0, pinned = false, timer = null;

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

function selectRun(path, manual) {
  if (manual) pinned = true;
  if (path === currentRun) return;
  currentRun = path; after = 0;
  document.getElementById('timeline').innerHTML = '';
  document.querySelectorAll('.run').forEach(el =>
    el.classList.toggle('active', el.dataset.path === path));
  loadMarks();
  refreshEvents();
}

async function refreshRuns() {
  try {
    const runs = await getJSON('/api/runs');
    const list = document.getElementById('run-list');
    list.innerHTML = '';
    for (const run of runs) {
      const div = document.createElement('div');
      div.className = 'run' + (run.path === currentRun ? ' active' : '');
      div.dataset.path = run.path;
      const name = document.createElement('div');
      name.textContent = run.path;
      const sum = document.createElement('div');
      sum.className = 'summary';
      sum.textContent = run.first_summary || '';
      div.append(name, sum);
      div.onclick = () => selectRun(run.path, true);
      list.appendChild(div);
    }
    if (!pinned && runs.length && currentRun !== runs[0].path) {
      selectRun(runs[0].path, false);
    }
  } catch (e) {}
}

function renderEvent(ev) {
  const row = document.createElement('div');
  row.className = 'event';
  const chip = document.createElement('span');
  chip.className = 'chip kind-' + ev.kind;
  chip.textContent = KIND_LABELS[ev.kind] || ev.kind;
  const body = document.createElement('div');
  const meta = document.createElement('span');
  meta.className = 'meta';
  const step = ev.step != null ? 'step ' + ev.step + ' · ' : '';
  meta.textContent = step + new Date(ev.ts).toLocaleTimeString();
  const text = document.createElement('span');
  text.textContent = ev.summary || '';
  body.append(meta, text);
  if (ev.image) {
    const img = document.createElement('img');
    img.src = '/runs/' + currentRun + '/' + ev.image;
    img.loading = 'lazy';
    img.onclick = () => window.open(img.src, '_blank');
    body.appendChild(img);
  }
  row.append(chip, body);
  document.getElementById('timeline').appendChild(row);
}

async function refreshEvents() {
  if (!currentRun) return;
  try {
    const r = await getJSON('/api/events?run=' + encodeURIComponent(currentRun) +
                            '&after=' + after);
    r.events.forEach(renderEvent);
    after = r.next;
  } catch (e) {}
}

async function loadMarks() {
  const box = document.getElementById('marks');
  const annotate = document.getElementById('annotate');
  if (!currentRun) { box.style.display = 'none'; return; }
  try {
    const r = await fetch('/api/marks?run=' + encodeURIComponent(currentRun));
    if (!r.ok) throw new Error(r.status);
    const file = r.headers.get('X-Marks-File') || '';
    await r.json();
    const img = document.getElementById('marks-img');
    img.src = '/runs/' + currentRun + '/' + file.replace(/\\.json$/, '.png');
    img.onclick = () => {
      const id = prompt('批注哪个编号?');
      if (id === null) return;
      const text = prompt('批注内容:');
      if (text) sendAnnotation(id, text);
    };
    box.style.display = 'block';
    annotate.style.display = 'flex';
  } catch (e) {
    box.style.display = 'none';
    annotate.style.display = 'flex';
  }
}

async function sendAnnotation(id, text) {
  await fetch('/api/annotate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({run: currentRun, id: id, text: text}),
  });
  refreshEvents();
}

document.getElementById('ann-send').onclick = () => {
  const id = document.getElementById('ann-id').value.trim();
  const text = document.getElementById('ann-text').value.trim();
  if (currentRun && text) sendAnnotation(id || '?', text);
  document.getElementById('ann-text').value = '';
};

async function tick() { await refreshRuns(); await refreshEvents(); }
tick();
timer = setInterval(tick, 1500);
</script>
</body>
</html>
""".replace("__LABELS__", labels_js)


def make_handler(cache_root: Path, *, api_key: str | None = None):
    root = cache_root.resolve()

    def run_dir_from(query: dict) -> Path | None:
        rel = (query.get("run") or [""])[0]
        if not rel:
            return None
        path = resolve_run_path(root, rel)
        if path is None or path == root or not path.is_dir():
            return None
        return path

    class MonitorHandler(BaseHTTPRequestHandler):
        server_version = "VCO-Monitor/1"

        def _json(self, status: int, payload, headers: dict | None = None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, markup: str):
            body = markup.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not api_key:
                return True
            if self.headers.get("Authorization") == f"Bearer {api_key}":
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def do_GET(self):
            if not self._authorized():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/":
                self._html(200, render_index_html())
            elif path == "/api/runs":
                self._json(200, list_runs(root))
            elif path == "/api/events":
                run_dir = run_dir_from(query)
                if run_dir is None:
                    self._json(404, {"error": "unknown run"})
                    return
                try:
                    after = int((query.get("after") or ["0"])[0])
                except ValueError:
                    after = 0
                self._json(200, read_events(run_dir, max(0, after)))
            elif path == "/api/marks":
                run_dir = run_dir_from(query)
                marks = latest_marks(run_dir) if run_dir else None
                if marks is None:
                    self._json(404, {"error": "no marks"})
                    return
                try:
                    payload = json.loads(marks.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    self._json(500, {"error": "unreadable marks"})
                    return
                self._json(200, payload, {"X-Marks-File": marks.name})
            elif path.startswith("/runs/"):
                target = resolve_run_path(root, path[len("/runs/"):])
                if target is None or target == root:
                    self._json(403, {"error": "forbidden"})
                    return
                if not target.is_file():
                    self._json(404, {"error": "not found"})
                    return
                self._file(target)
            else:
                self._json(404, {"error": "not found"})

        def _file(self, target: Path):
            content_type = mimetypes.guess_type(target.name)[0]
            if target.suffix == ".jsonl":
                content_type = "text/plain; charset=utf-8"
            try:
                body = target.read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", content_type or "application/octet-stream"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self._authorized():
                return
            if urlparse(self.path).path != "/api/annotate":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("invalid Content-Length")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                rel = payload.get("run")
                text = payload.get("text")
                if not isinstance(rel, str) or not rel:
                    raise ValueError("run is required")
                if not isinstance(text, str) or not text:
                    raise ValueError("text is required")
                run_dir = resolve_run_path(root, rel)
                if run_dir is None or run_dir == root or not run_dir.is_dir():
                    self._json(404, {"error": "unknown run"})
                    return
                record = annotate_run(run_dir, payload.get("id"), text)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, record)

        def log_message(self, format, *args):
            return

    return MonitorHandler


def serve_monitor(
    cache_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    api_key: str | None = None,
) -> None:
    """Serve the monitor page and run APIs until interrupted."""

    if host not in {"127.0.0.1", "localhost", "::1"} and not api_key:
        raise ValueError("a bearer token is required when binding outside localhost")
    server = ThreadingHTTPServer((host, port), make_handler(cache_root, api_key=api_key))
    print(f"VCO monitor listening on http://{host}:{port}/ (cache: {cache_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
