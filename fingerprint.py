"""
Fingerprint backfill (#90) — background job that computes and stores
AcoustID/chromaprint fingerprints for items that don't have one yet.

Local-only: beetsplug.chroma.fingerprint_item() calls
acoustid.fingerprint_file() (chromaprint/fpcalc) directly — no network call,
no AcoustID lookup. That's deliberate: this job exists only to backfill
item.acoustid_fingerprint for items imported before chroma was enabled
(quiet_fallback: asis skips the matching stage entirely, see #90) — a
prerequisite for fingerprint-based duplicate detection in
importsession.py's _decide_duplicate, which reads this field for the
*existing* side of a comparison but never computes it there.

Needs pyacoustid installed (`pipx inject beets pyacoustid`) and the chroma
plugin enabled — same requirement the config-builder wizard already
recommends (beetsgui.html: `chroma:\n  auto: yes`).
"""
from beets import logging as beets_logging

import jobs
from importsession import get_library
from libops import split_query

log = beets_logging.getLogger('beetsgui.fingerprint')


def _run(job):
    from beetsplug import chroma   # raises ImportError -> reported as the
                                    # job's 'error' event if pyacoustid isn't
                                    # installed, same as sync.py's _plugin()
    lib = get_library()
    query = split_query(job.meta.get('query', ''))
    items = [i for i in lib.items(query) if not i.acoustid_fingerprint]
    total = len(items)
    job.result['total'] = total
    job.result['fingerprinted'] = 0
    job.emit('status', message=f'{total} item(s) without a fingerprint')
    for i, item in enumerate(items, 1):
        if job.aborted.is_set():
            return
        if chroma.fingerprint_item(log, item, write=False, quiet=True) is not None:
            job.result['fingerprinted'] += 1
        job.emit('status', message=f'{i}/{total}: {item}')


def start(query=''):
    """Start a fingerprint backfill job over the scope query. Raises
    RuntimeError if another job is running."""
    job = jobs.Job('fingerprint', query=query)
    return jobs.start(job, _run)
