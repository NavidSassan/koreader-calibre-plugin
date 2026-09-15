
from unittest.mock import MagicMock

import action as action_module
from action import KoreaderAction


def _config(**overrides):
    base = {'column_sidecar': '#ko_sidecar'}
    base.update(overrides)
    return base


class FakeMetadata(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _action(monkeypatch, config=None):
    monkeypatch.setattr(action_module, 'CONFIG', config or _config())
    action = KoreaderAction(MagicMock(), MagicMock())
    action.get_connected_device = MagicMock(return_value=MagicMock())
    action.check_device = MagicMock(return_value=True)
    # Normally set once device_metadata_available fires; see genesis().
    action.device_ready = True
    return action


def _book_info(sidecar_path, uuid, app_id, in_library='UUID'):
    return {
        'sidecar_path': sidecar_path,
        'uuid': uuid,
        'application_id': app_id,
        'in_library': in_library,
        'db_id': None,
        'title': f'Book {app_id}',
        'path': sidecar_path.replace('.sdr/metadata.epub.lua', '.epub'),
    }


def test_device_path_exists_correctly_splits_books(monkeypatch):
    # A mocked device whose native .exists() driver method differs per
    # path (as any wireless driver would) must still be split correctly -
    # device_path_exists() works for both USB/Folder and wireless, unlike
    # a raw os.path.exists() check would.
    action = _action(monkeypatch)
    action.get_paths = MagicMock(return_value={
        1: _book_info('Book1.sdr/metadata.epub.lua', 'uuid-1', 1),
        2: _book_info('Book2.sdr/metadata.epub.lua', 'uuid-2', 2),
    })
    action.push_metadata_to_koreader_sidecar = MagicMock(return_value=('success', {}))

    device = action.get_connected_device.return_value
    device.exists = MagicMock(side_effect=lambda path: path == 'Book1.sdr/metadata.epub.lua')
    action.gui.current_db.new_api.get_metadata.return_value = FakeMetadata(uuid='calibre-uuid', title='T')

    action.sync_missing_sidecars_to_koreader(silent=True)

    # Only book 2 (device.exists() == False) should be treated as missing
    # a sidecar and go through the create-new path.
    action.push_metadata_to_koreader_sidecar.assert_called_once()
    args = action.push_metadata_to_koreader_sidecar.call_args.args
    assert args[2] == 'Book2.sdr/metadata.epub.lua'


def test_matched_book_uses_calibre_uuid_not_device_uuid(monkeypatch):
    # Regression test for issues #94, #99, #115, #165: once Calibre has
    # matched the book via resolve_book_id(), use its own uuid rather than
    # trusting the device's potentially stale/missing one.
    action = _action(monkeypatch)
    action.get_paths = MagicMock(return_value={
        1: _book_info('Missing.sdr/metadata.epub.lua', 'stale-device-uuid', 1),
    })
    device = action.get_connected_device.return_value
    device.exists = MagicMock(return_value=False)
    action.push_metadata_to_koreader_sidecar = MagicMock(return_value=('success', {}))
    action.gui.current_db.new_api.get_metadata.return_value = FakeMetadata(uuid='correct-calibre-uuid', title='T')

    action.sync_missing_sidecars_to_koreader(silent=True)

    action.push_metadata_to_koreader_sidecar.assert_called_once_with(
        device, 'correct-calibre-uuid', 'Missing.sdr/metadata.epub.lua'
    )


def test_unmatched_book_falls_back_to_device_uuid(monkeypatch):
    # If Calibre couldn't match the book at all, fall back to the device's
    # own uuid (same as before this change) rather than skipping outright -
    # push_metadata_to_koreader_sidecar does its own lookup as a last resort.
    action = _action(monkeypatch)
    action.get_paths = MagicMock(return_value={
        1: _book_info('Missing.sdr/metadata.epub.lua', 'device-uuid', None, in_library=None),
    })
    device = action.get_connected_device.return_value
    device.exists = MagicMock(return_value=False)
    action.push_metadata_to_koreader_sidecar = MagicMock(return_value=('success', {}))

    action.sync_missing_sidecars_to_koreader(silent=True)

    action.push_metadata_to_koreader_sidecar.assert_called_once_with(
        device, 'device-uuid', 'Missing.sdr/metadata.epub.lua'
    )
    action.gui.current_db.new_api.get_metadata.assert_not_called()


def test_no_pushable_columns_shows_error_and_returns_early(monkeypatch):
    action = _action(monkeypatch, config=_config(column_sidecar=''))
    action.get_paths = MagicMock()
    # error_dialog is a module-level mock shared across the whole test
    # session (calibre.gui2 is mocked once at import time) - reset it so
    # this assertion doesn't depend on what other tests triggered first.
    action_module.error_dialog.reset_mock()

    action.sync_missing_sidecars_to_koreader(silent=True)

    action_module.error_dialog.assert_called_once()
    action.get_paths.assert_not_called()


def test_device_not_ready_aborts_without_touching_device(monkeypatch):
    # Regression test for the manual-sync race: a click right after
    # connecting, before Calibre has finished set_books_in_library(), must
    # not proceed with a broken/empty get_paths() result.
    action = _action(monkeypatch)
    action.device_ready = False
    action.get_paths = MagicMock()
    action_module.info_dialog.reset_mock()

    action.sync_missing_sidecars_to_koreader(silent=False)

    action.get_paths.assert_not_called()
    action_module.info_dialog.assert_called_once()


def test_device_not_ready_stays_silent_when_silent(monkeypatch):
    action = _action(monkeypatch)
    action.device_ready = False
    action.get_paths = MagicMock()
    action_module.info_dialog.reset_mock()

    action.sync_missing_sidecars_to_koreader(silent=True)

    action.get_paths.assert_not_called()
    action_module.info_dialog.assert_not_called()


def test_unreadable_book_list_aborts_cleanly(monkeypatch):
    # get_paths() itself shows an error_dialog and returns None when
    # memory_view can't be read; this must not crash trying to iterate it.
    action = _action(monkeypatch)
    action.get_paths = MagicMock(return_value=None)

    action.sync_missing_sidecars_to_koreader(silent=True)

    action.gui.current_db.new_api.get_metadata.assert_not_called()
