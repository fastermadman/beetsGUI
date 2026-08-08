"""
Recover a most-played / most-curated track list from old Traktor files (#42).

The owner lost their collection three times. What survived is metadata:
generations of Traktor `collection.nml`, per-gig `history_*.nml`, and `.m3u`
playlists. This turns those into a ranked, deduplicated re-acquisition list.

Three things about the real data drive the whole design:

**Play count can only ever be estimated, so it is labelled as an estimate.**
`INFO/@PLAYCOUNT` is real and confirmed. What is *not* documented anywhere by
Native Instruments — not the manual, not the support articles — is whether a
rebuilt collection keeps or resets it. The owner's own files can't settle it
either: the losses contaminate the evidence. So the merge rule is chosen to be
correct under *either* answer: **max across snapshots**. If backups accumulate,
the newest snapshot already holds the max; if a rebuild resets to zero, an
older snapshot still holds the real number. Summing would double-count, because
every backup is a full snapshot of the collection (that part *is* documented),
not a delta. Max can only ever under-report, never inflate — the honest
direction for a shopping list. `assumptions()` states this in the UI.

**A third of all entries have no ARTIST at all.** Measured: 1952/5983 (2018)
and 1543/4634 (2023). 80% of those fold the artist into TITLE, but in mutually
contradictory shapes — `'Discoshaman - Making A Cyborg Edit'` is artist-title
while `"U Can't Slow This - Mc Hammer"` is title-artist, and
`'Sydka - Sydka - Dark Hill Ep - 02 Sydka - Eclipse'` is neither. Guessing a
split would invent wrong artists, and a wrong artist sends the owner hunting
for a track that does not exist — worse than admitting we don't know. So rows
keep two tiers: `artist_known` rows key on (artist, title); the rest key on
title alone and are flagged, never silently presented as equals.

**Playlists and histories reference tracks by path, which doesn't survive a
reinstall.** So they resolve through a basename index built from the same
collection snapshots — the same reason `/unimported` matches on (artist, title)
rather than path. Basenames collide for 24 of 4634 files (0.5%); collisions
resolve to nothing and are counted, never guessed.
"""
import json
import os
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET

import jobs

STATE_VERSION = 1

# Extensions worth opening. `.m3u8` is the same format, UTF-8 by definition.
_SUFFIXES = ('.nml', '.m3u', '.m3u8')


# ── Normalisation ─────────────────────────────────────────────────────────

# Each rule below earns its place from a shape actually present in the
# owner's files; nothing here is speculative tidying.
_PREFIX_RES = (
    # '100 bpm - Arteriam & Wartemal feat. ... - Phantom FINAL PREMASTER'
    re.compile(r'^\d{1,3}\s*bpm\s*-\s*', re.I),
    # '6a - 105 - de cierto desierto' — 180 of these in the 2018 collection,
    # a DJ-prep convention that was later dropped (1 hit in 2023).
    re.compile(r'^\d{1,2}[adm]\s*-\s*\d{2,3}\s*-\s*', re.I),
    # '01 Inner Divinity', '10 Bistro Riots' — leading track number.
    # ponytail: two digits required, so '7 Nation Army' and '2 Bad' keep
    # their real titles. A single-digit track number stays unstripped;
    # that costs a few missed merges and risks no wrong ones.
    re.compile(r'^\d{2}\s+(?=\S)'),
)
# 'Meg - Dunya (Original Mix) [Deep Bali Records]' — trailing label.
_LABEL_RE = re.compile(r'\s*\[[^\]]*\]\s*$')
# A no-op qualifier: '(Original Mix)' names no remixer, so it must not split
# a track from itself. Every *other* parenthetical is kept — '(Satori Remix)'
# is a different record and merging it would be a real error.
_ORIGINAL_RE = re.compile(r'\s*\((?:original(?:\s+mix)?)\)\s*$', re.I)
_NONALNUM_RE = re.compile(r'[^a-z0-9]+')


