
from unittest.mock import MagicMock

from PyQt5.Qt import QThread

import action as action_module
from action import KoreaderAction

# Every column_* key the Phase 3 loop indexes via CONFIG[config_name] - all
# real dict access (not .get()), so every key must be present or it raises
# KeyError.
ALL_COLUMN_CONFIG_KEYS = [
    'column_percent_read', 'column_percent_read_int', 'column_status',
    'column_status_bool', 'column_last_read_location',
    'column_date_book_started', 'column_date_book_finished',
    'column_rating', 'column_review', 'column_bookmarks', 'column_md5',
    'column_device_name', 'column_device_id', 'column_date_synced',
    'column_date_sidecar_modified', 'column_sidecar',
]


def _config(**overrides):
    base = {key: '' for key in ALL_COLUMN_CONFIG_KEYS}
    base.update({
        'checkbox_sync_if_more_recent': False,
        'checkbox_no_sync_if_finished': False,
    })
    base.update(overrides)
    return base


class FakeMetadata(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def set(self, key, value):
        self[key] = value


def _book_info(sidecar_path='Book.sdr/metadata.epub.lua', uuid='device-uuid', app_id=1, in_library='UUID'):
    return {
        'sidecar_path': sidecar_path,
        'uuid': uuid,
        'application_id': app_id,
        'in_library': in_library,
        'db_id': None,
        'title': 'Device Title',
        'path': sidecar_path.replace('.sdr/metadata.epub.lua', '.epub'),
    }


def _action():
    action = KoreaderAction(MagicMock(), MagicMock())
    action.extension_callback = None
    action.device_ready = True
    action.get_connected_device = MagicMock(return_value=MagicMock())
    action.check_device = MagicMock(return_value=True)
    return action


def _run_koworker_synchronously(monkeypatch):
    # KOSyncWorker is a QThread subclass defined locally inside
    # sync_to_calibre(). Patch QThread.start to just call the instance's
    # own run() directly on the calling thread, avoiding any real
    # background-thread scheduling to race against (qapp still provides
    # the QApplication signals/slots need).
    def synchronous_start(self):
        self.run()
    monkeypatch.setattr(QThread, 'start', synchronous_start)


def test_sync_to_calibre_applies_device_value_via_real_thread(qapp, monkeypatch):
    # The one test exercising the full path end-to-end: the real
    # KOSyncWorker(QThread), matching via resolve_book_id(), applying the
    # device's value to Calibre.
    _run_koworker_synchronously(monkeypatch)
    monkeypatch.setattr(action_module, 'CONFIG', _config(column_percent_read_int='#ko_progint'))

    action = _action()
    action.get_paths = MagicMock(return_value={1: _book_info()})
    action.get_sidecar = MagicMock(return_value={'percent_finished': 0.75})

    metadata = FakeMetadata({'#ko_progint': 50, 'uuid': 'calibre-uuid', 'title': 'Calibre Title'})
    action.gui.current_db.new_api.get_metadata.return_value = metadata

    action.sync_to_calibre(silent=True)

    # transform: round(0.75 * 100) == 75
    assert metadata['#ko_progint'] == 75
    action.gui.current_db.new_api.set_metadata.assert_called_once()


def test_sync_to_calibre_skips_book_with_no_sidecar(qapp, monkeypatch):
    _run_koworker_synchronously(monkeypatch)
    monkeypatch.setattr(action_module, 'CONFIG', _config())

    action = _action()
    action.get_paths = MagicMock(return_value={1: _book_info()})
    action.gui.current_db.new_api.get_metadata.return_value = FakeMetadata({'title': 'Calibre Title'})

    from action import GetSidecarStatus
    action.get_sidecar = MagicMock(return_value=GetSidecarStatus.PATH_NOT_FOUND)

    action.sync_to_calibre(silent=True)

    action.gui.current_db.new_api.set_metadata.assert_not_called()


def test_sync_to_calibre_skips_book_calibre_and_uuid_lookup_both_fail(qapp, monkeypatch):
    _run_koworker_synchronously(monkeypatch)
    monkeypatch.setattr(action_module, 'CONFIG', _config())

    action = _action()
    # in_library falsy and no application_id -> resolve_book_id() returns
    # None; db.lookup_by_uuid() also finds nothing -> genuinely unmatched.
    action.get_paths = MagicMock(return_value={
        'Author/Book.epub': _book_info(app_id=None, in_library=None)
    })
    action.gui.current_db.new_api.lookup_by_uuid.return_value = None

    action.sync_to_calibre(silent=True)

    action.gui.current_db.new_api.get_metadata.assert_not_called()
    action.gui.current_db.new_api.set_metadata.assert_not_called()


def test_sync_to_calibre_falls_back_to_device_uuid_lookup(qapp, monkeypatch):
    # If Calibre's own matching cache is stale relative to the live library
    # (e.g. a book was added to the library after the device connected -
    # set_books_in_library() only refreshes its cache on reconnect),
    # resolve_book_id() finds nothing even though db.lookup_by_uuid() would
    # still resolve it live. Fall back to that rather than skipping - matches
    # what upstream `main` always did as its primary (only) lookup.
    _run_koworker_synchronously(monkeypatch)
    monkeypatch.setattr(action_module, 'CONFIG', _config(column_percent_read_int='#ko_progint'))

    action = _action()
    action.get_paths = MagicMock(return_value={
        1: _book_info(app_id=None, in_library=None, uuid='device-uuid')
    })
    action.get_sidecar = MagicMock(return_value={'percent_finished': 0.75})
    action.gui.current_db.new_api.lookup_by_uuid.return_value = 42

    metadata = FakeMetadata({'#ko_progint': 50, 'uuid': 'calibre-uuid', 'title': 'Calibre Title'})
    action.gui.current_db.new_api.get_metadata.return_value = metadata

    action.sync_to_calibre(silent=True)

    action.gui.current_db.new_api.lookup_by_uuid.assert_called_once_with('device-uuid')
    assert metadata['#ko_progint'] == 75
    action.gui.current_db.new_api.set_metadata.assert_called_once()


def test_device_not_ready_aborts_without_touching_device(qapp, monkeypatch):
    # Regression test for the manual-sync race: a click right after
    # connecting, before Calibre has finished set_books_in_library(), must
    # not proceed with a broken/empty get_paths() result.
    monkeypatch.setattr(action_module, 'CONFIG', _config())
    action = _action()
    action.device_ready = False
    action.get_paths = MagicMock()
    action_module.info_dialog.reset_mock()

    action.sync_to_calibre(silent=False)

    action.get_paths.assert_not_called()
    action_module.info_dialog.assert_called_once()


def test_device_not_ready_stays_silent_when_silent(qapp, monkeypatch):
    monkeypatch.setattr(action_module, 'CONFIG', _config())
    action = _action()
    action.device_ready = False
    action.get_paths = MagicMock()
    action_module.info_dialog.reset_mock()

    action.sync_to_calibre(silent=True)

    action.get_paths.assert_not_called()
    action_module.info_dialog.assert_not_called()


def test_unreadable_book_list_aborts_cleanly(qapp, monkeypatch):
    # get_paths() itself shows an error_dialog and returns None when
    # memory_view can't be read; this must not crash trying to iterate it.
    monkeypatch.setattr(action_module, 'CONFIG', _config())
    action = _action()
    action.get_paths = MagicMock(return_value=None)

    action.sync_to_calibre(silent=True)

    action.gui.current_db.new_api.set_metadata.assert_not_called()
