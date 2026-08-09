#!/usr/bin/env python3
"""
Unit test for /pick (#77) — the native Finder-panel endpoint.

Never calls real osascript (that would pop a modal panel and hang the
test); server.subprocess.run is mocked, same pattern as test_config_path.py.

Run: ~/.local/pipx/venvs/beets/bin/python test_pick.py
"""
from unittest import mock

import server


def _fake_run(stdout='', returncode=0):
    return mock.Mock(stdout=stdout, returncode=returncode)


def _client():
    # _reject_foreign_host() checks Host against the loopback origin, so the
    # test client has to speak as that origin or every request is a 403
    # (same pattern as test_scope.py's _client()).
    server.app.testing = True
    server.app.config['SERVER_NAME'] = f'localhost:{server.PORT}'
    return server.app.test_client()


def test_folder_returns_path():
    client = _client()
    with mock.patch('server.subprocess.run', return_value=_fake_run('/Volumes/USB\n')):
        r = client.post('/pick', json={'kind': 'folder'})
    assert r.get_json() == {'ok': True, 'path': '/Volumes/USB'}


def test_folders_multi_select_stays_comma_joined_from_the_script():
    """The 'folders' kind (traktor-roots) already comes back comma-joined
    from the AppleScript itself — the endpoint must not re-split or
    re-join it, just strip trailing whitespace."""
    client = _client()
    joined = '/Volumes/A,/Volumes/B'
    with mock.patch('server.subprocess.run', return_value=_fake_run(joined + '\n')):
        r = client.post('/pick', json={'kind': 'folders'})
    assert r.get_json() == {'ok': True, 'path': joined}


def test_cancel_is_not_an_error():
    """AppleScript raises when the user hits Cancel — that's a non-zero
    exit, not a server fault, and must not surface as one."""
    client = _client()
    with mock.patch('server.subprocess.run', return_value=_fake_run(returncode=1)):
        r = client.post('/pick', json={'kind': 'folder'})
    assert r.get_json() == {'ok': False}


def test_bad_kind_is_rejected_before_shelling_out():
    client = _client()
    with mock.patch('server.subprocess.run') as run:
        r = client.post('/pick', json={'kind': 'nonsense'})
    run.assert_not_called()
    assert r.status_code == 400
    assert r.get_json()['ok'] is False


def main():
    test_folder_returns_path()
    test_folders_multi_select_stays_comma_joined_from_the_script()
    test_cancel_is_not_an_error()
    test_bad_kind_is_rejected_before_shelling_out()
    print('ok')


if __name__ == '__main__':
    main()
