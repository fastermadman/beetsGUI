#!/usr/bin/env python3
"""
Browser smoke test — drives the real UI in a real (headless) browser (#12).

Every other test here calls the HTTP API directly. None of them would have
caught today's two live-verified UI bugs: the Command box producing a
`beet ls -f "..."` string that a shell mangles to nothing, and the BPM+Key
export preset referencing `$key`, a field that doesn't exist, which
silently rendered blank. Both were only visible by actually clicking the
button and reading the file it wrote.

Uses Playwright (`pipx inject beets playwright && playwright install
chromium`) against the same start_server()/stop_server() throwaway-BEETSDIR
harness as test_importsession.py — never the real library.

Run: ~/.local/pipx/venvs/beets/bin/python test_smoke.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from test_importsession import (BASE, STUB_PLUGIN, decide, events, make_album,
                                 post, run, start_server, stop_server)


def _make_track_with_bpm_key(path, artist, album, title, bpm, key):
    """Like test_importsession.make_track, plus real BPM/key tags — needed
    to actually exercise the BPM+Key export preset (#12): mediafile only
    reads FLAC's initial_key from the literal `INITIALKEY` Vorbis comment,
    confirmed by writing then reading one back before trusting this."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'ffmpeg', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono', '-t', '1',
        '-metadata', f'artist={artist}', '-metadata', f'album={album}',
        '-metadata', f'title={title}', '-metadata', 'track=1',
        '-metadata', f'bpm={bpm}', '-metadata', f'INITIALKEY={key}',
        str(path),
    ], check=True)


def _setup():
    tmp = Path(tempfile.mkdtemp(prefix='beetsgui-smoke-'))
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
        'import:\n    resume: ask\n    copy: yes\n    write: no\n')
    return tmp, beetsdir


def _import_one_album(tmp):
    """Populate the library through the real import pipeline (not raw SQL) —
    the smoke test should see what an actual user's library looks like."""
    src = tmp / 'incoming'
    make_album(src / 'one', "Sinéad O'Brien", 'Smoke Test Album', ['Alpha', 'Beta'])
    for event in run(src / 'one'):
        if event['type'] == 'decision':
            decide(event, choice='asis')

    # A second, singleton track with real BPM/key tags — make_album's
    # fixtures never set either, so BPM+Key would render blank whether the
    # preset's field names are right or wrong. This track exists so the
    # export assertions below can tell the difference.
    _make_track_with_bpm_key(src / 'two' / '01 Keyed.flac',
                             'BPM Test Artist', 'Keyed Album', 'Keyed', 126, '8A')
    for event in run(src / 'two', singleton=True):
        if event['type'] == 'decision':
            decide(event, choice='asis')


def main():
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg needed to generate test audio — skipping')

    tmp, beetsdir = _setup()
    proc = start_server(beetsdir)
    errors = []

    try:
        _import_one_album(tmp)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
            page.on('pageerror', lambda e: errors.append(str(e)))

            page.goto(BASE, wait_until='networkidle')
            assert page.locator('text=BeetsGUI').first.is_visible()

            tab_btn = lambda name: page.locator('.tab-btn', has_text=name)

            # Every tab switches and renders without a JS error.
            for tab in ('Library', 'Export', 'Recover', 'Inbox'):
                tab_btn(tab).click()
                page.wait_for_timeout(150)
            assert not errors, f'console errors while switching tabs: {errors}'

            # Library tab shows the album that was actually just imported —
            # end to end: real import -> real library.db -> real /library
            # query -> real DOM.
            tab_btn('Library').click()
            page.wait_for_timeout(300)
            assert page.locator('text=Smoke Test Album').first.is_visible(), \
                'imported album never appeared in the Library tab'

            # Apostrophe search through the real filter UI (#12): type a
            # condition, not just hit the API directly.
            page.get_by_role('button', name='+ Add condition').click()
            page.locator('.f-field').first.fill('artist')
            page.locator('.f-value').first.fill("O'Brien")
            page.wait_for_timeout(400)
            assert page.locator('text=Smoke Test Album').first.is_visible(), \
                "apostrophe filter (artist:O'Brien) found nothing in the UI"
            assert not errors, f'console errors after apostrophe filter: {errors}'

            # Clear the filter row before exporting, so export sees the
            # whole (one-album) library rather than the filtered subset —
            # keeps this test's export-count assertion independent of the
            # filter test above.
            clear_btn = page.locator('#filter-clear')
            if clear_btn.is_visible():
                clear_btn.click()
                page.wait_for_timeout(200)

            # Export via the real preset button and the real Export button,
            # into a throwaway file — typing a format string by hand would
            # miss the actual bug found live in this issue: the "BPM + Key"
            # preset referenced a field ($key) that doesn't exist, and
            # silently rendered blank. Only clicking the button and reading
            # the written file catches that, so this does exactly that.
            dest = tmp / 'export.txt'
            page.locator('#tab-library').get_by_role('button', name='BPM + Key', exact=True).click()
            page.locator('#lib-export-dest').fill(str(dest))
            page.locator('#tab-library').get_by_role('button', name='Export', exact=True).click()
            page.wait_for_timeout(400)
            assert dest.exists(), 'Export button did not write the destination file'
            content = dest.read_text()
            assert 'Alpha' in content and 'Beta' in content, content
            assert 'BPM Test Artist' in content, content
            assert 'BPM: 126' in content, f'BPM field did not render:\n{content}'
            # beets normalises the key string (8A -> 8a); rendering *some*
            # non-empty value is what the field-name bug actually breaks.
            assert 'key: 8a' in content.lower(), (
                f'Key field did not render — the preset likely references a '
                f'field beets does not have:\n{content}')
            assert not errors, f'console errors during export: {errors}'

            # The Origin the browser actually sends must pass the #12 guard
            # — this is what a curl-based check cannot prove.
            result = page.evaluate('''
                fetch('/library/count', {method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({scope:{query:''}})
                }).then(r => r.status)
            ''')
            assert result == 200, f'same-origin POST from the real page got {result}'

            browser.close()

        with urllib.request.urlopen(f'{BASE}/jobs/current', timeout=10) as r:
            cur = json.load(r)
        assert cur['ok'] and cur.get('id') is None, cur
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)

    print('browser smoke test: all tabs render, apostrophe search works, '
          'export writes real fields, same-origin POST passes the CSRF guard - ok')


if __name__ == '__main__':
    main()