def _fold(s):
    """Case/accent/punctuation-insensitive form. NFKD because the collection
    is Danish and German — 'siebdrück', 'Brombär', 'luçïd' must match their
    beets counterparts however either side happened to encode them."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def norm_title(title):
    t = _fold(title)
    for _ in range(3):          # prefixes stack: '100 bpm - 6a - 105 - x'
        before = t
        for rx in _PREFIX_RES:
            t = rx.sub('', t)
        t = _LABEL_RE.sub('', t)
        t = _ORIGINAL_RE.sub('', t)
        if t == before:
            break
    return _NONALNUM_RE.sub(' ', t).strip()


def norm_artist(artist):
    return _NONALNUM_RE.sub(' ', _fold(artist)).strip()


def collapse(s):
    """Separator-free form, for linking a title-only row to a full one."""
    return _NONALNUM_RE.sub('', _fold(s))


def track_key(artist, title):
    """Identity for a track. Returns (key, artist_known).

    Two namespaces that can never collide: a row whose artist Traktor
    actually recorded is not the same claim as a row where we only have a
    title, and the UI has to be able to tell them apart.
    """
    nt = norm_title(title)
    na = norm_artist(artist)
    if na and nt:
        return f'A\x00{na}\x00{nt}', True
    return f'T\x00{nt or collapse(title)}', False


# ── iCloud ────────────────────────────────────────────────────────────────

def is_dataless(st):
    """True for an iCloud placeholder: a real size with no blocks on disk."""
    return st.st_blocks == 0 and st.st_size > 0


def request_download(path):
    """Ask iCloud for a placeholder's bytes and return immediately.

    Deliberately fire-and-forget. Waiting here looks obvious and is a trap:
    once a download has been requested, `os.stat` on that file *itself*
    blocks until the fetch completes, so a poll loop with a deadline never
    gets to check its deadline — measured on the owner's files, where six
    of them stalled a scan indefinitely rather than for the intended 20s.
    A blocking read has the same problem.

    So a placeholder is skipped this run and reported as such, with its
    download started in the background. Scans are incremental, so the next
    one picks it up once iCloud has delivered it — no waiting, no threads to
    abandon, and nothing silently dropped.
    """
    try:
        subprocess.Popen(['brctl', 'download', path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ── Source discovery and classification ───────────────────────────────────

def find_sources(roots):
    """Every .nml/.m3u under `roots`. stdlib walk, so no `fd` dependency for
    a core feature (the README only requires fd for the Inbox utilities)."""
    found = []
    for root in roots:
        root = os.path.expanduser(root)
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.lower().endswith(_SUFFIXES):
                    found.append(os.path.join(dirpath, name))
    return sorted(set(found))


def classify(path):
    low = path.lower()
    base = os.path.basename(low)
    if base.endswith(('.m3u', '.m3u8')):
        return 'm3u'
    if '/history/' in low or base.startswith('history'):
        return 'history'
    if '/backup/collection/' in low:
        return 'collection'
    if base == 'collection.nml':
        return 'collection'
    return 'collection'


def era_of(path):
    """The Traktor root directory a file belongs to.

    Native Instruments documents that every update gets its own root
    directory named for its version, so that directory *is* the generation
    boundary. Keyed on the full path, not just the version number, because
    the owner turned out to have two independent installations synced into
    iCloud (a second Mac) sharing version numbers — and a future external
    drive would make a third.
    """
    m = re.search(r'^(.*/Traktor \d[\d.]*)/', path)
    return m.group(1) if m else os.path.dirname(path)


# ── Parsing ───────────────────────────────────────────────────────────────

def _entry_records(collection_el):
    """(key, artist_known, fields, basename) per ENTRY."""
    out = []
    for e in collection_el:
        artist = (e.get('ARTIST') or '').strip()
        title = (e.get('TITLE') or '').strip()
        if not title:
            continue
        info = e.find('INFO')
        loc = e.find('LOCATION')
        key, known = track_key(artist, title)
        try:
            play = int((info.get('PLAYCOUNT') if info is not None else 0) or 0)
        except (TypeError, ValueError):
            play = 0
        try:
            rank = int((info.get('RANKING') if info is not None else 0) or 0)
        except (TypeError, ValueError):
            rank = 0
        out.append({
            'key': key,
            'artist_known': known,
            'artist': artist,
            'title': title,
            'play_count': play,
            'rating': rank,
            'last_played': (info.get('LAST_PLAYED') if info is not None else None),
            'basename': ((loc.get('FILE') or '').lower()
                         if loc is not None else ''),
        })
    return out


def _playlist_nodes(playlists_el):
    """(name, [primarykey, ...]) for every real playlist node.

    SMARTLIST nodes are skipped: they are saved queries ('$PLAYED == TRUE'),
    not curation — their membership is computed at display time and says
    nothing about what the owner chose to put together.
    """
    out = []
    if playlists_el is None:
        return out
    for node in playlists_el.iter('NODE'):
        if node.get('TYPE') not in ('PLAYLIST',):
            continue
        pl = node.find('PLAYLIST')
        if pl is None:
            continue
        keys = [pk.get('KEY') for pk in pl.iter('PRIMARYKEY') if pk.get('KEY')]
        out.append((node.get('NAME') or '?', keys))
    return out


def parse_nml(path):
    """{'entries': [...], 'playlists': [(name, [pathkey,...]), ...]}"""
    root = ET.parse(path).getroot()
    coll = root.find('COLLECTION')
    return {
        'entries': _entry_records(coll) if coll is not None else [],
        'playlists': _playlist_nodes(root.find('PLAYLISTS')),
    }


def parse_m3u(path):
    """Path lines only — .m3u carries no artist/title, so these resolve
    through the basename index like any other path reference."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return [l.strip() for l in f
                if l.strip() and not l.strip().startswith('#')]


