#!/usr/bin/env python3
"""
Tests for the config.yaml writer and /config/save (#128).

The load-bearing test here is test_no_request_field_becomes_a_key: EMITTABLE
below is a hand-reviewed list of every key the app is allowed to write, and
a hostile request must not produce anything outside it. #128's design pass
found three code-execution vectors a first enumeration missed
(`duplicates.checksum`, `keyfinder.bin`, and `statefile` — core, reaching
pickle.load, invisible to any plugin allowlist), which is the argument for
checking the allowlist rather than chasing the denylist.

Safety: every test writes to a throwaway tmp dir. Nothing here reads or
writes ~/.config/beets — this is literally the "add a code path that writes
config.yaml" issue, so it is the exact case CLAUDE.md's rule exists for.

Run: ~/.local/pipx/venvs/beets/bin/python test_config_save.py
"""
import tempfile
from pathlib import Path
from unittest import mock

import yaml

import configwriter
import server
from configwriter import DROP

# Every key path this app may write. Derived by reading configwriter, not
# generated from it — a generated expectation would pass no matter what the
# code emitted.
EMITTABLE = {
    'directory', 'library', 'plugins',
    'import', 'import.move', 'import.copy', 'import.write',
    'import.valid_extensions', 'import.ignore',
    'paths', 'paths.default', 'paths.singleton', 'paths.comp',
    'autotagger', 'autotagger.strong_rec_thresh',
    'musicbrainz', 'musicbrainz.data_source_mismatch_penalty',
    'musicbrainz.user', 'musicbrainz.pass',
    'chroma', 'chroma.auto',
    'beatport4', 'beatport4.art', 'beatport4.data_source_mismatch_penalty',
    'beatport4.username', 'beatport4.password',
    'discogs', 'discogs.data_source_mismatch_penalty', 'discogs.user_token',
    'fetchart', 'fetchart.auto', 'fetchart.sources',
    'embedart', 'embedart.auto', 'embedart.if_empty',
    'lastgenre', 'lastgenre.auto', 'lastgenre.source', 'lastgenre.count',
    'lastgenre.min_weight', 'lastgenre.fallback',
    'convert', 'convert.format', 'convert.never_convert_lossy_files',
    'convert.formats',
    'convert.formats.alac', 'convert.formats.alac.command',
    'convert.formats.alac.extension',
    'convert.formats.mp3_320', 'convert.formats.mp3_320.command',
    'convert.formats.mp3_320.extension',
    'importfeeds', 'importfeeds.formats', 'importfeeds.dir',
    'importfeeds.relative_to',
    'dirfields', 'dirfields.field',
}


def _fields(tmp, **over):
    """A valid request against throwaway paths."""
    body = {
        'directory': str(tmp),
        'library': str(tmp / 'library.db'),
        'import_strategy': 'copy',
        'paths': {'artist': '$albumartist', 'album': '$year - $album',
                  'track': '$track - $title'},
        'plugins': ['musicbrainz', 'chroma', 'convert', 'importfeeds'],
        'exclusions': ['Samples', 'Stems'],
        'credentials': {},
    }
    body.update(over)
    return body


def _keys(d, prefix=''):
    for k, v in d.items():
        if v is DROP:
            continue
        yield f'{prefix}{k}'
        if isinstance(v, dict):
            yield from _keys(v, f'{prefix}{k}.')


def test_dangerous_plugins_have_no_preset():
    """inline evaluates Python from the config, hook runs a shell command
    per event, loadext loads a .so; the rest take an executable path. None
    may become a checkbox without someone thinking about this first."""
    overlap = set(configwriter.PLUGIN_PRESETS) & configwriter.NEVER_PRESET
    assert not overlap, f'PLUGIN_PRESETS must not contain {sorted(overlap)}'


