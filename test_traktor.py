"""
Checks for the Traktor recovery merge (#42). Run: python test_traktor.py

No beets, no Flask, no network — everything here works on NML written into a
temp dir, so the rules that decide what the owner goes and re-buys are
checkable in isolation.
"""
import os
import shutil
import tempfile

import traktor


def write_nml(path, entries, playlists=(), history=False):
    """Minimal but structurally real NML — same element/attribute names the
    owner's own files use."""
    rows = []
    for e in entries:
        info = []
        if e.get('play') is not None:
            info.append(f'PLAYCOUNT="{e["play"]}"')
        if e.get('last'):
            info.append(f'LAST_PLAYED="{e["last"]}"')
        if e.get('rank') is not None:
            info.append(f'RANKING="{e["rank"]}"')
        artist = f' ARTIST="{e["artist"]}"' if e.get('artist') else ''
        rows.append(
            f'<ENTRY{artist} TITLE="{e["title"]}">'
            f'<LOCATION DIR="/:m/:" FILE="{e.get("file", e["title"] + ".mp3")}" '
            f'VOLUME="V"/>'
            f'<INFO {" ".join(info)}/></ENTRY>')
    nodes = []
    for name, files in playlists:
        pk = ''.join(
            f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY="V/:m/:{f}"/></ENTRY>'
            for f in files)
        nodes.append(
            f'<NODE TYPE="PLAYLIST" NAME="{name}">'
            f'<PLAYLIST ENTRIES="{len(files)}" '
            f'TYPE="{"PROTOCOL" if history else "LIST"}">{pk}</PLAYLIST>'
            f'</NODE>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<NML VERSION="19"><HEAD COMPANY="x" PROGRAM="Traktor"/>'
            f'<COLLECTION ENTRIES="{len(entries)}">{"".join(rows)}</COLLECTION>'
            f'<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES '
            f'COUNT="{len(nodes)}">{"".join(nodes)}</SUBNODES></NODE>'
            '</PLAYLISTS></NML>')


def test_normalisation():
    # Prefix shapes taken from the owner's real titles.
    assert traktor.norm_title('6a - 105 - De Cierto Desierto') == 'de cierto desierto'
    assert traktor.norm_title('100 bpm - Phantom') == 'phantom'
    assert traktor.norm_title('01 Inner Divinity') == 'inner divinity'
    assert traktor.norm_title('Dunya (Original Mix) [Deep Bali]') == 'dunya'
    # A single-digit leading number is part of the title, not a track number.
    assert traktor.norm_title('7 Nation Army') == '7 nation army'
    # A named remix is a different record and must not be folded away.
    assert traktor.norm_title('Natur (Satori Remix)') == 'natur satori remix'
    # Danish/German accents must not split a track from its beets counterpart.
    assert traktor.norm_title('Siebdrück') == traktor.norm_title('Siebdruck')


def test_track_key_tiers():
    known, is_known = traktor.track_key('Meg', 'Dunya')
    assert is_known and known.startswith('A\x00')
    unknown, is_known2 = traktor.track_key('', 'Dirty Relapse')
    assert not is_known2 and unknown.startswith('T\x00')
    assert known != unknown


def test_unpadded_dates_compare_correctly():
    # '2018/9/9' > '2018/10/13' as strings; the real order is the opposite.
    assert traktor._date_sort_key('2018/10/13') > traktor._date_sort_key('2018/9/9')


