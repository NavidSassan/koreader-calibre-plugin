
from unittest.mock import MagicMock

import action as action_module
from action import KoreaderAction


def _action():
    return KoreaderAction(MagicMock(), MagicMock())


def _field_names(conflicts):
    return {c.field_name for c in conflicts}


def test_push_to_device_false_column_excluded_only_for_to_device(monkeypatch):
    # column_bookmarks has push_to_device=False and a real (non-calculated,
    # non-empty) data_location, so it isolates the direction-specific
    # exclusion from the calculated/raw-sidecar exclusions below.
    monkeypatch.setattr(action_module, 'CONFIG', {'column_bookmarks': '#ko_bookmarks'})
    action = _action()

    sidecar_contents = {
        'annotations': {
            1: {
                'chapter': 'Chapter 1',
                'note': 'a highlight note',
                'text': 'highlighted text',
                'datetime': '2024-01-01 12:00:00',
            }
        }
    }
    calibre_metadata = {'#ko_bookmarks': 'definitely different from the device value'}

    to_device = action.detect_conflicts(
        'uuid-1', 'Title', 'path.lua', calibre_metadata, sidecar_contents, 'to_device'
    )
    to_calibre = action.detect_conflicts(
        'uuid-1', 'Title', 'path.lua', calibre_metadata, sidecar_contents, 'to_calibre'
    )

    assert 'column_bookmarks' not in _field_names(to_device)
    assert 'column_bookmarks' in _field_names(to_calibre)


def test_raw_sidecar_backup_column_always_excluded(monkeypatch):
    # column_sidecar has data_location=[] (the whole sidecar dict) - never
    # meaningful as a single diffed field, in either direction.
    monkeypatch.setattr(action_module, 'CONFIG', {'column_sidecar': '#ko_sidecar'})
    action = _action()

    sidecar_contents = {'percent_finished': 0.9, 'summary': {'status': 'reading'}}
    calibre_metadata = {'#ko_sidecar': '{"totally": "different"}'}

    for direction in ('to_device', 'to_calibre'):
        conflicts = action.detect_conflicts(
            'uuid-1', 'Title', 'path.lua', calibre_metadata, sidecar_contents, direction
        )
        assert 'column_sidecar' not in _field_names(conflicts)


def test_calculated_column_always_excluded(monkeypatch):
    monkeypatch.setattr(action_module, 'CONFIG', {'column_date_book_started': '#ko_started'})
    action = _action()

    sidecar_contents = {'calculated': {'date_book_started': 'some-date'}}
    calibre_metadata = {'#ko_started': 'a-completely-different-date'}

    for direction in ('to_device', 'to_calibre'):
        conflicts = action.detect_conflicts(
            'uuid-1', 'Title', 'path.lua', calibre_metadata, sidecar_contents, direction
        )
        assert 'column_date_book_started' not in _field_names(conflicts)


def test_float_rounding_avoids_false_conflict(monkeypatch):
    monkeypatch.setattr(action_module, 'CONFIG', {'column_percent_read': '#ko_prog'})
    action = _action()

    sidecar_contents = {'percent_finished': 0.451128000001}
    calibre_metadata = {'#ko_prog': 0.451128}

    conflicts = action.detect_conflicts(
        'uuid-1', 'Title', 'path.lua', calibre_metadata, sidecar_contents, 'to_calibre'
    )

    assert 'column_percent_read' not in _field_names(conflicts)


def test_none_and_empty_string_treated_as_equivalent(monkeypatch):
    monkeypatch.setattr(action_module, 'CONFIG', {'column_last_read_location': '#ko_loc'})
    action = _action()

    # Device has no value for last_xpointer at all (data_location misses).
    sidecar_contents = {}
    calibre_metadata = {'#ko_loc': ''}

    conflicts = action.detect_conflicts(
        'uuid-1', 'Title', 'path.lua', calibre_metadata, sidecar_contents, 'to_calibre'
    )

    assert 'column_last_read_location' not in _field_names(conflicts)


def test_real_conflict_is_detected_and_reported(monkeypatch):
    monkeypatch.setattr(action_module, 'CONFIG', {'column_percent_read_int': '#ko_progint'})
    action = _action()

    sidecar_contents = {'percent_finished': 0.75}
    calibre_metadata = {'#ko_progint': 50}

    conflicts = action.detect_conflicts(
        'book-uuid', 'My Book', 'Some/path.sdr/metadata.epub.lua',
        calibre_metadata, sidecar_contents, 'to_calibre',
    )

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.field_name == 'column_percent_read_int'
    assert conflict.calibre_value == 50
    assert conflict.device_value == 75
    assert conflict.book_uuid == 'book-uuid'
    assert conflict.book_title == 'My Book'
    assert conflict.sidecar_path == 'Some/path.sdr/metadata.epub.lua'
    assert conflict.resolution == 'skip'
