#!/usr/bin/env python3
"""
Unit test for #61: /library/modify rejects unknown fields.

The datalist input that replaced the field <select> is free text again —
exactly the hazard the dropdown had closed. Before this, an unknown field
name landed as a beets flexible attribute that never reached the file (the
genre/genres bug, hidden for four releases). _resolve_field() must now
reject anything outside beets' known field list, and it must do so before
ever touching the library — proven here by making get_library() explode if
reached, rather than just asserting the (still-empty) result.

Run: ~/.local/pipx/venvs/beets/bin/python test_libops.py
"""
from beets.ui import UserError

import libops


def test_split_query_apostrophes():
    """An apostrophe is a letter here, not an unclosed quote (#12).

    shlex reads `Don't` as the start of a quoted string and raises, which
    used to surface as a 400 from /library?q= — for a search term that is
    entirely ordinary in a music library. Balanced quoting still has to keep
    working, because that is what the UI itself generates.
    """
    for q, expected in [
        ("O'Brien", ["O'Brien"]),
        ("Don't Stop", ["Don't", 'Stop']),
        ("Guns N' Roses", ['Guns', "N'", 'Roses']),
        ("artist:O'Brien year:2020..", ["artist:O'Brien", 'year:2020..']),
        # What the UI compiles is balanced and must be unquoted, not split.
        ("artist:'it'\\''s here'", ['artist:it\'s here']),
        ('album:"a quoted bit"', ['album:a quoted bit']),
        ('', []),
    ]:
        assert libops.split_query(q) == expected, (
            f'split_query({q!r}) == {libops.split_query(q)!r}, want {expected!r}')


def main():
    test_split_query_apostrophes()

    libops.get_library = lambda: (_ for _ in ()).throw(
        AssertionError('should not reach the library'))

    for fn in (libops.preview_modify, libops.apply_modify):
        try:
            fn('not_a_real_field', 'x', '')
            assert False, f'{fn.__name__} should have rejected an unknown field'
        except UserError as e:
            assert 'not_a_real_field' in str(e), e

    # A real field passes validation and falls through to the library call
    # (proven by the patched get_library() firing, not a silent pass).
    try:
        libops.apply_modify('artist', 'x', '')
        assert False, 'expected get_library() to be reached for a real field'
    except AssertionError as e:
        assert 'should not reach' in str(e)

    # The legacy singular->plural translation runs before the reject check.
    try:
        libops.apply_modify('genre', 'x', '')
        assert False, 'expected get_library() to be reached for genre->genres'
    except AssertionError as e:
        assert 'should not reach' in str(e)

    # Still-protected fields keep their own, more specific message.
    try:
        libops.apply_modify('path', 'x', '')
        assert False, 'path should still be rejected'
    except UserError as e:
        assert 'editable metadata field' in str(e), e

    print('ok')


if __name__ == '__main__':
    main()
