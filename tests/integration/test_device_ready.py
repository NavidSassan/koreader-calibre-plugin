
from unittest.mock import MagicMock

from action import KoreaderAction


def _action():
    return KoreaderAction(MagicMock(), MagicMock())


def test_mark_device_ready_sets_flag():
    action = _action()
    action.device_ready = False

    action._mark_device_ready()

    assert action.device_ready is True


def test_connection_changed_clears_flag_on_connect():
    # A fresh connection means Calibre hasn't annotated this device's
    # books yet, even though a previous device might have left the flag
    # set True.
    action = _action()
    action.device_ready = True

    action._on_device_connection_changed(True)

    assert action.device_ready is False


def test_connection_changed_clears_flag_on_disconnect():
    action = _action()
    action.device_ready = True

    action._on_device_connection_changed(False)

    assert action.device_ready is False