def path_basename(primarykey_or_path):
    """Last path component of either a Traktor PRIMARYKEY
    ('VOLUME/:Users/:vsm/:Music/:x.mp3') or a plain .m3u line."""
    s = (primarykey_or_path or '').replace('/:', '/')
    return os.path.basename(s).lower()


# ── Merge ─────────────────────────────────────────────────────────────────

def _date_sort_key(d):
    """'2018/10/13' sorts *before* '2018/9/9' as a string — Traktor writes
    unpadded month/day, so comparing these lexically silently picks the
    wrong 'most recent'. Parse instead."""
    if not d:
        return ()
    try:
        return tuple(int(p) for p in str(d).split('/'))
    except ValueError:
        return ()


def _new_track(rec):
    return {
        'artist': rec['artist'],
        'title': rec['title'],
        'artist_known': rec['artist_known'],
        'play_count': 0,
        'play_count_source': None,
        'rating': 0,
        'last_played': None,
        'snapshots': 0,
        'eras': set(),
        'playlists': set(),
        'histories': set(),
    }


def _absorb_entry(track, rec, source, era, count_snapshot=True):
    """Fold one snapshot's view of a track into the running aggregate.

    `count_snapshot` is False for history files: they are a record of one
    gig, not a census of the collection, so counting them would inflate
    "seen in N snapshots" — the number the owner reads as "how much
    corroboration is behind this play count".
    """
    if rec['play_count'] > track['play_count']:
        track['play_count'] = rec['play_count']
        track['play_count_source'] = source
    track['rating'] = max(track['rating'], rec['rating'])
    if _date_sort_key(rec['last_played']) > _date_sort_key(track['last_played']):
        track['last_played'] = rec['last_played']
    if count_snapshot:
        track['snapshots'] += 1
    track['eras'].add(era)
    # Prefer a version of the strings that actually names an artist.
    if rec['artist_known'] and not track['artist_known']:
        track['artist'] = rec['artist']
        track['title'] = rec['title']
        track['artist_known'] = True


