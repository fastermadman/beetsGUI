#!/usr/bin/env python3
"""
BeetsGUI Server — local Flask API
Run: python3 server.py
Stop: Ctrl+C

Serves beetsgui.html on http://localhost:1612.
"""
import functools
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
    from flask import (Flask, Response, request, send_file, send_from_directory,
                       jsonify)
except ImportError:
    print("Flask not found.")
    print("Install: pip install flask   (or: pip3 install flask)")
    sys.exit(1)

try:
    import artwork
    import duplicates
    import importsession
    import jobs
    import libops
    import sync
    import traktor
    import transcode
    from beets.ui import UserError
    from beets.util import displayable_path
    from mediafile import MediaFile, UnreadableFileError
except ImportError as e:
    print(f"beets not importable: {e}")
    print("Run this with the Python that has beets installed, e.g.")
    print("  ~/.local/pipx/venvs/beets/bin/python server.py")
    sys.exit(1)

# Extensions scanned by /unimported — matches beets' default valid_extensions.
AUDIO_EXTENSIONS = {'.mp3', '.aiff', '.aif', '.wav', '.flac', '.m4a', '.aac', '.ogg'}

# Types the stdlib guesses wrong for /stream. mimetypes says 'audio/mp4a-latm'
# for .m4a and 'audio/x-flac' for .flac; neither is a container Chrome
# registers, so both play only by content-sniffing and canPlayType() lies.
# Only formats a browser can actually decode are worth correcting — nothing
# here rescues ALAC, AIFF or WavPack, which no amount of Content-Type makes
# playable outside Safari. The player surfaces those as an error instead.
AUDIO_MIMETYPES = {'.m4a': 'audio/mp4', '.mp4': 'audio/mp4',
                   '.flac': 'audio/flac', '.opus': 'audio/ogg'}

# ── Configuration ──────────────────────────────────────────────────────────────
PORT       = int(os.environ.get('BEETSGUI_PORT', 1612))
OPEN_APP   = os.environ.get('BEETSGUI_NO_OPEN') != '1'
SCRIPT_DIR = Path(__file__).parent.resolve()
HTML_FILE  = 'beetsgui.html'

# Name of the Safari Web App (File → Add to Dock)
# Change this if you gave the app a different name
SAFARI_APP_NAME = 'BeetsGUI'

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.before_request
def _reject_foreign_host():
    """Block DNS-rebinding: only serve requests addressed to this loopback
    origin. Without this, a page on any hostname that resolves to
    127.0.0.1 is same-origin from the browser's perspective and can hit
    every endpoint here, CORS preflight or not."""
    if request.host not in (f'localhost:{PORT}', f'127.0.0.1:{PORT}'):
        return jsonify({'ok': False, 'error': 'bad host'}), 403


@app.errorhandler(UserError)
def _bad_query(e):
    """A malformed beets query is the caller's mistake, not a server fault.
    Endpoints that catch UserError themselves still do; this catches the ones
    that don't, so an unbalanced quote is a 400 instead of a 500."""
    return jsonify({'ok': False, 'error': str(e)}), 400


# ── Helper functions ──────────────────────────────────────────────────────────
def _busy_response():
    """409 if a background job is running, else None.

    beets' `config` and `Library` are process-global (see jobs.py), and Flask
    is threaded, so a mutating request served while a job's worker thread is
    mid-run touches the same singletons. The job registry only serialises
    jobs against each other — this extends that to the mutating endpoints.
    Read-only endpoints are deliberately not gated.
    """
    job = jobs.current()
    if not job:
        return None
    return jsonify({'ok': False,
                    'error': f'a {job.kind} job is running — wait for it to finish'}), 409


# Writing a list to a caller-supplied path: refuse anything that isn't
# obviously one of this app's own outputs. Same-user localhost is still the
# trust model, but "empty match set silently truncates ~/.zshrc" is not a
# capability this app needs to have.
LIST_SUFFIXES = {'.txt', '.m3u', '.m3u8'}


def _write_list(dest, lines):
    """Write `lines` to `dest`. Returns (path, None) or (None, error)."""
    path = Path(os.path.expanduser(dest))
    if path.suffix.lower() not in LIST_SUFFIXES:
        return None, f'dest must end in one of {sorted(LIST_SUFFIXES)}'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Filenames can hold bytes that aren't valid UTF-8; they arrive here
        # surrogate-escaped, so write them back the same way rather than
        # raising UnicodeEncodeError halfway through.
        path.write_text('\n'.join(lines) + ('\n' if lines else ''),
                        errors='surrogateescape')
    except OSError as e:
        return None, str(e)
    return path, None


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


@functools.lru_cache(maxsize=1)
def get_config_path() -> str:
    """Find the beets config file via 'beet config --path'.

    Cached for the life of the process: this used to shell out on every
    /library request (~700ms measured — `beet` is a Python CLI wrapper that
    imports the whole beets package plus every enabled plugin on each
    invocation), which dwarfed the actual query time (~6ms) by two orders
    of magnitude. The config path doesn't move while this server is
    running, same assumption `importsession.get_library()` already makes
    about the Library object itself.
    """
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


