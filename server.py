#!/usr/bin/env python3
"""
BeetsGUI Server — local Flask API
Run: python3 server.py
Stop: Ctrl+C

Serves beetsgui.html on http://localhost:1312
and offers /run?cmd=... to execute beet commands with live output.
"""
import os
import re
import sqlite3
import sys
import subprocess
import threading
import time
from pathlib import Path

try:
    from flask import Flask, Response, request, send_from_directory, jsonify
except ImportError:
    print("Flask not found.")
    print("Install: pip install flask   (or: pip3 install flask)")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────────────
PORT       = 1312
SCRIPT_DIR = Path(__file__).parent.resolve()
HTML_FILE  = 'beetsgui.html'

# Name of the Safari Web App (File → Add to Dock)
# Change this if you gave the app a different name
SAFARI_APP_NAME = 'BeetsGUI'

# Commands allowed to execute (security)
ALLOWED_PREFIXES = ('beet ', 'beet\t', 'fd ', 'du ', 'for ')

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Helper functions ──────────────────────────────────────────────────────────
def find_beet() -> str:
    """Find the beet executable. Checks Homebrew paths first."""
    candidates = [
        '/opt/homebrew/bin/beet',       # Apple Silicon Homebrew
        '/usr/local/bin/beet',           # Intel Homebrew
        os.path.expanduser('~/.local/bin/beet'),
        os.path.expanduser('~/.local/pipx/venvs/beets/bin/beet'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    try:
        r = subprocess.run(['which', 'beet'], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return 'beet'  # Fallback: hope it's on PATH


def list_volumes() -> list:
    """Return names of mounted external volumes (excluding Macintosh HD)."""
    skip = {'Macintosh HD', '.timemachine', 'Recovery', 'VM', 'Preboot', 'com.apple.TimeMachine.localsnapshots'}
    vols = []
    vol_path = Path('/Volumes')
    if vol_path.exists():
        for v in sorted(vol_path.iterdir()):
            if v.name not in skip and not v.name.startswith('.'):
                vols.append(v.name)
    return vols


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r'\x1b\[[0-9;]*[mGKHABCDJM]', '', text)


def get_config_path() -> str:
    """Find the beets config file via 'beet config --path'."""
    try:
        r = subprocess.run(
            [find_beet(), 'config', '--path'],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return os.path.expanduser('~/.config/beets/config.yaml')


def get_library_db_path() -> str:
    """Find the beets library.db path from the 'library:' key in config.yaml."""
    config_path = get_config_path()
    try:
        for line in Path(config_path).read_text().splitlines():
            m = re.match(r'^library:\s*(.+)$', line.strip())
            if m:
                return os.path.expanduser(m.group(1).strip().strip('\'"'))
    except Exception:
        pass
    return os.path.expanduser('~/.config/beets/library.db')



def open_app_when_ready():
    """Open the Safari Web App (or Safari) once the server is ready."""
    time.sleep(1.2)

    # Try the Safari Web App first (requires File → Add to Dock in Safari)
    result = subprocess.run(
        ['osascript', '-e', f'tell application "{SAFARI_APP_NAME}" to activate'],
        capture_output=True, timeout=5
    )
    if result.returncode != 0:
        # Fallback: open in Safari (not Zen, even if Zen is the default browser)
        subprocess.run(
            ['open', '-a', 'Safari', f'http://localhost:{PORT}'],
            capture_output=True
        )


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/config', methods=['GET'])
def read_config():
    """Read beets config.yaml."""
    path = get_config_path()
    try:
        content = Path(path).read_text() if Path(path).exists() else ''
        return jsonify({'ok': True, 'content': content, 'path': path})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/config', methods=['POST'])
def write_config():
    """Write beets config.yaml. Automatically makes a .bak backup first."""
    import shutil
    data    = request.get_json()
    content = data.get('content', '')
    path    = Path(get_config_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, str(path) + '.bak')
        path.write_text(content)
        return jsonify({'ok': True, 'path': str(path)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500



@app.route('/library')
def library():
    """Read albums directly from the beets library.db (read-only)."""
    q      = request.args.get('q', '').strip()
    limit  = min(int(request.args.get('limit', 200)), 1000)
    offset = int(request.args.get('offset', 0))
    db_path = get_library_db_path()

    if not Path(db_path).exists():
        return jsonify({'ok': True, 'albums': [], 'total': 0})

    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        qlike = f'%{q}%'
        where = '''
            :q = '' OR a.albumartist LIKE :qlike OR a.album LIKE :qlike OR EXISTS (
                SELECT 1 FROM items i WHERE i.album_id = a.id
                AND (i.title LIKE :qlike OR i.artist LIKE :qlike)
            )
        '''
        rows = con.execute(f'''
            SELECT a.id, a.albumartist, a.album, a.year,
                   (SELECT format FROM items i WHERE i.album_id = a.id LIMIT 1) AS format,
                   (SELECT COUNT(*) FROM items i WHERE i.album_id = a.id) AS track_count,
                   (SELECT COALESCE(SUM(length), 0) FROM items i WHERE i.album_id = a.id) AS duration
            FROM albums a
            WHERE {where}
            ORDER BY a.albumartist, a.year, a.album
            LIMIT :limit OFFSET :offset
        ''', {'q': q, 'qlike': qlike, 'limit': limit, 'offset': offset}).fetchall()
        total = con.execute(f'SELECT COUNT(*) FROM albums a WHERE {where}', {'q': q, 'qlike': qlike}).fetchone()[0]
        con.close()
        albums = [dict(r) for r in rows]
        return jsonify({'ok': True, 'albums': albums, 'total': total})
    except sqlite3.Error as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/playlists')
def list_playlists():
    """Find .m3u playlists in a folder — used by USB Mirror."""
    dir_path = os.path.expanduser(request.args.get('dir', '~/Playlister'))
    try:
        p = Path(dir_path)
        playlists = sorted([f.stem for f in p.rglob('*.m3u') if not f.name.startswith('.')]) if p.exists() else []
        return jsonify({'ok': True, 'playlists': playlists, 'dir': str(p)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/')
def index():
    return send_from_directory(str(SCRIPT_DIR), HTML_FILE)


@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory(str(SCRIPT_DIR / 'assets'), filename)


@app.route('/status')
def status():
    """Server status, beet path and mounted volumes."""
    return jsonify({
        'ok':      True,
        'beet':    find_beet(),
        'volumes': list_volumes(),
        'port':    PORT,
    })


@app.route('/run')
def run_cmd():
    """
    Execute a command and stream output as Server-Sent Events (SSE).

    Parameter: ?cmd=beet import -A "/Volumes/Harddisk/Musik"

    SSE lines:
      data: <output-line>\n\n
      data: __END__\n\n     ← signals that the process has finished
    """
    cmd = request.args.get('cmd', '').strip()

    def sse(text: str) -> str:
        return f"data: {text}\n\n"

    def generate(cmd: str):
        # Validation
        if not cmd:
            yield sse("✗ No command given")
            yield sse("__END__")
            return

        if not any(cmd.startswith(p) for p in ALLOWED_PREFIXES):
            yield sse(f"✗ Not allowed: '{cmd}'")
            yield sse("  Only beet, fd and du commands are allowed.")
            yield sse("__END__")
            return

        # Replace 'beet ' with the full path
        beet = find_beet()
        if cmd.startswith('beet ') or cmd.startswith('beet\t'):
            full_cmd = beet + cmd[4:]
        else:
            full_cmd = cmd

        yield sse(f"$ {cmd}")
        yield sse("")

        env = os.environ.copy()
        env['TERM']          = 'dumb'
        env['NO_COLOR']      = '1'
        env['BEETS_NO_COLOR'] = '1'

        try:
            proc = subprocess.Popen(
                full_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                cwd=os.path.expanduser('~'),
            )

            for line in iter(proc.stdout.readline, ''):
                clean = strip_ansi(line.rstrip('\r\n'))
                yield sse(clean)

            proc.stdout.close()
            rc = proc.wait()
            yield sse("")
            yield sse("✓ Done" if rc == 0 else f"✗ Exit {rc}")

        except Exception as e:
            yield sse(f"ERROR: {e}")

        yield sse("__END__")

    return Response(
        generate(cmd),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'keep-alive',
        }
    )


# ── Start ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    beet = find_beet()
    vols = list_volumes()

    print(f"\n{'─'*52}")
    print(f"  BeetsGUI Server  →  http://localhost:{PORT}")
    print(f"{'─'*52}")
    print(f"  beet:    {beet}")
    print(f"  volumes: {', '.join(vols) if vols else '(none mounted)'}")
    print(f"  html:    {SCRIPT_DIR / HTML_FILE}")
    print(f"{'─'*52}")
    print(f"  Stop: Ctrl+C\n")

    threading.Thread(target=open_app_when_ready, daemon=True).start()

    try:
        app.run(
            host='127.0.0.1',
            port=PORT,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    except KeyboardInterrupt:
        print("\nServer stopped.")