def _absorb_track(target, other):
    """Fold a title-only row into the full artist+title row it belongs to."""
    if other['play_count'] > target['play_count']:
        target['play_count'] = other['play_count']
        target['play_count_source'] = other['play_count_source']
    target['rating'] = max(target['rating'], other['rating'])
    if _date_sort_key(other['last_played']) > _date_sort_key(target['last_played']):
        target['last_played'] = other['last_played']
    target['snapshots'] += other['snapshots']
    for f in ('eras', 'playlists', 'histories'):
        target[f] |= other[f]


def link_title_only(tracks):
    """Merge each title-only row into a full row whose artist+title collapses
    to the same characters — `'Discoshaman - Making A Cyborg Edit'` is the
    same record as artist='Discoshaman', title='Making A Cyborg Edit'.

    ponytail: exact equality after normalisation only, no edit distance. A
    fuzzy match here would silently fuse two different records in a list whose
    whole purpose is telling the owner what to go buy; near-misses are left as
    separate rows for a human to judge.
    """
    full = {}
    for key, t in tracks.items():
        if t['artist_known']:
            full.setdefault(collapse(t['artist'] + t['title']), key)
    merged = 0
    for key in [k for k, t in tracks.items() if not t['artist_known']]:
        target = full.get(collapse(tracks[key]['title']))
        if target and target != key:
            _absorb_track(tracks[target], tracks.pop(key))
            merged += 1
    return merged


# ── Scan ──────────────────────────────────────────────────────────────────

_SET_FIELDS = ('eras', 'playlists', 'histories')


def new_state():
    return {'version': STATE_VERSION, 'sources': {}, 'tracks': {},
            'basename_index': {}}


def _note_basename(index, basename, key):
    """basename -> key, or None once two different tracks claim it.

    Ambiguity resolves to nothing rather than to a guess: 24 of 4634 files
    share a basename, and attributing a gig play to the wrong record is the
    error this whole list exists to avoid.
    """
    if not basename:
        return
    if basename in index:
        if index[basename] not in (key, None):
            index[basename] = None
    else:
        index[basename] = key


