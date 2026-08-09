#!/usr/bin/env python3
"""
End-to-end check for the rebuilt USB Mirror feature (#43).

USB Mirror is "point transcode.py's existing convert job at a dest outside
the library, scoped to a playlist" — so this proves the actual parity
checklist from the issue rather than re-testing transcode.py itself
(already covered by test_transcode.py): a custom format (mp3_320/LAME, not
a convert-plugin builtin), playlist-scoped convert, dry-run writing
nothing, and a real run being incremental (unchanged on rerun).

Playlist scoping does NOT use beets' own `playlist` plugin — verified
during implementation that PlaylistQuery never normalizes for the beets
2.11 relative-path migration (upstream gap), so `playlist:name` matches
zero items against a library that has directory: set (the normal case).
/playlists/resolve (libops.playlist_item_ids) reads the .m3u itself and
resolves it via PathQuery instead, which does normalize.

Run: ~/.local/pipx/venvs/beets/bin/python test_usb_mirror.py
Needs ffmpeg (with a libmp3lame encoder) to make and convert test audio.
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

import test_importsession as ti

FMT = 'mp3_320'


def main():
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg needed to generate test audio — skipping')

    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-usbmirror-test-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
    plugindir = beetsdir / 'plugins'
    plugindir.mkdir()
    (plugindir / 'stubsource.py').write_text(ti.STUB_PLUGIN)
    dest_dir = tmp / 'usb'
    playlist_dir = tmp / 'playlists'
    playlist_dir.mkdir()
    (beetsdir / 'config.yaml').write_text(
        f'directory: {tmp / "library"}\n'
        f'library: {beetsdir / "library.db"}\n'
        f'pluginpath: [{plugindir}]\n'
        f'plugins: [stubsource, convert]\n'
        'import:\n'
        '    resume: ask\n'
        '    copy: yes\n'
        '    write: no\n'
        'convert:\n'
        f'    dest: {dest_dir}\n'
        '    formats:\n'
        '        mp3_320:\n'
        '            command: ffmpeg -i $source -y -vn -c:a libmp3lame -b:a 320k $dest\n'
        '            extension: mp3\n'
    )

    src = tmp / 'incoming'
    ti.make_album(src / 'one', 'Mirror Artist', 'Mirror Album', ['Alpha', 'Beta'])
    ti.make_album(src / 'two', 'Other Artist', 'Other Album', ['Gamma'])

    ti.job[0] = None
    proc = ti.start_server(beetsdir)
    try:
        for path in (src / 'one', src / 'two'):
            for event in ti.run(path):
                if event['type'] == 'decision':
                    ti.decide(event, choice='asis')

        con = sqlite3.connect(beetsdir / 'library.db')
        rows = con.execute(
            "SELECT path FROM items WHERE artist = 'Mirror Artist'").fetchall()
        con.close()
        assert len(rows) == 2, f'expected 2 imported tracks, got {rows}'

        # Playlist scoping: only "Mirror Artist"'s tracks, library-relative
        # paths (beets >= 2.11 stores paths under `directory:` that way —
        # see server.py's resolve_item_path docstring) — the same shape
        # beets' own importfeeds plugin writes.
        rel_paths = sorted(p[0].decode() for p in rows)
        (playlist_dir / 'mirror.m3u').write_text('\n'.join(rel_paths) + '\n')

        status, body = ti.post('/playlists/resolve', {
            'dir': str(playlist_dir), 'names': ['mirror']})
        assert status == 200 and body['ok'], body
        assert len(body['ids']) == 2, f'expected 2 resolved ids: {body}'
        scope = {'scope': {'ids': body['ids']}}

        # Dry run: reports what would convert, writes nothing.
        status, body = ti.post('/convert/start', {
            'format': FMT, 'dest': str(dest_dir), 'pretend': True, **scope})
        assert status == 200 and body['ok'], body
        tail = list(ti.events(body['id']))
        done = tail[-1]
        assert done['type'] == 'done' and not done.get('aborted'), tail
        assert done.get('sent') == 2, f'dry run should preview 2 tracks: {done}'
        assert not list(dest_dir.rglob('*.mp3')), 'dry run wrote files'

        # Real run: converts only the scoped playlist's 2 tracks, not all 3.
        status, body = ti.post('/convert/start', {
            'format': FMT, 'dest': str(dest_dir), 'pretend': False, **scope})
        assert status == 200 and body['ok'], body
        tail = list(ti.events(body['id']))
        done = tail[-1]
        assert done['type'] == 'done' and not done.get('aborted'), tail
        converted = list(dest_dir.rglob('*.mp3'))
        assert len(converted) == 2, f'expected 2 converted files, got {converted}'
        assert all(f.stat().st_size > 0 for f in converted)
        mtimes = {f: f.stat().st_mtime_ns for f in converted}

        # Incremental: rerunning the same scope re-sends the same tracks to
        # the converter (transcode.py can't see past that — see its
        # docstring) but the convert plugin itself must skip re-encoding
        # ones whose target already exists, so the files are untouched.
        status, body = ti.post('/convert/start', {
            'format': FMT, 'dest': str(dest_dir), 'pretend': False, **scope})
        assert status == 200 and body['ok'], body
        tail = list(ti.events(body['id']))
        assert tail[-1]['type'] == 'done' and not tail[-1].get('aborted'), tail
        assert len(list(dest_dir.rglob('*.mp3'))) == 2, 'rerun added/removed files'
        for f, mtime in mtimes.items():
            assert f.stat().st_mtime_ns == mtime, f'{f} was re-encoded on rerun'

        print('ok')
    finally:
        ti.stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