def test_unknown_plugin_is_refused(tmp):
    for name in ['hook', 'inline', 'loadext', 'not-a-plugin', '../evil']:
        try:
            configwriter.render(_fields(tmp, plugins=['chroma', name]))
        except configwriter.ConfigError as e:
            assert name in str(e)
        else:
            raise AssertionError(f'{name!r} was accepted into plugins:')


def test_unknown_credential_field_is_refused(tmp):
    try:
        configwriter.render(_fields(
            tmp, credentials={'pluginpath': '/tmp/evil', 'mb_user': 'x'}))
    except configwriter.ConfigError as e:
        assert 'pluginpath' in str(e)
    else:
        raise AssertionError('an unmapped credential field was accepted')


def test_no_request_field_becomes_a_key(tmp):
    """The whole security property, exercised adversarially: unmodelled
    top-level fields, a YAML-shaped exclusion, and values that name the
    known execution vectors. None may appear as a key."""
    hostile = _fields(
        tmp,
        plugins=list(configwriter.PLUGIN_PRESETS),
        exclusions=['Samples', "evil'\n\npluginpath: /tmp/evil\n",
                    'statefile: /tmp/pickle'],
        credentials={'mb_user': 'u', 'mb_pass': 'p',
                     'discogs_token': 't', 'bp4_user': 'b', 'bp4_pass': 'q'},
        bp4={'art': True, 'priority': 0.3},
    )
    hostile['pluginpath'] = '/tmp/evil'
    hostile['statefile'] = '/tmp/pickle'
    hostile['duplicates'] = {'checksum': 'sh -c id'}
    hostile['keyfinder'] = {'bin': '/tmp/evil'}
    hostile['convert'] = {'command': 'id'}

    cfg = configwriter.render(hostile)
    extra = set(_keys(cfg)) - EMITTABLE
    assert not extra, f'emitted keys outside the allowlist: {sorted(extra)}'

    back = yaml.safe_load(configwriter.dumps(configwriter.merge({}, cfg)))
    for key in ('pluginpath', 'statefile'):
        assert key not in back, f'{key} reached the rendered YAML'
    assert 'checksum' not in back.get('duplicates', {})
    assert 'bin' not in back.get('keyfinder', {})
    assert back['convert']['formats']['alac']['command'].startswith('ffmpeg ')
    assert 'command' not in {k for k in back['convert'] if k != 'formats'}


def test_password_with_quote_and_newline_round_trips(tmp):
    """buildConfig() does `'  pass: "' + mbPass + '"'` with no escaping, so
    a quote or newline in a password produces broken — or attacker-shaped —
    YAML in the text the user is told to paste. safe_dump removes the class."""
    nasty = 'p"a\'ss\nlibrary: /tmp/evil.db\n#'
    cfg = configwriter.render(_fields(
        tmp, plugins=['musicbrainz'], credentials={'mb_pass': nasty}))
    back = yaml.safe_load(configwriter.dumps(configwriter.merge({}, cfg)))
    assert back['musicbrainz']['pass'] == nasty
    assert back['library'] == str(tmp / 'library.db'), 'a password rewrote library:'


def test_handwritten_keys_survive_and_risky_ones_are_surfaced(tmp):
    old = {
        'pluginpath': '~/my-plugins',
        'statefile': '~/.config/beets/state.pickle',
        'replace': {'[\\\\/]': '_'},
        'import': {'ignore_hidden': True},
        'paths': {'albumtype:soundtrack': 'Soundtracks/$album'},
        'plugins': ['chroma', 'hook'],
        'hook': {'hooks': [{'event': 'import', 'command': 'say done'}]},
    }
    rendered = configwriter.render(_fields(tmp, plugins=['chroma']))
    merged = configwriter.merge(old, rendered)

    assert merged['replace'] == {'[\\\\/]': '_'}, 'a hand-written block was eaten'
    assert merged['import']['ignore_hidden'] is True, 'sub-key of an owned block lost'
    assert merged['paths']['albumtype:soundtrack'] == 'Soundtracks/$album'
    assert merged['paths']['default'] == '$albumartist/$year - $album/$track - $title'
    assert merged['pluginpath'] == '~/my-plugins', 'preserved, because it is theirs'

    risky = configwriter.preserved_risky(merged, rendered)
    assert 'pluginpath' in risky and 'statefile' in risky, risky
    assert configwriter.dropped_plugins(old, rendered) == ['hook']