def test_max_playcount_across_snapshots():
    """The core rule: an older snapshot holding a higher count must win.

    This is the case a 'newest snapshot wins' merge would lose, and the one
    the owner's three collection losses make likely.
    """
    tmp = tempfile.mkdtemp()
    try:
        old = os.path.join(tmp, 'Traktor 2.11.3', 'Backup', 'Collection')
        new = os.path.join(tmp, 'Traktor 3.9.0', 'Backup', 'Collection')
        os.makedirs(old)
        os.makedirs(new)
        write_nml(os.path.join(old, 'collection_2018y11m07d_02h35m35s.nml'),
                  [{'artist': 'Mom', 'title': 'Black Sunrise', 'play': 35,
                    'last': '2018/9/9', 'rank': 255}])
        # Same track, rebuilt collection, play history gone.
        write_nml(os.path.join(new, 'collection_2023y08m25d_14h48m17s.nml'),
                  [{'artist': 'Mom', 'title': 'Black Sunrise', 'play': 0,
                    'last': None, 'rank': 0}])
        state = traktor.scan([tmp])
        assert len(state['tracks']) == 1, state['tracks']
        track = next(iter(state['tracks'].values()))
        assert track['play_count'] == 35, track
        assert track['rating'] == 255
        assert track['snapshots'] == 2
        assert len(track['eras']) == 2, 'each Traktor root is its own era'
        assert '2018y11m07d' in track['play_count_source']
    finally:
        shutil.rmtree(tmp)


def test_title_only_row_merges_into_full_row():
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, 'Traktor 3.9.0'))
        write_nml(os.path.join(tmp, 'Traktor 3.9.0', 'collection.nml'), [
            {'artist': 'Discoshaman', 'title': 'Making A Cyborg Edit',
             'play': 2, 'file': 'a.mp3'},
            # Same record, artist folded into the title by Traktor.
            {'artist': '', 'title': 'Discoshaman - Making A Cyborg Edit',
             'play': 9, 'file': 'b.mp3'},
            # Genuinely artist-less and unrecoverable — must stay separate.
            {'artist': '', 'title': 'Dirty Relapse', 'play': 4, 'file': 'c.mp3'},
        ])
        state = traktor.scan([tmp])
        by_title = {t['title']: t for t in state['tracks'].values()}
        assert len(state['tracks']) == 2, by_title
        merged = by_title['Making A Cyborg Edit']
        assert merged['artist_known'] and merged['play_count'] == 9
        assert not by_title['Dirty Relapse']['artist_known']
    finally:
        shutil.rmtree(tmp)


def test_playlist_and_history_membership():
    tmp = tempfile.mkdtemp()
    try:
        root = os.path.join(tmp, 'Traktor 3.9.0')
        os.makedirs(os.path.join(root, 'History'))
        write_nml(os.path.join(root, 'collection.nml'),
                  [{'artist': 'Meg', 'title': 'Dunya', 'play': 1,
                    'file': 'dunya.mp3'}],
                  playlists=[('all23', ['dunya.mp3']),
                             ('gig', ['dunya.mp3']),
                             ('ghost', ['missing.mp3'])])
        write_nml(os.path.join(root, 'History', 'history_2023y08m27d.nml'),
                  [{'artist': '', 'title': 'Meg - Dunya', 'play': 1}],
                  playlists=[('HISTORY', ['dunya.mp3'])], history=True)
        # An .m3u carries paths only, so it must resolve via the basename index.
        with open(os.path.join(tmp, 'set.m3u'), 'w', encoding='utf-8') as f:
            f.write('# comment\n/Music/whatever/dunya.mp3\n')

        state = traktor.scan([tmp])
        track = next(t for t in state['tracks'].values()
                     if t['title'] == 'Dunya')
        assert track['playlists'] == {'all23', 'gig', 'set.m3u (m3u)'}, track['playlists']
        assert track['histories'] == {'history_2023y08m27d.nml'}, track['histories']
        # A history file is one gig, not a census — it must not inflate this.
        assert track['snapshots'] == 1, track['snapshots']
        # The reference to a file no snapshot knows is reported, not guessed.
        assert state['stats']['unresolved_path_refs'] >= 1
    finally:
        shutil.rmtree(tmp)