@functools.lru_cache(maxsize=1)
def get_library_db_path() -> str:
    """Find the beets library.db path from the 'library:' key in config.yaml.

    Cached alongside get_config_path() for the same reason — same process
    lifetime, same "doesn't move while we're running" assumption.
    """
    config_path = get_config_path()
    try:
        for line in Path(config_path).read_text().splitlines():
            m = re.match(r'^library:\s*(.+)$', line.strip())
            if m:
                return os.path.expanduser(m.group(1).strip().strip('\'"'))
    except Exception:
        pass
    return os.path.expanduser('~/.config/beets/library.db')


@functools.lru_cache(maxsize=1)
def get_library_directory() -> str:
    """The beets `directory:` key from config.yaml — same parse as
    get_library_db_path(), same cache lifetime assumption."""
    config_path = get_config_path()
    try:
        for line in Path(config_path).read_text().splitlines():
            m = re.match(r'^directory:\s*(.+)$', line.strip())
            if m:
                return os.path.expanduser(m.group(1).strip().strip('\'"'))
    except Exception:
        pass
    return os.path.expanduser('~/Music')


def resolve_item_path(raw) -> str:
    """Decode a path column (items.path / albums.artpath) to an absolute path.

    Since beets 2.11 (upstream beetbox/beets#6460) paths under `directory:`
    are stored relative to it, and a one-time `relative_path` migration
    rewrote the existing rows — so relative is the normal form, not damage.
    Rows outside `directory:` stay absolute, so both forms occur.
    """
    path = os.fsdecode(raw) if isinstance(raw, bytes) else raw
    if not path:
        return ''
    if os.path.isabs(path):
        return path
    return os.path.join(get_library_directory(), path)


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


# Sort orders offered by the Library tab. The request only ever picks a key
# here — column names never come from the query string, because this is the
# one place in the app where user input reaches SQL that can't be a bound
# parameter. Each value is a tuple so ?dir=desc can flip every column.
ALBUM_SORTS = {
    'artist': ('a.albumartist COLLATE NOCASE', 'a.year', 'a.album COLLATE NOCASE'),
    'album':  ('a.album COLLATE NOCASE', 'a.albumartist COLLATE NOCASE'),
    'year':   ('a.year', 'a.albumartist COLLATE NOCASE', 'a.album COLLATE NOCASE'),
    'added':  ('a.added',),
}
TRACK_SORTS = {
    'artist': ('i.artist COLLATE NOCASE', 'i.album COLLATE NOCASE', 'i.disc', 'i.track'),
    'album':  ('i.album COLLATE NOCASE', 'i.disc', 'i.track'),
    'title':  ('i.title COLLATE NOCASE',),
    'year':   ('i.year', 'i.album COLLATE NOCASE', 'i.disc', 'i.track'),
    'added':  ('i.added',),
}
ARTIST_SORTS = {
    'artist': ('name COLLATE NOCASE',),
    'added':  ('MAX(i.added)',),
    'tracks': ('tracks',),
}


def _resolve_sort(con, table, alias, col):
    """(expr,) sorting by any real column on `table`, or None if `col` isn't
    one. Validated against the live schema on every call — the only way a
    user-typed field name (bpm, initial_key, label, ...) can reach an ORDER
    BY without ever being spliced in as arbitrary SQL."""
    info = {r['name']: r['type'] for r in con.execute(f'PRAGMA table_info({table})')}
    coltype = info.get(col)
    if coltype is None:
        return None
    is_text = not coltype or any(t in coltype.upper() for t in ('CHAR', 'TEXT', 'CLOB'))
    return (f'{alias}.{col}' + (' COLLATE NOCASE' if is_text else ''),)


def _order_by(sorts, default, dynamic=None):
    """ORDER BY fragment from ?sort= / ?dir=.

    `sorts` is the curated, multi-column whitelist (e.g. artist sort also
    breaks ties by album/track). `dynamic`, if given, is (con, table, alias)
    — a fallback that accepts any other real column on that table, so sort
    isn't limited to the fields curated here.
    """
    key = request.args.get('sort', default)
    cols = sorts.get(key)
    if cols is None and dynamic is not None:
        cols = _resolve_sort(*dynamic, key)
    if cols is None:
        cols = sorts[default]
    suffix = ' DESC' if request.args.get('dir') == 'desc' else ''
    return ', '.join(c + suffix for c in cols)


def _page(default_limit=200, max_limit=1000):
    """(limit, offset) from the query string. Raises ValueError on garbage."""
    return (min(int(request.args.get('limit', default_limit)), max_limit),
            max(int(request.args.get('offset', 0)), 0))


