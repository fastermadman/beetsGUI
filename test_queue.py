#!/usr/bin/env python3
"""
Queue-editing test for #71: move, play-next and remove.

Same trick as test_filter.py — the functions live in beetsgui.html, so this
lifts that one block out and runs it in node. Only needs node, not beets.

The thing worth pinning down is `queueIdx`, not the array. Every one of these
functions splices the queue underneath the index that says what is playing,
and the rule is that the index follows its track: reordering or removing
*something else* must never change what comes out of the speakers. Getting
that wrong is silent — the array looks right, and the wrong song plays.

Run: python3 test_queue.py   (needs node)
"""
import json
import subprocess
import tempfile
from pathlib import Path

HTML = Path(__file__).parent / 'beetsgui.html'

# start (queue, idx) → call → expected (queue, idx). 'stopped' means the
# whole queue was cleared, which is what removing the last track must do.
CASES = [
    # Moving something else must not change what is playing.
    ('abc', 0, ['queueMove', 0, 1],  'bac', 1),
    ('abc', 0, ['queueMove', 1, -1], 'bac', 1),
    ('abc', 2, ['queueMove', 0, 1],  'bac', 2),
    # No wrap at either end.
    ('abc', 1, ['queueMove', 0, -1], 'abc', 1),
    ('abc', 1, ['queueMove', 2, 1],  'abc', 1),
    # Play next: the track lands directly after the playing one, from either
    # side of it, and the playing track keeps playing.
    ('abcd', 0, ['queuePlayNext', 3], 'adbc', 0),
    ('abcd', 2, ['queuePlayNext', 0], 'bcad', 1),
    ('abcd', 1, ['queuePlayNext', 1], 'abcd', 1),   # already playing: no-op
    # Removing around the playing track.
    ('abc', 2, ['queueRemove', 0], 'bc', 1),
    ('abc', 0, ['queueRemove', 2], 'ab', 0),
    # Removing the playing track: the next one takes its place, or the last
    # one does if that was the end of the queue.
    ('abc', 1, ['queueRemove', 1], 'ac', 1),
    ('ab',  1, ['queueRemove', 1], 'a',  0),
    ('a',   0, ['queueRemove', 0], '',  -1),
]


def js_block():
    """The queue editing functions, lifted out of the page."""
    src = HTML.read_text()
    start = src.index('function queueMove(')
    end = src.index('function queueMenu(')
    return src[start:end]


def run_js():
    script = '''
let queue=[],queueIdx=-1,played=0;
const renderPlayer=()=>{};
const playCurrent=()=>{played++;};
const stopPlayback=()=>{queue=[];queueIdx=-1;};
''' + js_block() + '''
const out=[];
for(const [start,idx,call] of ''' + json.dumps(
        [[c[0], c[1], c[2]] for c in CASES]) + '''){
  queue=start.split('').map(ch=>({id:ch}));
  queueIdx=idx;
  const [fn,...args]=call;
  ({queueMove,queuePlayNext,queueRemove})[fn](...args);
  out.push([queue.map(t=>t.id).join(''),queueIdx]);
}
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


def main():
    for case, got in zip(CASES, run_js()):
        start, idx, call, want_q, want_i = case
        assert got == [want_q, want_i], (
            f'{call} on {start!r} @ {idx} gave {got[0]!r} @ {got[1]}, '
            f'expected {want_q!r} @ {want_i}')
    print('ok')


if __name__ == '__main__':
    main()
