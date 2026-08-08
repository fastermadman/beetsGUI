#!/usr/bin/env python3
"""
Unit test for #63: actions take a scope, not a hand-typed beets query.

Three things here are load-bearing enough to break silently:

1. The comma in `id:12 , id:88` needs the spaces around it — `id:12,id:88`
   is a single keyword and beets parses it as one invalid numeric term.
   Asserted against beets' own parser, not against the string, so a beets
   change to the OR syntax fails here instead of in production.
2. An empty or oversized `ids` list must not become a query. Empty would
   shlex to `[]`, which is "the whole library" — the exact unbounded-delete
   shape this issue exists to close.
3. Preview and apply must resolve the *same* scope to the same query.
   Checked through the real endpoints, because that pairing is a property of
   the two handlers, not of scope_query().

Run: ~/.local/pipx/venvs/beets/bin/python test_scope.py
"""
from beets.dbcore.queryparse import parse_sorted_query
from beets.library import Item
from beets.ui import UserError

import libops
import server


def test_scope_query():
    # Legacy string still works — this adds a shape, it doesn't remove one.
    assert libops.scope_query({'query': 'albumartist:Burial'}) == 'albumartist:Burial'
    assert libops.scope_query({}) == ''
    assert libops.scope_query({'scope': {'query': 'album:Untrue'}}) == 'album:Untrue'
    # A scope wins over a stale top-level query rather than merging with it.
    assert libops.scope_query({'scope': {'query': 'a'}, 'query': 'b'}) == 'a'

    for bad, why in (
        ({'scope': 'albumartist:Burial'}, 'a bare string scope'),
        ({'scope': {'ids': 12}}, 'a non-list ids'),
        ({'scope': {'ids': ['12']}}, 'string ids'),
        ({'scope': {'ids': [True]}}, 'a bool id (bool is an int)'),
        ({'scope': {'ids': []}}, 'an empty ids list'),
        ({'scope': {'ids': list(range(libops.MAX_SCOPE_IDS + 1))}}, 'oversized ids'),
    ):
        try:
            libops.scope_query(bad)
            assert False, f'{why} should have been rejected'
        except UserError:
            pass

    # Exactly at the cap is fine — the boundary is inclusive.
    assert libops.scope_query(
        {'scope': {'ids': list(range(1, libops.MAX_SCOPE_IDS + 1))}})


def test_ids_become_an_or_query():
    """The ids scope selects those ids and nothing else — per beets."""
    parts = libops.split_query(libops.scope_query({'scope': {'ids': [12, 88, 341]}}))
    assert parts == ['id:12', ',', 'id:88', ',', 'id:341'], parts
    query, _ = parse_sorted_query(Item, parts)
    sql, subvals = query.clause()
    assert sql.count('items.id=?') == 3, sql
    assert ' or ' in sql, sql
    assert subvals == [12, 88, 341], subvals


def _client():
    server.app.testing = True
    # _reject_foreign_host() checks Host against the loopback origin, so the
    # test client has to speak as that origin or every request is a 403.
    server.app.config['SERVER_NAME'] = f'localhost:{server.PORT}'
    return server.app.test_client()


def test_preview_and_apply_share_a_scope():
    """Same scope in, same query out of both halves of each pair."""
    client = _client()
    scope = {'scope': {'ids': [12, 88]}}
    seen = []

    libops.preview_remove = lambda q: seen.append(q) or [{'id': 12}, {'id': 88}]
    libops.remove = lambda q, delete_files: seen.append(q)
    libops.preview_modify = lambda f, v, q: seen.append(q) or []
    libops.apply_modify = lambda f, v, q: seen.append(q) or 0

    for url, body in (
        ('/library/remove/preview', scope),
        # remove previews again internally to check `expect`, so it appends
        # the query twice — both are the query it is about to act on.
        ('/library/remove', {**scope, 'expect': 2}),
        ('/library/modify/preview', {**scope, 'field': 'artist', 'value': 'x'}),
        ('/library/modify', {**scope, 'field': 'artist', 'value': 'x'}),
    ):
        r = client.post(url, json=body)
        assert r.status_code == 200, (url, r.status_code, r.get_json())

    assert seen and len(set(seen)) == 1, seen
    assert seen[0] == 'id:12 , id:88', seen[0]


def test_bad_scope_is_a_400():
    """Not a 500, and not a silently-widened query."""
    client = _client()
    for url, body in (
        ('/library/remove/preview', {'scope': {'ids': []}}),
        ('/library/remove', {'scope': {'ids': list(range(999))}, 'expect': 1}),
        ('/library/modify', {'scope': {'ids': []}, 'field': 'artist', 'value': 'x'}),
        ('/library/update', {'scope': {'ids': []}}),
        ('/library/write', {'scope': {'ids': []}}),
        ('/artwork/start', {'action': 'fetch', 'scope': {'ids': []}}),
        ('/convert/start', {'scope': {'ids': []}}),
        ('/sync/start', {'kind': 'mbsync', 'scope': {'ids': []}}),
    ):
        r = client.post(url, json=body)
        assert r.status_code == 400, (url, r.status_code, r.get_json())
        assert r.get_json()['ok'] is False, url

    # An empty body is still "no scope" (the legacy default), not an error —
    # /library/remove is the one that refuses it, and it already did.
    assert client.post('/library/remove', json={'expect': 0}).status_code == 400


def main():
    test_scope_query()
    test_ids_become_an_or_query()
    test_preview_and_apply_share_a_scope()
    test_bad_scope_is_a_400()
    print('ok')


if __name__ == '__main__':
    main()
