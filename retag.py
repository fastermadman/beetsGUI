"""
Dry-run re-tag preview + apply (#90) — AcoustID/chroma-assisted
MusicBrainz re-matching for items whose *tags* are wrong (a record label
sitting in the artist field is the motivating case — a plain re-fetch
against the existing, wrong tags just fails the same way it did on
import).

Runs the exact matching beets' own importer uses — autotag.tag_album()/
tag_item(), the same AlbumMatch/TrackMatch objects the Import tab's
candidate cards already render (importsession._candidate()/_track_row(),
reused here unchanged) — directly against already-in-library items
instead of files on disk. preview() only proposes and writes nothing;
apply() is a separate, explicit, per-album/item step, gated by the same
"one beets job at a time" rule as every other mutating endpoint
(server.py's _busy_response(), checked before calling apply() here) since
it touches the same process-global Library.

Network-bound like mbsync/bpsync (one MusicBrainz search, and — if
pyacoustid/chroma is installed — one AcoustID lookup, per album/item), so
this runs as a background job with the same "can be slow on a real
library" caveat as those.
"""
from beets import autotag
from beets import logging as beets_logging

import jobs
from importsession import _candidate, get_library
from libops import split_query

try:
    from beetsplug import chroma
except ImportError:
    chroma = None

log = beets_logging.getLogger('beetsgui.retag')


def _prime_acoustid(items):
    """Populate chroma's fingerprint/AcoustID match cache for these items
    (a network AcoustID lookup per item) so tag_album()/tag_item()'s
    metadata-source aggregation picks up AcoustID-derived MusicBrainz
    candidates alongside whatever plain-text search the enabled source
    plugins (musicbrainz, discogs, ...) already do. A no-op if chroma/
    pyacoustid isn't installed — matching then falls back to those other
    sources alone, same as it always has.

    Always looks fingerprints up fresh rather than reusing a stored
    item.acoustid_fingerprint — matching chroma's own import-time
    behavior. ponytail: one AcoustID call per item per preview run, even
    on a re-scan; skip only if that turns out to be a real API-courtesy
    problem on a large library.
    """
    if chroma is None:
        return
    for item in items:
        chroma.acoustid_match(log, item.path)


def _serialize(kind, target_id, cur_artist, cur_name, item_count, proposal):
    return {
        'kind':           kind,
        'target_id':      target_id,
        'current':        ({'artist': cur_artist, 'album': cur_name} if kind == 'album'
                           else {'artist': cur_artist, 'title': cur_name}),
        'item_count':     item_count,
        'recommendation': proposal.recommendation.name,
        'candidates':     [_candidate(m, i) for i, m in enumerate(proposal.candidates or [])],
    }


class RetagJob(jobs.Job):
    """Adds the proposal store to the base job: _run() fills
    self.proposals[(kind, target_id)] with the live AlbumMatch/TrackMatch
    candidates (holding the actual beets Item objects matched against —
    not JSON, never emitted, kept only for apply() to use directly rather
    than re-fetching and risking a mismatch). Cleared implicitly whenever
    another job starts and this one is swept from the registry (jobs.py) —
    apply() reports that plainly rather than silently doing nothing.
    """

    def __init__(self, query):
        super().__init__('retag', query=query)
        self.proposals = {}
        self.result['albums_scanned'] = 0
        self.result['items_scanned'] = 0
        self.result['proposals_found'] = 0


def _run(job):
    lib = get_library()
    query = split_query(job.meta.get('query', ''))

    for album in lib.albums(query):
        if job.aborted.is_set():
            return
        items = album.items()
        _prime_acoustid(items)
        cur_artist, cur_album, proposal = autotag.tag_album(items)
        job.result['albums_scanned'] += 1
        if proposal.candidates:
            job.proposals[('album', album.id)] = proposal.candidates
            job.result['proposals_found'] += 1
            job.emit('proposal', **_serialize(
                'album', album.id, cur_artist, cur_album, len(items), proposal))
        job.emit('status', message=(
            f"{job.result['albums_scanned']} album(s) scanned, "
            f"{job.result['proposals_found']} with a candidate"))

    singletons = [i for i in lib.items(query) if not i.album_id]
    for item in singletons:
        if job.aborted.is_set():
            return
        _prime_acoustid([item])
        proposal = autotag.tag_item(item)
        job.result['items_scanned'] += 1
        if proposal.candidates:
            job.proposals[('item', item.id)] = proposal.candidates
            job.result['proposals_found'] += 1
            job.emit('proposal', **_serialize(
                'item', item.id, item.artist, item.title, 1, proposal))
        job.emit('status', message=(
            f"{job.result['albums_scanned']} album(s), "
            f"{job.result['items_scanned']} singleton(s) scanned, "
            f"{job.result['proposals_found']} with a candidate"))


def start(query=''):
    """Start a retag preview job over the scope query. Raises RuntimeError
    if another job is running."""
    job = RetagJob(query)
    return jobs.start(job, _run)


def apply(job_id, kind, target_id, candidate_index):
    """Apply one previewed candidate: writes tags to the file and updates
    the library. This is the only place a retag preview actually changes
    anything — preview() itself never does. Returns an error string, or
    None on success.

    Applies through the *same* Item objects the preview matched against
    (held on the job, not re-fetched) — apply_metadata() only knows how to
    update the objects it was built from. A light existence check catches
    the case where an item/album was removed since the preview ran.
    """
    job = jobs.get(job_id)
    if job is None or not hasattr(job, 'proposals'):
        return 'unknown retag job — its proposals are gone; preview again'
    candidates = job.proposals.get((kind, target_id))
    if candidates is None:
        return 'no proposal for that album/item — preview again'
    if not 0 <= candidate_index < len(candidates):
        return f'candidate must be 0..{len(candidates) - 1}'
    match = candidates[candidate_index]

    lib = get_library()
    items = match.items if kind == 'album' else [match.item]
    if any(lib.get_item(i.id) is None for i in items):
        return 'one or more items no longer exist — preview again'

    match.apply_metadata()
    # Free synergy with fingerprint.py's backfill (#90): the AcoustID
    # lookup during preview already computed a local fingerprint for
    # these items (chroma._fingerprints, keyed by path) even when no
    # network match came back for the *chosen* candidate — persist it now
    # that the item is being written anyway, instead of leaving it for a
    # separate backfill pass to redo the same fpcalc work.
    if chroma is not None:
        for item in items:
            fp = chroma._fingerprints.get(item.path)
            if fp:
                item.acoustid_fingerprint = fp

    if kind == 'album':
        album = lib.get_album(target_id)
        if album is None:
            return 'album no longer exists — preview again'
        match.apply_album_metadata(album)
        with lib.transaction():
            for item in items:
                item.try_sync(True, False)
            album.store()
    else:
        with lib.transaction():
            items[0].try_sync(True, False)

    del job.proposals[(kind, target_id)]
    return None