def _filter_sql(q, level):
    """SQL restricting a list query to what the shared filter matches (#64).

    Returns '' when there is no filter, otherwise ` AND <col> IN (...)`. The
    ids come out of beets as real ints, so inlining them is safe — sqlite
    can't bind a variable-length list, and a temp table is more machinery
    than one page of results is worth.

    `AND 0` for an empty match set is deliberate: "the filter matched
    nothing" and "there is no filter" must not collapse into the same SQL,
    which is the same distinction /library/remove/preview draws for the same
    reason.
    """
    if not q:
        return ''
    item_ids, album_ids = libops.matching_ids(q)
    ids, col = ((album_ids, 'a.id') if level == 'albums' else (item_ids, 'i.id'))
    if not ids:
        return ' AND 0'
    return f' AND {col} IN ({",".join(str(i) for i in ids)})'


def _library_con():
    """Read-only connection to the beets library.db, or None if there isn't one."""
    db_path = get_library_db_path()
    if not Path(db_path).exists():
        return None
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/library')
def library():
    """Read albums directly from the beets library.db (read-only)."""
    q = request.args.get('q', '').strip()
    try:
        limit, offset = _page()
    except ValueError:
        return jsonify({'ok': False,
                        'error': 'limit and offset must be integers'}), 400

    con = _library_con()
    if con is None:
        return jsonify({'ok': True, 'albums': [], 'total': 0})

    where = '1' + _filter_sql(q, 'albums')
    try:
        rows = con.execute(f'''
            SELECT a.id, a.albumartist, a.album, a.year,
                   (SELECT format FROM items i WHERE i.album_id = a.id LIMIT 1) AS format,
                   (SELECT COUNT(*) FROM items i WHERE i.album_id = a.id) AS track_count,
                   (SELECT COALESCE(SUM(length), 0) FROM items i WHERE i.album_id = a.id) AS duration
            FROM albums a
            WHERE {where}
            ORDER BY {_order_by(ALBUM_SORTS, 'artist')}
            LIMIT :limit OFFSET :offset
        ''', {'limit': limit, 'offset': offset}).fetchall()
        total = con.execute(f'SELECT COUNT(*) FROM albums a WHERE {where}').fetchone()[0]
        con.close()
        albums = [dict(r) for r in rows]
        return jsonify({'ok': True, 'albums': albums, 'total': total})
    except sqlite3.Error as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/library/tracks')
def library_tracks():
    """Track-level rows, each with the item id /stream plays by.

    A separate endpoint from /library rather than a mode flag on it: this is a
    different row shape, not a different ordering of the same one.
    """
    q = request.args.get('q', '').strip()
    album_id = request.args.get('album_id', '').strip()
    try:
        limit, offset = _page()
        params = {'album_id': int(album_id)} if album_id else {}
    except ValueError:
        return jsonify({'ok': False,
                        'error': 'limit, offset and album_id must be integers'}), 400

    con = _library_con()
    if con is None:
        return jsonify({'ok': True, 'tracks': [], 'total': 0})

    where = '1' + _filter_sql(q, 'tracks')
    try:
        params.update({'limit': limit, 'offset': offset})
        if album_id:
            where += ' AND i.album_id = :album_id'
        rows = con.execute(f'''
            SELECT i.id, i.title, i.artist, i.album, i.albumartist, i.album_id,
                   i.year, i.length, i.format, i.track, i.disc, i.path
            FROM items i
            WHERE {where}
            ORDER BY {_order_by(TRACK_SORTS, 'artist', dynamic=(con, 'items', 'i'))}
            LIMIT :limit OFFSET :offset
        ''', params).fetchall()
        total = con.execute(f'SELECT COUNT(*) FROM items i WHERE {where}',
                            params).fetchone()[0]
        con.close()
        tracks = []
        for r in rows:
            t = dict(r)
            t['path'] = resolve_item_path(t['path'])
            tracks.append(t)
        return jsonify({'ok': True, 'tracks': tracks, 'total': total})
    except sqlite3.Error as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/library/artists')
def library_artists():
    """Album artists with their album and track counts."""
    q = request.args.get('q', '').strip()
    try:
        limit, offset = _page()
    except ValueError:
        return jsonify({'ok': False,
                        'error': 'limit and offset must be integers'}), 400

    con = _library_con()
    if con is None:
        return jsonify({'ok': True, 'artists': [], 'total': 0})

    where = '1' + _filter_sql(q, 'tracks')
    try:
        params = {'limit': limit, 'offset': offset}
        # Tracks with no albumartist fall back to artist so nothing vanishes
        # from this view; unattributed files group under one empty name.
        name = "COALESCE(NULLIF(i.albumartist, ''), i.artist, '')"
        rows = con.execute(f'''
            SELECT {name} AS name,
                   COUNT(DISTINCT i.album_id) AS albums,
                   COUNT(*) AS tracks
            FROM items i
            WHERE {where}
            GROUP BY name COLLATE NOCASE
            ORDER BY {_order_by(ARTIST_SORTS, 'artist')}
            LIMIT :limit OFFSET :offset
        ''', params).fetchall()
        total = con.execute(f'''
            SELECT COUNT(*) FROM (
                SELECT 1 FROM items i WHERE {where} GROUP BY {name} COLLATE NOCASE
            )
        ''', params).fetchone()[0]
        con.close()
        return jsonify({'ok': True, 'artists': [dict(r) for r in rows], 'total': total})
    except sqlite3.Error as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/stream/<int:item_id>')
