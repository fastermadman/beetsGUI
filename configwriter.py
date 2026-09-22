#!/usr/bin/env python3
"""
Renders beets config.yaml from field values (#128).

The security property this module exists for: **the server never accepts
YAML.** It accepts field values and renders the YAML itself, so no request
field can reach the serializer as a *key*. That is structural rather than a
mitigation — a key absent from the maps below cannot appear in the output no
matter what the request says.

Stated as an allowlist of emittable keys, deliberately, not a denylist of
dangerous ones. #128's own first pass named `convert.command` as "the"
code-execution vector and missed three more: `duplicates.checksum`
(shlex.split -> command_output), `keyfinder.bin` (argv[0]) and `statefile`
— which is a *core* key, always active, reaching `pickle.load` in
beets/importer/state.py, so no plugin allowlist sees it at all. Enumeration
already failed once here, and beets' config surface drifts every release, so
any denylist is wrong again at the next upgrade.

What this module can and cannot do, precisely:

  - It cannot *introduce or modify* `pluginpath`, `statefile`,
    `duplicates.checksum`, `keyfinder.bin` or `convert.*.command`. None is
    reachable from a request field; the convert commands come from
    PLUGIN_PRESETS below, exactly as beetsgui.html hardcoded them.
  - It *preserves* one already present in the user's file, because that key
    is theirs and eating it would be the worse failure (merge_config).
    preserved_risky() surfaces those so the UI can show what is being
    carried forward rather than the app silently reasserting it every save.

Not checked here, having been checked against beets 2.13.1 sources rather
than assumed: `paths:` templates cannot execute Python (functemplate builds
its AST structurally and resolves function names from a dict parameter, so
`%__import__{os}` is a missing key, not a builtin), and neither
fetchart/embedart nor musicbrainz expose an executable or a redirectable
host as a config key in this version.
"""
import os
import re
import time
from pathlib import Path

import yaml

# Every plugin Preferences can enable, mapped to the config block it emits
# ({} = enabled, no block of its own). This dict is the allowlist twice over:
# a name that isn't a key here is refused rather than passed through into
# `plugins:`, and a key that isn't written literally in a value here is not
# emittable. Blocks are the ones beetsgui.html's buildConfig() hardcoded
# client-side (#128 moves them here, where a request cannot reach them).
#
# `keyfinder`, `duplicates` and `lyrics` are enableable but get no block —
# that is what keeps `keyfinder.bin`, `duplicates.checksum` and
# `lyrics.rest_directory` unemittable while the plugins themselves stay
# available.
PLUGIN_PRESETS: dict[str, dict] = {
    'musicbrainz': {'data_source_mismatch_penalty': 0.4},
    'chroma':      {'auto': True},
    'beatport4':   {},          # art/priority filled in by render()
    'discogs':     {'data_source_mismatch_penalty': 0.5},
    'deezer':      {},
    'spotify':     {},
    'tidal':       {},
    'fetchart':    {'auto': True,
                    'sources': ['filesystem', 'coverart', 'itunes', 'amazon']},
    'embedart':    {'auto': True, 'if_empty': True},
    'lastgenre':   {'auto': True, 'source': 'album', 'count': 1,
                    'min_weight': 10, 'fallback': 'Electronic'},
    'fromfilename': {},
    'bpsync':      {},
    'convert':     {'format': 'alac',
                    'never_convert_lossy_files': True,
                    'formats': {
                        'alac': {'command': 'ffmpeg -i $source -c:a alac '
                                            '-map_metadata 0 $dest',
                                 'extension': 'm4a'},
                        'mp3_320': {'command': 'ffmpeg -i $source -y -vn '
                                               '-c:a libmp3lame -b:a 320k $dest',
                                    'extension': 'mp3'}}},
    'autobpm':     {},
    'keyfinder':   {},
    'lyrics':      {},
    'duplicates':  {},
    'missing':     {},
    'mbsync':      {},
    'importfeeds': {'formats': 'm3u_multi', 'dir': '~/Playlister/'},
    'dirfields':   {'field': 'original_folder'},
}

# Plugins that are one config line away from arbitrary code execution:
# `inline` evaluates Python from the config, `hook` runs a shell command per
# event, `loadext` loads a SQLite extension .so, and the rest take an
# executable path. None is in PLUGIN_PRESETS, so none is reachable — but
# that is a property of a hand-written list, so test_config_save.py asserts
# the intersection stays empty.
NEVER_PRESET = frozenset({'inline', 'hook', 'loadext', 'play', 'replaygain',
                          'absubmit', 'badfiles', 'edit'})

