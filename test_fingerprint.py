#!/usr/bin/env python3
"""
Unit test for #90's fingerprint backfill job (fingerprint.py).

fingerprint_item() itself is beets' own, already-tested code (chroma.py) —
what's actually new here is: filtering to items that don't have a
fingerprint yet (so a re-run doesn't recompute 1600 fingerprints it already
has), turning the results into job.result counts, and stopping on abort.
This fakes get_library()/chroma.fingerprint_item() so the check runs
without fpcalc, a real audio file, or a beets library on disk.

Run: ~/.local/pipx/venvs/beets/bin/python test_fingerprint.py
"""
from beetsplug import chroma

import fingerprint
import jobs


class FakeItem:
    def __init__(self, id, fp=None):
        self.id = id
        self.acoustid_fingerprint = fp

    def __str__(self):
        return f'item {self.id}'


class FakeLib:
    def __init__(self, items):
        self._items = items

    def items(self, query):
        return list(self._items)


def test_skips_items_already_fingerprinted():
    """Items with a fingerprint already stored are never touched — that's
    what makes a re-run of this job cheap on a mostly-backfilled library."""
    items = [FakeItem(1, fp=None), FakeItem(2, fp='already-there'), FakeItem(3, fp=None)]
    fingerprint.get_library = lambda: FakeLib(items)

    seen = []
    def fake_fingerprint_item(log, item, write=False, quiet=False):
        seen.append(item.id)
        return 'computed'
    chroma.fingerprint_item = fake_fingerprint_item

    job = jobs.Job('fingerprint', query='')
    fingerprint._run(job)

    assert seen == [1, 3], seen
    assert job.result['total'] == 2, job.result
    assert job.result['fingerprinted'] == 2, job.result


def test_abort_stops_the_loop():
    """job.aborted mid-loop stops before the next item, same contract as
    every other job in this codebase (sync.py, transcode.py)."""
    items = [FakeItem(1), FakeItem(2), FakeItem(3)]
    fingerprint.get_library = lambda: FakeLib(items)

    job = jobs.Job('fingerprint', query='')
    calls = []
    def fake_fingerprint_item(log, item, write=False, quiet=False):
        calls.append(item.id)
        job.aborted.set()   # abort right after the first item is processed
        return 'computed'
    chroma.fingerprint_item = fake_fingerprint_item

    fingerprint._run(job)
    assert calls == [1], calls
    assert job.result['fingerprinted'] == 1, job.result


def test_a_failed_fingerprint_is_not_counted():
    """fingerprint_item() returns None when generation fails (e.g. no
    duration, or fpcalc errors) — that must not count as backfilled."""
    items = [FakeItem(1)]
    fingerprint.get_library = lambda: FakeLib(items)
    chroma.fingerprint_item = lambda log, item, write=False, quiet=False: None

    job = jobs.Job('fingerprint', query='')
    fingerprint._run(job)
    assert job.result['fingerprinted'] == 0, job.result


def main():
    test_skips_items_already_fingerprinted()
    test_abort_stops_the_loop()
    test_a_failed_fingerprint_is_not_counted()
    print('fingerprint backfill: ok')


if __name__ == '__main__':
    main()
