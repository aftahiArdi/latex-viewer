#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DOCUMENTS_DIR = Path("/documents")
BUILD_DIRNAME = ".build"
PORT = 8080

_tex_mtimes: dict[str, float] = {}
_compiling: set[str] = set()
# Projects whose source changed while a compile was already running.
_pending: set[str] = set()
_lock = threading.Lock()
_watch_heartbeat = time.monotonic()
WATCH_STALE_AFTER = 30  # seconds without a watcher pass before /healthz fails
PDF_WAIT_SECONDS = 5  # how long /pdf waits for an in-progress write to finish


def _pdflatex(project_dir: Path) -> bool:
    result = subprocess.run(
        [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={BUILD_DIRNAME}",
            "main.tex",
        ],
        cwd=project_dir,
        capture_output=True,
        timeout=120,
    )
    return result.returncode == 0


def _compile_once(project_dir: Path) -> None:
    try:
        # pdflatex truncates and rewrites its output in place, fonts last, so
        # serving that file mid-compile hands out a PDF with missing glyphs.
        # Build in a private directory and swap the finished PDF in atomically.
        build_dir = project_dir / BUILD_DIRNAME
        build_dir.mkdir(exist_ok=True)
        built_pdf = build_dir / "main.pdf"
        built_log = build_dir / "main.log"
        # A failed earlier run can leave a partial PDF here; never promote it.
        built_pdf.unlink(missing_ok=True)
        ok = _pdflatex(project_dir)
        if not ok:
            # A killed or crashed run can leave a truncated .aux that breaks
            # every later compile. Retry once from a clean build directory so
            # only genuine errors in the document keep failing.
            for leftover in build_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink()
            ok = _pdflatex(project_dir)
        if built_log.exists():
            os.replace(built_log, project_dir / "main.log")
        # On failure keep serving the last good PDF rather than a partial one.
        if ok and _is_complete_pdf_file(built_pdf):
            os.replace(built_pdf, project_dir / "main.pdf")
    except Exception:
        # A hung or failing pdflatex must never take down the worker thread.
        traceback.print_exc()


def _run_pdflatex(project_dir: Path) -> None:
    name = project_dir.name
    with _lock:
        if name in _compiling:
            # The running worker recompiles once its current pass finishes, so
            # a save that lands mid-compile is never lost.
            _pending.add(name)
            return
        _compiling.add(name)
    try:
        while True:
            with _lock:
                _pending.discard(name)
            _compile_once(project_dir)
            with _lock:
                # Check and release under one lock hold, so a request arriving
                # now either sees _compiling and queues, or starts a new worker.
                if name not in _pending:
                    _compiling.discard(name)
                    return
    except BaseException:
        with _lock:
            _compiling.discard(name)
            _pending.discard(name)
        raise


def _compile(project_dir: Path) -> None:
    threading.Thread(target=_run_pdflatex, args=(project_dir,), daemon=True).start()


def _is_complete_pdf(data: bytes) -> bool:
    # pdfTeX writes the %%EOF marker last; a truncated file never ends with it.
    return data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]


def _is_complete_pdf_file(path: Path) -> bool:
    try:
        return _is_complete_pdf(path.read_bytes())
    except OSError:
        return False