def scan(roots, state=None, job=None, reset=False):
    """Merge every .nml/.m3u under `roots` into `state`. Returns the state.

    Sources already merged and unchanged (same mtime+size) are skipped, so
    pointing this at an external drive later adds to the picture instead of
    rebuilding it. Every file is accounted for in state['sources'] with a
    status — parsed, skipped or failed, always with a reason.
    """
    state = new_state() if (reset or not state
                            or state.get('version') != STATE_VERSION) else state
    sources, tracks, index = state['sources'], state['tracks'], state['basename_index']

    files = find_sources(roots)
    todo = []
    for path in files:
        try:
            st = os.stat(path)
        except OSError as exc:
            sources[path] = {'kind': classify(path), 'status': 'failed',
                             'error': f'{type(exc).__name__}: {exc}'}
            continue
        prev = sources.get(path)
        if (prev and prev.get('status') == 'ok'
                and prev.get('mtime') == st.st_mtime
                and prev.get('size') == st.st_size):
            continue
        todo.append((path, st))

    # Playlist / history membership is resolved after every collection in
    # this run has contributed to the basename index, so a playlist listed
    # before its own collection file still resolves.
    #
    # Keyed by (kind, label) rather than appended per file: the same playlist
    # is present in all 122 collection snapshots, so a flat list would resolve
    # 'all23' a hundred times over and report its unresolvable entries just as
    # often — turning a number the owner is meant to read into noise.
    pending = {}          # (kind, label) -> {basename, ...}
    parsed = skipped = 0

    for i, (path, st) in enumerate(todo, 1):
        if job is not None and job.aborted.is_set():
            break
        kind, era = classify(path), era_of(path)
        name = os.path.basename(path)
        if job is not None:
            job.emit('status', message=f'[{i}/{len(todo)}] {name}')
        rec = {'kind': kind, 'era': era, 'mtime': st.st_mtime,
               'size': st.st_size, 'status': 'ok', 'error': None, 'entries': 0}

        if st.st_size == 0:
            rec.update(status='skipped', error='empty file (0 bytes)')
        elif is_dataless(st):
            # Reading it here would block the whole scan — see request_download.
            request_download(path)
            rec.update(status='skipped',
                       error='not downloaded from iCloud — download started, '
                             'scan again once Finder shows it as local')
        else:
            try:
                if kind == 'm3u':
                    lines = parse_m3u(path)
                    rec['entries'] = len(lines)
                    pending.setdefault(('playlist', f'{name} (m3u)'), set()).update(
                        path_basename(l) for l in lines)
                else:
                    doc = parse_nml(path)
                    rec['entries'] = len(doc['entries'])
                    is_history = kind == 'history'
                    for entry in doc['entries']:
                        track = tracks.setdefault(entry['key'], _new_track(entry))
                        _absorb_entry(track, entry, path, era,
                                      count_snapshot=not is_history)
                        if is_history:
                            track['histories'].add(name)
                        else:
                            _note_basename(index, entry['basename'], entry['key'])
                    for pl_name, keys in doc['playlists']:
                        slot = ('history', name) if is_history \
                            else ('playlist', pl_name)
                        pending.setdefault(slot, set()).update(
                            path_basename(k) for k in keys)
            except ET.ParseError as exc:
                rec.update(status='failed', error=f'malformed XML: {exc}')
            except (OSError, UnicodeError) as exc:
                rec.update(status='failed',
                           error=f'{type(exc).__name__}: {exc}')

        sources[path] = rec
        parsed += rec['status'] == 'ok'
        skipped += rec['status'] != 'ok'

    unresolved = 0
    for (kind, label), basenames in pending.items():
        field = 'histories' if kind == 'history' else 'playlists'
        for basename in basenames:
            key = index.get(basename)
            if key and key in tracks:
                tracks[key][field].add(label)
            else:
                unresolved += 1

    linked = link_title_only(tracks)
    # Every figure here describes the whole list, not just this run — they are
    # shown to the owner as caveats on the data, and an incremental re-scan
    # that read nothing new would otherwise reset them all to zero while the
    # merges and dropped references they describe are still in force.
    prior = state.get('stats') or {}
    state['stats'] = {
        'files_parsed': sum(1 for r in sources.values() if r.get('status') == 'ok'),
        'files_skipped': sum(1 for r in sources.values() if r.get('status') != 'ok'),
        'files_read_this_run': parsed + skipped,
        'tracks': len(tracks),
        'title_only_merged': prior.get('title_only_merged', 0) + linked,
        'unresolved_path_refs': (prior.get('unresolved_path_refs', 0)
                                 + unresolved),
    }
    return state


# ── Persistence ───────────────────────────────────────────────────────────

def state_path(library_db_path):
    return os.path.join(os.path.dirname(library_db_path), 'traktor_recovery.json')


def load_state(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        return new_state()
    if state.get('version') != STATE_VERSION:
        return new_state()
    for track in state.get('tracks', {}).values():
        for field in _SET_FIELDS:
            track[field] = set(track.get(field) or ())
    return state


def save_state(state, path):
    out = dict(state)
    out['tracks'] = {
        key: {**track, **{f: sorted(track[f]) for f in _SET_FIELDS}}
        for key, track in state['tracks'].items()
    }
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f)
    os.replace(tmp, path)       # never leave a half-written recovery list


# ── Library matching ──────────────────────────────────────────────────────

def annotate_library(tracks, db_path):
    """Mark which recovered tracks are already back in beets.

    Same read-only sqlite route `/unimported` takes, and the same reason it
    matches on (artist, title) rather than path: beets' stored paths don't
    survive a reimport, and neither do Traktor's.

    Title-only rows can only be matched on title, which is a weaker claim —
    they get `library_match: 'title-only'` so the UI never presents a guess
    as a confirmed hit.
    """
    import sqlite3
    full, titles = set(), set()
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    except sqlite3.Error:
        return
    try:
        for artist, title in con.execute(
                'SELECT DISTINCT artist, title FROM items'):
            key, known = track_key(artist or '', title or '')
            if known:
                full.add(key)
                full.add(collapse((artist or '') + (title or '')))
            titles.add(norm_title(title or ''))
    finally:
        con.close()

    for track in tracks.values():
        if track['artist_known']:
            key, _ = track_key(track['artist'], track['title'])
            hit = (key in full
                   or collapse(track['artist'] + track['title']) in full)
            track['library_match'] = 'exact' if hit else None
        else:
            hit = norm_title(track['title']) in titles
            track['library_match'] = 'title-only' if hit else None