# The only request fields that become YAML *values*, each at a key path
# written literally right here. A credential field not in this map is a 400.
CREDENTIAL_KEYS = {
    'mb_user':       ('musicbrainz', 'user'),
    'mb_pass':       ('musicbrainz', 'pass'),
    'discogs_token': ('discogs', 'user_token'),
    'bp4_user':      ('beatport4', 'username'),
    'bp4_pass':      ('beatport4', 'password'),
}

# Radio-button values in Preferences, not free text (#128: an enum).
IMPORT_STRATEGIES = {
    'move': {'move': True,  'write': True},
    'copy': {'copy': True,  'write': True},
    'none': {'copy': False, 'write': False},
}

VALID_EXTENSIONS = ['.mp3', '.aiff', '.aif', '.wav', '.flac', '.alac',
                    '.m4a', '.aac', '.ogg']

# Keys we never emit but may carry forward from the user's own file. Shown
# in the save dialog so a preserved one is visible rather than silent — the
# endpoint's claim is "cannot introduce or modify", not "cannot exist".
RISKY_KEYS = frozenset({'pluginpath', 'statefile', 'duplicates.checksum',
                        'keyfinder.bin', 'lyrics.rest_directory'})

# Sentinel: a key the UI owns but that this save leaves unset — cleared from
# the merged config rather than left stale. Preferences showing an empty
# password field while config.yaml still holds the old one is the UI lying.
DROP = object()


class ConfigError(ValueError):
    """A request this module refuses to render. Becomes a 400."""


def _text(fields, name, default=None):
    v = fields.get(name, default)
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f'{name} must be a non-empty string')
    return v.strip()


def render(fields: dict) -> dict:
    """Field values -> the config structure this app owns.

    Every key in the result is a literal in this function or in
    PLUGIN_PRESETS. Raises ConfigError on anything it won't render.
    """
    directory = _text(fields, 'directory')
    library = _text(fields, 'library')
    if not os.path.isdir(os.path.expanduser(directory)):
        raise ConfigError(f'directory does not exist: {directory}')
    lib_parent = os.path.dirname(os.path.expanduser(library)) or '.'
    if not os.path.isdir(lib_parent):
        raise ConfigError(f"library's folder does not exist: {lib_parent}")

    strategy = fields.get('import_strategy')
    if strategy not in IMPORT_STRATEGIES:
        raise ConfigError('import_strategy must be one of '
                          + ', '.join(sorted(IMPORT_STRATEGIES)))

    plugins = fields.get('plugins') or []
    if not isinstance(plugins, list):
        raise ConfigError('plugins must be a list')
    unknown = [str(p) for p in plugins if p not in PLUGIN_PRESETS]
    if unknown:
        raise ConfigError('unknown plugin(s): ' + ', '.join(unknown))

    exclusions = fields.get('exclusions') or []
    if not isinstance(exclusions, list) or any(not isinstance(e, str)
                                               for e in exclusions):
        raise ConfigError('exclusions must be a list of strings')

    paths = fields.get('paths') or {}
    artist = _text(paths, 'artist', '$albumartist')
    album = _text(paths, 'album', '$year - $album')
    track = _text(paths, 'track', '$track - $title')

    imp = dict(IMPORT_STRATEGIES[strategy])
    imp['valid_extensions'] = list(VALID_EXTENSIONS)
    imp['ignore'] = list(exclusions) if exclusions else DROP

    cfg = {
        'directory': directory,
        'library': library,
        'import': imp,
        'paths': {
            'default': f'{artist}/{album}/{track}',
            'singleton': 'Various/$artist - $title',
            'comp': f'Compilations/$album/{track}',
        },
        'plugins': list(plugins),
        'autotagger': {'strong_rec_thresh': 0.04},
    }

    # A block is emitted only for an enabled plugin. Disabling one drops it
    # from `plugins:` — which is what actually turns it off — and leaves any
    # block alone rather than deleting settings the user may come back to.
    for name in plugins:
        preset = PLUGIN_PRESETS[name]
        if preset:
            cfg[name] = _deepcopy(preset)
    if 'importfeeds' in plugins:
        cfg['importfeeds']['relative_to'] = directory
    if 'beatport4' in plugins:
        bp4 = fields.get('bp4') or {}
        cfg['beatport4'] = {'art': bool(bp4.get('art', True)),
                            'data_source_mismatch_penalty':
                                _penalty(bp4.get('priority', 0.3))}

    creds = fields.get('credentials') or {}
    if not isinstance(creds, dict):
        raise ConfigError('credentials must be an object')
    unknown = sorted(set(creds) - set(CREDENTIAL_KEYS))
    if unknown:
        raise ConfigError('unknown credential field(s): ' + ', '.join(unknown))
    for field, (plugin, key) in CREDENTIAL_KEYS.items():
        if plugin not in plugins:
            continue
        value = creds.get(field)
        value = value.strip() if isinstance(value, str) else ''
        cfg.setdefault(plugin, {})[key] = value or DROP

    return cfg


