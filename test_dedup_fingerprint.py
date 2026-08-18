#!/usr/bin/env python3
"""
Unit tests for #90's fingerprint-based duplicate detection.

Three things get exercised without a real beets library, real audio, or a
running server (same style as test_scope.py/test_libops.py):

1. importsession._incoming_fingerprints()/_fingerprint_match() — the actual
   new decision logic — with acoustid/chroma faked out. In particular: a
   fingerprint match must override a title-overlap guess in both
   directions (confirm a same-audio/different-tag duplicate a title check
   would miss; rescue a same-title/different-audio false positive), and
   the *existing* side's fingerprint must only ever be read, never
   computed live (that's fingerprint.py's backfill job, run ahead of time).
2. settings.py's load/save round-trip and default/corrupt-file fallback.
3. server.py's /dedup/settings validation, with settings.py's own
   load/save monkeypatched so nothing here ever resolves a real beets
   config.yaml/library.db (same discipline test_scope.py uses for
   /library/remove etc: an in-process Flask test must never reach the
   real config).

Run: ~/.local/pipx/venvs/beets/bin/python test_dedup_fingerprint.py
"""
import pathlib
import tempfile

import importsession
import server
import settings


class FakeItem:
    def __init__(self, id, title='', fp=None):
        self.id = id
        self.title = title
        self.acoustid_fingerprint = fp

    def __str__(self):
        return f'item {self.id}'


class FakeAcoustid:
    def __init__(self, score):
        self.score = score

    def compare_fingerprints(self, a, b):
        return self.score


class FakeChroma:
    def __init__(self, fps_by_id):
        self.fps_by_id = fps_by_id

    def fingerprint_item(self, log, item, write=False, quiet=False):
        return self.fps_by_id.get(item.id)


def test_fingerprint_match_confirms_even_without_title_overlap():
    """Same audio, different tags — a real duplicate a title-only check
    would miss entirely (#90)."""
    importsession.acoustid = FakeAcoustid(score=0.99)
    importsession.chroma = FakeChroma({1: 'fp-a'})
    incoming = importsession._incoming_fingerprints([FakeItem(1, title='Track One')])
    dup_items = [FakeItem(2, title='Completely Different Name', fp='fp-a')]
    assert importsession._fingerprint_match(incoming, dup_items, 0.95) is True


def test_fingerprint_mismatch_overrides_title_overlap():
    """Same title, different audio — the false positive #90 exists to
    rescue (two different DJ-pool tracks sharing a generic title)."""
    importsession.acoustid = FakeAcoustid(score=0.10)
    importsession.chroma = FakeChroma({1: 'fp-a'})
    incoming = importsession._incoming_fingerprints([FakeItem(1, title='Original Mix')])
    dup_items = [FakeItem(2, title='Original Mix', fp='fp-b')]
    assert importsession._fingerprint_match(incoming, dup_items, 0.95) is False


def test_falls_back_to_none_without_fingerprints():
    """Chroma/pyacoustid unavailable — caller falls back to title overlap
    instead of getting a decisive answer from here."""
    importsession.acoustid = None
    importsession.chroma = None
    incoming = importsession._incoming_fingerprints([FakeItem(1)])
    assert incoming == []
    assert importsession._fingerprint_match(incoming, [FakeItem(2, fp='fp-b')], 0.95) is None


def test_existing_side_fingerprint_never_computed_live():
    """No acoustid_fingerprint stored on the existing item -> unavailable,
    even though the incoming side fingerprinted fine. Confirms this never
    tries to compute one for the existing item itself (fingerprint.py's
    backfill job is the only thing that does that, ahead of time)."""
    importsession.acoustid = FakeAcoustid(score=0.99)
    importsession.chroma = FakeChroma({1: 'fp-a'})
    incoming = importsession._incoming_fingerprints([FakeItem(1)])
    assert importsession._fingerprint_match(incoming, [FakeItem(2, fp=None)], 0.95) is None


def test_threshold_boundary():
    importsession.acoustid = FakeAcoustid(score=0.95)
    importsession.chroma = FakeChroma({1: 'fp-a'})
    incoming = importsession._incoming_fingerprints([FakeItem(1)])
    assert importsession._fingerprint_match(incoming, [FakeItem(2, fp='fp-b')], 0.95) is True
    assert importsession._fingerprint_match(incoming, [FakeItem(2, fp='fp-b')], 0.96) is False


def test_settings_round_trip(tmp_path):
    settings._path = lambda: str(tmp_path / 'beetsgui_settings.json')
    assert settings.load() == settings.DEFAULTS
    saved = settings.save({'dedup_fingerprint_threshold': 0.8})
    assert saved['dedup_fingerprint_threshold'] == 0.8
    assert settings.load()['dedup_fingerprint_threshold'] == 0.8
    # Unknown keys are dropped, not persisted.
    settings.save({'not_a_real_setting': 'x'})
    assert 'not_a_real_setting' not in settings.load()


def test_settings_survives_a_missing_or_corrupt_file(tmp_path):
    settings._path = lambda: str(tmp_path / 'missing.json')
    assert settings.load() == settings.DEFAULTS
    corrupt = tmp_path / 'corrupt.json'
    corrupt.write_text('not json')
    settings._path = lambda: str(corrupt)
    assert settings.load() == settings.DEFAULTS


def _client():
    server.app.testing = True
    # _reject_foreign_host() checks Host against the loopback origin, so the
    # test client has to speak as that origin or every request is a 403.
    server.app.config['SERVER_NAME'] = f'localhost:{server.PORT}'
    return server.app.test_client()


def test_dedup_settings_save_validation():
    """Bad thresholds are a 400 before ever touching settings.py's file
    I/O — proven by making settings.save() explode if reached."""
    client = _client()
    settings.save = lambda values: (_ for _ in ()).throw(
        AssertionError('should not reach settings.save for a bad value'))
    for bad in (None, 'x', 0, -0.1, 1.1, [0.9]):
        r = client.post('/dedup/settings', json={'dedup_fingerprint_threshold': bad})
        assert r.status_code == 400, (bad, r.status_code, r.get_json())
        assert r.get_json()['ok'] is False

    seen = []
    settings.save = lambda values: seen.append(values) or {**settings.DEFAULTS, **values}
    r = client.post('/dedup/settings', json={'dedup_fingerprint_threshold': 0.9})
    assert r.status_code == 200, r.get_json()
    assert seen == [{'dedup_fingerprint_threshold': 0.9}], seen


def main():
    test_fingerprint_match_confirms_even_without_title_overlap()
    test_fingerprint_mismatch_overrides_title_overlap()
    test_falls_back_to_none_without_fingerprints()
    test_existing_side_fingerprint_never_computed_live()
    test_threshold_boundary()

    with tempfile.TemporaryDirectory() as d:
        test_settings_round_trip(pathlib.Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_settings_survives_a_missing_or_corrupt_file(pathlib.Path(d))

    test_dedup_settings_save_validation()
    print('dedup fingerprint: ok')


if __name__ == '__main__':
    main()
