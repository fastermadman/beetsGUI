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


def _make_unimported_album(tmp):
    """A real audio file on disk that never went through import — /unimported
    (the real endpoint) needs something genuine to find. Deliberately not
    _import_one_album's helper: that one lands in the library, which is
    exactly what must NOT show up here."""
    folder = tmp / 'incoming' / 'unscanned'
    make_album(folder, 'Scan Test Artist', 'Scan Test Album', ['Scan Test Track'])
    return folder


def main():
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg needed to generate test audio — skipping')

    tmp, beetsdir = _setup()
    proc = start_server(beetsdir)
    errors = []

    try:
        _import_one_album(tmp)
        unimported_dir = _make_unimported_album(tmp)

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

            # Import decision panel: confidence-driven disclosure (#99).
            # Driving a real 'strong'/'none'-recommendation match through
            # the stub metadata source isn't worth the fixture complexity —
            # decisionConfidence()/candidateCard()/candidateSummary() are
            # pure functions of the decision payload, so exercise the real
            # DOM/JS the same way #96's own verification did: feed
            # renderDecision() a synthetic payload and read the rendered
            # DOM back. Still on the Inbox tab from the loop above.
            disclosure = page.evaluate('''() => {
                const out = {};
                const realFetch = window.fetch;
                window.fetch = () => Promise.resolve({json:()=>Promise.resolve({ok:true})});

                const confident = {
                    decision_id:'t1', kind:'album', item_count:10,
                    current:{artist:'A', album:'B'}, recommendation:'strong',
                    candidates:[
                        {similarity:99, artist:'A', title:'B', year:2000, tracks:[]},
                        {similarity:60, artist:'A', title:'B2', year:2001, tracks:[]},
                    ],
                };
                importState.decision = confident; importState.selected = 0; importState.expanded = false;
                renderDecision();
                out.confidentCollapsed = document.querySelectorAll('.candidate-list').length === 0;
                out.confidentBadge = (document.querySelector('.rec-badge') || {}).textContent;

                // number key still reaches a candidate hidden by the collapse
                window.__calls = [];
                window.fetch = (url, opts) => {
                    window.__calls.push(JSON.parse(opts.body));
                    return Promise.resolve({json:()=>Promise.resolve({ok:true})});
                };
                document.dispatchEvent(new KeyboardEvent('keydown', {key:'2', bubbles:true}));
                out.numberKeyAppliesHiddenCandidate = window.__calls.length === 1
                    && window.__calls[0].candidate === 1;

                // M expands the collapsed card into the full list
                importState.decision = confident; importState.selected = 0; importState.expanded = false;
                renderDecision();
                document.dispatchEvent(new KeyboardEvent('keydown', {key:'m', bubbles:true}));
                out.expandedAfterM = document.querySelectorAll('.candidate-card').length === 2;

                const ambiguous = {
                    decision_id:'t2', kind:'album', item_count:11,
                    current:{artist:'2Cellos', album:'Score'}, recommendation:'none',
                    candidates:[
                        {similarity:53.6, artist:'2Cellos', title:'Score', year:2017,
                         label:'Sony Masterworks', country:'US', media:'CD', tracks:[]},
                        {similarity:53.4, artist:'2Cellos', title:'Score', year:2017,
                         label:'Sony Classical', country:'EU', media:'Digital Media', tracks:[]},
                    ],
                };
                importState.decision = ambiguous; importState.selected = 0; importState.expanded = false;
                renderDecision();
                out.ambiguousShowsBoth = document.querySelectorAll('.candidate-card').length === 2;
                out.ambiguousHighlightsDecider = Array.from(document.querySelectorAll('.candidate-decider'))
                    .some(el => el.textContent.includes('Sony Masterworks'));

                const weak = {
                    decision_id:'t3', kind:'album', item_count:8,
                    current:{artist:'X', album:'Y'}, recommendation:'low',
                    candidates:[{similarity:22, artist:'Z', title:'W', year:2005, tracks:[]}],
                };
                importState.decision = weak; importState.selected = 0; importState.expanded = false;
                renderDecision();
                out.weakUnchanged = document.querySelectorAll('.candidate-list').length === 1
                    && document.querySelector('.rec-badge') === null;

                importState.decision = null; renderDecision();
                window.fetch = realFetch;
                return out;
            }''')
            assert disclosure['confidentCollapsed'], disclosure
            assert disclosure['confidentBadge'] == 'Strong match', disclosure
            assert disclosure['numberKeyAppliesHiddenCandidate'], disclosure
            assert disclosure['expandedAfterM'], disclosure
            assert disclosure['ambiguousShowsBoth'], disclosure
            assert disclosure['ambiguousHighlightsDecider'], disclosure
            assert disclosure['weakUnchanged'], disclosure
            assert not errors, f'console errors during import decision panel test: {errors}'

            # Scan results render inline in #scan-section, not the docked
            # activity panel (#100) — real /unimported call against a real
            # file on disk, end to end through Import ->.
            page.locator('#scan-path').fill(str(unimported_dir))
            page.get_by_role('button', name='Scan for unimported music').click()
            page.wait_for_timeout(500)
            scan_results = page.locator('#scan-unimported-results')
            # #105: the row shows the leaf folder name (the identifying part),
            # not the full absolute path, which truncated away with no way to
            # recover it. Full path still lives in the row's title attribute.
            assert scan_results.get_by_text(unimported_dir.name).first.is_visible(), \
                'scan result did not render the leaf folder name inside #scan-section'
            assert scan_results.locator('.lib-row').first.get_attribute('title') == str(unimported_dir), \
                'scan result row is missing the full path as a title attribute'
            assert page.locator('#job-queue-panel').is_hidden(), \
                'scan result leaked into the docked activity panel instead of staying in #scan-section'

            scan_results.get_by_role('button', name='Import →').click()
            page.wait_for_timeout(200)
            assert page.locator('#import-path').input_value() == str(unimported_dir), \
                'Import -> did not fill Music path with the scanned folder'
            assert not errors, f'console errors during scan/import handoff: {errors}'

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

            # Track table view (#93): real ID3 fields as real table columns,
            # not just present somewhere in a row's text — and a column the
            # user picks via the Columns panel actually shows up too.
            page.locator('input[name="lib-mode"][value="tracks"]').click()
            page.wait_for_timeout(300)
            table = page.locator('#lib-results table.lib-table')
            assert table.is_visible(), 'tracks mode did not render a table'
            # .lib-table th is styled text-transform:uppercase — Chromium's
            # inner_text() reflects that rendering, not the literal DOM text.
            headers = [h.upper() for h in table.locator('thead th').all_inner_texts()]
            assert any('BPM' in h for h in headers), headers
            assert any('KEY' in h for h in headers), headers
            keyed_row = table.locator('tbody tr', has_text='Keyed')
            assert keyed_row.is_visible(), 'the BPM/key test track is not in the table'
            row_text = keyed_row.inner_text()
            assert '126' in row_text, f'BPM column did not render the real value:\n{row_text}'
            assert '8a' in row_text.lower(), f'Key column did not render the real value:\n{row_text}'

            # A column the user didn't have on: pick "Label" from the panel,
            # confirm the header actually appears (not just saved to
            # localStorage without a re-render).
            page.locator('button', has_text='Columns…').click()
            page.wait_for_timeout(150)
            page.locator('#lib-columns-panel').get_by_role('checkbox', name='Label', exact=True).check()
            page.wait_for_timeout(300)
            headers = [h.upper() for h in table.locator('thead th').all_inner_texts()]
            assert any('LABEL' in h for h in headers), \
                f'checking a column in the panel did not add it to the table: {headers}'
            assert not errors, f'console errors in the track table view: {errors}'
            # Close the panel (outside click) before it can intercept the
            # header click below — it's an absolutely-positioned overlay.
            page.locator('#lib-count').click()
            page.wait_for_timeout(150)

            # Clicking a column header sorts by it, through the same
            # lib-sort/lib-dir mechanism the Sort-by field uses — proven by
            # the field actually changing, not just "no error was thrown".
            table.locator('thead th', has_text='BPM').click()
            page.wait_for_timeout(300)
            assert page.locator('#lib-sort').input_value() == 'bpm', \
                'clicking the BPM column header did not set it as the sort field'
            assert not errors, f'console errors after sorting by a column header: {errors}'

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
          'track table columns/sort work, export writes real fields, '
          'same-origin POST passes the CSRF guard - ok')


