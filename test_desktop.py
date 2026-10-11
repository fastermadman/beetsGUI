#!/usr/bin/env python3
"""
Tests for app.py, the desktop window — everything except the window.

pywebview needs a real GUI session to open a window, which CI doesn't
have, so this covers the parts that decide whether the window has
anything working to show: which port the server ends up on, attaching to
a server that's already running instead of starting a second one, the
server actually stopping on quit, and the JS bridge's contracts.

The in-process server is pointed at a throwaway BEETSDIR, never the
user's real library — and the test asserts that from /status before it
does anything else.

Run: ~/.local/pipx/venvs/beets/bin/python test_desktop.py
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='beetsgui-desktop-'))
BEETSDIR = TMP / 'beets'
BEETSDIR.mkdir()
(BEETSDIR / 'config.yaml').write_text(
    f'directory: {TMP / "library"}\n'
    f'library: {BEETSDIR / "library.db"}\n')
# Before importing app (and, through it, server): beets resolves its config
# from BEETSDIR, and app.py's window state lives under BEETSGUI_SUPPORT_DIR.
os.environ['BEETSDIR'] = str(BEETSDIR)
os.environ['BEETSGUI_SUPPORT_DIR'] = str(TMP / 'support')
os.environ.pop('BEETSGUI_PORT', None)

import app  # noqa: E402


def get_json(port, path, origin=None, data=None):
    req = urllib.request.Request(f'http://localhost:{port}{path}', data=data)
    if origin:
        req.add_header('Origin', origin)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_probe_and_port_choice():
    port = app.free_port()
    assert app.probe(port) == 'free'
    assert app.choose_port(port) == (port, False)

    # Something that isn't beetsGUI on the preferred port: don't attach to
    # it, don't fight it for the port — take another one.
    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass

    other = http.server.HTTPServer(('127.0.0.1', port), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        assert app.probe(port) == 'other'
        chosen, attach = app.choose_port(port)
        assert not attach and chosen != port, (chosen, attach)
    finally:
        other.shutdown()
        other.server_close()


def test_backend_lifecycle():
    port = app.free_port()
    url, srv = app.start_backend(port)
    try:
        assert url == f'http://localhost:{port}/', url
        assert srv is not None
        # The first /status on a brand-new library triggers beets' one-time
        # schema migration, whose noisy stdout makes server.py's resolver
        # refuse that answer (by design, uncached — see CLAUDE.md); the
        # second call is the real one.
        get_json(port, '/status')
        status, data = get_json(port, '/status')
        assert status == 200 and data['ok'], data
        # The safety rule: the in-process server must be on the sandbox.
        assert str(data['library_db']).startswith(str(TMP)), data['library_db']
        assert str(data['config_path']).startswith(str(TMP)), data['config_path']

        # BEETSGUI_PORT reached server.py before it built LOCAL_ORIGINS —
        # otherwise every POST from the window would be refused as foreign.
        import server
        assert server.PORT == port, server.PORT
        status, data = get_json(port, '/library/count', origin=f'http://localhost:{port}',
                                data=json.dumps({'scope': {'query': ''}}).encode())
        assert status != 403, data
        status, _ = get_json(port, '/library/count', origin='http://evil.example',
                             data=b'{}')
        assert status == 403

        # A second launch finds this server and attaches instead of starting
        # its own — the LaunchAgent case.
        assert app.probe(port) == 'beetsgui'
        url2, srv2 = app.start_backend(port)
        assert srv2 is None and url2 == url

        assert app.running_job_kind(srv) is None
        assert app.running_job_kind(None) is None
    finally:
        srv.stop()
    for _ in range(50):
        if app.probe(port, timeout=0.2) == 'free':
            break
        time.sleep(0.05)
    assert app.probe(port) == 'free', 'quitting must stop the server'


def test_stop_jobs_aborts_running_and_drops_queued():
    import jobs
    started, release = threading.Event(), threading.Event()

    def work(job):
        started.set()
        release.wait(timeout=10)   # ignore the setup's abort toggle below
        job.aborted.wait(timeout=10)

    running = jobs.Job('convert')
    jobs.start(running, work)
    assert started.wait(5)
    assert app.running_job_kind(object()) == 'convert'
    # Abort-then-queue is the only way a second job gets in line (#32).
    running.aborted.set()
    queued = jobs.Job('artwork')
    jobs.start(queued, lambda j: None)
    running.aborted.clear()
    release.set()
    assert [j.id for j in jobs.queued()] == [queued.id]

    app.stop_jobs(timeout=5)
    assert running.done.is_set() and running.aborted.is_set()
    assert jobs.queued() == []
    assert jobs.current() is None


def test_window_state():
    assert app.load_window_state()['width'] == app.DEFAULT_SIZE[0]
    app.save_window_state({'width': 1500, 'height': 900, 'x': 40, 'y': 60})
    assert app.load_window_state() == {'width': 1500, 'height': 900, 'x': 40, 'y': 60}

    # Implausible or half-missing values fall back instead of opening a
    # 3px or off-screen window.
    app.WINDOW_STATE.write_text(json.dumps({'width': 3, 'height': 'tall', 'x': 10}))
    state = app.load_window_state()
    assert (state['width'], state['height']) == app.DEFAULT_SIZE, state
    assert state['x'] is None and state['y'] is None, state

    app.WINDOW_STATE.write_text('not json')
    assert app.load_window_state()['width'] == app.DEFAULT_SIZE[0]


def test_notification_argv_never_splices_text():
    title, body = 'beetsGUI', 'Done — "Burial" & "Untrue"\nend tell'
    argv = app.notification_argv(title, body)
    script = [argv[i + 1] for i, a in enumerate(argv) if a == '-e']
    assert all('Burial' not in line for line in script), script
    assert argv[-2:] == [title, body]


def test_pick_path_contract():
    import webview

    class FakeWindow:
        def __init__(self, result):
            self.result, self.calls = result, []

        def create_file_dialog(self, dialog, allow_multiple=False):
            self.calls.append((dialog, allow_multiple))
            return self.result

    api = app.Api()
    assert api.pick_path('folder') == {'ok': False}, 'no window yet'

    api._window = FakeWindow(('/Users/x/Music',))
    assert api.pick_path('folder') == {'ok': True, 'path': '/Users/x/Music'}
    assert api._window.calls == [(webview.FileDialog.FOLDER, False)]

    api._window = FakeWindow(('/a', '/b'))
    assert api.pick_path('folders') == {'ok': True, 'path': '/a,/b'}
    assert api._window.calls == [(webview.FileDialog.FOLDER, True)]

    api._window = FakeWindow(None)
    assert api.pick_path('file') == {'ok': False}, 'cancel'
    assert api.pick_path('rm -rf') == {'ok': False}


def test_tool_paths():
    path = app.with_tool_paths('/usr/bin:/bin:/opt/homebrew/bin')
    parts = path.split(os.pathsep)
    assert parts[:3] == ['/usr/bin', '/bin', '/opt/homebrew/bin']
    assert parts.count('/opt/homebrew/bin') == 1
    assert '/usr/local/bin' in parts


def main():
    tests = [v for k, v in globals().items() if k.startswith('test_')]
    for t in tests:
        t()
        print(f'ok  {t.__name__}')
    print(f'\nall {len(tests)} passed')


if __name__ == '__main__':
    sys.exit(main())