def stream(item_id):
    """Serve one library file to the browser's <audio> element.

    Addressed by item id, never by path: the only files this can ever hand
    out are ones beets already has in its database, so there's no traversal
    to defend against. send_file's conditional=True does the Range/206
    handling — that, and nothing else, is what makes seeking work.
    """
    con = _library_con()
    if con is None:
        return jsonify({'ok': False, 'error': 'no library database'}), 404
    row = con.execute('SELECT path FROM items WHERE id = ?', (item_id,)).fetchone()
    con.close()
    if not row:
        return jsonify({'ok': False, 'error': 'no such track'}), 404

    path = resolve_item_path(row['path'])
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'file is missing from disk'}), 404
    return send_file(path, conditional=True,
                     mimetype=AUDIO_MIMETYPES.get(Path(path).suffix.lower()))


@app.route('/reveal/<int:item_id>', methods=['POST'])
def reveal(item_id):
    """Select one library file in Finder (#71).

    Same id-only addressing as /stream: the path is looked up here, never
    sent by the caller, so this can only ever point Finder at a file beets
    already knows about.
    """
    con = _library_con()
    if con is None:
        return jsonify({'ok': False, 'error': 'no library database'}), 404
    row = con.execute('SELECT path FROM items WHERE id = ?', (item_id,)).fetchone()
    con.close()
    if not row:
        return jsonify({'ok': False, 'error': 'no such track'}), 404
    path = resolve_item_path(row['path'])
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'file is missing from disk'}), 404
    subprocess.run(['open', '-R', path], check=False)
    return jsonify({'ok': True, 'path': path})


@app.route('/art/<int:album_id>')
def album_art(album_id):
    """Cover art for an album, if beets has any on disk. Same id-only
    addressing as /stream — nothing here ever fetches or generates art, it
    only serves what fetchart/embedart already put at albums.artpath."""
    con = _library_con()
    if con is None:
        return jsonify({'ok': False, 'error': 'no library database'}), 404
    row = con.execute('SELECT artpath FROM albums WHERE id = ?', (album_id,)).fetchone()
    con.close()
    if not row or not row['artpath']:
        return jsonify({'ok': False, 'error': 'no art'}), 404
    path = resolve_item_path(row['artpath'])
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'art file is missing from disk'}), 404
    return send_file(path, conditional=True)


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
    if busy := _busy_response():
        return busy
    data = request.get_json(silent=True) or {}
    album_id = data.get('id')
    if not isinstance(album_id, int):
        return jsonify({'ok': False, 'error': 'id must be an album id'}), 400
    found = duplicates.delete_album(album_id, bool(data.get('delete_files')))
    if not found:
        return jsonify({'ok': False, 'error': 'no such album'}), 404
    return jsonify({'ok': True})


@app.route('/info/stats')
def info_stats():
    return jsonify({'ok': True, **libops.stats()})


@app.route('/info/fields')
def info_fields():
    data = {'ok': True, **libops.fields()}
    # Real items columns, not beets' full Model.all_keys() (that includes
    # flexible attributes with no SQL column) — this is specifically what
    # the Library tab's "sort by" dropdown can safely offer.
    con = _library_con()
    if con is not None:
        data['item_columns'] = sorted(r['name'] for r in con.execute('PRAGMA table_info(items)'))
        con.close()
    return jsonify(data)


@app.route('/info/version')
def info_version():
    return jsonify({'ok': True, **libops.version()})


@app.route('/library/modify/preview', methods=['POST'])
def library_modify_preview():
    """Body: {"field": "artist", "value": "Burial", "scope": {...}}."""
    data = request.get_json(silent=True) or {}
    field, value = data.get('field'), data.get('value')
    if not field or value is None:
        return jsonify({'ok': False, 'error': 'field and value are required'}), 400
    changes = libops.preview_modify(field, value, libops.scope_query(data))
    return jsonify({'ok': True, 'changes': changes})


@app.route('/library/modify', methods=['POST'])
def library_modify():
    if busy := _busy_response():
        return busy
    data = request.get_json(silent=True) or {}
    field, value = data.get('field'), data.get('value')
    if not field or value is None:
        return jsonify({'ok': False, 'error': 'field and value are required'}), 400
    changed = libops.apply_modify(field, value, libops.scope_query(data))
    return jsonify({'ok': True, 'changed': changed})


@app.route('/library/update', methods=['POST'])
def library_update():
    """Body: {"scope": {...}, "pretend": true}. `pretend` defaults to true:
    this rewrites library rows, so an omitted flag must not mean "do it"."""
    if busy := _busy_response():
        return busy
    data = request.get_json(silent=True) or {}
    try:
        output = libops.update(libops.scope_query(data),
                               bool(data.get('pretend', True)))
    except UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True, 'output': output})


