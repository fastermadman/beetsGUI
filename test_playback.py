#!/usr/bin/env python3
"""
Unit test for the Library browse endpoints and /stream (#58).

Three things here can break silently and are worth pinning down:

  * the ?sort= whitelist — it is the one place a request string reaches SQL
    that can't be a bound parameter. A curated key (artist, title, ...) maps
    to a fixed column list; anything else is only accepted after being
    checked against the live schema (PRAGMA table_info), never spliced in
    raw — an unresolvable key has to fall back to the default rather than
    reach sqlite;
  * Range/206 on /stream — that, and only that, is what lets the <audio>
    element seek. A 200 with the whole body "works" in the browser right up
    until you drag the scrubber;
  * /art addressed by album id only, same as /stream by item id — no path
    ever comes from the request.

Runs against a synthetic library.db and a generated WAV, so no beets library
and no real music are needed.

Run: python test_playback.py
"""
import os
import shutil
import sqlite3
import tempfile
import wave
from pathlib import Path

from beets.library import Library

import libops
import server

BASE = f'http://localhost:{server.PORT}'  # _reject_foreign_host wants the real host


def make_wav(path, seconds=1):
    """A silent mono WAV — something with a real length to serve and seek in."""
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b'\0\0' * 8000 * seconds)


def make_library(dir_path, audio_path, art_path):
    """A library.db with the columns these endpoints actually read.

    bpm/initial_key aren't used by any hardcoded sort — they're here so
    ?sort=bpm can be exercised as a *dynamic* (schema-validated, not
    hardcoded) sort column, same as a real beets library would offer.
    """
    db = dir_path / 'library.db'
    con = sqlite3.connect(db)
    con.executescript('''
        CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT,
                             album TEXT, year INTEGER, added REAL, artpath BLOB);
        CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT,
                            artist TEXT, album TEXT, albumartist TEXT,
                            album_id INTEGER, year INTEGER, length REAL,
                            format TEXT, track INTEGER, disc INTEGER,
                            added REAL, bpm INTEGER, initial_key TEXT);
    ''')
    con.execute("INSERT INTO albums VALUES (1,'Burial','Untrue',2007,100.0,?)",
               (os.fsencode(str(art_path)),))
    con.execute("INSERT INTO albums VALUES (2,'Autechre','Amber',1994,200.0,NULL)")
    con.execute(
        'INSERT INTO items VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (os.fsencode(str(audio_path)), 'Archangel', 'Burial', 'Untrue',
         'Burial', 1, 2007, 1.0, 'WAV', 1, 1, 100.0, 140, 'F#m'))
    con.execute(
        'INSERT INTO items VALUES (2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (os.fsencode('/nonexistent/gone.flac'), 'Near Dark', 'Burial',
         'Untrue', 'Burial', 1, 2007, 2.0, 'FLAC', 2, 1, 100.0, 122, 'Am'))
    con.execute(
        'INSERT INTO items VALUES (3,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (os.fsencode('/nonexistent/foil.flac'), 'Foil', 'Autechre', 'Amber',
         'Autechre', 2, 1994, 3.0, 'FLAC', 1, 1, 200.0, 160, 'Cm'))
    con.commit()
    con.close()
    return db