def test_owned_key_left_unset_is_cleared(tmp):
    """Clearing the password field must remove `pass:`, not leave the old
    one behind — otherwise Preferences shows empty while the file doesn't."""
    old = {'musicbrainz': {'user': 'old', 'pass': 'old', 'foo': 'mine'}}
    rendered = configwriter.render(_fields(
        tmp, plugins=['musicbrainz'], credentials={'mb_user': 'new'}))
    merged = configwriter.merge(old, rendered)
    assert merged['musicbrainz']['user'] == 'new'
    assert 'pass' not in merged['musicbrainz']
    assert merged['musicbrainz']['foo'] == 'mine'


def test_write_backs_up_and_replaces_atomically(tmp):
    path = tmp / 'config.yaml'
    path.write_text('# hand written\ndirectory: /old\n')
    backup = configwriter.write_config(path, 'directory: /new\n')
    assert path.read_text() == 'directory: /new\n'
    assert Path(backup).read_text() == '# hand written\ndirectory: /old\n'
    assert not (tmp / 'config.yaml.tmp').exists(), 'temp file left behind'
    assert configwriter.count_comments('# a\nb: 1  # c\nd: 2\n') == 2


def _client(tmp):
    return server.app.test_client()


def test_endpoint_dry_run_does_not_write(tmp):
    path = tmp / 'config.yaml'
    path.write_text('# keep me\ndirectory: /old\npluginpath: ~/p\n')
    with mock.patch('server.get_config_path', return_value=str(path)):
        r = _client(tmp).post('/config/save', json=_fields(tmp, dry_run=True),
                              base_url='http://localhost:1612')
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body['ok'] and body['dry_run']
    assert body['comments_dropped'] == 1
    assert body['preserved_risky'] == ['pluginpath']
    assert 'directory: /old' in body['previous']
    assert path.read_text().startswith('# keep me'), 'dry run wrote the file'


def test_endpoint_saves_and_clears_the_path_caches(tmp):
    path = tmp / 'config.yaml'
    path.write_text('directory: /old\nlibrary: /old/library.db\n')
    with mock.patch('server.get_config_path', return_value=str(path)), \
         mock.patch.object(server._resolve_config_path, 'cache_clear') as c1, \
         mock.patch.object(server._resolve_config_key, 'cache_clear') as c2:
        r = _client(tmp).post('/config/save', json=_fields(tmp),
                              base_url='http://localhost:1612')
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body['restart_required'] and body['backup']
    # A saved directory:/library: makes a cached *successful* resolution
    # stale — the same silent misdirection #12 fixed from the other end.
    assert c1.called and c2.called, 'the lru_caches were not cleared on save'
    saved = yaml.safe_load(path.read_text())
    assert saved['directory'] == str(tmp)
    assert saved['import']['ignore'] == ['Samples', 'Stems']


def test_endpoint_refuses_a_bad_request(tmp):
    path = tmp / 'config.yaml'
    path.write_text('directory: /old\n')
    with mock.patch('server.get_config_path', return_value=str(path)):
        r = _client(tmp).post('/config/save',
                              json=_fields(tmp, plugins=['hook']),
                              base_url='http://localhost:1612')
    assert r.status_code == 400, r.data
    assert 'hook' in r.get_json()['error']
    assert path.read_text() == 'directory: /old\n', 'a refused request wrote'


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in tests:
        if fn.__code__.co_argcount == 0:
            fn()
            continue
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
    print('ok')


if __name__ == '__main__':
    main()