def _watch() -> None:
    global _watch_heartbeat
    while True:
        try:
            projects = [d for d in DOCUMENTS_DIR.iterdir() if d.is_dir()]
        except Exception:
            projects = []
        for d in projects:
            # One unreadable project must not stop the others being watched.
            try:
                tex = d / "main.tex"
                if not tex.exists():
                    continue
                mtime = tex.stat().st_mtime
                # First sight (server start, or a project created while running)
                # compiles too, so a new project gets a PDF without a second save.
                if _tex_mtimes.get(d.name) != mtime:
                    _tex_mtimes[d.name] = mtime
                    _compile(d)
            except Exception:
                traceback.print_exc()
        _watch_heartbeat = time.monotonic()
        time.sleep(1)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>LaTeX Workspace</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1e1e2e; color: #cdd6f4; height: 100vh; display: flex; flex-direction: column; }
  header { background: #181825; border-bottom: 1px solid #313244; padding: 10px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  header h1 { font-size: 15px; font-weight: 600; color: #cba6f7; letter-spacing: .02em; }
  .layout { display: flex; flex: 1; overflow: hidden; }
  .sidebar { width: 220px; background: #181825; border-right: 1px solid #313244; padding: 12px 8px; overflow-y: auto; flex-shrink: 0; transition: width 0.2s, padding 0.2s; }
  .sidebar.collapsed { width: 0; padding: 0; overflow: hidden; border-right: none; }
  .sidebar h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #6c7086; padding: 0 8px 10px; }
  .project { padding: 8px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; color: #bac2de; }
  .project:hover { background: #313244; }
  .project.active { background: #45475a; color: #cdd6f4; font-weight: 500; }
  .toggle-btn { background: none; border: none; color: #6c7086; cursor: pointer; font-size: 16px; padding: 2px 4px; line-height: 1; }
  .toggle-btn:hover { color: #cdd6f4; }
  .viewer { flex: 1; display: flex; flex-direction: column; }
  .toolbar { background: #181825; border-bottom: 1px solid #313244; padding: 8px 14px; display: flex; align-items: center; gap: 10px; flex-shrink: 0; font-size: 13px; }
  .toolbar .name { font-weight: 500; color: #cdd6f4; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: #313244; color: #6c7086; }
  .badge.compiling { background: #f9e2af20; color: #f9e2af; }
  .badge.ready { background: #a6e3a120; color: #a6e3a1; }
  .badge.updated { background: #89b4fa20; color: #89b4fa; }
  .badge.error { background: #f38ba820; color: #f38ba8; }
  .btn { margin-left: auto; padding: 4px 14px; background: #cba6f7; color: #1e1e2e; border: none; border-radius: 5px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .btn:hover { background: #d4b3f8; }
  iframe { flex: 1; border: none; background: white; }
  .empty { flex: 1; display: flex; align-items: center; justify-content: center; color: #45475a; font-size: 14px; flex-direction: column; gap: 8px; }
  .empty span { font-size: 32px; }
</style>
</head>
<body>
<header>
  <button class="toggle-btn" onclick="toggleSidebar()" title="Toggle sidebar">☰</button>
  <h1>LaTeX Workspace</h1>
</header>
<div class="layout">
  <nav class="sidebar" id="sidebar">
    <h2>Projects</h2>
    <div id="project-list"></div>
  </nav>
  <div class="viewer">
    <div class="toolbar">
      <span class="name" id="proj-name">No project selected</span>
      <span class="badge" id="status">—</span>
      <button class="btn" onclick="compileNow()">Compile</button>
    </div>
    <div id="viewer-area" class="empty"><span>📄</span>Select a project to view its PDF</div>
  </div>
</div>
<script>
let current = null, lastMtime = 0, pollTimer = null;

async function refreshProjects() {
  const res = await fetch('/projects').catch(() => null);
  if (!res) return;
  const projects = await res.json();
  const list = document.getElementById('project-list');
  list.innerHTML = '';
  projects.forEach(p => {
    const el = document.createElement('div');
    el.className = 'project' + (current === p.name ? ' active' : '');
    el.textContent = p.name;
    el.onclick = () => select(p.name);
    list.appendChild(el);
  });
}

function select(name) {
  current = name;
  lastMtime = 0;
  document.getElementById('proj-name').textContent = name;
  document.querySelectorAll('.project').forEach(el =>
    el.classList.toggle('active', el.textContent === name));
  const area = document.getElementById('viewer-area');
  area.outerHTML = '<iframe id="viewer-area" src="about:blank"></iframe>';
  setStatus('compiling', 'Loading…');
  loadPdf();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 2000);
}

function loadPdf() {
  const frame = document.getElementById('viewer-area');
  if (frame && frame.tagName === 'IFRAME') {
    frame.src = '/pdf/' + current + '?t=' + Date.now() + '#pagemode=none';
    setStatus('ready', 'Ready');
  }
}

async function poll() {
  if (!current) return;
  const res = await fetch('/mtime/' + current).catch(() => null);
  if (!res) return;
  const { mtime } = await res.json();
  if (!mtime) return;
  if (!lastMtime) { lastMtime = mtime; return; }
  if (mtime !== lastMtime) {
    lastMtime = mtime;
    setStatus('updated', 'Updated!');
    loadPdf();
    setTimeout(() => setStatus('ready', 'Ready'), 2000);
  }
}

async function compileNow() {
  if (!current) return;
  setStatus('compiling', 'Compiling…');
  await fetch('/compile/' + current);
  setTimeout(poll, 4000);
}

function setStatus(cls, text) {
  const el = document.getElementById('status');
  el.className = 'badge ' + cls;
  el.textContent = text;
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('collapsed');
}

refreshProjects();
setInterval(refreshProjects, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def handle_one_request(self) -> None:
        # A client that disconnects mid-response raises here; that is normal and
        # must never surface as a traceback or take the connection thread down.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True

    def do_GET(self) -> None:
        p = urlparse(self.path).path.rstrip("/") or "/"

        try:
            if p in ("/", "/index.html"):
                self._send(200, "text/html", INDEX_HTML.encode())
            elif p == "/healthz":
                self._serve_health()
            elif p == "/projects":
                self._json(self._list_projects())
            elif p.startswith("/pdf/"):
                self._serve_pdf(p[5:])
            elif p.startswith("/mtime/"):
                self._serve_mtime(p[7:])
            elif p.startswith("/compile/"):
                self._trigger_compile(p[9:])
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True
        except Exception:
            # Any unexpected failure must return an error, not kill the handler.
            traceback.print_exc()
            try:
                self.send_error(500)
            except Exception:
                self.close_connection = True

    def _project_dir(self, name: str) -> Path | None:
        """Resolve a project name to a directory, refusing anything outside /documents."""
        candidate = (DOCUMENTS_DIR / unquote(name)).resolve()
        if candidate.parent != DOCUMENTS_DIR.resolve() or not candidate.is_dir():
            return None
        return candidate

    def _list_projects(self) -> list:
        out = []
        if DOCUMENTS_DIR.exists():
            for d in sorted(DOCUMENTS_DIR.iterdir()):
                if d.is_dir() and (d / "main.tex").exists():
                    out.append({"name": d.name, "has_pdf": (d / "main.pdf").exists()})
        return out

    def _serve_pdf(self, name: str) -> None:
        d = self._project_dir(name)
        if d is None:
            self.send_error(404)
            return
        pdf = d / "main.pdf"
        # Our own compiles swap the PDF in atomically, but a pdflatex run
        # outside this server (e.g. on the host) still writes it in place.
        # Wait for it to finish instead of sending a truncated file.
        deadline = time.monotonic() + PDF_WAIT_SECONDS
        while True:
            try:
                data = pdf.read_bytes()
            except OSError:
                data = None
            if data is not None and _is_complete_pdf(data):
                break
            if time.monotonic() >= deadline:
                if data is None:
                    self.send_error(404)
                else:
                    self._send(
                        503,
                        "text/plain",
                        b"PDF is being written, retry shortly\n",
                        {"Retry-After": "2", "Cache-Control": "no-store"},
                    )
                return
            time.sleep(0.25)
        self._send(200, "application/pdf", data, {"Cache-Control": "no-cache"})

    def _serve_health(self) -> None:
        stale = time.monotonic() - _watch_heartbeat
        if stale > WATCH_STALE_AFTER:
            # A dead watcher means edits silently stop compiling; report
            # unhealthy so autoheal restarts the container.
            body = json.dumps({"status": "watcher stalled", "seconds": round(stale)})
            self._send(503, "application/json", body.encode())
            return
        self._json({"status": "ok"})

    def _serve_mtime(self, name: str) -> None:
        d = self._project_dir(name)
        try:
            mtime = (d / "main.pdf").stat().st_mtime if d else 0
        except OSError:
            mtime = 0
        self._json({"mtime": mtime})

    def _trigger_compile(self, name: str) -> None:
        d = self._project_dir(name)
        if d and (d / "main.tex").exists():
            _compile(d)
        self._json({"status": "compiling"})

    def _json(self, obj: object) -> None:
        self._send(200, "application/json", json.dumps(obj).encode())

    def _send(self, code: int, ct: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_) -> None:
        pass


BUILD_OUTPUTS = ("main.pdf", "main.log", "main.aux", "main.out", "main.fls", "main.fdb_latexmk")


def _drop_privileges() -> None:
    """Run as whoever owns /documents instead of root.

    Otherwise, on Linux hosts, every PDF and log the compiler writes into the
    bind mount is owned by root and the user cannot edit or delete it. Docker
    Desktop (macOS/Windows) mounts often report root, in which case staying
    root is harmless because the host side maps ownership itself.
    """
    if os.getuid() != 0 or not DOCUMENTS_DIR.exists():
        return
    st = DOCUMENTS_DIR.stat()
    uid, gid = st.st_uid, st.st_gid
    if uid == 0:
        return
    # Hand back root-owned output left by earlier versions that ran as root,
    # or pdflatex could not overwrite it after the switch.
    for d in DOCUMENTS_DIR.iterdir():
        if not d.is_dir():
            continue
        paths = [d / name for name in BUILD_OUTPUTS]
        build_dir = d / BUILD_DIRNAME
        if build_dir.is_dir():
            paths.append(build_dir)
            paths.extend(build_dir.iterdir())
        for path in paths:
            try:
                if path.lstat().st_uid == 0:
                    os.lchown(path, uid, gid)
            except OSError:
                pass
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    # root's HOME is unwritable now; TeX writes caches under $HOME.
    os.environ["HOME"] = "/tmp"
    print(f"Running as uid={uid} gid={gid} (owner of {DOCUMENTS_DIR})")


if __name__ == "__main__":
    _drop_privileges()
    # The watcher's first pass compiles every project.
    threading.Thread(target=_watch, daemon=True).start()

    print(f"LaTeX Workspace running on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
