
from unittest.mock import MagicMock

import action as action_module
from action import KoreaderAction


class MockBook:
    def __init__(self, path, uuid=None, application_id=None, in_library=None, db_id=None, title='Unknown'):
        self.path = path
        self.uuid = uuid
        self.application_id = application_id
        self.in_library = in_library
        self.db_id = db_id
        self.title = title


def _action():
    return KoreaderAction(MagicMock(), MagicMock())


def test_memory_view_primary_path_uses_in_library_annotations():
    action = _action()
    book = MockBook(
        path="Author/Some Book.epub",
        uuid='book-uuid-1',
        application_id=7,
        in_library='UUID',
        db_id=None,
        title='Some Book',
    )
    action.gui.memory_view.model.return_value.db = [book]

    paths = action.get_paths()

    assert len(paths) == 1
    entry = paths[7]
    assert entry['sidecar_path'] == "Author/Some Book.sdr/metadata.epub.lua"
    assert entry['uuid'] == 'book-uuid-1'
    assert entry['application_id'] == 7
    assert entry['in_library'] == 'UUID'
    assert entry['title'] == 'Some Book'


def test_memory_view_unreadable_shows_error_and_returns_none():
    # No fallback to device.books(): matching via device.books() alone only
    # ever covers application_id matches, making which books synced depend
    # on unpredictable internal GUI state. A clear error is preferable.
    action = _action()
    action.gui.memory_view.model.side_effect = Exception('no model in this test double')
    action_module.error_dialog.reset_mock()

    paths = action.get_paths()

    assert paths is None
    action_module.error_dialog.assert_called_once()


def test_hidden_folder_is_skipped():
    action = _action()
    book = MockBook(
        path=".hidden/Secret Book.epub",
        uuid='hidden-uuid',
        application_id=1,
        in_library='UUID',
    )
    action.gui.memory_view.model.return_value.db = [book]

    paths = action.get_paths()

    assert len(paths) == 0
