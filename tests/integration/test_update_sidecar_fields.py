
import re
from unittest.mock import MagicMock, patch

from calibre.devices.usbms.driver import USBMS

from action import KoreaderAction
from slpp import slpp as lua


class FakeUSBDevice(USBMS):
    pass


class FakeWirelessDevice:
    def __init__(self):
        self.written = {}

    def put_file(self, path, stream):
        self.written[path] = stream.read()


def _action():
    return KoreaderAction(MagicMock(), MagicMock())


def _read_written_sidecar(path):
    """Round-trips a file written by update_sidecar_fields back through the
    real slpp decoder, mirroring action.py's own parse_sidecar_lua()."""
    with open(path, encoding='utf-8') as f:
        content = f.read()
    clean = re.sub(r'^[^{]*', '', content).strip()
    return lua.decode(clean)


def _read_written_bytes(raw_bytes):
    clean = re.sub(rb'^[^{]*', b'', raw_bytes).strip().decode('utf-8')
    return lua.decode(clean)


def test_reverse_transform_is_applied(tmp_path):
    action = _action()
    sidecar_path = tmp_path / 'metadata.epub.lua'

    status, details = action.update_sidecar_fields(
        FakeUSBDevice(), str(sidecar_path), {}, {'column_percent_read_int': 50}
    )

    assert status == 'success'
    assert details['fields_updated'] == ['column_percent_read_int']
    written = _read_written_sidecar(sidecar_path)
    assert written['percent_finished'] == 0.5


def test_push_to_device_false_field_is_skipped(tmp_path):
    action = _action()
    sidecar_path = tmp_path / 'metadata.epub.lua'

    status, _details = action.update_sidecar_fields(
        FakeUSBDevice(), str(sidecar_path), {}, {'column_bookmarks': 'some html'}
    )

    assert status == 'no_updates'
    assert not sidecar_path.exists()


def test_unknown_config_name_is_skipped(tmp_path):
    action = _action()
    sidecar_path = tmp_path / 'metadata.epub.lua'

    status, _details = action.update_sidecar_fields(
        FakeUSBDevice(), str(sidecar_path), {}, {'not_a_real_column': 'x'}
    )

    assert status == 'no_updates'


def test_nested_data_location_is_created_when_missing(tmp_path):
    action = _action()
    sidecar_path = tmp_path / 'metadata.epub.lua'

    status, _details = action.update_sidecar_fields(
        FakeUSBDevice(), str(sidecar_path), {}, {'column_status': 'complete'}
    )

    assert status == 'success'
    written = _read_written_sidecar(sidecar_path)
    assert written['summary']['status'] == 'complete'


def test_calculated_key_is_stripped_before_writing(tmp_path):
    action = _action()
    sidecar_path = tmp_path / 'metadata.epub.lua'
    current_sidecar = {
        'calculated': {'date_synced': 'should not be written back'},
        'percent_finished': 0.1,
    }

    status, _details = action.update_sidecar_fields(
        FakeUSBDevice(), str(sidecar_path), current_sidecar, {'column_percent_read': 0.9}
    )

    assert status == 'success'
    written = _read_written_sidecar(sidecar_path)
    assert 'calculated' not in written


def test_no_fields_to_update_returns_no_updates(tmp_path):
    action = _action()
    sidecar_path = tmp_path / 'metadata.epub.lua'

    status, _details = action.update_sidecar_fields(FakeUSBDevice(), str(sidecar_path), {}, {})

    assert status == 'no_updates'
    assert not sidecar_path.exists()


def test_permission_error_returns_failure():
    action = _action()
    with patch('builtins.open', MagicMock(side_effect=PermissionError('denied'))):
        status, details = action.update_sidecar_fields(
            FakeUSBDevice(), '/fake/path.lua', {}, {'column_percent_read_int': 10}
        )
    assert status == 'failure'
    assert 'Permission denied' in details['result']


def test_os_error_returns_failure():
    action = _action()
    with patch('builtins.open', MagicMock(side_effect=OSError('disk full'))):
        status, _details = action.update_sidecar_fields(
            FakeUSBDevice(), '/fake/path.lua', {}, {'column_percent_read_int': 10}
        )
    assert status == 'failure'


def test_wireless_device_uses_put_file():
    # Regression test: update_sidecar_fields() used to always write via a
    # plain local open(), which silently never worked over wireless. It now
    # shares push_metadata_to_koreader_sidecar's device-aware write path.
    action = _action()
    device = FakeWirelessDevice()

    status, details = action.update_sidecar_fields(
        device, 'wireless/path/metadata.epub.lua', {}, {'column_percent_read_int': 50}
    )

    assert status == 'success'
    assert details['fields_updated'] == ['column_percent_read_int']
    written = _read_written_bytes(device.written['wireless/path/metadata.epub.lua'])
    assert written['percent_finished'] == 0.5


def test_wireless_device_without_put_file_support_fails_gracefully():
    action = _action()
    device = object()  # no put_file method at all

    status, details = action.update_sidecar_fields(
        device, 'wireless/path/metadata.epub.lua', {}, {'column_percent_read_int': 50}
    )

    assert status == 'failure'
    assert 'Wireless write not supported' in details['result']
