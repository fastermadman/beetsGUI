#!/usr/bin/env python3
"""
End-to-end check for transcode.py's background convert job (#50).

`ConvertPlugin.convert_item`'s call shape changed completely between
beets 2.11 and 2.13 (positional pipeline args vs. plugin.config-backed
cached_property reads) — this actually runs a conversion end-to-end
rather than just checking transcode.py imports cleanly, since the old
signature would type-error and a wrong-but-importable fix could still
silently write nothing.

Run: python test_transcode.py
Needs ffmpeg (with an mp3 encoder) to make and convert test audio.
"""
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import test_importsession as ti

DEST_FMT = 'mp3'


def main():
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg needed to generate test audio — skipping')

    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-convert-test-'))
    beetsdir = tmp / 'beets'
    beetsdir.mkdir()
    plugindir = beetsdir / 'plugins'
    plugindir.mkdir()
    (plugindir / 'stubsource.py').write_text(ti.STUB_PLUGIN)
    dest_dir = tmp / 'converted'
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
        f'    format: {DEST_FMT}\n'
    )

    src = tmp / 'incoming'
    # FLAC source: DEST_FMT (mp3) differs from it, so should_transcode()
    # is true and convert_item actually encodes rather than just copying.
    ti.make_album(src / 'one', 'Convert Artist', 'Convert Album', ['Alpha'],
                  ext='flac')

    ti.job[0] = None
    proc = ti.start_server(beetsdir)
    try:
        for event in ti.run(src / 'one'):
            if event['type'] == 'decision':
                ti.decide(event, choice='asis')
        con = sqlite3.connect(beetsdir / 'library.db')
        (path,) = con.execute(
            "SELECT path FROM items WHERE artist = 'Convert Artist'").fetchone()
        con.close()
        assert path, 'import did not land the track'

        status, body = ti.post('/convert/start', {
            'query': 'artist:"Convert Artist"', 'pretend': False})
        assert status == 200 and body['ok'], body
        tail = list(ti.events(body['id']))
        assert tail and tail[-1]['type'] == 'done' and not tail[-1].get('aborted'), tail

        converted = list(dest_dir.rglob(f'*.{DEST_FMT}'))
        assert converted, f'no .{DEST_FMT} file landed in {dest_dir}'
        assert converted[0].stat().st_size > 0, 'converted file is empty'
        print('ok')
    finally:
        ti.stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
