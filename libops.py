"""
Library maintenance — modify, update, write, missing, remove — driven
against the beets Library object instead of shelling out to `beet modify`
etc.

`update_items`/`write_items` are beets' own CLI internals, reused as-is:
both already support a non-interactive `pretend` flag, so no reimplementing
was needed. `remove_items` doesn't have a pretend mode (only an interactive
confirm), so remove gets its own preview step here instead; same for
modify, which beets only exposes via its interactive `modify_items`.
"""
import io
import os
import re
import shlex
import threading
from contextlib import redirect_stdout
from pathlib import Path

from platform import python_version

import beets
from beets import library, plugins
from beets.dbcore.query import OrQuery, PathQuery
from beets.ui import UserError
from beets.ui.commands.remove import remove_items as _remove_items
from beets.ui.commands.update import update_items as _update_items
from beets.ui.commands.utils import do_query
from beets.ui.commands.write import write_items as _write_items
from beets.util import displayable_path, functemplate
from beets.util.deprecation import maybe_replace_legacy_field
from beets.util.units import human_bytes, human_seconds

from importsession import get_library

# update_items/write_items only print their diff (via beets.ui.print_) —
# capture stdout instead of reimplementing their (correct, already-tested)
# diffing logic. beets' config/library are already process-global and
# single-flight (see importsession.py's docstring); this lock extends that
# same discipline to console-capturing calls.
_console_lock = threading.Lock()

# beets colourises its diff output for a terminal, so the captured text
# carries ANSI escapes that a browser renders as literal "[1;31m deleted".
# Issue #11 deleted the old strip_ansi along with /run, correctly — it was
# dead at the time. Capturing beets' own console output (added in #14)
# brought the need back.
_ANSI = re.compile(r'\x1b\[[0-9;]*m')


# Fields that aren't metadata. `path` is the sharpest: setting it redirects
# try_sync's tag write to whatever file the new path names, and then store()
# persists it, so the real file is orphaned and an unrelated media file gets
# this item's tags. `id`/`album_id` collide database rows. beets' own `beet
# modify` allows all three, but there the user typed the field name; here it
# arrives in a JSON body behind a free-text input.
PROTECTED_FIELDS = {'path', 'id', 'album_id'}


def split_query(query):
    """Split a beets query string the way the shell would.

    An unbalanced quote falls back to a plain whitespace split rather than
    erroring. `Don't Stop`, `O'Brien`, `Guns N' Roses` are ordinary things to
    type into a search box, and shlex reads that apostrophe as an unclosed
    quote — a 400 there is the wrong answer for a music library, where the
    apostrophe is a letter far more often than it is punctuation. Everything
    the UI quotes itself is balanced (shellQuote in beetsgui.html), so this
    fallback only ever sees hand-typed input, where matching the literal
    words beats refusing to search.
    """
    if not query:
        return []
    try:
        return shlex.split(query)
    except ValueError:
        return query.split()


# An `ids` scope becomes one `id:` term per id, so the whole set has to fit
# in a query. Beyond this the caller is describing a filter, not a
# selection, and should send that filter as scope.query instead.
MAX_SCOPE_IDS = 500


