"""
Post-import duplicate scan and resolution — the Library tab's Duplicates
section, driven against the beets Library object instead of shelling out
to `beet duplicates`.

Groups existing albums by (albumartist, album) — the same keys beets itself
uses for import.duplicate_keys (see config_default.yaml) and that
importsession.py's own duplicate handling relies on — rather than the
`beetsplug.duplicates` command's `-k`/grouping-key option, which groups by
raw field *equality* (e.g. `-k format` groups every album sharing a format
value as "duplicates" of each other, deleting almost the whole library).
Quality ranking reuses importsession.quality_rank(), so "which copy is
better" is decided by one rule everywhere in this app.
"""
from collections import defaultdict

from importsession import LOSSLESS_FORMATS, get_library, quality_rank


def _album_summary(album, items, rank, is_best):
    first = items[0] if items else None
    out = {
        'id':      album.id,
        'artist':  album.albumartist,
        'album':   album.album,
        'year':    album.year,
        'tracks':  len(items),
        'format':  first.format if first else None,
        'is_best': is_best,
    }
    if first is not None:
        fmt = (first.format or '').upper()
        if fmt in LOSSLESS_FORMATS:
            out['bitdepth'] = first.bitdepth
            out['samplerate'] = first.samplerate
        else:
            out['bitrate'] = first.bitrate
    return out


def find_duplicate_groups():
    """Existing albums sharing (albumartist, album), each group best-first.

    ponytail: album-level only — a singleton with no album never appears
    here, since /library itself is album-only. Add item-level scanning if
    singleton duplicates turn out to matter in practice.
    """
    lib = get_library()
    by_key = defaultdict(list)
    for album in lib.albums():
        key = ((album.albumartist or '').strip().lower(),
               (album.album or '').strip().lower())
        if not key[0] and not key[1]:
            continue
        by_key[key].append(album)

    groups = []
    for albums in by_key.values():
        if len(albums) < 2:
            continue
        ranked = sorted(
            ((quality_rank(album.items()), album, list(album.items()))
             for album in albums),
            key=lambda r: r[0], reverse=True,
        )
        best_rank = ranked[0][0]
        # A tie for best is only possible when every group member ties —
        # otherwise the sort already put the unique best first.
        groups.append([
            _album_summary(album, items, rank, rank == best_rank)
            for rank, album, items in ranked
        ])
    return groups


def delete_album(album_id, delete_files):
    """Remove one album (and its items) from the library.

    delete_files also removes the files from disk. Always a single,
    explicitly-chosen target — never inferred from a rank or a bulk rule,
    same discipline as the 'remove' duplicate choice during import.
    """
    lib = get_library()
    album = lib.get_album(album_id)
    if album is None:
        return False
    album.remove(delete=delete_files)
    return True