def _penalty(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f'priority must be a number, got {v!r}')
    if not 0 <= f <= 1:
        raise ConfigError(f'priority must be between 0 and 1, got {f}')
    return f


def _deepcopy(d):
    return {k: _deepcopy(v) if isinstance(v, dict) else
            (list(v) if isinstance(v, list) else v) for k, v in d.items()}


def merge(old: dict, new: dict) -> dict:
    """Overlay the keys this app owns onto the user's existing config.

    Everything `new` doesn't mention survives verbatim — a settings dialog
    that silently eats hand-written `replace:` rules is worse than no save
    button. Recurses per block so a hand-added `import.ignore_hidden` sits
    beside the strategy keys we do own.
    """
    out = dict(old)
    for k, v in new.items():
        if v is DROP:
            out.pop(k, None)
        elif isinstance(v, dict):
            base = out[k] if isinstance(out.get(k), dict) else {}
            out[k] = merge(base, v)
        else:
            out[k] = v
    return out


def _flat_keys(d, prefix=''):
    for k, v in d.items():
        key = f'{prefix}{k}'
        yield key
        if isinstance(v, dict):
            yield from _flat_keys(v, key + '.')


def _is_risky(key):
    return (key in RISKY_KEYS
            or (key.startswith('convert.') and key.endswith('command')))


def preserved_risky(merged: dict, rendered: dict) -> list:
    """Dotted keys in the saved file that reach an executable, an import
    path or a deserializer, and that came from the user's file rather than
    from us. Surfaced, not refused — see the module docstring."""
    ours = set(_flat_keys(rendered))
    return sorted(k for k in _flat_keys(merged)
                  if _is_risky(k) and k not in ours)


def dropped_plugins(old: dict, rendered: dict) -> list:
    """Plugins the file enables that the checkboxes don't. Preferences owns
    `plugins:`, so these go away on save — loudly, via the diff."""
    keep = set(rendered.get('plugins') or [])
    return [p for p in (old.get('plugins') or [])
            if isinstance(p, str) and p not in keep]


# ponytail: whole-line and after-whitespace `#` only, no quote tracking. A
# false positive costs one extra warning line in the save dialog; parsing
# YAML scalars to find out exactly is not worth it for a count.
_COMMENT = re.compile(r'^\s*#|\s#')


def count_comments(text: str) -> int:
    """How many comment lines a save would destroy. safe_load/safe_dump
    can't round-trip comments, and beets configs are conventionally full of
    them — so the UI warns with a number instead of the .bak being the only
    thing standing between the user and losing them (ruamel.yaml would
    preserve them, but that's a new dependency for a one-user app)."""
    return sum(1 for line in text.splitlines() if _COMMENT.search(line))


def dumps(cfg: dict) -> str:
    """Serialize, and prove the result parses back to what went in.

    The realistic way to brick this app is a config beets can't load, since
    get_config_path() shells out to `beet config --path` and everything
    downstream depends on it. safe_dump quoting whatever it's handed makes
    that unlikely; reading it back makes it checked.
    """
    text = yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    if yaml.safe_load(text) != cfg:
        raise ConfigError('rendered config did not survive a YAML round-trip')
    return text


def write_config(path, text: str):
    """Back up, then replace atomically. Returns the backup path, or None.

    `config.yaml.bak-<timestamp>` is the shape beets itself already uses for
    its schema-migration backups, so the pattern and the user's expectation
    both exist. The temp file is in the same directory so os.replace is a
    rename within one filesystem — no half-written config is ever visible.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if p.exists():
        backup = p.with_name(f"{p.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(p.read_bytes())
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, p)
    return str(backup) if backup else None
