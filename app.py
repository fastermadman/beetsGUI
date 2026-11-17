#!/usr/bin/env python3
"""
beetsGUI desktop app — a native window around server.py.

Run:  ~/.local/pipx/venvs/beets/bin/python app.py
Build a double-clickable beetsGUI.app: ./scripts/build-app.sh

The window is pywebview, which on macOS is WKWebView — the same engine as
Safari, so everything the README says about in-app playback (ALAC and AIFF
play, unlike in Chrome) still holds. Electron would have lost exactly that.

The server runs in this process on a background thread, so opening the app
starts it and quitting stops it — no LaunchAgent, no Safari Web App, no
Terminal. If a beetsGUI server is already answering on the port (the
LaunchAgent from scripts/install-launchagent.sh, or a `python server.py`
left running), the window attaches to it instead of starting a second one:
two servers would be two processes mutating one library.db, which is the
thing jobs.py's single-flight registry exists to prevent.

Nothing in this file is needed by the browser path: `python server.py` and
the Safari Web App keep working exactly as before.
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

APP_NAME = 'beetsGUI'
DEFAULT_PORT = 1612

# Window geometry and WebKit's own storage (localStorage holds the theme
# and the Library's column choice). pywebview's default is private mode,
# which would forget both on every launch.
SUPPORT_DIR = Path(os.environ.get(
    'BEETSGUI_SUPPORT_DIR',
    Path.home() / 'Library' / 'Application Support' / APP_NAME))
WINDOW_STATE = SUPPORT_DIR / 'window.json'
DEFAULT_SIZE = (1280, 860)
MIN_SIZE = (720, 520)


# Launched from Finder, an app inherits launchd's PATH (/usr/bin:/bin:...),
# not your shell's — so the `ffmpeg` that beets' convert plugin shells out
# to, and Homebrew/pipx tools generally, aren't found. Same directories
# find_beet() in server.py already probes for `beet`.
EXTRA_PATH = ('/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local' / 'bin'))
LOG_FILE = Path.home() / 'Library' / 'Logs' / 'beetsGUI.log'


def with_tool_paths(path: str) -> str:
    parts = [p for p in path.split(os.pathsep) if p]
    parts += [p for p in EXTRA_PATH if p not in parts]
    return os.pathsep.join(parts)


def log_to_file_if_detached():
    """No Terminal means print() goes nowhere — keep the server's output
    somewhere a bug report can point at."""
    if sys.stdout is not None and sys.stdout.isatty():
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG_FILE, 'a', buffering=1, encoding='utf-8', errors='replace')
    except OSError:
        return
    sys.stdout = sys.stderr = log


# ── Backend ────────────────────────────────────────────────────────────────────

def probe(port: int, timeout: float = 1.0, status_timeout: float = 8.0) -> str:
    """What is listening on 127.0.0.1:<port>: 'free', 'beetsgui' or 'other'.

    'beetsgui' means /status answered with this app's shape — the only
    case where attaching is safe. /status gets a long timeout: on a cold
    server it shells out to `beet config --path` (and beets >= 2.13 may
    run a schema migration first), and misreading a slow beetsGUI as
    'other' would start a second server next to it. Anything else on the port (another dev
    server, a half-dead process) is 'other', and the caller picks a
    different port rather than pointing the window at a stranger.
    """
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            pass
    except OSError:
        return 'free'
    try:
        # Host must be localhost:<port> or _reject_foreign_host() refuses
        # it — urllib derives it from the URL, so use that spelling.
        with urllib.request.urlopen(f'http://localhost:{port}/status',
                                    timeout=status_timeout) as r:
            data = json.loads(r.read())
    except (OSError, ValueError):
        return 'other'
    if isinstance(data, dict) and data.get('ok') and 'library_db' in data:
        return 'beetsgui'
    return 'other'


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def choose_port(preferred: int) -> tuple:
    """(port, attach). Attach to a beetsGUI server already on `preferred`;
    otherwise serve on `preferred` if it's free, or on any free port."""
    state = probe(preferred)
    if state == 'beetsgui':
        return preferred, True
    if state == 'free':
        return preferred, False
    return free_port(), False


