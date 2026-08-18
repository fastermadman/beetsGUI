#!/usr/bin/env python3
"""
Tests for #90's retag dry-run preview + apply (retag.py).

_run()/_prime_acoustid() (the actual MusicBrainz/AcoustID network calls)
aren't covered here — same precedent as sync.py, which has no test file
either, for the same reason: it's thin glue over real network-dependent
beets plugin calls, unsuitable for a deterministic offline test. That path
was smoke-tested by hand against real MusicBrainz during development: a
synthetic file tagged artist="Sofa Beets" (a fabricated wrong tag, the
motivating #90 scenario) matched real MusicBrainz candidates, one of which
was applied end-to-end and verified with ffprobe to have actually
rewritten the file.

What's tested here, offline and deterministic:
1. _serialize() — the exact JSON shape beetsgui.html's candidateCard()
   renders, built from real (but network-free, hand-constructed)
   AlbumMatch/TrackMatch objects — the same classes tag_album()/
   tag_item() return.
2. apply()'s actual write path — a real beets Item/Library/audio file,
   with a fake AlbumInfo/TrackInfo standing in for what a real
   MusicBrainz match would carry. Proves apply_metadata() + try_sync()
   actually rewrites the file and the database, including mb_trackid,
   and that a successfully-applied proposal can't be double-applied.
3. apply()'s error paths: unknown job, no matching proposal, an
   out-of-range candidate index, and an item deleted since preview.

Needs ffmpeg (a real, tiny test audio file), same as
test_importsession.py/test_fingerprint.py.

Run: ~/.local/pipx/venvs/beets/bin/python test_retag.py
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from beets import library
from beets.autotag.distance import Distance
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.autotag.match import AlbumMatch, Proposal, Recommendation, TrackMatch
from mediafile import MediaFile

import jobs
import retag


class FakeItem:
    """Stand-in for a beets Item — only the attributes _candidate()/
    _track_row() actually read, for the offline _serialize() tests."""
    def __init__(self, title, artist, length=3.0):
        self.title = title
        self.artist = artist
        self.length = length


def _fake_album_proposal(item):
    tracks = [TrackInfo(title='Fiaker (Driving Home to Hasenearl)', artist='Sofa Surfers',
                        track_id='track-1', index=1)]
    info = AlbumInfo(tracks=tracks, album='Sofa Rockers', artist='Sofa Surfers',
                     album_id='album-1', artist_id='artist-1', data_source='Test',
                     data_url='https://example.com/album-1', label='Klein Records',
                     country='AT', media='CD', albumtype='ep', year=1997)
    match = AlbumMatch(distance=Distance(), info=info, mapping={item: tracks[0]})
    return Proposal(candidates=[match], recommendation=Recommendation.low)


def test_serialize_album():
    """The exact shape candidateCard() (beetsgui.html) reads."""
    item = FakeItem(title='Untitled Track', artist='Sofa Beets')
    proposal = _fake_album_proposal(item)
    payload = retag._serialize('album', 42, 'Sofa Beets', 'Sofa Beets', 1, proposal)

    assert payload['kind'] == 'album'
    assert payload['target_id'] == 42
    assert payload['current'] == {'artist': 'Sofa Beets', 'album': 'Sofa Beets'}
    assert payload['item_count'] == 1
    assert payload['recommendation'] == 'low'
    c = payload['candidates'][0]
    assert c['artist'] == 'Sofa Surfers', c
    assert c['title'] == 'Sofa Rockers', c
    assert c['label'] == 'Klein Records', c
    assert c['data_source'] == 'Test', c
    track = c['tracks'][0]
    assert track['current'] == 'Untitled Track' and track['new'] == 'Fiaker (Driving Home to Hasenearl)', track
    assert track['current_artist'] == 'Sofa Beets' and track['new_artist'] == 'Sofa Surfers', track


def test_serialize_item():
    """Singleton (TrackMatch) shape — same field names as the album case,
    per candidateCard()'s expectations."""
    info = TrackInfo(title='Fiaker (Driving Home to Hasenearl)', artist='Sofa Surfers',
                     track_id='track-1', data_source='Test', data_url='https://example.com/t-1')
    item = FakeItem(title='Untitled', artist='Magician On Duty')
    match = TrackMatch(distance=Distance(), info=info, item=item)
    proposal = Proposal(candidates=[match], recommendation=Recommendation.medium)

    payload = retag._serialize('item', 7, 'Magician On Duty', 'Untitled', 1, proposal)
    assert payload['kind'] == 'item'
    assert payload['current'] == {'artist': 'Magician On Duty', 'title': 'Untitled'}
    assert payload['recommendation'] == 'medium'
    assert payload['candidates'][0]['title'] == 'Fiaker (Driving Home to Hasenearl)'


