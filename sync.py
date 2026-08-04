"""
mbsync/bpsync as a background job (#21) — resync library metadata from
MusicBrainz/Beatport, driven without the CLI wrapper.

Both plugins expose the real work as two plain methods —
`singletons(lib, query, move, pretend, write)` and
`albums(lib, query, move, pretend, write)` — already split from their
`func()` CLI handler, so no opts/optparse faking needed here (unlike
convert in transcode.py).

ponytail: coarse progress only — one status event before singletons,
one before albums, no per-item events and no abort mid-phase. Both
methods run their own internal `lib.items(query)`/`lib.albums(query)`
loop with no yield point; hooking into it would mean reimplementing the
MusicBrainz/Beatport track-matching logic (release-track-id mapping,
distance scoring) instead of reusing it, which is a worse trade than a
coarser progress bar. Upgrade only if this turns out to be genuinely
painful on a real library — same call made for convert in #16.
"""
from beets import plugins, ui

import jobs
from importsession import get_library
from libops import split_query

KINDS = ('mbsync', 'bpsync')


def _plugin(name):
    for p in plugins.find_plugins():
        if p.name == name:
            return p
    raise RuntimeError(
        f"the '{name}' plugin is not enabled — add it under plugins: in your "
        f"beets config")


def _run(job):
    lib = get_library()
    kind = job.kind
    plugin = _plugin(kind)
    query = split_query(job.meta.get('query', ''))
    move = ui.should_move(None)
    write = ui.should_write(None)

    job.emit('status', message=f'{kind}: singletons')
    plugin.singletons(lib, [*query], move, False, write)
    if job.aborted.is_set():
        return

    job.emit('status', message=f'{kind}: albums')
    plugin.albums(lib, [*query], move, False, write)


def start(kind, query=''):
    """Start an mbsync/bpsync job. Raises ValueError on bad input,
    RuntimeError if another job is running."""
    if kind not in KINDS:
        raise ValueError(f'kind must be one of {list(KINDS)}')
    job = jobs.Job(kind, query=query)
    return jobs.start(job, _run)
