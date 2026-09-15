
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import action as action_module
from action import KoreaderAction, OperationStatus


def _config(**overrides):
    base = {
        'column_percent_read': '',
        'column_percent_read_int': '',
        'checkbox_sync_if_more_recent': False,
        'column_date_sidecar_modified': '',
        'column_status': '',
        'checkbox_no_sync_if_finished': False,
        'column_status_bool': '',
    }
    base.update(overrides)
    return base


class FakeMetadata:
    def __init__(self, values=None):
        self._values = dict(values or {})
        self.set_calls = {}

    def get(self, key):
        return self._values.get(key)

    def set(self, key, value):
        self.set_calls[key] = value
        self._values[key] = value


def _action(monkeypatch, config=None, metadata=None, lookup_result=None):
    monkeypatch.setattr(action_module, 'CONFIG', config or _config())
    action = KoreaderAction(MagicMock(), MagicMock())
    # Normally set by genesis(), which we don't call in these tests.
    action.extension_callback = None
    db = MagicMock()
    db.lookup_by_uuid.return_value = lookup_result
    db.get_metadata.return_value = metadata if metadata is not None else FakeMetadata()
    return action, db


def test_int_book_id_used_directly(monkeypatch):
    action, db = _action(monkeypatch)
    action.update_metadata(42, db, {})
    db.get_metadata.assert_called_once_with(42)
    db.lookup_by_uuid.assert_not_called()


def test_numeric_string_book_id_is_coerced(monkeypatch):
    action, db = _action(monkeypatch)
    action.update_metadata('42', db, {})
    db.get_metadata.assert_called_once_with(42)
    db.lookup_by_uuid.assert_not_called()


def test_non_numeric_string_uses_uuid_lookup(monkeypatch):
    action, db = _action(monkeypatch, lookup_result=7)
    action.update_metadata('some-uuid-string', db, {})
    db.lookup_by_uuid.assert_called_once_with('some-uuid-string')
    db.get_metadata.assert_called_once_with(7)


def test_failed_lookup_returns_skip(monkeypatch):
    action, db = _action(monkeypatch, lookup_result=None)
    status, _details = action.update_metadata('unknown-uuid', db, {})
    assert status == OperationStatus.SKIP
    db.get_metadata.assert_not_called()


def test_sync_if_more_recent_skips_older_update(monkeypatch):
    now = datetime.now(tz=timezone.utc)
    older = now - timedelta(days=1)
    metadata = FakeMetadata({'#ko_modified': now})
    config = _config(checkbox_sync_if_more_recent=True, column_date_sidecar_modified='#ko_modified')
    action, db = _action(monkeypatch, config=config, metadata=metadata)

    status, _details = action.update_metadata(1, db, {'#ko_modified': older})

    assert status == OperationStatus.SKIP
    db.set_metadata.assert_not_called()


def test_no_sync_if_finished_skips_finished_book(monkeypatch):
    metadata = FakeMetadata({'#ko_status': 'complete'})
    config = _config(checkbox_no_sync_if_finished=True, column_status='#ko_status')
    action, db = _action(monkeypatch, config=config, metadata=metadata)

    status, _details = action.update_metadata(1, db, {'#ko_status': 'complete'})

    assert status == OperationStatus.SKIP
    db.set_metadata.assert_not_called()


def test_set_metadata_failure_returns_fail(monkeypatch):
    metadata = FakeMetadata({'#ko_prog': 10})
    action, db = _action(monkeypatch, metadata=metadata)
    db.set_metadata.side_effect = Exception('database is locked')

    status, details = action.update_metadata(1, db, {'#ko_prog': 90})

    assert status == OperationStatus.FAIL
    assert details.get('error') is True


def test_successful_update_writes_metadata_and_returns_pass(monkeypatch):
    metadata = FakeMetadata({'#ko_prog': 10})
    action, db = _action(monkeypatch, metadata=metadata)

    status, _details = action.update_metadata(1, db, {'#ko_prog': 90})

    assert status == OperationStatus.PASS
    assert metadata.set_calls == {'#ko_prog': 90}
    db.set_metadata.assert_called_once_with(1, metadata, set_title=False, set_authors=False)


def test_no_actual_changes_skips_set_metadata_but_still_passes(monkeypatch):
    metadata = FakeMetadata({'#ko_prog': 90})
    action, db = _action(monkeypatch, metadata=metadata)

    status, _details = action.update_metadata(1, db, {'#ko_prog': 90})

    assert status == OperationStatus.PASS
    db.set_metadata.assert_not_called()
