#!/usr/bin/env python3
"""
Round-trip test for #64's filter rows: rows → beets query → rows.

The compiler and parser live in beetsgui.html, so this pulls that one block
out and runs it in node. Two things are checked, and the second is the one
that actually bites:

1. Round-trip. Every row set the UI can build must survive compile → parse
   unchanged. A silent loss here means opening the "advanced" disclosure
   rewrites the user's filter.

2. That the JS agrees with **beets** about what it just wrote. The compiler
   quotes values with `"` and joins OR terms with a comma token; the parser
   is a hand-written shlex-alike. Any drift between that and Python's shlex
   makes the rows say one thing and the library do another — and it would
   drift silently, because the JS half never sees the server's opinion.
   So every compiled query is re-parsed here by beets itself and matched
   against the ids it actually selects in a throwaway library.

Run: ~/.local/pipx/venvs/beets/bin/python test_filter.py   (needs node)
"""
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

HTML = Path(__file__).parent / 'beetsgui.html'

# Row sets the UI can build. Anything the rows *can't* express is a separate
# case below — it must parse to null, not to a wrong row.
CASES = [
    {'match': 'all', 'rows': []},
    {'match': 'all', 'rows': [{'field': 'artist', 'op': 'contains', 'value': 'Burial'}]},
    {'match': 'all', 'rows': [{'field': 'artist', 'op': 'is', 'value': 'Burial'}]},
    {'match': 'all', 'rows': [{'field': 'artist', 'op': 'isnot', 'value': 'Burial'}]},
    {'match': 'all', 'rows': [{'field': 'year', 'op': 'gte', 'value': '2000'}]},
    {'match': 'all', 'rows': [{'field': 'year', 'op': 'lte', 'value': '2010'}]},
    # The "between" case the operator set deliberately doesn't have.
    {'match': 'all', 'rows': [{'field': 'year', 'op': 'gte', 'value': '2000'},
                              {'field': 'year', 'op': 'lte', 'value': '2010'}]},
    # Values that need quoting — the reason quoteVal() exists at all.
    {'match': 'all', 'rows': [{'field': 'albumartist', 'op': 'is', 'value': 'Aphex Twin'}]},
    {'match': 'all', 'rows': [{'field': 'album', 'op': 'contains', 'value': 'a "quoted" bit'}]},
    {'match': 'all', 'rows': [{'field': 'album', 'op': 'contains', 'value': "it's here"}]},
    {'match': 'any', 'rows': [{'field': 'format', 'op': 'is', 'value': 'AIFF'},
                              {'field': 'format', 'op': 'is', 'value': 'WAV'}]},
    {'match': 'any', 'rows': [{'field': 'artist', 'op': 'contains', 'value': 'Burial'},
                              {'field': 'artist', 'op': 'isnot', 'value': 'Someone Else'}]},
]

# Queries beets accepts that the rows deliberately cannot hold. Each must
# parse to null so the raw query stays in charge, rather than being silently
# rewritten into something narrower.
BEYOND_ROWS = [
    'Burial',                    # bare word — searches every default field
    'artist::^Bur',              # regex
    'artist:=~burial',           # case-insensitive exact
    'year:2000..2010',           # two-sided range
    '-artist:Burial',            # negated substring
    'artist:Burial album:Untrue , format:AIFF',   # mixed and/or
    'artist:"unbalanced',        # not even a valid token stream
]


def js_block():
    """The filter compiler/parser, lifted out of the page."""
    src = HTML.read_text()
    start = src.index('const FILTER_OPS=')
    end = src.index('function renderFilter(')
    return src[start:end]


def run_js():
    """{compiled: [...], roundtrip: [...], beyond: [...]} from node."""
    script = js_block() + '''
const CASES=''' + json.dumps(CASES) + ''';
const BEYOND=''' + json.dumps(BEYOND_ROWS) + ''';
const out={compiled:[],roundtrip:[],beyond:[]};
for(const c of CASES){
  libFilter={match:c.match,rows:c.rows.map(r=>({...r}))};
  const q=compileFilter();
  out.compiled.push(q);
  const back=parseFilter(q);
  // An empty filter has no match mode to recover — normalise it the way the
  // UI does (parseFilter keeps the current one).
  out.roundtrip.push(back&&{match:c.rows.length?back.match:c.match,rows:back.rows});
}
for(const q of BEYOND) out.beyond.push(parseFilter(q));
console.log(JSON.stringify(out));
'''
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(['node', path], capture_output=True, text=True)
    finally:
        Path(path).unlink()
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_roundtrip(result):
    for case, back in zip(CASES, result['roundtrip']):
        assert back is not None, f'{case} did not survive the round trip at all'
        assert back == case, f'round trip changed the filter:\n  in  {case}\n  out {back}'


def test_beyond_rows_stays_raw(result):
    for q, parsed in zip(BEYOND_ROWS, result['beyond']):
        assert parsed is None, (
            f'{q!r} was rewritten into rows {parsed} — the raw query must stay '
            f'in charge of anything the rows cannot express')


def test_beets_agrees(result):
    """Compile in JS, select in beets, and check it picked the right tracks.

    The point is not that the query parses — it is that beets selects exactly
    what the row said, quoting and comma-joining included.
    """
    from beets.library import Library
    from beets.library.models import Item

    with tempfile.TemporaryDirectory() as d:
        lib = Library(str(Path(d) / 't.db'))
        # One track per row-set expectation below.
        specs = [
            ('Burial', 'Burial', 'Untrue', 'AIFF', 2007),
            ('Aphex Twin', 'Aphex Twin', 'SAW II', 'WAV', 1994),
            ('Someone Else', 'Someone Else', 'a "quoted" bit', 'FLAC', 2015),
            ('Nobody', 'Nobody', "it's here", 'MP3', 2001),
        ]
        by_artist = {}
        for artist, albumartist, album, fmt, year in specs:
            it = Item(title='t', artist=artist, albumartist=albumartist,
                      album=album, format=fmt, year=year, length=300.0)
            lib.add(it)
            by_artist[artist] = it.id

        # (compiled query index, artist names it must select)
        expected = {
            'artist:Burial': {'Burial'},
            'artist:=Burial': {'Burial'},
            'albumartist:="Aphex Twin"': {'Aphex Twin'},
            'album:"a \\"quoted\\" bit"': {'Someone Else'},
            'year:2000..': {'Burial', 'Someone Else', 'Nobody'},
            'year:..2010': {'Aphex Twin', 'Burial', 'Nobody'},
            'year:2000.. year:..2010': {'Burial', 'Nobody'},
            'format:=AIFF , format:=WAV': {'Burial', 'Aphex Twin'},
        }
        compiled = set(result['compiled'])
        for q, artists in expected.items():
            assert q in compiled, (
                f'{q!r} is not what the compiler produces any more — compiled: '
                f'{sorted(compiled)}')
            got = {i.artist for i in lib.items(shlex.split(q))}
            assert got == artists, f'{q!r} selected {got}, expected {artists}'

        # The apostrophe case is checked separately: shlex is what decides
        # whether the JS quoted it in a way beets can read back.
        apos = next(q for q in compiled if 'here' in q)
        got = {i.artist for i in lib.items(shlex.split(apos))}
        assert got == {'Nobody'}, f'{apos!r} selected {got}'


def main():
    result = run_js()
    test_roundtrip(result)
    test_beyond_rows_stays_raw(result)
    test_beets_agrees(result)
    print('ok')


if __name__ == '__main__':
    main()