@app.route('/library/write', methods=['POST'])
def library_write():
    """Body: {"scope": {...}, "pretend": true}. `pretend` defaults to true:
    this writes tags into files, so an omitted flag must not mean "do it"."""
    if busy := _busy_response():
        return busy
    data = request.get_json(silent=True) or {}
    try:
        output = libops.write(libops.scope_query(data),
                              bool(data.get('pretend', True)))
    except UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True, 'output': output})


@app.route('/library/missing')
def library_missing():
    return jsonify({'ok': True, 'albums': libops.missing_albums()})


@app.route('/library/count', methods=['POST'])
def library_count():
    """Albums a scope matches — the preview step before artwork/sync jobs,
    which have no pretend mode of their own to preview with.

    POST with a scope body rather than GET with a query string (#64): the
    caller is an album-level action, and an `ids` scope is the one shape a
    URL parameter can't carry without the client rebuilding the id-query
    that libops.scope_query() already owns.
    """
    data = request.get_json(silent=True) or {}
    try:
        return jsonify({'ok': True,
                        'count': libops.album_count(libops.scope_query(data))})
    except UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/library/remove/preview', methods=['POST'])
def library_remove_preview():
    """Body: {"scope": {"query": "..."}} or {"scope": {"ids": [...]}}."""
    data = request.get_json(silent=True) or {}
    query = libops.scope_query(data)
    # Parse first, so a malformed query is a 400 (via the UserError handler)
    # rather than being swallowed below and reported as "nothing matches" —
    # this preview is what gates the destructive remove, so the two cases
    # must not look identical.
    libops.split_query(query)
    try:
        items = libops.preview_remove(query)
    except UserError:
        items = []          # beets raises this when nothing matches
    return jsonify({'ok': True, 'items': items})


@app.route('/library/remove', methods=['POST'])
def library_remove():
    """Body: {"scope": {...}, "expect": 12, "delete_files": false}.
    "Remove short files" is the same call with scope.query=length:..N, built
    client-side.

    `expect` is the item count the caller previewed and is required, because
    libops.remove() passes force=True to beets — the preview *is* the
    confirmation, so this endpoint has to verify one actually happened and
    still describes the same set. Mismatch means the library changed under
    the caller, and the answer is to preview again rather than to guess.
    """
    if busy := _busy_response():
        return busy
    data = request.get_json(silent=True) or {}
    query = libops.scope_query(data)
    expect = data.get('expect')
    if not query.strip():
        return jsonify({'ok': False, 'error': 'a scope is required'}), 400
    if not isinstance(expect, int) or isinstance(expect, bool):
        return jsonify({'ok': False,
                        'error': 'expect (the previewed item count) is required'}), 400
    try:
        actual = len(libops.preview_remove(query))
        if actual != expect:
            return jsonify({'ok': False, 'error': (
                f'the library changed since the preview — {actual} items match '
                f'now, not {expect}. Preview again.')}), 409
        libops.remove(query, bool(data.get('delete_files')))
    except UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True, 'removed': actual})


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


def _parse_exclude(raw):
    return [e.strip().lower() for e in (raw or '').split(',') if e.strip()]


# A caller-named huge root (or one huge immediate subfolder under it) is
# intended use, not a bug — but a request that never returns, with no way
# to cancel, is a real usability failure regardless of intent (#35). A
# wall-clock budget shared across the whole request turns "forever" into
# "a few seconds with a truncated flag," which is all this needs to be.
_SCAN_BUDGET_SECONDS = 10


def _scan_files(dir_path, exclude, exts=None, deadline=None, truncated=None):
    """Audio files under dir_path, matching `exts` (default:
    AUDIO_EXTENSIONS) and not matching any exclude substring — same
    filter /unimported already uses.

    `deadline` (a time.monotonic() timestamp) stops the walk once passed;
    `truncated`, if given a list, gets `True` appended so the caller can
    tell "found everything" apart from "gave up".

    ponytail: Path.rglob has no documented guarantee against FIFOs or
    symlink loops on a user-supplied tree. /unimported's use of the same
    call was spot-checked against both during the issue #11 audit and
    returned promptly, but that was an observation, not a contract.
    """
    exts = exts or AUDIO_EXTENSIONS
    root = Path(dir_path)
    for f in root.rglob('*'):
        if deadline and time.monotonic() > deadline:
            if truncated is not None:
                truncated.append(True)
            return
        if not f.is_file() or f.suffix.lower() not in exts:
            continue
        if exclude and any(e in str(f).lower() for e in exclude):
            continue
        yield f


def _sized_or_truncated(paths, deadline):
    """Sum file sizes under `paths`, stopping early past `deadline`.
    Returns (total_bytes, hit_deadline)."""
    total = 0
    for f in paths:
        if time.monotonic() > deadline:
            return total, True
        if f.is_file():
            total += f.stat().st_size
    return total, False


