"""
Cover art fetch/embed as a background job (#15).

Both plugins expose their real work as plain methods — `art_for_album`
/ `_set_art` on fetchart, `art.embed_album` on embedart — so this drives
them per album rather than calling their CLI-level batch helpers. That
buys the two things a browser needs and `beet fetchart` doesn't: a status
event per album, and a cooperative abort checked between albums.

Album-scoped by design: both operations are album-level in beets, and
`/library` is album-level too.
"""
import os

from beets import plugins
from beets.util import syspath
from beetsplug._utils import art

import jobs
from importsession import get_library
from libops import split_query

ACTIONS = ('fetch', 'embed')


def _plugin(name):
    """The already-loaded plugin instance.

    Deliberately not `FetchArtPlugin()` — a fresh instance would re-read
    config into a second, subtly independent source list. get_library()
    has already called plugins.load_plugins(), so the configured instance
    exists by the time this runs.
    """
    for p in plugins.find_plugins():
        if p.name == name:
            return p
    raise RuntimeError(
        f"the '{name}' plugin is not enabled — add it under plugins: in your "
        f"beets config")


def _albums(lib, job):
    albums = list(lib.albums(split_query(job.meta.get('query', ''))))
    if not albums:
        job.emit('status', message='No albums match that query.')
    return albums


def _fetch(job, lib):
    plugin = _plugin('fetchart')
    force = bool(job.meta.get('force'))
    albums = _albums(lib, job)
    found = skipped = 0
    for i, album in enumerate(albums, 1):
        if job.aborted.is_set():
            break
        label = f'{album.albumartist} — {album.album}'
        job.emit('status', message=f'[{i}/{len(albums)}] {label}')
        if (album.artpath and not force
                and os.path.isfile(syspath(album.artpath))):
            skipped += 1
            continue
        # When forcing, skip the local filesystem sources and go to the web —
        # same rule batch_fetch_art applies.
        candidate = plugin.art_for_album(album, None if force else [album.path])
        if candidate:
            plugin._set_art(album, candidate)
            found += 1
    job.result.update(found=found, skipped=skipped, total=len(albums))


def _embed(job, lib):
    plugin = _plugin('embedart')
    cfg = plugin.config
    maxwidth = cfg['maxwidth'].get(int)
    quality = cfg['quality'].get(int)
    compare_threshold = cfg['compare_threshold'].get(int)
    ifempty = cfg['ifempty'].get(bool)
    albums = _albums(lib, job)
    embedded = 0
    for i, album in enumerate(albums, 1):
        if job.aborted.is_set():
            break
        label = f'{album.albumartist} — {album.album}'
        job.emit('status', message=f'[{i}/{len(albums)}] {label}')
        art.embed_album(plugin._log, album, maxwidth, True, compare_threshold,
                        ifempty, quality=quality)
        plugin.remove_artfile(album)   # itself gated on remove_art_file
        embedded += 1
    job.result.update(embedded=embedded, total=len(albums))


def _run(job):
    lib = get_library()
    action = job.meta['action']
    job.emit('status', message=('Fetching cover art' if action == 'fetch'
                                else 'Embedding cover art'))
    (_fetch if action == 'fetch' else _embed)(job, lib)


def start(action, query='', force=False):
    """Start a cover art job. Raises ValueError on bad input, RuntimeError
    if another job is running."""
    if action not in ACTIONS:
        raise ValueError(f'action must be one of {list(ACTIONS)}')
    job = jobs.Job('artwork', action=action, query=query, force=bool(force))
    return jobs.start(job, _run)