# ── Output ────────────────────────────────────────────────────────────────

def assumptions(state):
    """Stated in the UI beside the numbers, not buried here (#42).

    The owner has to be able to tell which figures are estimates.
    """
    eras = {rec.get('era') for rec in state.get('sources', {}).values()
            if rec.get('status') == 'ok'}
    stats = state.get('stats', {})
    return [
        f'Play count is the highest value seen for a track across '
        f'{stats.get("files_parsed", 0)} source files in {len(eras)} Traktor '
        f'installations — not a total. Native Instruments does not document '
        f'whether rebuilding a collection resets the count, so summing could '
        f'inflate and taking the newest could lose a number an older backup '
        f'still holds. The maximum can only under-report.',
        f'{stats.get("unresolved_path_refs", 0)} playlist/history entries '
        f'referenced a file that no snapshot could identify, and are not '
        f'counted against any track.',
        f'{stats.get("title_only_merged", 0)} title-only rows were merged '
        f'into a matching artist+title row. Rows still marked "artist '
        f'unknown" are ones Traktor never recorded an artist for.',
    ]


_SORTS = {
    'play_count': lambda t: (t['play_count'], len(t['histories']),
                             len(t['playlists'])),
    'histories': lambda t: (len(t['histories']), t['play_count'],
                            len(t['playlists'])),
    'playlists': lambda t: (len(t['playlists']), t['play_count'],
                            len(t['histories'])),
    'rating': lambda t: (t['rating'], t['play_count']),
}


def results(state, sort='play_count', missing_only=True, limit=200, offset=0):
    rows = [t for t in state['tracks'].values()
            if not (missing_only and t.get('library_match'))]
    rows.sort(key=_SORTS.get(sort, _SORTS['play_count']), reverse=True)
    total = len(rows)
    page = [{
        'artist': t['artist'] if t['artist_known'] else None,
        'title': t['title'],
        'play_count': t['play_count'],
        'play_count_source': (os.path.basename(t['play_count_source'])
                              if t['play_count_source'] else None),
        'rating': round(t['rating'] / 51) if t['rating'] else 0,
        'last_played': t['last_played'],
        'snapshots': t['snapshots'],
        'eras': len(t['eras']),
        'playlists': sorted(t['playlists'])[:8],
        'playlist_count': len(t['playlists']),
        'histories': len(t['histories']),
        'library_match': t.get('library_match'),
    } for t in rows[offset:offset + limit]]
    return {'total': total, 'tracks': page}


def source_report(state):
    """Every file, with why it did or didn't contribute. Nothing is dropped
    silently — that is one of the issue's acceptance criteria."""
    out = []
    for path, rec in sorted(state.get('sources', {}).items()):
        out.append({'path': path, 'name': os.path.basename(path),
                    'kind': rec.get('kind'), 'status': rec.get('status'),
                    'error': rec.get('error'), 'entries': rec.get('entries', 0)})
    return out


# ── Job ───────────────────────────────────────────────────────────────────

def _run(job):
    path = job.meta['state_path']
    state = new_state() if job.meta.get('reset') else load_state(path)
    state = scan(job.meta['roots'], state, job=job,
                 reset=bool(job.meta.get('reset')))
    if job.meta.get('db_path'):
        annotate_library(state['tracks'], job.meta['db_path'])
    save_state(state, path)
    job.result.update(state.get('stats', {}))


def start(roots, state_path_, db_path=None, reset=False):
    """Start a Traktor recovery scan. Raises RuntimeError if a job is running."""
    job = jobs.Job('traktor', roots=roots, state_path=state_path_,
                   db_path=db_path, reset=bool(reset))
    return jobs.start(job, _run)