def _walk_roots(dir_path):
    """Immediate subdirectories of dir_path, sorted, symlinks excluded.

    `rglob` doesn't follow symlinks inside a tree, but it does walk whatever
    root it is handed — and `is_dir()` follows symlinks, so a symlinked
    subfolder pointing at `/` used to become a walk root and take the whole
    filesystem with it (confirmed live in issue #30: the request never
    returned). Walking the `dir` the caller explicitly named is intended;
    silently following a symlink found inside it is not.
    """
    return sorted(p for p in Path(dir_path).iterdir()
                  if p.is_dir() and not p.is_symlink())


def _parse_exts(raw):
    """'wav,aiff' -> {'.wav', '.aiff'}, or None (meaning AUDIO_EXTENSIONS)
    if empty."""
    exts = {('.' + e.strip().lstrip('.').lower()) for e in (raw or '').split(',') if e.strip()}
    return exts or None


@app.route('/scan/count')
def scan_count():
    """Count audio files under `dir` — same result as
    `fd -e mp3 ... . dir | wc -l`. Optional `ext=wav,aiff` narrows the
    match, same as `fd -e wav -e aiff`."""
    dir_path = os.path.expanduser(request.args.get('dir', '').strip())
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({'ok': False, 'error': 'no such directory'}), 400
    exclude = _parse_exclude(request.args.get('exclude', ''))
    exts = _parse_exts(request.args.get('ext', ''))
    truncated = []
    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    total = sum(1 for _ in _scan_files(dir_path, exclude, exts, deadline, truncated))
    return jsonify({'ok': True, 'total_files': total, 'truncated': bool(truncated)})


@app.route('/scan/list')
def scan_list():
    """List matching audio files under `dir`, optionally filtered to
    `ext=wav,aiff` — same result as `fd -e wav -e aiff . dir`."""
    dir_path = os.path.expanduser(request.args.get('dir', '').strip())
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({'ok': False, 'error': 'no such directory'}), 400
    exclude = _parse_exclude(request.args.get('exclude', ''))
    exts = _parse_exts(request.args.get('ext', ''))
    truncated = []
    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    files = sorted(str(f) for f in _scan_files(dir_path, exclude, exts, deadline, truncated))
    return jsonify({'ok': True, 'files': files, 'truncated': bool(truncated)})


@app.route('/scan/save-list', methods=['POST'])
def scan_save_list():
    """Write the sorted matching file list to `dest`. Body:
    {"dir": "...", "exclude": "Samples,Stems", "dest": "~/Desktop/music_list.txt"}."""
    data = request.get_json(silent=True) or {}
    dir_path = os.path.expanduser((data.get('dir') or '').strip())
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({'ok': False, 'error': 'no such directory'}), 400
    exclude = _parse_exclude(data.get('exclude', ''))
    dest = (data.get('dest') or '~/Desktop/music_list.txt').strip()
    truncated = []
    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    files = sorted(str(f) for f in _scan_files(dir_path, exclude, deadline=deadline, truncated=truncated))
    path, error = _write_list(dest, files)
    if error:
        return jsonify({'ok': False, 'error': error}), 400
    return jsonify({'ok': True, 'count': len(files), 'path': str(path), 'truncated': bool(truncated)})


@app.route('/scan/sizes')
def scan_sizes():
    """Per-immediate-subfolder disk usage under `dir` — same shape as
    `du -sh "$dir/"*/`."""
    dir_path = os.path.expanduser(request.args.get('dir', '').strip())
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({'ok': False, 'error': 'no such directory'}), 400
    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    folders = []
    truncated = False
    for sub in _walk_roots(dir_path):
        if time.monotonic() > deadline:
            truncated = True
            break
        size, hit_deadline = _sized_or_truncated(sub.rglob('*'), deadline)
        folders.append({'path': str(sub), 'size_bytes': size})
        if hit_deadline:
            truncated = True
            break
    return jsonify({'ok': True, 'folders': folders, 'truncated': truncated})


# The XML library file only ever exists here — iTunes/Music.app write it
# to one of these paths when "Share library XML" is on, never elsewhere.
# A full-home rglob (what the old `fd -H ... ~` command did) takes minutes
# on a real ~/Library-sized tree; checking these directly is both faster
# and exactly as correct, not a reduced-functionality shortcut.
ITUNES_LIBRARY_CANDIDATES = [
    '~/Music/iTunes/iTunes Library.xml',
    '~/Music/iTunes/iTunes Music Library.xml',
    '~/Music/Music/Music Library.xml',
]


@app.route('/scan/itunes-library')
def scan_itunes_library():
    """Find the iTunes/Music.app XML library file, if XML sharing is on."""
    matches = [p for p in (os.path.expanduser(c) for c in ITUNES_LIBRARY_CANDIDATES)
               if os.path.exists(p)]
    return jsonify({'ok': True, 'paths': matches})


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


@app.route('/playlists/resolve', methods=['POST'])
def resolve_playlists():
    """Item ids listed by one or more .m3u playlists — used by USB Mirror
    (#43) to scope a convert job to a playlist. Body: {"dir": "~/Playlister",
    "names": ["My Playlist"]}. Not beets' own `playlist:` query — see
    libops.playlist_item_ids's docstring for why."""
    data = request.get_json(silent=True) or {}
    names = data.get('names')
    if not isinstance(names, list) or not names or not all(
            isinstance(n, str) and n for n in names):
        return jsonify({'ok': False, 'error': 'names must be a non-empty list of strings'}), 400
    try:
        ids = libops.playlist_item_ids(data.get('dir') or '~/Playlister', names)
    except UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True, 'ids': ids})