class ServerThread:
    """server.app on a werkzeug server in a daemon thread, stoppable.

    Flask's app.run() blocks and has no stop; ThreadedWSGIServer is the
    same server it uses underneath, with a shutdown() the window's close
    path can call. Threaded for the same reason server.py passes it: the
    import SSE stream holds a request open for the whole import.
    """

    def __init__(self, app, port: int):
        from werkzeug.serving import ThreadedWSGIServer

        class Server(ThreadedWSGIServer):
            def server_bind(self):
                # HTTPServer.server_bind() reverse-resolves 127.0.0.1 via
                # socket.getfqdn() only to set server_name, which werkzeug
                # never reads. Launched from Finder that lookup goes through
                # mDNS and times out: ~30s before the window's page loads.
                socketserver.TCPServer.server_bind(self)

        self.port = port
        self._server = Server('127.0.0.1', port, app)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name='beetsgui-server', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def start_backend(preferred: int = None):
    """Start (or find) the server. Returns (url, ServerThread or None).

    BEETSGUI_PORT is set before server.py is imported, because server.py
    reads it at import time to build LOCAL_ORIGINS — the Origin check
    would otherwise refuse every POST from a window on a fallback port.
    """
    if preferred is None:
        preferred = int(os.environ.get('BEETSGUI_PORT', DEFAULT_PORT))
    port, attach = choose_port(preferred)
    url = f'http://localhost:{port}/'
    if attach:
        return url, None
    os.environ['BEETSGUI_PORT'] = str(port)
    sys.path.insert(0, str(Path(__file__).parent.resolve()))
    import server
    # ServerThread() has already bound and is listening, so the window's
    # first request just waits in the backlog — no readiness poll needed.
    srv = ServerThread(server.app, port)
    srv.start()
    return url, srv


def running_job_kind(srv):
    """Kind of the job in progress ('import', 'convert', ...) or None.
    Only meaningful when this process owns the server — an attached
    window closing doesn't stop the other process's job."""
    if srv is None:
        return None
    import jobs
    job = jobs.current()
    return job.kind if job else None


def stop_jobs(timeout: float = 10.0):
    """Abort the running job (and drop the queue) before the process
    exits, so an import the user chose to quit out of stops at a beets
    checkpoint rather than wherever the interpreter happens to die."""
    import jobs
    for queued in jobs.queued():
        jobs.dequeue(queued.id)
    job = jobs.current()
    if job is None:
        return
    job.abort()
    job.done.wait(timeout=timeout)


# ── Window state ───────────────────────────────────────────────────────────────

def load_window_state() -> dict:
    """Last size/position, or defaults. Anything unreadable or implausible
    falls back rather than opening a window off-screen or 3px wide."""
    state = {'width': DEFAULT_SIZE[0], 'height': DEFAULT_SIZE[1], 'x': None, 'y': None}
    try:
        saved = json.loads(WINDOW_STATE.read_text())
    except (OSError, ValueError):
        return state
    if not isinstance(saved, dict):
        return state
    for key, low in (('width', MIN_SIZE[0]), ('height', MIN_SIZE[1])):
        v = saved.get(key)
        if isinstance(v, (int, float)) and low <= v <= 10000:
            state[key] = int(v)
    for key in ('x', 'y'):
        v = saved.get(key)
        if isinstance(v, (int, float)) and -10000 <= v <= 10000:
            state[key] = int(v)
    if state['x'] is None or state['y'] is None:
        state['x'] = state['y'] = None
    return state


def save_window_state(state: dict):
    try:
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE.write_text(json.dumps(state))
    except OSError:
        pass  # losing the window size is not worth a crash on quit


# ── JS bridge ──────────────────────────────────────────────────────────────────

def notification_argv(title: str, body: str) -> list:
    """osascript argv for a macOS notification. The text goes in as argv,
    read by `on run argv` — never spliced into the script, so a track
    title with a quote in it can't become AppleScript."""
    return ['osascript',
            '-e', 'on run argv',
            '-e', 'display notification (item 2 of argv) with title (item 1 of argv)',
            '-e', 'end run',
            str(title)[:200], str(body)[:500]]


