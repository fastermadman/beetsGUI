#!/usr/bin/env python3
"""
Unit test for the config-path resolvers in server.py (#12).

beets >= 2.13 can run a one-time schema migration as a side effect of ANY
`beet` invocation against a library that hasn't been opened this way before
— including a brand-new one, so this hits every fresh beetsGUI install on
its very first request. It prints "Created database backup at: ..." lines
to *stdout*, ahead of the actual path `beet config --path` returns.

_resolve_config_path() used to do `r.stdout.strip()` and trust the whole
multi-line result as a path. That "path" doesn't exist on disk, so
get_library_db_path()/get_library_directory() silently fell through to
their hardcoded defaults — `~/.config/beets/library.db`, the user's real
library — and because all three functions were `@functools.lru_cache`d
with no distinction between a real answer and a fallback, that wrong
answer stuck for the rest of the server's life. Confirmed live: with
`/unimported` (which is what triggers the first `get_library_db_path()`
call in the Inbox tab's normal first-use flow) called before any import,
every subsequent `/library` search silently searched the wrong database
and reported zero results, `ok: true`, no error.

Run: ~/.local/pipx/venvs/beets/bin/python test_config_path.py
"""
import subprocess
from unittest import mock

import server


def _reset_caches():
    server._resolve_config_path.cache_clear()
    server._resolve_config_key.cache_clear()


def _fake_run(stdout, returncode=0):
    return mock.Mock(stdout=stdout, stderr='', returncode=returncode)


def test_migration_noise_on_stdout_is_stripped(tmp_config):
    """The path is always the first line; trailing migration/backup notices
    on later lines must not become part of it."""
    _reset_caches()
    noisy = (f'{tmp_config}\n'
             "Created database backup at: '/tmp/x/library.db-before-y.bak'.\n"
             "Created database backup at: '/tmp/x/library.db-before-z.bak'.\n")
    with mock.patch('server.subprocess.run', return_value=_fake_run(noisy)):
        assert server.get_config_path() == str(tmp_config)


def test_unusable_output_is_not_cached_as_success(tmp_path):
    """A failed resolution (non-existent 'path', non-zero exit, empty
    stdout) must be retried on the next call, not remembered forever — the
    whole point of this fix. Simulated here as: first call fails, second
    call (as if the transient cause had cleared) succeeds.
    """
    _reset_caches()
    good = tmp_path / 'config.yaml'
    good.write_text('directory: ~/Music\nlibrary: ~/Music/library.db\n')

    calls = [_fake_run('/does/not/exist/config.yaml\n'),  # first: unusable
             _fake_run(f'{good}\n')]                        # second: real
    with mock.patch('server.subprocess.run', side_effect=calls):
        first = server.get_config_path()
        second = server.get_config_path()

    # The bug: first (bad) call would get cached and returned forever, so
    # `second` would equal `first` instead of the real path. `first`'s
    # exact value is just get_config_path()'s fallback (not asserted
    # against a redundant re-derivation here — os.path.expanduser and
    # Path.expanduser aren't guaranteed byte-identical on every platform);
    # what actually matters is that it is *not* the real config, and that
    # the retry finds the real one instead of repeating it.
    assert first != str(good), f'first call should have hit the fallback, not {good}'
    assert second == str(good), (
        f'a failed resolution was cached — second call returned {second!r} '
        f'instead of retrying and finding the real config')


def test_library_db_path_reads_through_the_retrying_resolver(tmp_path):
    """get_library_db_path() must not read a *different*, independently
    parseable fallback config (~/.config/beets/config.yaml, which may well
    exist and have its own real library: line) and cache that as if it
    were correct — it has to go through the same retry-until-genuinely-
    resolved path get_config_path() uses.
    """
    _reset_caches()
    real = tmp_path / 'real' / 'config.yaml'
    real.parent.mkdir()
    real.write_text('directory: /throwaway/music\nlibrary: /throwaway/library.db\n')

    calls = [_fake_run('garbage, not a real path\n'),  # unimported's first call
             _fake_run(f'{real}\n')]                     # library's later call
    with mock.patch('server.subprocess.run', side_effect=calls):
        first = server.get_library_db_path()   # sees the bug's failure mode
        second = server.get_library_db_path()   # must retry, not reuse `first`

    assert second == '/throwaway/library.db', (
        f'expected the real throwaway library.db, got {second!r} — '
        f'a failed first call was cached as if it had succeeded')
    assert first != second, (
        'first call should have hit the (uncached, harmless) fallback, '
        'not the same wrong value forever')


def main():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        tmp_config = Path(d) / 'config.yaml'
        tmp_config.write_text('directory: ~/Music\nlibrary: ~/Music/library.db\n')
        test_migration_noise_on_stdout_is_stripped(tmp_config)

    with tempfile.TemporaryDirectory() as d:
        test_unusable_output_is_not_cached_as_success(Path(d))

    with tempfile.TemporaryDirectory() as d:
        test_library_db_path_reads_through_the_retrying_resolver(Path(d))

    _reset_caches()
    print('ok')


if __name__ == '__main__':
    main()