@app.route('/export/m3u', methods=['POST'])
def export_m3u():
    """Write matching tracks' paths, one per line, to `dest` — same content
    as `beet list -f '$path' > dest`. Body: {"dest": "...", "query": ""}."""
    data = request.get_json(silent=True) or {}
    dest = (data.get('dest') or '').strip()
    if not dest:
        return jsonify({'ok': False, 'error': 'dest is required'}), 400
    lib = importsession.get_library()
    items = list(lib.items(libops.split_query(data.get('query', ''))))
    path, error = _write_list(dest, [displayable_path(i.path) for i in items])
    if error:
        return jsonify({'ok': False, 'error': error}), 400
    return jsonify({'ok': True, 'count': len(items), 'path': str(path)})


@app.route('/export/playlists', methods=['POST'])
def export_playlists():
    """Write one .m3u per immediate subfolder of `src` into
    ~/Desktop/playlister/, listing that subfolder's audio files —
    same result as the old `for dir in $(fd -d 1 -t d ...)` shell loop,
    without a shell. Body: {"src": "..."}."""
    data = request.get_json(silent=True) or {}
    src = os.path.expanduser((data.get('src') or '').strip())
    if not src or not os.path.isdir(src):
        return jsonify({'ok': False, 'error': 'no such directory'}), 400

    dest_dir = Path(os.path.expanduser('~/Desktop/playlister'))
    dest_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for sub in _walk_roots(src):
        files = sorted(
            f for f in sub.rglob('*')
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        )
        if not files:
            continue
        out = dest_dir / f'{sub.name}.m3u'
        out.write_text('\n'.join(str(f) for f in files) + '\n',
                       errors='surrogateescape')
        written.append(out.name)

    return jsonify({'ok': True, 'playlists': written, 'dest': str(dest_dir)})


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


@app.route('/artwork/start', methods=['POST'])
def artwork_start():
    """Fetch or embed cover art. Body: {"action": "fetch"|"embed",
    "scope": {...}, "force": false}. Progress streams on /jobs/<id>/events."""
    data = request.get_json(silent=True) or {}
    try:
        job = artwork.start(data.get('action', 'fetch'),
                            query=libops.scope_query(data),
                            force=data.get('force', False))
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    return jsonify({'ok': True, **job.summary()})


@app.route('/convert/start', methods=['POST'])
def convert_start():
    """Convert tracks with beets' convert plugin. Body: {"scope":
    {"query": "format:AIFF"}, "format": "alac", "pretend": true,
    "dest": null}. Progress streams on /jobs/<id>/events."""
    data = request.get_json(silent=True) or {}
    try:
        job = transcode.start(query=libops.scope_query(data),
                              fmt=data.get('format'),
                              pretend=data.get('pretend', True),
                              dest=data.get('dest'))
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    return jsonify({'ok': True, **job.summary()})


@app.route('/sync/start', methods=['POST'])
def sync_start():
    """Body: {"kind": "mbsync"|"bpsync", "scope": {...}}. Progress streams
    on /jobs/<id>/events."""
    data = request.get_json(silent=True) or {}
    try:
        job = sync.start(data.get('kind', ''), query=libops.scope_query(data))
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    return jsonify({'ok': True, **job.summary()})


@app.route('/traktor/scan/start', methods=['POST'])
def traktor_scan_start():
    """Rebuild the most-played list from old Traktor files (#42). Body:
    {"roots": ["~/Documents"], "reset": false}. Progress streams on
    /jobs/<id>/events.

    Read-only on the sources: the owner has lost this collection three
    times, so nothing here opens a .nml for writing, moves it, or deletes
    it. The only write is this app's own state file next to library.db.
    """
    data = request.get_json(silent=True) or {}
    roots = data.get('roots') or ['~/Documents']
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        return jsonify({'ok': False, 'error': 'roots must be a list of paths'}), 400
    roots = [os.path.expanduser(r.strip()) for r in roots if r.strip()]
    missing = [r for r in roots if not os.path.isdir(r)]
    if not roots or missing:
        return jsonify({'ok': False,
                        'error': f'no such directory: {missing[0]}' if missing
                                 else 'no roots given'}), 400
    db_path = get_library_db_path()
    try:
        job = traktor.start(roots, traktor.state_path(db_path),
                            db_path=db_path if Path(db_path).exists() else None,
                            reset=bool(data.get('reset')))
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    return jsonify({'ok': True, **job.summary()})