def _make_library(tmp_dir):
    music = tmp_dir / 'music'
    music.mkdir()
    src = music / 'track.mp3'
    subprocess.run([
        'ffmpeg', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-ar', '44100',
        '-metadata', 'artist=Sofa Beets', '-metadata', 'title=Untitled Track',
        '-metadata', 'album=Sofa Beets', str(src), '-y', '-loglevel', 'error',
    ], check=True)
    lib = library.Library(str(tmp_dir / 'library.db'), str(music))
    item = library.Item.from_path(str(src))
    item.add(lib)
    album = lib.add_album([item])
    return lib, album, item


def test_apply_album_writes_file_and_database():
    if shutil.which('ffmpeg') is None:
        print('ffmpeg not found — skipping test_apply_album_writes_file_and_database')
        return
    with tempfile.TemporaryDirectory() as d:
        lib, album, item = _make_library(Path(d))
        retag.get_library = lambda: lib
        proposal = _fake_album_proposal(item)
        job = retag.RetagJob('')
        job.proposals[('album', album.id)] = proposal.candidates
        jobs.get = lambda job_id: job if job_id == job.id else None

        error = retag.apply(job.id, 'album', album.id, 0)
        assert error is None, error

        mf = MediaFile(item.path)
        assert mf.artist == 'Sofa Surfers', mf.artist
        assert mf.title == 'Fiaker (Driving Home to Hasenearl)', mf.title
        assert mf.album == 'Sofa Rockers', mf.album

        fresh_item = lib.get_item(item.id)
        assert fresh_item.artist == 'Sofa Surfers', fresh_item.artist
        assert fresh_item.mb_trackid == 'track-1', fresh_item.mb_trackid
        fresh_album = lib.get_album(album.id)
        assert fresh_album.albumartist == 'Sofa Surfers', fresh_album.albumartist

        # Consumed — can't be double-applied.
        assert ('album', album.id) not in job.proposals
        assert retag.apply(job.id, 'album', album.id, 0) == \
            'no proposal for that album/item — preview again'


def test_apply_unknown_job():
    jobs.get = lambda job_id: None
    error = retag.apply('does-not-exist', 'album', 1, 0)
    assert error and 'unknown retag job' in error, error


def test_apply_no_proposal_for_target():
    job = retag.RetagJob('')
    jobs.get = lambda job_id: job
    error = retag.apply(job.id, 'album', 999, 0)
    assert error and 'no proposal' in error, error


def test_apply_candidate_index_out_of_range():
    job = retag.RetagJob('')
    job.proposals[('album', 1)] = ['placeholder-match']
    jobs.get = lambda job_id: job
    error = retag.apply(job.id, 'album', 1, 5)
    assert error and 'candidate must be' in error, error


def test_apply_stale_item_is_rejected():
    """An item removed from the library since preview — must not try to
    write through a Match holding a since-deleted Item."""
    class FakeMatch:
        items = [FakeItem('x', 'y')]
        items[0].id = 1
    class FakeLib:
        def get_item(self, item_id):
            return None   # deleted since preview
    job = retag.RetagJob('')
    job.proposals[('album', 1)] = [FakeMatch()]
    jobs.get = lambda job_id: job
    retag.get_library = lambda: FakeLib()

    error = retag.apply(job.id, 'album', 1, 0)
    assert error and 'no longer exist' in error, error


def main():
    test_serialize_album()
    test_serialize_item()
    test_apply_album_writes_file_and_database()
    test_apply_unknown_job()
    test_apply_no_proposal_for_target()
    test_apply_candidate_index_out_of_range()
    test_apply_stale_item_is_rejected()
    print('retag: ok')


if __name__ == '__main__':
    main()
