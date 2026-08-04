#!/usr/bin/env python3
"""
End-to-end check for the in-process importer.

Boots server.py against a throwaway beets config (BEETSDIR in a temp dir,
never the real library), then drives /import/* and /jobs/* over HTTP as curl
would: start → read the decision off the SSE stream → answer → assert the
result landed in library.db.

Run: ~/.local/pipx/venvs/beets/bin/python test_importsession.py
Needs ffmpeg to make the test audio.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 1319
BASE = f'http://127.0.0.1:{PORT}'
HERE = Path(__file__).parent.resolve()


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def events(job_id):
    """Yield decoded SSE events until 'done'."""
    stream = urllib.request.urlopen(f'{BASE}/jobs/{job_id}/events', timeout=180)
    for raw in stream:
        line = raw.decode().rstrip('\n')
        if not line.startswith('data: '):
            continue
        event = json.loads(line[6:])
        yield event
        if event['type'] == 'done':
            stream.close()
            return


job = [None]   # id of the import currently under test


def run(path, mode='interactive', **extra):
    """Start an import and return its event stream."""
    status, body = post('/import/start', {'path': str(path), 'mode': mode, **extra})
    assert status == 200 and body['ok'], body
    job[0] = body['id']
    return events(job[0])


def run_multi(paths, mode='interactive'):
    """Start an import over a curated list of folders."""
    status, body = post('/import/start', {'paths': [str(p) for p in paths], 'mode': mode})
    assert status == 200 and body['ok'], body
    job[0] = body['id']
    return events(job[0])


def decide(event, expect=200, **choice):
    code, reply = post(f'/import/{job[0]}/decide',
                       {'decision_id': event['decision_id'], **choice})
    assert code == expect, (code, reply)
    return code, reply


def make_track(path, artist, album, title, track):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'ffmpeg', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono', '-t', '1',
        '-metadata', f'artist={artist}', '-metadata', f'album={album}',
        '-metadata', f'title={title}', '-metadata', f'track={track}',
        str(path),
    ], check=True)


def make_album(directory, artist, album, titles, ext='mp3'):
    """MP3 by default (lossy); pass ext='flac' for a lossless fixture."""
    for i, title in enumerate(titles, 1):
        make_track(directory / f'{i:02d} {title}.{ext}', artist, album, title, i)


# A fake metadata source, so the test gets candidates without the network.
STUB_PLUGIN = '''
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.metadata_plugins import MetadataSourcePlugin


class StubsourcePlugin(MetadataSourcePlugin):
    def album_for_id(self, album_id):
        return None

    def track_for_id(self, track_id):
        return None

    def candidates(self, items, artist, album, va_likely):
        return [AlbumInfo(
            album='Canonical Album', album_id='stub-album',
            artist='Canonical Artist', artist_id='stub-artist',
            data_source='Stubsource', year=1999,
            tracks=[TrackInfo(title=f'Canonical {i}', track_id=f'stub-{i}',
                              artist='Canonical Artist', index=i, length=1.0)
                    for i in range(1, len(items) + 1)],
        )]

    def item_candidates(self, item, artist, title):
        return []
'''


def start_server(beetsdir, **extra_env):
    env = {**os.environ, 'BEETSDIR': str(beetsdir),
           'BEETSGUI_PORT': str(PORT), 'BEETSGUI_NO_OPEN': '1', **extra_env}
    proc = subprocess.Popen(
        [sys.executable, str(HERE / 'server.py')], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        try:
            urllib.request.urlopen(f'{BASE}/status', timeout=1).read()
            return proc
        except Exception:
            if proc.poll() is not None:
                raise SystemExit(f'server died:\n{proc.stderr.read()}')
            time.sleep(0.2)
    raise SystemExit('server did not come up')


def stop_server(proc):
    proc.terminate()
    proc.wait(timeout=10)
    for _ in range(50):          # let the port come free again
        try:
            urllib.request.urlopen(f'{BASE}/status', timeout=1).read()
            time.sleep(0.2)
        except Exception:
            return


def albums_in(beetsdir):
    import sqlite3
    con = sqlite3.connect(beetsdir / 'library.db')
    rows = con.execute('SELECT albumartist, album FROM albums').fetchall()
    con.close()
    return rows


def formats_in(beetsdir, albumartist=None, artist=None, title=None):
    """Distinct item formats for an album (by albumartist) or a singleton
    (by artist+title) — used to check whether a duplicate was replaced."""
    import sqlite3
    con = sqlite3.connect(beetsdir / 'library.db')
    if albumartist is not None:
        rows = con.execute(
            'SELECT DISTINCT i.format FROM items i JOIN albums a ON i.album_id = a.id '
            'WHERE a.albumartist = ?', (albumartist,)).fetchall()
    else:
        rows = con.execute(
            'SELECT DISTINCT format FROM items WHERE artist = ? AND title = ?',
            (artist, title)).fetchall()
    con.close()
    return sorted(r[0] for r in rows)


def resolve_asis(stream):
    """Drive a stream that should ask exactly one match decision (answered
    asis) and then either a duplicate decision or none. Returns the
    duplicate decision event, or None if it was auto-skipped."""
    dup_event = None
    for event in stream:
        if event['type'] != 'decision':
            continue
        if event['kind'] in ('album', 'item'):
            decide(event, choice='asis')
        elif event['kind'] == 'duplicate':
            dup_event = event
            return dup_event, stream
    return dup_event, stream


def main():
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg needed to generate test audio — skipping')

    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-test-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
    # Never hit MusicBrainz and never touch the real library: the only
    # metadata source is the stub, and everything lives under BEETSDIR.
    plugindir = beetsdir / 'plugins'
    plugindir.mkdir()
    (plugindir / 'stubsource.py').write_text(STUB_PLUGIN)
    (beetsdir / 'config.yaml').write_text(
        f'directory: {tmp / "library"}\n'
        f'library: {beetsdir / "library.db"}\n'
        f'pluginpath: [{plugindir}]\n'
        'plugins: [stubsource]\n'
        'import:\n'
        '    resume: ask\n'
        '    copy: yes\n'
        '    write: no\n'
    )
    src = tmp / 'incoming'
    make_album(src / 'one', 'Test Artist', 'Test Album', ['Alpha', 'Beta'])
    make_album(src / 'two', 'Other Artist', 'Other Album', ['Gamma'])
    make_album(src / 'three', 'Third Artist', 'Third Album', ['Delta'])
    make_album(src / 'multi' / 'a', 'Multi Artist', 'Multi A', ['Eps'])
    make_album(src / 'multi' / 'b', 'Multi Artist', 'Multi B', ['Zeta'])
    make_album(src / 'sel' / 'x', 'Selection Artist', 'Selection X', ['Eta'])
    make_album(src / 'sel' / 'y', 'Selection Artist', 'Selection Y', ['Theta'])

    proc = start_server(beetsdir)
    try:
        # 1. APPLY: the decision carries the candidate, we pick it, and the
        #    chosen metadata is what lands in the library.
        decisions = 0
        for event in run(src / 'one'):
            if event['type'] == 'decision':
                assert event['kind'] == 'album', event
                assert event['item_count'] == 2, event
                assert event['current'] == {'artist': 'Test Artist',
                                            'album': 'Test Album'}, event
                candidate = event['candidates'][0]
                assert candidate['title'] == 'Canonical Album', candidate
                assert [t['new'] for t in candidate['tracks']] == \
                    ['Canonical 1', 'Canonical 2'], candidate
                decide(event, choice='apply', candidate=0)
                decisions += 1
        assert decisions == 1, f'expected one decision, got {decisions}'
        assert ('Canonical Artist', 'Canonical Album') in albums_in(beetsdir), \
            albums_in(beetsdir)

        # 2. ASIS keeps the metadata already on the files.
        for event in run(src / 'two'):
            if event['type'] == 'decision':
                decide(event, choice='asis')
        assert ('Other Artist', 'Other Album') in albums_in(beetsdir), \
            albums_in(beetsdir)

        # 3. A bad answer is rejected and the decision stays open; SKIP then
        #    leaves the library untouched.
        before = albums_in(beetsdir)
        for event in run(src / 'three'):
            if event['type'] == 'decision':
                code, reply = decide(event, expect=409, choice='nonsense')
                code, reply = post(f'/import/{job[0]}/decide',
                                   {'decision_id': 'wrong-id', 'choice': 'skip'})
                assert code == 409, (code, reply)
                decide(event, choice='skip')
        assert albums_in(beetsdir) == before, 'skip should not add an album'

        # 4. Abort while a decision is blocked: the job finishes, nothing added.
        stream = run(src / 'three')
        for event in stream:
            if event['type'] == 'decision':
                code, reply = post(f'/jobs/{job[0]}/abort', {})
                assert code == 200 and reply['finished'], reply
                break
        tail = list(stream)
        assert tail and tail[-1]['type'] == 'done' and tail[-1]['aborted'], tail
        assert albums_in(beetsdir) == before, 'abort should not add an album'

        # 5. Starting is refused while a job is running.
        stream = run(src / 'three')
        code, reply = post('/import/start', {'path': str(src / 'three')})
        assert code == 409, (code, reply)
        for event in stream:
            if event['type'] == 'decision':
                decide(event, choice='skip')

        # 6. Quiet mode never asks: no decision events at all.
        assert not [e for e in run(src / 'three', mode='quiet')
                    if e['type'] == 'decision']
        assert albums_in(beetsdir) == before, 'quiet fallback is skip'

        # 7. Resume. Aborting after one album of two leaves progress in
        #    state.pickle; the next run must ask about it, and resuming must
        #    skip the album that already finished.
        stream = run(src / 'multi')
        seen = []
        for event in stream:
            if event['type'] != 'decision':
                continue
            seen.append(event['current'].get('album'))
            if len(seen) == 1:
                decide(event, choice='asis')     # finishes 'Multi A'
            else:
                post(f'/jobs/{job[0]}/abort', {})
                break
        list(stream)
        assert seen == ['Multi A', 'Multi B'], seen
        assert ('Multi Artist', 'Multi A') in albums_in(beetsdir)

        resumed = []
        for event in run(src / 'multi'):
            if event['type'] != 'decision':
                continue
            if event['kind'] == 'resume':
                resumed.append('asked')
                decide(event, choice='resume')
            else:
                resumed.append(event['current'].get('album'))
                decide(event, choice='asis')
        assert resumed == ['asked', 'Multi B'], resumed
        assert ('Multi Artist', 'Multi B') in albums_in(beetsdir)

        # 8. A curated `paths` list — e.g. picked in the Unimported tab or
        #    via fd — is passed straight through and beets groups it itself,
        #    exactly like `beet import path1 path2`.
        seen = []
        for event in run_multi([src / 'sel' / 'x', src / 'sel' / 'y']):
            if event['type'] == 'decision':
                seen.append(event['current'].get('album'))
                decide(event, choice='asis')
        assert sorted(seen) == ['Selection X', 'Selection Y'], seen
        assert ('Selection Artist', 'Selection X') in albums_in(beetsdir)
        assert ('Selection Artist', 'Selection Y') in albums_in(beetsdir)

        # 9. Bad input.
        code, reply = post('/import/start', {'path': '/nope/nope'})
        assert code == 400, (code, reply)
        code, reply = post('/import/start', {'path': str(src), 'mode': 'wat'})
        assert code == 400, (code, reply)
        code, reply = post('/jobs/does-not-exist/abort', {})
        assert code == 404, (code, reply)

        # 10. Quality-aware duplicates. `duplicate_keys` defaults to plain
        #    albumartist+album (or artist+title) text, no MusicBrainz ID
        #    needed, so asis imports of the same name are enough to collide.

        # 10a. Lossless replacing lossy: still asks, and recommends 'remove'.
        make_album(src / 'q1' / 'existing', 'Quality One', 'Album One', ['A'], ext='mp3')
        dup, stream = resolve_asis(run(src / 'q1' / 'existing'))
        assert dup is None
        make_album(src / 'q1' / 'incoming', 'Quality One', 'Album One', ['A'], ext='flac')
        dup, stream = resolve_asis(run(src / 'q1' / 'incoming'))
        assert dup is not None, 'lossless duplicate of a lossy album should still ask'
        assert dup.get('recommendation') == 'remove', dup
        decide(dup, choice='remove')
        list(stream)
        assert formats_in(beetsdir, albumartist='Quality One') == ['FLAC'], \
            formats_in(beetsdir, albumartist='Quality One')

        # 10b. Lossy vs existing lossless: auto-skipped, no decision, no
        #     'remove' reaching the existing (better) copy.
        make_album(src / 'q2' / 'existing', 'Quality Two', 'Album Two', ['A'], ext='flac')
        dup, stream = resolve_asis(run(src / 'q2' / 'existing'))
        assert dup is None
        make_album(src / 'q2' / 'incoming', 'Quality Two', 'Album Two', ['A'], ext='mp3')
        dup, stream = resolve_asis(run(src / 'q2' / 'incoming'))
        assert dup is None, 'a lossy duplicate of a lossless album must not ask'
        assert formats_in(beetsdir, albumartist='Quality Two') == ['FLAC'], \
            'auto-skip must not touch the existing lossless copy'

        # 10c. Equal quality: still asks, with no recommendation.
        make_album(src / 'q3' / 'existing', 'Quality Three', 'Album Three', ['A'], ext='mp3')
        dup, stream = resolve_asis(run(src / 'q3' / 'existing'))
        assert dup is None
        make_album(src / 'q3' / 'incoming', 'Quality Three', 'Album Three', ['A'], ext='mp3')
        dup, stream = resolve_asis(run(src / 'q3' / 'incoming'))
        assert dup is not None
        assert 'recommendation' not in dup, dup
        decide(dup, choice='keep')
        list(stream)

        # 10d. Worst-track rule: one lossy track among lossless ranks the
        #     whole album as lossy, so it loses to a fully lossless existing
        #     copy exactly like 10b — auto-skipped, no decision.
        (src / 'q4' / 'existing').mkdir(parents=True)
        make_track(src / 'q4' / 'existing' / '01 A.flac', 'Quality Four', 'Album Four', 'A', 1)
        make_track(src / 'q4' / 'existing' / '02 B.flac', 'Quality Four', 'Album Four', 'B', 2)
        dup, stream = resolve_asis(run(src / 'q4' / 'existing'))
        assert dup is None
        (src / 'q4' / 'incoming').mkdir(parents=True)
        make_track(src / 'q4' / 'incoming' / '01 A.flac', 'Quality Four', 'Album Four', 'A', 1)
        make_track(src / 'q4' / 'incoming' / '02 B.mp3', 'Quality Four', 'Album Four', 'B', 2)
        dup, stream = resolve_asis(run(src / 'q4' / 'incoming'))
        assert dup is None, 'one lossy track must make the whole album rank as lossy'
        assert formats_in(beetsdir, albumartist='Quality Four') == ['FLAC'], \
            formats_in(beetsdir, albumartist='Quality Four')

        # 10e. Singletons follow the same rule as albums. A bare file path
        #     works the same as a directory; `singleton=True` is what makes
        #     beets group it as an individual track instead of an album.
        make_track(src / 'q5existing.mp3', 'Quality Five', '', 'Solo Track', 1)
        dup, stream = resolve_asis(run(src / 'q5existing.mp3', singleton=True))
        assert dup is None
        make_track(src / 'q5incoming.flac', 'Quality Five', '', 'Solo Track', 1)
        dup, stream = resolve_asis(run(src / 'q5incoming.flac', singleton=True))
        assert dup is not None, 'lossless duplicate of a lossy singleton should still ask'
        assert dup.get('recommendation') == 'remove', dup
        decide(dup, choice='remove')
        list(stream)
        assert formats_in(beetsdir, artist='Quality Five', title='Solo Track') == ['FLAC'], \
            formats_in(beetsdir, artist='Quality Five', title='Solo Track')
    finally:
        stop_server(proc)

    # 10. A decision nobody answers times out instead of pinning the thread
    #    forever. Same server, restarted with a short timeout.
    proc = start_server(beetsdir, BEETSGUI_DECISION_TIMEOUT='3')
    try:
        before = albums_in(beetsdir)
        tail = None
        for event in run(src / 'three'):
            tail = event                       # answer nothing at all
        assert tail['type'] == 'done' and tail['aborted'], tail
        assert albums_in(beetsdir) == before, 'a timed-out import must not import'
        code, body = post('/import/start', {'path': str(src / 'three')})
        assert code == 200, (code, body)       # the slot is free again
        post(f'/jobs/{body["id"]}/abort', {})
        print('ok')
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
