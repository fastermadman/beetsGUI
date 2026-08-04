#!/usr/bin/env python3
"""
BeetsGUI Server — local Flask API
Run: python3 server.py
Stop: Ctrl+C

Serves beetsgui.html on http://localhost:1312.
"""
import json
import os
import queue
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

try:
    import duplicates
    import importsession
    from mediafile import MediaFile, UnreadableFileError
except ImportError as e:
    print(f"beets not importable: {e}")
    print("Run this with the Python that has beets installed, e.g.")
    print("  ~/.local/pipx/venvs/beets/bin/python server.py")
    sys.exit(1)

# Extensions scanned by /unimported — matches beets' default valid_extensions.
AUDIO_EXTENSIONS = {'.mp3', '.aiff', '.aif', '.wav', '.flac', '.m4a', '.aac', '.ogg'}

# ── Configuration ──────────────────────────────────────────────────────────────
PORT       = int(os.environ.get('BEETSGUI_PORT', 1312))
OPEN_APP   = os.environ.get('BEETSGUI_NO_OPEN') != '1'
SCRIPT_DIR = Path(__file__).parent.resolve()
HTML_FILE  = 'beetsgui.html'

# Name of the Safari Web App (File → Add to Dock)
# Change this if you gave the app a different name
SAFARI_APP_NAME = 'BeetsGUI'

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


@app.route('/duplicates')
def list_duplicates():
    """Existing albums grouped by (albumartist, album), best copy first."""
    try:
        groups = duplicates.find_duplicate_groups()
        return jsonify({'ok': True, 'groups': groups})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/duplicates/delete', methods=['POST'])
def delete_duplicate():
    """Remove one album (by id) from the library. Body: {"id": 1,
    "delete_files": true}. Always a single explicit target."""
    data = request.get_json(silent=True) or {}
    album_id = data.get('id')
    if not isinstance(album_id, int):
        return jsonify({'ok': False, 'error': 'id must be an album id'}), 400
    found = duplicates.delete_album(album_id, bool(data.get('delete_files')))
    if not found:
        return jsonify({'ok': False, 'error': 'no such album'}), 404
    return jsonify({'ok': True})


def _known_track_keys(con):
    """(artist, title) pairs already in the library.

    Matching on metadata, not path, is required: beets stores a copied or
    moved file's path relative to the library `directory:`, not the path it
    was imported from — so diffing the scan folder against `items.path`
    would report every already-imported file as unimported again. This
    mirrors beets' own default duplicate_keys (artist title).
    """
    rows = con.execute('SELECT DISTINCT artist, title FROM items').fetchall()
    return {(a or '', t or '') for a, t in rows}


@app.route('/unimported')
def unimported():
    """Audio files under `dir` whose (artist, title) isn't in the library yet."""
    dir_path = os.path.expanduser(request.args.get('dir', '').strip())
    exclude = [e.strip().lower() for e in request.args.get('exclude', '').split(',') if e.strip()]
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({'ok': False, 'error': 'no such directory'}), 400

    known = set()
    db_path = get_library_db_path()
    if Path(db_path).exists():
        try:
            con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
            known = _known_track_keys(con)
            con.close()
        except sqlite3.Error:
            pass

    root = Path(dir_path)
    folders = {}
    total = 0
    for f in root.rglob('*'):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        rel_parts = f.relative_to(root).parts[:-1]
        if any(part.startswith('.') for part in rel_parts):
            continue
        fstr = str(f).lower()
        if exclude and any(e in fstr for e in exclude):
            continue
        try:
            mf = MediaFile(f)
        except (UnreadableFileError, OSError):
            continue
        if (mf.artist or '', mf.title or '') in known:
            continue
        folder = str(f.parent)
        folders[folder] = folders.get(folder, 0) + 1
        total += 1

    result = [{'path': p, 'count': c} for p, c in sorted(folders.items())]
    return jsonify({'ok': True, 'dir': str(root), 'folders': result, 'total_files': total})


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
    """Server status, beet path and mounted volumes. Used by the test suite
    to poll for server readiness; the UI itself never calls it."""
    return jsonify({
        'ok':      True,
        'beet':    find_beet(),
        'volumes': list_volumes(),
        'port':    PORT,
    })


# ── Import: the beets importer driven in-process ──────────────────────────────
# See importsession.py. /run still exists for the other tabs.

@app.route('/import/start', methods=['POST'])
def import_start():
    """Start an import. Body: {"path": "...", "mode": "interactive",
    "handling": "copy", "incremental": false, "singleton": false,
    "log_path": null} or {"paths": [...]} instead of "path" for a curated
    multi-folder selection (e.g. from Unimported or an fd search)."""
    data = request.get_json(silent=True) or {}
    try:
        job = importsession.start(
            data.get('path'), data.get('mode', 'interactive'),
            paths=data.get('paths'),
            handling=data.get('handling', 'copy'),
            incremental=bool(data.get('incremental', False)),
            singleton=bool(data.get('singleton', False)),
            log_path=data.get('log_path'),
        )
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    return jsonify({'ok': True, 'id': job.id, 'paths': job.paths, 'mode': job.mode,
                    'handling': job.handling, 'incremental': job.incremental,
                    'singleton': job.singleton})


@app.route('/import/current')
def import_current():
    """The running import, if any — lets a reloaded page rejoin its stream."""
    job = importsession.current_job()
    if not job:
        return jsonify({'ok': True, 'id': None})
    return jsonify({'ok': True, 'id': job.id, 'paths': job.paths, 'mode': job.mode,
                    'handling': job.handling, 'incremental': job.incremental,
                    'singleton': job.singleton, 'decision': job.pending()})


@app.route('/import/<job_id>/events')
def import_events(job_id):
    """SSE stream of status, decision and done events. One consumer at a time."""
    job = importsession.get_job(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'unknown import id'}), 404

    def generate():
        # A reconnecting client must not have to wait out the decision timeout,
        # so replay the waiting decision — but send each one only once, since
        # the queue may still hold the copy this replaces.
        sent = set()
        pending = job.pending()
        if pending:
            sent.add(pending['decision_id'])
            yield f"data: {json.dumps(pending)}\n\n"
        while True:
            try:
                event = job.events.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if event['type'] == 'decision':
                if event['decision_id'] in sent:
                    continue
                sent.add(event['decision_id'])
            yield f"data: {json.dumps(event)}\n\n"
            if event['type'] == 'done':
                return

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control':     'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection':        'keep-alive',
    })


@app.route('/import/<job_id>/decide', methods=['POST'])
def import_decide(job_id):
    """Answer the waiting decision. Body: {"decision_id": "...", "choice": "...",
    "candidate": 0}."""
    job = importsession.get_job(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'unknown import id'}), 404
    data = request.get_json(silent=True) or {}
    error = job.answer(data.get('decision_id'), data)
    if error:
        return jsonify({'ok': False, 'error': error}), 409
    return jsonify({'ok': True})


@app.route('/import/<job_id>/abort', methods=['POST'])
def import_abort(job_id):
    """Cancel the import and release any blocked decision."""
    job = importsession.get_job(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'unknown import id'}), 404
    job.abort()
    job.done.wait(timeout=30)
    return jsonify({'ok': True, 'finished': job.done.is_set()})


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

    if OPEN_APP:
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
