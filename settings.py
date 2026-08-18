"""
Small persisted settings for beetsGUI itself (#90) — distinct from beets'
own config.yaml, which this app only ever reads (Preferences generates a
config.yaml block for the user to paste in by hand; nothing here writes to
that file). Stored as JSON next to library.db, same "app-owned state file"
pattern as traktor.py's recovery list.

One key so far: the AcoustID/chromaprint similarity score
(acoustid.compare_fingerprints(), 0..1) importsession._decide_duplicate
needs to call two fingerprints "the same recording". Read fresh (no
caching) on every duplicate decision and every /dedup/settings request —
it's a few bytes of JSON, and a stale in-memory copy after a Preferences
change is a worse trade than the file read.
"""
import json
import os

from beets import config

DEFAULTS = {
    'dedup_fingerprint_threshold': 0.95,
}


def _path():
    return os.path.join(os.path.dirname(config['library'].as_filename()),
                        'beetsgui_settings.json')


def load():
    try:
        with open(_path(), 'r', encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save(values):
    """Merge `values` into the stored settings (keys outside DEFAULTS are
    ignored) and write atomically. Returns the settings afterward."""
    current = load()
    current.update({k: v for k, v in values.items() if k in DEFAULTS})
    path = _path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(current, f)
    os.replace(tmp, path)
    return current