def test_ambiguous_basename_resolves_to_nothing():
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, 'Traktor 3.9.0'))
        write_nml(os.path.join(tmp, 'Traktor 3.9.0', 'collection.nml'), [
            {'artist': 'A', 'title': 'One', 'file': 'dupe.mp3'},
            {'artist': 'B', 'title': 'Two', 'file': 'dupe.mp3'},
        ], playlists=[('mix', ['dupe.mp3'])])
        state = traktor.scan([tmp])
        assert state['basename_index']['dupe.mp3'] is None
        for track in state['tracks'].values():
            assert track['playlists'] == set(), 'must not guess between the two'
        assert state['stats']['unresolved_path_refs'] == 1
    finally:
        shutil.rmtree(tmp)


def test_bad_files_are_reported_never_dropped():
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, 'Traktor 4.5.0'))
        open(os.path.join(tmp, 'Traktor 4.5.0', 'empty.nml'), 'w').close()
        with open(os.path.join(tmp, 'Traktor 4.5.0', 'broken.nml'), 'w') as f:
            f.write('<NML><COLLECTION>truncated')
        write_nml(os.path.join(tmp, 'Traktor 4.5.0', 'collection.nml'),
                  [{'artist': 'A', 'title': 'Fine', 'play': 1}])

        state = traktor.scan([tmp])
        report = {r['name']: r for r in traktor.source_report(state)}
        assert len(report) == 3, report
        assert report['empty.nml']['status'] == 'skipped'
        assert '0 bytes' in report['empty.nml']['error']
        assert report['broken.nml']['status'] == 'failed'
        assert 'malformed XML' in report['broken.nml']['error']
        assert report['collection.nml']['status'] == 'ok'
        assert state['stats']['files_parsed'] == 1
        assert state['stats']['files_skipped'] == 2
    finally:
        shutil.rmtree(tmp)


def test_icloud_placeholder_is_skipped_not_waited_on():
    """A placeholder must never be opened during a scan.

    Reading one blocks until iCloud delivers it, and `os.stat` blocks the
    same way once a download has been requested — which is why there is no
    timeout to test here, only a skip.
    """
    tmp = tempfile.mkdtemp()
    real_is_dataless, real_request = traktor.is_dataless, traktor.request_download
    requested = []
    try:
        os.makedirs(os.path.join(tmp, 'Traktor 3.11.1'))
        evicted = os.path.join(tmp, 'Traktor 3.11.1', 'collection.nml')
        write_nml(evicted, [{'artist': 'Meg', 'title': 'Dunya', 'play': 9}])
        traktor.is_dataless = lambda st: True
        traktor.request_download = lambda p: requested.append(p)

        state = traktor.scan([tmp])
        report = traktor.source_report(state)
        assert report[0]['status'] == 'skipped'
        assert 'not downloaded' in report[0]['error']
        assert requested == [evicted], 'the download must still be started'
        assert state['tracks'] == {}

        # Once iCloud delivers it, the next scan picks it up — the file was
        # never marked done, so incremental mode still has it on the list.
        traktor.is_dataless = real_is_dataless
        state = traktor.scan([tmp], state)
        assert state['stats']['files_parsed'] == 1
        assert next(iter(state['tracks'].values()))['play_count'] == 9
    finally:
        traktor.is_dataless, traktor.request_download = real_is_dataless, real_request
        shutil.rmtree(tmp)