@app.route('/traktor/results')
def traktor_results():
    """The recovered list. Query: sort, missing_only, limit, offset.

    `assumptions` travels with the data on purpose — the play counts are
    estimates, and the UI has to be able to say so next to the numbers.
    """
    try:
        limit = min(int(request.args.get('limit', 200)), 2000)
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        return jsonify({'ok': False,
                        'error': 'limit and offset must be integers'}), 400
    path = traktor.state_path(get_library_db_path())
    if not Path(path).exists():
        return jsonify({'ok': True, 'total': 0, 'tracks': [], 'scanned': False,
                        'assumptions': [], 'sources': [], 'stats': {}})
    state = traktor.load_state(path)
    # Re-checked on every read rather than only at scan time: the point of the
    # list is "what do I still need", and importing a recovered track is
    # exactly what makes a row stale. Costs one query against a read-only
    # connection, and nothing is written back.
    db_path = get_library_db_path()
    if Path(db_path).exists():
        traktor.annotate_library(state['tracks'], db_path)
    out = traktor.results(
        state,
        sort=request.args.get('sort', 'play_count'),
        missing_only=request.args.get('missing_only', '1') != '0',
        limit=limit, offset=offset)
    return jsonify({'ok': True, 'scanned': True, **out,
                    'stats': state.get('stats', {}),
                    'assumptions': traktor.assumptions(state),
                    'sources': traktor.source_report(state)})


@app.route('/jobs/current')
def jobs_current():
    """The running job, if any — lets a reloaded page rejoin its stream.

    Also reports the jobs queued behind it (#32), in run order, so a
    client can show the whole line instead of just whichever one it
    happens to be subscribed to.

    When nothing is running, reports the most recently finished job too
    (#65) — current() itself deliberately excludes done jobs, so without
    this a reload right after a job finished showed nothing."""
    job = jobs.current()
    if not job:
        return jsonify({'ok': True, 'id': None, 'last_finished': jobs.last_finished()})
    decision = job.pending() if hasattr(job, 'pending') else None
    return jsonify({'ok': True, **job.summary(), 'decision': decision,
                    'queued': [q.summary() for q in jobs.queued()]})


@app.route('/jobs/<job_id>/dequeue', methods=['POST'])
def job_dequeue(job_id):
    """Remove a job still waiting in line (#32). The running job can't be
    dequeued this way — that's what /abort is for."""
    if jobs.dequeue(job_id):
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'job is not queued'}), 404


@app.route('/jobs/<job_id>/move', methods=['POST'])
def job_move(job_id):
    """Reorder a queued job (#32). Body: {"direction": "up"|"down"}."""
    data = request.get_json(silent=True) or {}
    delta = {'up': -1, 'down': 1}.get(data.get('direction'))
    if delta is None:
        return jsonify({'ok': False, 'error': 'direction must be "up" or "down"'}), 400
    if jobs.move_queued(job_id, delta):
        return jsonify({'ok': True})
    return jsonify({'ok': False,
                     'error': 'job is not queued, or already at that end'}), 404


@app.route('/jobs/<job_id>/events')
def job_events(job_id):
    """SSE stream of status, decision and done events. Each connection gets
    its own fan-out queue (jobs.py), so multiple tabs each see the complete
    stream instead of splitting one shared queue between them."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'unknown job id'}), 404

    def generate():
        q = job.subscribe()
        try:
            # A reconnecting client must not have to wait out the decision
            # timeout, so replay whatever the job says it owes a fresh
            # listener (only import has anything) — but send each decision
            # only once, since the queue may still hold the copy this replaces.
            sent = set()
            for event in job.replay():
                sent.add(event['decision_id'])
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = q.get(timeout=15)
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
        finally:
            job.unsubscribe(q)

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control':     'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection':        'keep-alive',
    })


@app.route('/jobs/<job_id>/abort', methods=['POST'])
def job_abort(job_id):
    """Cancel the job. Abort is cooperative — the worker stops at its next
    checkpoint, so this waits for the job to actually finish.

    mbsync/bpsync (sync.py) checkpoint only between their two phases, not
    between items within a phase — each phase is one plain `for` loop inside
    the beets plugin itself, with no hook to interrupt mid-loop without
    duplicating its matching logic. So this can legitimately time out with
    the job still running; `finished: false` is a true answer, not a bug,
    and the caller is expected to say so rather than imply it's stuck.

    A still-running-but-aborted job no longer blocks the next one, though
    (#32): starting a new job now queues it behind this one instead of
    being rejected — see jobs.start()."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'unknown job id'}), 404
    job.abort()
    job.done.wait(timeout=30)
    return jsonify({'ok': True, 'finished': job.done.is_set(), **job.summary()})


@app.route('/import/<job_id>/decide', methods=['POST'])
def import_decide(job_id):
    """Answer the waiting decision. Body: {"decision_id": "...", "choice": "...",
    "candidate": 0}."""
    job = jobs.get(job_id)
    if not job or not hasattr(job, 'answer'):
        return jsonify({'ok': False, 'error': 'unknown import id'}), 404
    data = request.get_json(silent=True) or {}
    error = job.answer(data.get('decision_id'), data)
    if error:
        return jsonify({'ok': False, 'error': error}), 409
    return jsonify({'ok': True})


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
