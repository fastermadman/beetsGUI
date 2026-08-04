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
import shlex
import threading
from contextlib import redirect_stdout

from platform import python_version

import beets
from beets import library, plugins
from beets.ui import UserError
from beets.ui.commands.remove import remove_items as _remove_items
from beets.ui.commands.update import update_items as _update_items
from beets.ui.commands.utils import do_query
from beets.ui.commands.write import write_items as _write_items
from beets.util import displayable_path, functemplate
from beets.util.units import human_bytes, human_seconds

from importsession import get_library

# update_items/write_items only print their diff (via beets.ui.print_) —
# capture stdout instead of reimplementing their (correct, already-tested)
# diffing logic. beets' config/library are already process-global and
# single-flight (see importsession.py's docstring); this lock extends that
# same discipline to console-capturing calls.
_console_lock = threading.Lock()


# Fields that aren't metadata. `path` is the sharpest: setting it redirects
# try_sync's tag write to whatever file the new path names, and then store()
# persists it, so the real file is orphaned and an unrelated media file gets
# this item's tags. `id`/`album_id` collide database rows. beets' own `beet
# modify` allows all three, but there the user typed the field name; here it
# arrives in a JSON body behind a free-text input.
PROTECTED_FIELDS = {'path', 'id', 'album_id'}


def split_query(query):
    """Split a beets query string. Raises UserError on unbalanced quotes,
    which the endpoints turn into a 400 rather than a 500."""
    if not query:
        return []
    try:
        return shlex.split(query)
    except ValueError as e:
        raise UserError(f'could not parse query: {e}')


def _check_field(field):
    if field in PROTECTED_FIELDS:
        raise UserError(f'{field} is not an editable metadata field')


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with _console_lock, redirect_stdout(buf):
        fn(*args, **kwargs)
    return [line for line in buf.getvalue().splitlines() if line]


# ── modify ───────────────────────────────────────────────────────────────

def preview_modify(field, value, query):
    """Items the query matches, with their current and would-be new value."""
    _check_field(field)
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
    _check_field(field)
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
    lib = get_library()
    items = lib.items()
    total_size = total_time = total_items = 0
    artists, albums, album_artists = set(), set(), set()
    for item in items:
        total_size += int(item.length * item.bitrate / 8)
        total_time += item.length
        total_items += 1
        artists.add(item.artist)
        album_artists.add(item.albumartist)
        if item.album_id:
            albums.add(item.album_id)
    return {
        'tracks': total_items,
        'total_time': human_seconds(total_time),
        'total_size': human_bytes(total_size),
        'artists': len(artists),
        'albums': len(albums),
        'album_artists': len(album_artists),
    }


def fields():
    return {
        'item_fields': sorted(library.Item.all_keys()),
        'album_fields': sorted(library.Album.all_keys()),
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