def test_rescan_is_incremental_and_does_not_double_count():
    """Re-running must not inflate 'seen in N snapshots' — that number is how
    the owner judges how much corroboration a play count has."""
    tmp = tempfile.mkdtemp()
    try:
        root = os.path.join(tmp, 'Traktor 3.9.0')
        os.makedirs(root)
        write_nml(os.path.join(root, 'collection.nml'),
                  [{'artist': 'Meg', 'title': 'Dunya', 'play': 3}])
        state = traktor.scan([tmp])
        assert state['stats']['files_parsed'] == 1
        first = next(iter(state['tracks'].values()))['snapshots']

        state = traktor.scan([tmp], state)
        assert state['stats']['files_read_this_run'] == 0, 'unchanged file re-parsed'
        # The headline count describes the whole list, not just this run.
        assert state['stats']['files_parsed'] == 1
        assert next(iter(state['tracks'].values()))['snapshots'] == first

        # Caveats shown to the owner must not reset just because a re-scan
        # happened to read nothing.
        merged_before = state['stats']['title_only_merged']
        assert traktor.scan([tmp], state)['stats']['title_only_merged'] \
            == merged_before

        # A newly discovered source adds on top rather than rebuilding.
        write_nml(os.path.join(root, 'collection_2024y01m01d.nml'),
                  [{'artist': 'Meg', 'title': 'Dunya', 'play': 11}])
        state = traktor.scan([tmp], state)
        track = next(iter(state['tracks'].values()))
        assert state['stats']['files_read_this_run'] == 1, 'only the new file'
        assert state['stats']['files_parsed'] == 2, 'both now in the list'
        assert track['play_count'] == 11 and track['snapshots'] == first + 1
    finally:
        shutil.rmtree(tmp)


def test_library_match_marks_what_is_already_back():
    """The whole point of the list is 'what do I still need', so a track
    already back in beets must be distinguishable — and a title-only row
    must never be presented as a confirmed hit."""
    import sqlite3
    tmp = tempfile.mkdtemp()
    try:
        db = os.path.join(tmp, 'library.db')
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE items (artist TEXT, title TEXT)')
        con.executemany('INSERT INTO items VALUES (?,?)', [
            ('Meg', 'Dunya'),               # exact
            ('Laroz', 'Miombo'),            # differs only by decoration
            ('Someone', 'Dirty Relapse'),   # only the title can match
        ])
        con.commit()
        con.close()

        os.makedirs(os.path.join(tmp, 'Traktor 3.9.0'))
        write_nml(os.path.join(tmp, 'Traktor 3.9.0', 'collection.nml'), [
            {'artist': 'Meg', 'title': 'Dunya', 'play': 5},
            {'artist': 'Laroz', 'title': '05 Miombo (Original Mix)', 'play': 3},
            {'artist': 'Depart', 'title': 'Madman', 'play': 9},
            {'artist': '', 'title': 'Dirty Relapse', 'play': 4},
        ])
        state = traktor.scan([tmp])
        traktor.annotate_library(state['tracks'], db)
        by_title = {t['title']: t for t in state['tracks'].values()}
        assert by_title['Dunya']['library_match'] == 'exact'
        assert by_title['05 Miombo (Original Mix)']['library_match'] == 'exact'
        assert by_title['Madman']['library_match'] is None
        # Artist unknown on our side — a title hit is a lead, not a match.
        assert by_title['Dirty Relapse']['library_match'] == 'title-only'

        shopping = traktor.results(state, missing_only=True)
        assert [t['title'] for t in shopping['tracks']] == ['Madman'], shopping
    finally:
        shutil.rmtree(tmp)


def test_state_round_trip():
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, 'Traktor 3.9.0'))
        write_nml(os.path.join(tmp, 'Traktor 3.9.0', 'collection.nml'),
                  [{'artist': 'Meg', 'title': 'Dunya', 'play': 3}],
                  playlists=[('gig', ['Dunya.mp3'])])
        path = os.path.join(tmp, 'state.json')
        traktor.save_state(traktor.scan([tmp]), path)
        state = traktor.load_state(path)
        track = next(iter(state['tracks'].values()))
        assert isinstance(track['playlists'], set), 'sets must survive JSON'
        assert track['playlists'] == {'gig'}
        # And the reloaded state still skips work it already did.
        assert traktor.scan([tmp], state)['stats']['files_read_this_run'] == 0
    finally:
        shutil.rmtree(tmp)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    print('ok')


if __name__ == '__main__':
    main()