class Api:
    """Exposed to the page as window.pywebview.api.*. Everything here is
    reachable from page JS, so it's limited to what a native window adds
    over a browser tab: an attached picker sheet and notifications."""

    def __init__(self):
        self._window = None

    def pick_path(self, kind):
        """Same contract as POST /pick: {'ok': True, 'path': ...} or
        {'ok': False} on cancel. A sheet on this window instead of
        osascript's free-floating dialog, which can open behind it."""
        import webview
        dialog = {
            'folder': webview.FileDialog.FOLDER,
            'folders': webview.FileDialog.FOLDER,
            'file': webview.FileDialog.OPEN,
            'save': webview.FileDialog.SAVE,
        }.get(kind)
        if dialog is None or self._window is None:
            return {'ok': False}
        result = self._window.create_file_dialog(
            dialog, allow_multiple=(kind == 'folders'))
        if not result:
            return {'ok': False}
        if isinstance(result, str):
            result = [result]
        # 'folders' is traktor-roots' comma-separated field, same as /pick.
        return {'ok': True, 'path': ','.join(result) if kind == 'folders' else result[0]}

    def notify(self, title, body):
        if sys.platform != 'darwin':
            return False
        try:
            subprocess.run(notification_argv(title, body),
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return True


# Section drop targets → the field a dropped folder fills. Mirrors the
# setupDrop() calls in beetsgui.html. WKWebView hides real paths from page
# JS (a dropped File only has a name), but pywebview sees the drag on the
# native side and hands the full path to a Python-registered drop handler.
DROP_TARGETS = {
    'import-section': 'import-path',
    'scan-section': 'scan-path',
    'find-drop2': 'conv-src',
}


def register_drop_targets(window):
    from webview.dom import DOMEventHandler

    def handler_for(field_id):
        def on_drop(event):
            files = (event.get('dataTransfer') or {}).get('files') or []
            paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
            if paths:
                window.evaluate_js('desktopDropPath(%s,%s)'
                                   % (json.dumps(field_id), json.dumps(paths[0])))
        return on_drop

    for element_id, field_id in DROP_TARGETS.items():
        element = window.dom.get_element('#' + element_id)
        if element is not None:
            element.events.drop += DOMEventHandler(handler_for(field_id))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    try:
        import webview
    except ImportError:
        print('pywebview is not installed in this Python. Run:')
        print('  pipx inject beets pywebview')
        sys.exit(1)

    log_to_file_if_detached()
    os.environ['PATH'] = with_tool_paths(os.environ.get('PATH', ''))
    url, srv = start_backend()
    state = load_window_state()
    api = Api()
    window = webview.create_window(
        APP_NAME, url, js_api=api,
        width=state['width'], height=state['height'], x=state['x'], y=state['y'],
        min_size=MIN_SIZE, background_color='#0f0f0f', text_select=True)
    api._window = window

    window.events.resized += lambda w, h: state.update(width=int(w), height=int(h))
    window.events.moved += lambda x, y: state.update(x=int(x), y=int(y))
    window.events.loaded += lambda: register_drop_targets(window)

    quitting = {'confirmed': False}

    def confirm_quit(kind):
        # Off the main thread: on macOS, closing handlers run on the main
        # thread and create_confirmation_dialog() waits for it — calling it
        # from the handler itself would deadlock.
        # The message carries everything: pywebview's Cocoa dialog drops the title.
        if window.create_confirmation_dialog(
                'Quit beetsGUI?',
                f'The {kind} job is still running. '
                'Quitting now stops it partway through. Quit anyway?'):
            quitting['confirmed'] = True
            window.destroy()

    def on_closing():
        kind = running_job_kind(srv)
        if kind and not quitting['confirmed']:
            threading.Thread(target=confirm_quit, args=(kind,), daemon=True).start()
            return False
        save_window_state(state)
        return True

    window.events.closing += on_closing

    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    webview.start(private_mode=False, storage_path=str(SUPPORT_DIR / 'WebKit'))

    if srv is not None:
        stop_jobs()
        srv.stop()


if __name__ == '__main__':
    main()
