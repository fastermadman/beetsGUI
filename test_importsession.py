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
import urllib.parse
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

        # 3b. Answering the same decision twice: the duplicate is rejected and
        #     the server stays responsive. A duplicate answer used to be
        #     accepted and then block in put() on the maxsize=1 reply queue
        #     while holding the job lock, which wedged the importer's own
        #     cleanup, /jobs/current and abort for the life of the process.
        for event in run(src / 'three'):
            if event['type'] == 'decision':
                decide(event, choice='skip')
                code, reply = decide(event, expect=409, choice='skip')
                assert 'decision' in reply['error'], reply
                with urllib.request.urlopen(f'{BASE}/jobs/current', timeout=5) as r:
                    assert json.load(r)['ok'], 'jobs/current must not hang'
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


def make_track_encoded(path, artist, album, title, track, extra_args):
    """Like make_track, but with extra ffmpeg args (bitrate/sample format/
    codec) so the fixture is real encoder output, not the anullsrc default —
    for the quality_rank() check below, which needs genuine bitdepth/bitrate
    numbers per format, not zeros."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'ffmpeg', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
        '-metadata', f'artist={artist}', '-metadata', f'album={album}',
        '-metadata', f'title={title}', '-metadata', f'track={track}',
        *extra_args, str(path),
    ], check=True)


def test_quality_rank_real_encoders():
    """#12 scope 2: quality_rank() and the 24-bit cap against real
    ffmpeg-encoded fixtures, not synthetic bitdepth/bitrate numbers.

    Real MP3 128/192/320kbps, FLAC and WAV at 16- and 24-bit, ALAC, and a
    32-bit-float WAV to exercise _MAX_RANKED_BITDEPTH. No server needed —
    this drives importsession.quality_rank() directly against MediaFile
    readings of real files.
    """
    from mediafile import MediaFile
    from importsession import quality_rank, LOSSLESS_FORMATS

    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-quality-'))

    class Track:
        def __init__(self, mf):
            self.format = mf.format          # real property: TYPES[mf.type],
                                              # e.g. 'WAVE' not 'WAV'.
            self.bitrate = mf.bitrate
            self.bitdepth = getattr(mf, 'bitdepth', None)
            self.samplerate = mf.samplerate

    def make(name, extra_args):
        p = tmp / name
        make_track_encoded(p, 'Q', 'Q', 'Q', 1, extra_args)
        return Track(MediaFile(p))

    mp3_128 = make('mp3_128.mp3', ['-b:a', '128k'])
    mp3_192 = make('mp3_192.mp3', ['-b:a', '192k'])
    mp3_320 = make('mp3_320.mp3', ['-b:a', '320k'])
    flac_16 = make('flac_16.flac', ['-sample_fmt', 's16'])
    flac_24 = make('flac_24.flac', ['-sample_fmt', 's32', '-bits_per_raw_sample', '24'])
    wav_16  = make('wav_16.wav', ['-c:a', 'pcm_s16le'])
    wav_24  = make('wav_24.wav', ['-c:a', 'pcm_s24le'])
    wav_32f = make('wav_32f.wav', ['-c:a', 'pcm_f32le'])
    alac    = make('alac.m4a', ['-c:a', 'alac'])

    assert mp3_128.format == 'MP3' and mp3_128.bitdepth in (0, None), mp3_128.__dict__
    assert wav_16.format == 'WAVE', (
        'real MediaFile.format for a .wav is "WAVE" (TYPES[type]), not "WAV" — '
        'importsession.LOSSLESS_FORMATS must key off that exact string or a '
        'lossless WAV silently ranks as lossy')
    assert flac_16.format == 'FLAC' and alac.format == 'ALAC'

    # Lossy always ranks below lossless, regardless of bitrate/bitdepth.
    assert quality_rank([mp3_320]) < quality_rank([flac_16]), \
        'a 320kbps MP3 must never outrank a lossless FLAC'
    assert quality_rank([mp3_320]) < quality_rank([wav_16]), (
        'a 320kbps MP3 must never outrank a lossless WAV — this is the case '
        'the WAVE/WAV format-string mismatch would silently break')

    # Bitrate orders lossy tracks against each other.
    assert quality_rank([mp3_128]) < quality_rank([mp3_192]) < quality_rank([mp3_320])

    # Bitdepth orders lossless tracks of the same format.
    assert quality_rank([flac_16]) < quality_rank([flac_24])
    assert quality_rank([wav_16]) < quality_rank([wav_24])

    # _MAX_RANKED_BITDEPTH: a real 32-bit-float WAV (bitdepth=32 as MediaFile
    # reports it) must rank exactly like a real 24-bit file, not above it —
    # the app's own convert step never produces better than 24-bit ALAC.
    assert wav_32f.bitdepth == 32, wav_32f.__dict__       # confirm the fixture is genuine
    assert quality_rank([wav_32f]) == quality_rank([wav_24]), \
        (quality_rank([wav_32f]), quality_rank([wav_24]))
    assert quality_rank([wav_32f]) == quality_rank([flac_24])

    shutil.rmtree(tmp, ignore_errors=True)
    print('quality_rank real-encoder check: ok')


def test_unicode_and_edge_case_metadata():
    """#12 scope 3: Danish æøå, emoji, CJK, embedded quotes and a very long
    string through the real pipeline — tags -> /unimported -> import
    decision (asis, so the tag is what lands) -> /library free-text search.
    """
    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-unicode-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
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

    # (label, artist, album, title, search needle). The needle is a
    # quote-free substring for the /library free-text-search assertion;
    # searching *with* an apostrophe is checked separately below.
    cases = [
        ('Danish', 'Søren Ærøskøbing', 'Blåbær Café', 'Rødgrød med fløde', 'Søre'),
        ('Emoji', 'DJ 🔥🎧', '🎶 Party Mix 🎶', 'Track 😀 One', '🔥🎧'),
        ('CJK', '田中太郎', 'ラウンジ', '夜の東京', '田中太郎'),
        ('Quotes', 'O\'Brien "The Mixer"', 'Rock\'n\'Roll "Live"', 'She said "hi"', 'Brien'),
        ('LongString', 'A' * 5, 'B' * 400, 'C' * 400, 'A' * 5),
    ]

    proc = start_server(beetsdir)
    try:
        for label, artist, album, title, needle in cases:
            folder = src / label
            make_track(folder / '01.mp3', artist, album, title, 1)

            # /unimported must detect the real file with unicode tags intact.
            with urllib.request.urlopen(
                    f'{BASE}/unimported?dir={urllib.parse.quote(str(folder))}',
                    timeout=30) as r:
                un = json.load(r)
            assert un['ok'] and un['total_files'] == 1, (label, un)

            # Import asis so the metadata that lands is exactly the tag.
            for event in run(folder):
                if event['type'] == 'decision':
                    assert event['current']['artist'] == artist, (label, event)
                    decide(event, choice='asis')

            rows = albums_in(beetsdir)
            assert (artist, album) in rows, (label, artist, album, rows)

            # /library free-text search must find it by a substring of the
            # unicode/emoji/CJK/quoted artist name.
            with urllib.request.urlopen(
                    f'{BASE}/library?q={urllib.parse.quote(needle)}',
                    timeout=30) as r:
                found = json.load(r)
            assert found['ok'], (label, found)
            assert any(a['albumartist'] == artist for a in found['albums']), \
                (label, needle, found['albums'])

        # An apostrophe is a letter in a music library, not an unclosed
        # quote. /library's free-text search runs through beets' shlex-style
        # tokenizer (libops.split_query), which used to raise on names as
        # ordinary as O'Brien or Guns N' Roses and surface as a 400 (#12).
        # It has to search now, and it has to find the real track.
        for needle in ("O'Brien", "Don't Stop", "N' Roses"):
            with urllib.request.urlopen(
                    f'{BASE}/library?q={urllib.parse.quote(needle)}',
                    timeout=10) as r:
                found = json.load(r)
            assert found['ok'], (needle, found)
        # The one that must actually match: an artist carrying an apostrophe.
        with urllib.request.urlopen(
                f'{BASE}/library?q={urllib.parse.quote("O\'Brien")}',
                timeout=10) as r:
            found = json.load(r)
        assert any("O'Brien" in (a['albumartist'] or '') for a in found['albums']), \
            f"searching for O'Brien found {found['albums']}"
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    print('unicode/edge-case metadata pipeline: ok')


def test_shell_metacharacter_and_space_paths():
    """#12 scope 3: paths with spaces, a unicode directory name, and
    shell-metacharacter directory names ($, backticks, ;) all the way
    through /import/start. The importer runs in-process (no shell spawned
    for the actual import — see importsession.WebImportSession.run()), so
    these should just be inert bytes in a path; this proves it rather than
    assuming it from reading the code.
    """
    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-shellchars-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
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
    dirnames = [
        'has spaces',
        'has$dollar',
        'has`backtick`',
        'has;semicolon',
        'has&ampersand',
        'unicode-æøå-日本語',
    ]

    proc = start_server(beetsdir)
    try:
        for name in dirnames:
            folder = src / name
            make_album(folder, f'Artist {name}', f'Album {name}', ['Only'])
            for event in run(folder):
                if event['type'] == 'decision':
                    decide(event, choice='asis')
            assert (f'Artist {name}', f'Album {name}') in albums_in(beetsdir), name
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    print('shell-metacharacter / unicode / space path import: ok')


def test_missing_ffmpeg_dependency():
    """#12 scope 4: hide ffmpeg from PATH and exercise the one feature that
    actually shells out to it (POST /convert/start, via beets' convert
    plugin) — clear error vs silent failure/hang.

    fd and xld: grepping the whole repo turns up no subprocess/shell call
    to either — fd is mentioned only as descriptive UI copy (the /scan/*
    endpoints reimplement the same walk in Python, per their own
    docstrings), and xld doesn't appear anywhere in the app or README.
    Hiding them from PATH would exercise nothing this app calls, so there
    is nothing to test for those two beyond noting they're not
    dependencies of the current code.
    """
    if not shutil.which('ffmpeg'):
        print('ffmpeg not on host PATH - skipping (nothing to hide)')
        return

    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-noffmpeg-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
    plugindir = beetsdir / 'plugins'
    plugindir.mkdir()
    (plugindir / 'stubsource.py').write_text(STUB_PLUGIN)
    convert_dest = tmp / 'converted'
    (beetsdir / 'config.yaml').write_text(
        f'directory: {tmp / "library"}\n'
        f'library: {beetsdir / "library.db"}\n'
        f'pluginpath: [{plugindir}]\n'
        'plugins: [stubsource, convert]\n'
        f'convert:\n'
        f'    dest: {convert_dest}\n'
        '    format: alac\n'
        'import:\n'
        '    resume: ask\n'
        '    copy: yes\n'
        '    write: no\n'
    )
    src = tmp / 'incoming'
    make_album(src / 'one', 'Dep Artist', 'Dep Album', ['Only'], ext='flac')

    # Import normally first (ffmpeg on PATH — import itself never needs it).
    proc = start_server(beetsdir)
    try:
        for event in run(src / 'one'):
            if event['type'] == 'decision':
                decide(event, choice='asis')
        assert ('Dep Artist', 'Dep Album') in albums_in(beetsdir)
    finally:
        stop_server(proc)

    # Restart with ffmpeg hidden from the server process's PATH, then try
    # to convert the track that's now in the library.
    scrubbed = os.pathsep.join(
        p for p in os.environ.get('PATH', '').split(os.pathsep)
        if not (Path(p) / 'ffmpeg').exists())
    proc = start_server(beetsdir, PATH=scrubbed)
    try:
        status, body = post('/convert/start',
                            {'scope': {'query': ''}, 'format': 'alac', 'pretend': False})
        assert status == 200 and body['ok'], body
        job_id = body['id']
        saw_error = False
        saw_done = False
        stream = urllib.request.urlopen(f'{BASE}/jobs/{job_id}/events', timeout=60)
        for raw in stream:
            line = raw.decode().rstrip('\n')
            if not line.startswith('data: '):
                continue
            event = json.loads(line[6:])
            if event['type'] == 'error':
                saw_error = True
                assert 'ffmpeg' in event['message'].lower(), event
            if event['type'] == 'done':
                saw_done = True
                stream.close()
                break
        assert saw_done, 'convert job must finish (not hang) when ffmpeg is missing'
        assert saw_error, 'convert job must surface a clear error when ffmpeg is missing'
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    print('missing-ffmpeg dependency check: clear error, no hang - ok')


def test_scale_library_endpoints():
    """#12 scope 1: /library, /library/tracks, /library/artists,
    /stream, /art against a library well beyond the 2-album scratch size
    used everywhere else in this file — enough to exercise the unindexed
    LIKE-based free-text search's real cost, not just prove the endpoints
    don't crash.

    Kept to a few hundred albums/files here so the suite stays fast to run
    repeatedly; the actual #12 acceptance numbers (5,000 albums / 40,000
    items, 2,200 real audio files) were measured once by hand against this
    same code path and are reported in the #12 findings, not re-measured on
    every test run.
    """
    N_ALBUMS = 300
    TRACKS_PER_ALBUM = 6
    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-scale-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
    libdir = tmp / 'library'
    libdir.mkdir()
    dbpath = beetsdir / 'library.db'
    (beetsdir / 'config.yaml').write_text(
        f'directory: {libdir}\n'
        f'library: {dbpath}\n'
        'import:\n'
        '    write: no\n'
    )

    from beets.library import Library
    lib = Library(str(dbpath))
    con = lib._connection()
    acols = [r[1] for r in con.execute('PRAGMA table_info(albums)')]
    icols = [r[1] for r in con.execute('PRAGMA table_info(items)')]
    now = time.time()
    album_dicts, item_dicts = [], []
    for i in range(N_ALBUMS):
        artist = f'Scale Artist {i % 50}'
        album = f'Scale Album {i:04d}'
        a = {c: '' for c in acols}
        a.update(id=i + 1, added=now, album=album, albumartist=artist,
                albumartist_sort=artist, year=2000 + (i % 20), comp=0,
                disctotal=1, month=0, day=0, original_year=0,
                original_month=0, original_day=0)
        album_dicts.append(a)
        for t in range(1, TRACKS_PER_ALBUM + 1):
            it = {c: '' for c in icols}
            title = f'Scale Track {t:02d} of {album}'
            it.update(id=i * TRACKS_PER_ALBUM + t, album_id=i + 1,
                     path=f'{artist}/{album}/{t:02d} {title}.mp3'.encode(),
                     title=title, artist=artist, album=album,
                     albumartist=artist, year=2000 + (i % 20), format='MP3',
                     bitrate=192000, bitdepth=0, samplerate=44100, channels=2,
                     length=200.0, track=t, tracktotal=TRACKS_PER_ALBUM,
                     disc=1, disctotal=1, added=now, mtime=now, comp=0, bpm=0,
                     rg_track_gain=0, rg_track_peak=0, rg_album_gain=0,
                     rg_album_peak=0, r128_track_gain=0, r128_album_gain=0)
            item_dicts.append(it)
    con.executemany(
        f"INSERT INTO albums ({','.join(acols)}) VALUES ({','.join('?' * len(acols))})",
        [[d[c] for c in acols] for d in album_dicts])
    con.executemany(
        f"INSERT INTO items ({','.join(icols)}) VALUES ({','.join('?' * len(icols))})",
        [[d[c] for c in icols] for d in item_dicts])
    con.commit()
    lib._connection().close() if hasattr(lib, '_connection') else None

    proc = start_server(beetsdir)
    try:
        checks = [
            ('/library?limit=200', 'albums'),
            ('/library?q=Scale&limit=200', 'albums'),
            ('/library/tracks?limit=200', 'tracks'),
            ('/library/tracks?q=Scale+Track+05&limit=200', 'tracks'),
            ('/library/artists?limit=200', 'artists'),
        ]
        for path, key in checks:
            t0 = time.monotonic()
            with urllib.request.urlopen(f'{BASE}{path}', timeout=30) as r:
                body = json.load(r)
            dt = time.monotonic() - t0
            assert body['ok'], (path, body)
            # Generous ceiling: this is a correctness/hang guard, not a perf
            # assertion — see the #12 findings for actual measured numbers
            # at the real 5,000-album acceptance scale.
            assert dt < 10, f'{path} took {dt:.2f}s against {N_ALBUMS} albums'

        # /stream and /art: single-row PK lookups, id-only addressing. No
        # real file on disk here, so 404 is the correct answer — this is
        # checking the DB-lookup path responds promptly, not serving audio.
        t0 = time.monotonic()
        with urllib.request.urlopen(f'{BASE}/stream/1', timeout=10) as r:
            pass
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'scale endpoint check ({N_ALBUMS} albums / '
          f'{N_ALBUMS * TRACKS_PER_ALBUM} items): ok')


def test_kill_minus_9_mid_decision_recovery():
    """#12 scope 5: SIGKILL the server while a decision is blocked, restart
    against the same BEETSDIR, and confirm: state.pickle resume still works
    (should_resume asks), nothing was half-imported, and /jobs/current
    correctly reports no running job on the fresh process. (The issue text
    names this endpoint /import/current; the actual route — shared across
    import/convert/artwork/sync jobs — is /jobs/current, see server.py.)

    This goes further than the existing graceful-abort resume test (see
    main(), bullet 7): terminate()/wait() there gives the process a chance
    to run its finally-blocks, SIGKILL here gives it none.
    """
    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-kill9-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
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
    make_album(src / 'a', 'Kill Artist', 'Kill Album A', ['One'])
    make_album(src / 'b', 'Kill Artist', 'Kill Album B', ['One'])

    proc = start_server(beetsdir)
    try:
        stream = run(src)
        reached_decision = False
        for event in stream:
            if event['type'] == 'decision':
                reached_decision = True
                # Kill -9 right here: a decision is blocked mid-task, the
                # importer thread is parked in ImportJob.ask(), and beets'
                # state.pickle write for a *finished* album (if any ran
                # before this one) has already landed or not — either way,
                # nothing gets a chance to clean up.
                proc.kill()
                proc.wait(timeout=10)
                break
        assert reached_decision, 'never reached a decision to kill against'
    finally:
        pass  # proc is already dead; stop_server would hang polling /status

    for _ in range(50):
        try:
            urllib.request.urlopen(f'{BASE}/status', timeout=1).read()
            time.sleep(0.2)
        except Exception:
            break

    # Restart against the same BEETSDIR.
    proc = start_server(beetsdir)
    try:
        with urllib.request.urlopen(f'{BASE}/jobs/current', timeout=5) as r:
            cur = json.load(r)
        assert cur['ok'] and cur.get('id') is None, \
            f'a fresh process must report no running job after a kill -9: {cur}'

        before = albums_in(beetsdir)
        assert ('Kill Artist', 'Kill Album A') not in before or \
               ('Kill Artist', 'Kill Album B') not in before, \
            'both albums landing means nothing was actually interrupted'

        # Resume: the killed run left state.pickle with progress (if any
        # album finished before the kill) or nothing at all — either way
        # this must not crash, and re-importing must not duplicate an
        # album that already finished.
        resumed_kinds = []
        for event in run(src):
            if event['type'] != 'decision':
                continue
            resumed_kinds.append(event['kind'])
            if event['kind'] == 'resume':
                decide(event, choice='resume')
            else:
                decide(event, choice='asis')
        after = albums_in(beetsdir)
        assert ('Kill Artist', 'Kill Album A') in after
        assert ('Kill Artist', 'Kill Album B') in after
        # No duplicate rows for either album.
        assert after.count(('Kill Artist', 'Kill Album A')) == 1, after
        assert after.count(('Kill Artist', 'Kill Album B')) == 1, after
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    print('kill -9 mid-decision recovery: ok')


if __name__ == '__main__':
    main()
    test_quality_rank_real_encoders()
    test_unicode_and_edge_case_metadata()
    test_shell_metacharacter_and_space_paths()
    test_missing_ffmpeg_dependency()
    test_scale_library_endpoints()
    test_kill_minus_9_mid_decision_recovery()
    print('all #12 stress/correctness checks passed')
