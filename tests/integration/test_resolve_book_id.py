
from unittest.mock import MagicMock

from action import KoreaderAction


def _action():
    return KoreaderAction(MagicMock(), MagicMock())


def test_db_id_match_uses_db_id():
    action = _action()
    book_id = action.resolve_book_id({
        'in_library': 'DB_ID', 'db_id': 42, 'application_id': 99,
    })
    assert book_id == 42


def test_db_id_match_without_db_id_falls_back_to_app_id():
    # in_library == 'DB_ID' but db_id missing: the DB_ID branch's guard
    # (`db_id is not None`) fails, so it falls through to application_id.
    action = _action()
    book_id = action.resolve_book_id({
        'in_library': 'DB_ID', 'db_id': None, 'application_id': '7',
    })
    assert book_id == 7


def test_uuid_match_uses_application_id():
    action = _action()
    book_id = action.resolve_book_id({
        'in_library': 'UUID', 'db_id': None, 'application_id': 3,
    })
    assert book_id == 3


def test_author_match_uses_application_id():
    # Calibre's set_books_in_library() sets application_id for AUTHOR /
    # AUTH_SORT matches the same way it does for UUID/APP_ID matches, so
    # resolve_book_id's generic app_id fallback covers these too.
    action = _action()
    book_id = action.resolve_book_id({
        'in_library': 'AUTHOR', 'db_id': None, 'application_id': 5,
    })
    assert book_id == 5


def test_auth_sort_match_uses_application_id():
    action = _action()
    book_id = action.resolve_book_id({
        'in_library': 'AUTH_SORT', 'db_id': None, 'application_id': 6,
    })
    assert book_id == 6


def test_unmatched_book_returns_none():
    action = _action()
    for in_library in (None, False, ''):
        book_id = action.resolve_book_id({
            'in_library': in_library, 'db_id': 1, 'application_id': 1,
        })
        assert book_id is None


def test_non_numeric_application_id_returns_none():
    action = _action()
    book_id = action.resolve_book_id({
        'in_library': 'APP_ID', 'db_id': None, 'application_id': 'not-a-number',
    })
    assert book_id is None


def test_matched_but_no_usable_id_returns_none():
    action = _action()
    book_id = action.resolve_book_id({
        'in_library': 'UUID', 'db_id': None, 'application_id': None,
    })
    assert book_id is None