def test_connection_lost():
    """#97: reproduces the reported incident directly — kill the server
    while a real import is mid-stream and confirm the browser shows a
    lost-connection indicator instead of silently doing nothing forever.
    Its own server/browser lifecycle, run after main()'s: main() has a
    real HTTP call after browser.close() that needs its server alive,
    so the server that dies here can't be the one main() also uses.
    """
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg needed to generate test audio — skipping')

    tmp, beetsdir = _setup()
    proc = start_server(beetsdir)
    try:
        src = tmp / 'incoming' / 'pending'
        make_album(src, 'Pending Artist', 'Pending Album', ['One'])

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(BASE, wait_until='networkidle')

            job_id = page.evaluate('''async () => {
                const r = await fetch('/import/start', {method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({path: ''' + json.dumps(str(src)) + ''', mode: 'interactive'})
                });
                const data = await r.json();
                return data.id;
            }''')
            assert job_id, 'import did not start'

            page.evaluate('attachImport(' + json.dumps(job_id) + ')')
            page.wait_for_function('importState.decision !== null', timeout=15000)

            proc.kill()
            proc.wait(timeout=10)

            # A killed process (no replacement listening on the port) is
            # ECONNREFUSED, which WHATWG's EventSource spec treats as a
            # network error, not a fatal one — the browser just keeps
            # silently retrying (readyState stays CONNECTING, never
            # CLOSED), so onerror's fast path never fires here. The stall
            # timer (IMPORT_STALL_MS, 45s) is the one actually catching
            # this case, hence the generous timeout.
            page.wait_for_selector('#import-connection-lost', state='visible', timeout=60000)

            browser.close()
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)

    print('connection-lost indicator: appears after the server dies mid-import - ok')


if __name__ == '__main__':
    main()
    test_connection_lost()