def main():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    audio = root / 'archangel.wav'
    art = root / 'cover.jpg'
    make_wav(audio)
    art.write_bytes(b'\xff\xd8\xff\xe0fake jpeg')
    db = make_library(root, audio, art)

    # The real one shells out to `beet config --path`.
    server.get_library_db_path = lambda: str(db)
    # `?q=` is a beets query since #64: the list endpoints resolve it through
    # beets and then read the matching rows straight out of the database, so
    # the test needs both halves. They point at a *copy* rather than the same
    # file because opening a beets Library migrates the schema it finds —
    # which would quietly add every real beets column to the deliberately
    # partial `items` table above and take the `sort=composers` fallback case
    # below with it. Same ids, same rows, so the two agree on everything the
    # endpoints actually compare.
    beets_db = root / 'beets-copy.db'
    shutil.copy(db, beets_db)
    libops.get_library = lambda: Library(str(beets_db))
    client = server.app.test_client()

    def get(path, **kw):
        return client.get(path, base_url=BASE, **kw)

    # ── browse ────────────────────────────────────────────────────────────
    r = get('/library')
    assert r.status_code == 200, r.status_code
    assert [a['album'] for a in r.json['albums']] == ['Amber', 'Untrue'], r.json

    r = get('/library/tracks')
    assert r.json['total'] == 3, r.json
    assert r.json['tracks'][0]['title'] == 'Foil', r.json  # Autechre sorts first

    r = get('/library/tracks?album_id=1')
    assert [t['title'] for t in r.json['tracks']] == ['Archangel', 'Near Dark'], r.json

    r = get('/library/tracks?q=archangel')
    assert [t['id'] for t in r.json['tracks']] == [1], r.json

    r = get('/library/artists')
    assert [(a['name'], a['albums'], a['tracks']) for a in r.json['artists']] == \
        [('Autechre', 1, 1), ('Burial', 1, 2)], r.json

    r = get('/library/tracks?album_id=notanumber')
    assert r.status_code == 400, r.status_code

    # ── sorting ───────────────────────────────────────────────────────────
    asc = [t['title'] for t in get('/library/tracks?sort=title').json['tracks']]
    desc = [t['title'] for t in get('/library/tracks?sort=title&dir=desc').json['tracks']]
    assert asc == ['Archangel', 'Foil', 'Near Dark'], asc
    assert desc == list(reversed(asc)), desc

    years = [a['year'] for a in get('/library?sort=year&dir=desc').json['albums']]
    assert years == [2007, 1994], years

    # An unknown or hostile sort key falls back to the default; nothing from
    # the query string is ever spliced into the ORDER BY.
    for hostile in ('bogus', '1; DROP TABLE items--', "a' OR '1'='1"):
        r = get('/library/tracks?sort=' + hostile)
        assert r.status_code == 200, (hostile, r.status_code)
        assert [t['title'] for t in r.json['tracks']] == ['Foil', 'Archangel', 'Near Dark'], \
            (hostile, r.json)
    assert get('/library/tracks').json['total'] == 3, 'items table survived'

    # A real column that isn't in TRACK_SORTS (bpm, an ID3-ish field a user
    # picked from the "All fields" dropdown) resolves dynamically.
    bpm_asc = [t['title'] for t in get('/library/tracks?sort=bpm').json['tracks']]
    assert bpm_asc == ['Near Dark', 'Archangel', 'Foil'], bpm_asc  # 122, 140, 160
    key_desc = [t['title'] for t in get('/library/tracks?sort=initial_key&dir=desc').json['tracks']]
    assert key_desc == ['Archangel', 'Foil', 'Near Dark'], key_desc  # F#m, Cm, Am

    # A column absent from *this* items table (e.g. a real beets field this
    # synthetic schema doesn't include) falls back the same way a bogus
    # string does — the dynamic path is schema-checked, not name-checked.
    fallback = [t['title'] for t in get('/library/tracks?sort=composers').json['tracks']]
    assert fallback == ['Foil', 'Archangel', 'Near Dark'], fallback  # default order

    # ── streaming ─────────────────────────────────────────────────────────
    body = audio.read_bytes()

    r = get('/stream/1')
    assert r.status_code == 200, r.status_code
    assert r.data == body, (len(r.data), len(body))
    assert r.headers['Content-Type'].startswith('audio/'), r.headers['Content-Type']
    assert r.headers.get('Accept-Ranges') == 'bytes', dict(r.headers)

    # The seek guarantee: a Range request must come back as a 206 with only
    # the bytes asked for, or dragging the scrubber restarts the download.
    r = get('/stream/1', headers={'Range': 'bytes=100-199'})
    assert r.status_code == 206, r.status_code
    assert r.data == body[100:200], (len(r.data), r.headers.get('Content-Range'))
    assert r.headers['Content-Range'] == f'bytes 100-199/{len(body)}', r.headers['Content-Range']

    assert get('/stream/2').status_code == 404, 'file missing from disk'
    assert get('/stream/9999').status_code == 404, 'no such item'

    # A path can only ever come out of the database, so there is no traversal
    # surface — the route doesn't accept anything but an integer id.
    assert get('/stream/../../etc/passwd').status_code == 404

    # ── cover art ─────────────────────────────────────────────────────────
    r = get('/art/1')
    assert r.status_code == 200, r.status_code
    assert r.data == art.read_bytes()
    assert get('/art/2').status_code == 404, 'album has no artpath'
    assert get('/art/9999').status_code == 404, 'no such album'

    # ── /info/fields' item_columns (feeds the Tracks sort dropdown) ────────
    cols = get('/info/fields').json['item_columns']
    assert {'bpm', 'initial_key', 'title', 'artist'} <= set(cols), cols

    # ── no library at all ─────────────────────────────────────────────────
    server.get_library_db_path = lambda: str(root / 'gone.db')
    assert get('/library').json == {'ok': True, 'albums': [], 'total': 0}
    assert get('/library/tracks').json == {'ok': True, 'tracks': [], 'total': 0}
    assert get('/library/artists').json == {'ok': True, 'artists': [], 'total': 0}
    assert get('/stream/1').status_code == 404
    assert get('/art/1').status_code == 404

    # ── relative paths (beetsGUI#78) ────────────────────────────────────────
    # This user's library has items.path/albums.artpath stored relative
    # rather than absolute (root cause not yet found) — /stream and /art
    # must resolve them against `directory:` rather than assume they're
    # already absolute, or every one of those rows 404s as "missing from
    # disk" and the player blames it on "no decoder" instead.
    rel_tmp = tempfile.TemporaryDirectory()
    rel_root = Path(rel_tmp.name)
    (rel_root / 'sub').mkdir()
    rel_audio = rel_root / 'sub' / 'archangel.wav'
    make_wav(rel_audio)
    rel_art = rel_root / 'sub' / 'cover.jpg'
    rel_art.write_bytes(b'\xff\xd8\xff\xe0fake jpeg')
    rel_db = rel_root / 'rel-library.db'
    con = sqlite3.connect(rel_db)
    con.executescript('''
        CREATE TABLE albums (id INTEGER PRIMARY KEY, artpath BLOB);
        CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB);
    ''')
    con.execute('INSERT INTO albums VALUES (1,?)', (b'sub/cover.jpg',))
    con.execute('INSERT INTO items VALUES (1,?)', (b'sub/archangel.wav',))
    con.commit()
    con.close()

    server.get_library_db_path = lambda: str(rel_db)
    server.get_library_directory = lambda: str(rel_root)

    r = get('/stream/1')
    assert r.status_code == 200, r.status_code
    assert r.data == rel_audio.read_bytes(), 'relative item path did not resolve'

    r = get('/art/1')
    assert r.status_code == 200, r.status_code
    assert r.data == rel_art.read_bytes(), 'relative artpath did not resolve'

    rel_tmp.cleanup()
    tmp.cleanup()
    print('test_playback: all checks passed')


if __name__ == '__main__':
    main()