def scope_query(data):
    """The beets query string a request body asks to operate on (#63).

    Accepts `{"scope": {"query": "..."}}`, `{"scope": {"ids": [12, 88]}}`,
    and the legacy top-level `{"query": "..."}` — so an `ids` scope stays a
    query string all the way down, and every caller of split_query() below
    (plus artwork/transcode/sync) keeps working unchanged.

    beets ORs on a comma *token*, so the spaces in `id:12 , id:88` are load
    bearing: `id:12,id:88` is one keyword and parses as a single, invalid
    numeric term.
    """
    scope = data.get('scope')
    if scope is None:
        return data.get('query', '') or ''
    if not isinstance(scope, dict):
        raise UserError('scope must be an object')
    if 'ids' not in scope:
        return scope.get('query', '') or ''

    ids = scope['ids']
    # bool is an int in Python; `[True]` would silently become `id:1`.
    if not isinstance(ids, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise UserError('scope.ids must be a list of integers')
    # An empty list means "these zero items", never "the whole library" —
    # which is what an empty query string would mean one line further down.
    if not ids:
        raise UserError('scope.ids must not be empty')
    if len(ids) > MAX_SCOPE_IDS:
        raise UserError(
            f'scope.ids is limited to {MAX_SCOPE_IDS} ids (got {len(ids)}) — '
            f'send the filter itself as scope.query instead')
    return ' , '.join(f'id:{i}' for i in ids)


def _resolve_field(field):
    """Reject non-metadata fields, and translate beets' renamed ones.

    beets 2.x renamed `genre`/`composer` to the list fields
    `genres`/`composers` and ships `maybe_replace_legacy_field` for the
    translation — its own `modify` command calls it. Without it, `genre`
    parses as an unknown field, lands as a *flexible attribute*, and is then
    filtered out by `write()` (which only writes `_media_fields`), so the
    caller is told N items changed while nothing reaches the files.
    """
    if field in PROTECTED_FIELDS:
        raise UserError(f'{field} is not an editable metadata field')
    field = maybe_replace_legacy_field(field, False, modify=True)
    # Anything else lands as a flexible attribute that write() silently
    # never writes to the file (the genre/genres class of bug — see
    # module docstring). Reject it here instead of letting it hide.
    if field not in library.Item.all_keys():
        raise UserError(f'{field} is not a known metadata field')
    return field


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with _console_lock, redirect_stdout(buf):
        fn(*args, **kwargs)
    return [_ANSI.sub('', line).rstrip()
            for line in buf.getvalue().splitlines() if line.strip()]


# ── modify ───────────────────────────────────────────────────────────────

def preview_modify(field, value, query):
    """Items the query matches, with their current and would-be new value."""
    field = _resolve_field(field)
    lib = get_library()
    items = list(lib.items(split_query(query)))
    template = functemplate.template(value)
    changes = []
    for item in items:
        new = library.Item._parse(field, item.evaluate_template(template))
        old = item.get(field)
        if old != new:
            changes.append({'id': item.id, 'label': str(item),
                             'old': old, 'new': new})
    return changes


def apply_modify(field, value, query):
    """Apply the change previewed by preview_modify(). Returns count changed."""
    field = _resolve_field(field)
    lib = get_library()
    items = list(lib.items(split_query(query)))
    template = functemplate.template(value)
    changed = []
    for item in items:
        new = library.Item._parse(field, item.evaluate_template(template))
        if item.get(field) != new:
            item[field] = new
            changed.append(item)
    with lib.transaction():
        for item in changed:
            item.try_sync(True, False)  # write tags to file, don't move
    return len(changed)


# ── update / write ──────────────────────────────────────────────────────
# Both beets internals already take a `pretend` flag — reused directly.

def update(query, pretend):
    """Returns the printed diff lines (deleted files, changed fields)."""
    lib = get_library()
    return _capture(_update_items, lib, split_query(query), False, False,
                     pretend, None)


def write(query, pretend):
    """Returns the printed diff lines (tags that would be/were written)."""
    lib = get_library()
    return _capture(_write_items, lib, split_query(query), pretend, False)


# ── info ─────────────────────────────────────────────────────────────────
# Same math as beets' own `stats`/`fields`/`version` commands
# (beets/ui/commands/stats.py, fields.py) — returned as data instead of
# printed text, since these are display-only, nothing to preview or apply.

def stats():
    """The same totals as beets' `stats`, but as one SQL aggregate.

    beets walks every Item to sum these, which is a full ORM materialisation
    of the library; measured at ~2.9s over 40k tracks (#12). That cost sits
    on the page-load path, not just this panel — the first-run banner asks
    /info/stats whether the library is empty. The CAST reproduces beets' own
    per-item `int()` truncation exactly, so the byte total is unchanged.
    """
    lib = get_library()
    with lib.transaction() as tx:
        tracks, total_time, total_size, artists, album_artists, albums = tx.query('''
            SELECT COUNT(*),
                   COALESCE(SUM(length), 0),
                   COALESCE(SUM(CAST(length * bitrate / 8 AS INTEGER)), 0),
                   COUNT(DISTINCT artist),
                   COUNT(DISTINCT albumartist),
                   COUNT(DISTINCT album_id)
            FROM items
        ''')[0]
    return {
        'tracks': tracks,
        'total_time': human_seconds(total_time),
        'total_size': human_bytes(total_size),
        'artists': artists,
        'albums': albums,
        'album_artists': album_artists,
    }


def album_count(query):
    """How many albums a beets query matches — for a preview step before a
    job that would otherwise run against the whole library unannounced."""
    lib = get_library()
    return len(list(lib.albums(split_query(query))))


def matching_ids(query):
    """(item_ids, album_ids) a beets query selects — item ids, and the ids of
    the albums those items belong to.

    The Library list and every action now share one filter (#64), so the two
    have to agree on what it selects. They agree by both going through beets
    here, rather than the list re-implementing the query language against the
    raw SQL it reads — which is what made `?q=` a `LIKE` over four columns
    while the actions got the real thing.

    Resolving against *items* is the single semantics the whole tab is built
    on: the filter selects tracks, and the Albums/Artists views group them.
    That is also why album ids are derived here instead of from
    `lib.albums(query)` — beets falls back to item fields for some album
    queries (`format:AIFF` matches) but not others (`length:..10` doesn't),
    and a list whose grouping disagreed with its own filter would be worse
    than either.

    ponytail: materialises the whole match set to render one page of 200.
    Measured at 5,000 albums / 40,000 items (#12): ~630ms for a free-text
    search, ~280ms for a field match, against ~5ms for the unfiltered list.
    Still under the 1s the issue asks for, so it stays. The upgrade, when it
    stops being fine, is to splice beets' own `Query.clause()` into the
    list SQL — which
    needs the custom SQL functions (regexp, bytelower) that the read-only
    connection in server.py deliberately doesn't register.
    """
    lib = get_library()
    items = list(lib.items(split_query(query)))
    return ([i.id for i in items],
            sorted({i.album_id for i in items if i.album_id}))


def fields():
    """Field names, plus which of them are numeric.

    The filter rows (#64) offer a different operator set per type — "contains"
    makes no sense for `year`, "at least" makes none for `artist`. Which is
    which comes from beets' own declared SQL type rather than a hardcoded
    list here, for the same reason the sort dropdown reads /info/fields:
    plugins add fields, and a list that drifts silently offers the wrong
    operators for them.
    """
    numeric = sorted(
        name for name, typ in library.Item._fields.items()
        if str(getattr(typ, 'sql', '')).upper().startswith(('INTEGER', 'REAL')))
    return {
        'item_fields': sorted(library.Item.all_keys()),
        'album_fields': sorted(library.Album.all_keys()),
        'numeric_fields': numeric,
    }


def version():
    return {
        'beets': beets.__version__,
        'python': python_version(),
        'plugins': sorted(p.name for p in plugins.find_plugins()),
    }


# ── missing ──────────────────────────────────────────────────────────────

def missing_albums():
    """Albums with fewer items than their tagged track total.

    ponytail: local-only (albumtotal vs. actual item count) — the full
    MusicBrainz-backed per-track listing (plain `beet missing`, no -c)
    makes a network call per album, which belongs with the job-streaming
    work (same category as mbsync/bpsync), not this fast-local-ops set.
    """
    lib = get_library()
    result = []
    for album in lib.albums():
        total = album.albumtotal or 0
        have = len(album.items())
        if total > have:
            result.append({
                'id': album.id, 'artist': album.albumartist,
                'album': album.album, 'have': have, 'total': total,
            })
    return result


# ── playlist scoping (#43) ──────────────────────────────────────────────
# beets' own `playlist` plugin (playlist:name queries) can't be reused
# here: PlaylistQuery builds absolute paths and compares them with a plain
# InQuery, which never calls dbcore.pathutils.normalize_path_for_db — so it
# silently matches nothing against a library whose paths were migrated to
# be relative to `directory:` (mandatory since beets 2.11, same migration
# server.py's resolve_item_path handles on the read side). PathQuery does
# normalize, so this resolves the .m3u itself and queries by path instead.

def playlist_item_ids(playlist_dir, names):
    """Item ids listed by one or more .m3u playlists (by stem, under
    playlist_dir — same files /playlists already lists for the picker).

    Lines are resolved the way beets' own importfeeds plugin writes them:
    absolute as-is, otherwise relative to the library directory.
    """
    lib = get_library()
    library_dir = beets.config['directory'].as_filename()
    ids = set()
    for name in names:
        m3u = Path(os.path.expanduser(playlist_dir)) / f'{name}.m3u'
        if not m3u.is_file():
            raise UserError(f'no such playlist: {name}')
        lines = [ln.strip() for ln in
                 m3u.read_text(errors='surrogateescape').splitlines()]
        paths = [ln if os.path.isabs(ln) else os.path.join(library_dir, ln)
                 for ln in lines if ln and not ln.startswith('#')]
        if not paths:
            continue
        query = OrQuery([PathQuery('path', p) for p in paths])
        ids.update(i.id for i in lib.items(query))
    return sorted(ids)


# ── remove ───────────────────────────────────────────────────────────────

def preview_remove(query):
    """Items the query matches — the preview step before a real remove call."""
    lib = get_library()
    items, _ = do_query(lib, split_query(query), False)
    return [{'id': i.id, 'label': str(i), 'path': displayable_path(i.path)} for i in items]


def remove(query, delete_files):
    """Remove matching items. force=True: the web UI's preview step already
    is the confirmation, so beets' own interactive re-confirmation is
    redundant here (and would block a non-interactive process forever).

    "Remove short files" (tracks under N seconds) is this same call with
    query=f'length:..{seconds}' — no separate endpoint needed, beets'
    query language already expresses it exactly."""
    lib = get_library()
    _remove_items(lib, split_query(query), False, delete_files, True)
