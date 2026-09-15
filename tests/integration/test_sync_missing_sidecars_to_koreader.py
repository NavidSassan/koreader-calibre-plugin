
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


def _book_info(sidecar_path, uuid, app_id):
    return {
        'sidecar_path': sidecar_path,
        'uuid': uuid,
        'application_id': app_id,
        'in_library': 'UUID',
        'db_id': None,
        'title': f'Book {app_id}',
        'path': sidecar_path.replace('.sdr/metadata.epub.lua', '.epub'),
    }


def test_device_path_exists_correctly_splits_books(monkeypatch):
    # Regression test for this session's fix: the split used to be a raw
    # os.path.exists(sidecar_path) call, which only ever resolves True for
    # USB/Folder devices. A device whose native .exists() driver method
    # disagrees with the local filesystem (as any wireless driver would)
    # must still be split correctly.
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


def test_book_without_sidecar_dispatches_to_push_metadata(monkeypatch):
    action = _action(monkeypatch)
    action.get_paths = MagicMock(return_value={
        1: _book_info('Missing.sdr/metadata.epub.lua', 'uuid-1', 1),
    })
    device = action.get_connected_device.return_value
    device.exists = MagicMock(return_value=False)
    action.push_metadata_to_koreader_sidecar = MagicMock(return_value=('success', {}))
    action.gui.current_db.new_api.get_metadata.return_value = FakeMetadata(uuid='calibre-uuid', title='T')

    action.sync_missing_sidecars_to_koreader(silent=True)

    action.push_metadata_to_koreader_sidecar.assert_called_once_with(
        device, 'calibre-uuid', 'Missing.sdr/metadata.epub.lua'
    )


def test_book_with_matching_sidecar_and_no_conflicts_is_left_alone(monkeypatch):
    action = _action(monkeypatch)
    action.get_paths = MagicMock(return_value={
        1: _book_info('Existing.sdr/metadata.epub.lua', 'uuid-1', 1),
    })
    device = action.get_connected_device.return_value
    device.exists = MagicMock(return_value=True)
    action.push_metadata_to_koreader_sidecar = MagicMock()
    action.update_sidecar_fields = MagicMock()
    action.get_sidecar = MagicMock(return_value={})
    action.detect_conflicts = MagicMock(return_value=[])
    action.gui.current_db.new_api.get_metadata.return_value = FakeMetadata(uuid='calibre-uuid', title='T')

    action.sync_missing_sidecars_to_koreader(silent=True)

    action.push_metadata_to_koreader_sidecar.assert_not_called()
    action.update_sidecar_fields.assert_not_called()


def test_resolved_conflict_applies_calibre_value_to_sidecar(monkeypatch):
    from action import ConflictItem

    class FakeConflictDialog:
        def __init__(self, gui, conflicts, direction):
            self._conflicts = conflicts

        def exec_(self):
            from PyQt5.Qt import QDialog
            return QDialog.Accepted

        def get_resolved_conflicts(self):
            resolved = []
            for c in self._conflicts:
                c.resolution = 'calibre'
                resolved.append(c)
            return resolved

    monkeypatch.setattr(action_module, 'ConflictResolutionDialog', FakeConflictDialog)
    # Real QDialog subclass - would reject the MagicMock gui as its parent
    # widget. We only care that update_sidecar_fields got called correctly,
    # not about the final results summary dialog.
    monkeypatch.setattr(action_module, 'SyncCompletionDialog', MagicMock())

    action = _action(monkeypatch)
    action.get_paths = MagicMock(return_value={
        1: _book_info('Existing.sdr/metadata.epub.lua', 'uuid-1', 1),
    })
    device = action.get_connected_device.return_value
    device.exists = MagicMock(return_value=True)
    action.get_sidecar = MagicMock(return_value={'percent_finished': 0.1})
    conflict = ConflictItem(
        book_uuid='calibre-uuid', book_title='T', sidecar_path='Existing.sdr/metadata.epub.lua',
        field_name='column_percent_read', field_display_name='Progress',
        calibre_value=0.9, device_value=0.1,
    )
    action.detect_conflicts = MagicMock(return_value=[conflict])
    action.update_sidecar_fields = MagicMock(return_value=('success', {'fields_updated': ['column_percent_read']}))
    action.gui.current_db.new_api.get_metadata.return_value = FakeMetadata(uuid='calibre-uuid', title='T')

    action.sync_missing_sidecars_to_koreader(silent=False)

    action.update_sidecar_fields.assert_called_once_with(
        device, 'Existing.sdr/metadata.epub.lua', {'percent_finished': 0.1}, {'column_percent_read': 0.9}
    )


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
