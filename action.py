#!/usr/bin/env python3

"""KOReader Sync Plugin for Calibre."""

from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
import io
import json
import os
import re
import sys
import importlib.util
import time
from typing import Any, List, Dict, Tuple, Optional

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from PyQt5.Qt import (
    QUrl,
    QTimer,
    QTime,
    QTableWidget,
    QTableWidgetItem,
    QHBoxLayout,
    QVBoxLayout,
    QDialog,
    QLabel,
    QIcon,
    QPushButton,
    QScrollArea,
    QProgressBar,
    QApplication,
    Qt,
    QThread,
    pyqtSignal,
    QComboBox,
    QHeaderView,
    QAbstractItemView,
)
from PyQt5.QtGui import QPixmap

from calibre_plugins.koreader.slpp import slpp as lua
from calibre_plugins.koreader.config import (
    SUPPORTED_DEVICES,
    UNSUPPORTED_DEVICES,
    CUSTOM_COLUMN_DEFAULTS as COLUMNS,
    CONFIG,
)
from calibre_plugins.koreader import (
    DEBUG,
    DRY_RUN,
    PYDEVD,
    KoreaderSync,
)

from calibre.utils.iso8601 import utc_tz, local_tz
from calibre.gui2.dialogs.message_box import MessageBox
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.device import device_signals
from calibre.gui2 import (
    error_dialog,
    warning_dialog,
    info_dialog,
    open_url,
)
from calibre.devices.usbms.driver import debug_print as root_debug_print
from calibre.constants import numeric_version
from enum import Enum, auto

__license__ = 'GNU GPLv3'
__copyright__ = '2021, harmtemolder <mail at harmtemolder.com>'
__modified_by__ = 'kyxap kyxappp@gmail.com'
__modification_date__ = '2024'
__docformat__ = 'restructuredtext en'

if numeric_version >= (5, 5, 0):
    module_debug_print = partial(root_debug_print, ' koreader:action:', sep='')
else:
    module_debug_print = partial(root_debug_print, 'koreader:action:')

if DEBUG and PYDEVD:
    try:
        sys.path.append(
            # '/Applications/PyCharm.app/Contents/debug-eggs/pydevd-pycharm.egg'  # macOS
            '/opt/pycharm-professional/debug-eggs/pydevd-pycharm.egg'
            # Manjaro Linux
        )
        import pydevd_pycharm

        pydevd_pycharm.settrace(
            'localhost', stdoutToServer=True, stderrToServer=True,
            suspend=False
        )
    except Exception as e:
        module_debug_print('could not start pydevd_pycharm, e = ', e)
        PYDEVD = False


class GetSidecarStatus(Enum):
    PATH_NOT_FOUND = auto()
    DECODE_FAILED = auto()


class OperationStatus(Enum):
    PASS = auto()
    FAIL = auto()
    SKIP = auto()


@dataclass
class ConflictItem:
    """Represents a single conflict between Calibre and device values."""
    book_uuid: str
    book_title: str
    sidecar_path: str
    field_name: str           # config key, e.g., 'column_percent_read'
    field_display_name: str   # human-readable, e.g., 'Reading Progress'
    calibre_value: Any
    device_value: Any
    resolution: str = 'skip'  # 'calibre', 'device', 'skip'


def is_system_path(path):
    """
    KOreader user may have some files in the root which we want to skip to
    avoid showing warning message

    :param path: path to sidecar file (*.lua)
    :return: true/false if partial match found
    """
    to_ignore = ['kfmon.sdr', 'koreader.sdr']
    return any(substring in path for substring in to_ignore)


def append_results(results, title, status_msg, book_uuid, sidecar_path):
    debug_print = partial(
        module_debug_print,
        'KoreaderAction:append_results:'
    )
    debug_print(f'{sidecar_path} - {status_msg}')
    return results.append(
        {
            'title': title,
            'result': status_msg,
            'book_uuid': book_uuid,
            'sidecar_path': sidecar_path,
        }
    )


def parse_sidecar_lua(sidecar_lua):
    """Parses a sidecar Lua file into a Python dict

    :param sidecar_lua: the contents of a sidecar Lua as a str
    :return: a dict of those contents
    """
    debug_print = partial(
        module_debug_print,
        'KoreaderAction:parse_sidecar_lua:'
    )

    try:
        clean_lua = re.sub('^[^{]*', '', sidecar_lua).strip()
        decoded_lua = lua.decode(clean_lua)
    except:
        debug_print('could not decode sidecar_lua')
        decoded_lua = None

    if 'bookmarks' in decoded_lua:
        if type(decoded_lua['bookmarks']) is list:
            decoded_lua['bookmarks'] = {
                # Starts from 1
                i+1: bookmark for i, bookmark in enumerate(decoded_lua['bookmarks'])}

        debug_print('calculating first and last bookmark dates')
        bookmark_dates = [
            datetime.strptime(
                bookmark['datetime'],
                '%Y-%m-%d %H:%M:%S'
            ).replace(tzinfo=utc_tz)
            for bookmark in decoded_lua['bookmarks'].values()
        ]

        if len(bookmark_dates) > 0:
            decoded_lua['calculated'] = {
                'first_bookmark': min(bookmark_dates),
                'last_bookmark': max(bookmark_dates),
            }

    return decoded_lua


class KoreaderAction(InterfaceAction):
    name = KoreaderSync.name
    action_spec = (name, 'edit-redo.png', KoreaderSync.description, None)
    action_add_menu = True
    action_menu_clone_qaction = 'Sync from KOReader'
    dont_add_to = frozenset(
        [
            'context-menu', 'context-menu-device', 'menubar',
            'menubar-device', 'context-menu-cover-browser',
            'context-menu-split']
    )
    dont_remove_from = InterfaceAction.all_locations - dont_add_to
    action_type = 'current'

    def genesis(self):
        debug_print = partial(module_debug_print, 'KoreaderAction:genesis:')
        debug_print('start')

        base = self.interface_action_base_plugin
        self.version = f'{base.name} (v{".".join(map(str, base.version))})'
        self.extension_callback = None

        # Overwrite icon with actual KOReader logo
        icon = get_icons(
            'images/icon.png'
        )
        self.qaction.setIcon(icon)

        # Left-click action
        self.qaction.triggered.connect(self.sync_to_calibre)

        # Right-click menu (already includes left-click action)

        # TODO: Sync calibre to KOReader is disabled see more in #8
        self.create_menu_action(
            self.qaction.menu(),
            'Sync missing to KOReader',
            'Sync missing to KOReader',
            icon='edit-undo.png',
            description='If calibre has an entry in the "Raw sidecar column", '
                        'but KOReader does not have a sidecar file, push the '
                        'metadata from calibre to a new sidecar file.',
            triggered=self.sync_missing_sidecars_to_koreader
        )

        self.create_menu_action(
            self.qaction.menu(),
            'Sync from ProgressSync',
            'Sync from ProgressSync',
            icon='convert.png',
            description='Use KOReader''s built in ProgressSync Plugin '
                        'to update percentRead int or float.',
            triggered=self.sync_progress_from_progresssync
        )

        self.qaction.menu().addSeparator()

        self.create_menu_action(
            self.qaction.menu(),
            'Configure KOReader Sync',
            'Configure',
            icon='config.png',
            description='Configure KOReader Sync',
            triggered=self.show_config
        )

        self.qaction.menu().addSeparator()

        self.create_menu_action(
            self.qaction.menu(),
            'Readme for KOReader Sync',
            'Readme',
            icon='dialog_question.png',
            description='Readme for KOReader Sync',
            triggered=self.show_readme
        )

        self.create_menu_action(
            self.qaction.menu(),
            'About KOReader Sync',
            'About',
            icon='dialog_information.png',
            description='About KOReader Sync',
            triggered=self.show_about
        )

        # Start the scheduled progress sync if enabled
        if CONFIG["checkbox_enable_scheduled_progressync"]:
            self.scheduled_progress_sync()

        # Start the device connection watcher if enabled
        if CONFIG["checkbox_enable_automatic_sync"]:
            device_signals.device_metadata_available.connect(
                self._on_device_metadata_available)

        basedir = os.path.dirname(base.plugin_path)
        for filename in os.listdir(basedir):
            if filename.startswith("KOSync_extension") and filename.endswith(".py"):
                filepath = os.path.join(basedir, filename)
                try:
                    spec = importlib.util.spec_from_file_location(
                        "KOSync_extension", filepath)
                    extension = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(extension)
                    if hasattr(extension, "onItemUpdate"):
                        self.extension_callback = extension.onItemUpdate
                        print(f"Loaded onItemUpdate from {filename}")
                        return
                except Exception as e:
                    print(f"Failed to load extension: {e}")

    def show_config(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def show_readme(self):
        debug_print = partial(module_debug_print,
                              'KoreaderAction:show_readme:')
        debug_print('start')
        readme_url = QUrl(
            'https://github.com/harmtemolder/koreader-calibre-plugin#readme'
        )
        open_url(readme_url)

    def show_about(self):
        debug_print = partial(module_debug_print, 'KoreaderAction:show_about:')
        debug_print('start')
        text = get_resources('about.txt').decode(
            'utf-8'
        )
        if DEBUG:
            text += '\n\nRunning in debug mode'
        icon = get_icons(
            'images/icon.png'
        )

        about_dialog = MessageBox(
            MessageBox.INFO,
            f'About {self.version}',
            text,
            det_msg='',
            q_icon=icon,
            show_copy_button=False,
            parent=None,
        )

        return about_dialog.exec_()

    def apply_settings(self):
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:apply_settings:'
        )
        debug_print('start')

    def get_connected_device(self):
        """Tries to get the connected device, if any

        :return: the connected device object or None
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:get_connected_device:'
        )

        try:
            is_device_present = self.gui.device_manager.is_device_present
        except:
            is_device_present = False

        if not is_device_present:
            debug_print('is_device_present = ', is_device_present)
            error_dialog(
                self.gui,
                'No device found',
                'No device found',
                det_msg='',
                show=True,
                show_copy_button=False
            )
            return None

        try:
            connected_device = self.gui.device_manager.connected_device
            connected_device_type = connected_device.__class__.__name__
        except:
            debug_print('could not get connected_device')
            error_dialog(
                self.gui,
                'Could not connect to device',
                'Could not connect to device',
                det_msg='',
                show=True,
                show_copy_button=False
            )
            return None

        debug_print('connected_device_type = ', connected_device_type)
        return connected_device

    def _on_device_metadata_available(self):
        self.sync_to_calibre(silent=True if not DEBUG else False)

    def get_paths(self, device):
        """Retrieves paths to sidecars of all books in calibre's library
        on the device

        :param device: a device object
        :return: a dict of book_keys with corresponding book info dicts
                 Each book info dict contains: sidecar_path, uuid, application_id, title
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:get_paths:'
        )

        debug_print(
            f'found {len(device.books())} paths to books:\n\t',
            '\n\t'.join([book.path for book in device.books()])
        )

        debug_print(
            f'found {len(device.books())} lpaths to books:\n\t',
            '\n\t'.join([book.lpath for book in device.books()])
        )

        # Debug: print all available attributes on the first book
        books = list(device.books())
        if books:
            sample_book = books[0]
            debug_print(f'Sample book attributes: {dir(sample_book)}')
            # Try common attributes
            for attr in ['uuid', 'application_id', 'title', 'authors', 'path', 'lpath', 'id']:
                if hasattr(sample_book, attr):
                    debug_print(f'  {attr} = {getattr(sample_book, attr, "N/A")}')

        book_info = {}
        for book in books:
            # Get application_id if available (this is the Calibre library book_id)
            app_id = getattr(book, 'application_id', None)
            book_uuid = getattr(book, 'uuid', None)
            title = getattr(book, 'title', 'Unknown')

            debug_print(f'Book: "{title}" - uuid={book_uuid}, application_id={app_id}, path={book.path}')

            sidecar_path = re.sub(r'\.(\w+)$', r'.sdr/metadata.\1.lua', book.path)

            # Use a unique key - prefer application_id, fall back to uuid, then path
            key = app_id or book_uuid or book.path

            book_info[key] = {
                'sidecar_path': sidecar_path,
                'uuid': book_uuid,
                'application_id': app_id,
                'title': title,
                'path': book.path
            }

        debug_print(
            f'generated {len(book_info)} path(s) to sidecar Lua files:\n\t',
            '\n\t'.join([info['sidecar_path'] for info in book_info.values()])
        )

        return book_info

    def get_sidecar(self, device, path):
        """Requests the given path from the given device and returns the
        contents of a sidecar Lua as Python dict

        :param device: a device object
        :param path: a path to a sidecar Lua on the device
        :return: dict or None
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:get_sidecar:'
        )

        with io.BytesIO() as outfile:
            try:
                device.get_file(path, outfile)
            except:
                debug_print('could not get ', path)
                return GetSidecarStatus.PATH_NOT_FOUND

            contents = outfile.getvalue()

            try:
                decoded_contents = contents.decode()
            except UnicodeDecodeError:
                debug_print('could not decode ', contents)
                return GetSidecarStatus.DECODE_FAILED

            debug_print(f'Parsing: {path}')
            parsed_contents = parse_sidecar_lua(decoded_contents)
            parsed_contents['calculated'] = {}
            try:
                parsed_contents['calculated'][
                'date_synced'] = datetime.now().replace(tzinfo=local_tz)
                parsed_contents['calculated'][
                    'date_status_changed'] = datetime.strptime(
                    parsed_contents['summary']['modified'], "%Y-%m-%d").replace(tzinfo=local_tz)
                parsed_contents['calculated'][
                    'date_sidecar_modified'] = datetime.fromtimestamp(
                    os.path.getmtime(path)).replace(tzinfo=local_tz)
            except:
                pass

        return parsed_contents

    def detect_conflicts(self, book_uuid: str, book_title: str, sidecar_path: str,
                         calibre_metadata, sidecar_contents: dict,
                         direction: str) -> List[ConflictItem]:
        """Compare Calibre metadata with device sidecar, return list of conflicts.

        :param book_uuid: The book's UUID in Calibre
        :param book_title: The book's title for display
        :param sidecar_path: Path to the sidecar file
        :param calibre_metadata: Calibre's current metadata object for the book
        :param sidecar_contents: Parsed sidecar dict from device
        :param direction: 'to_calibre' or 'to_device'
        :return: List of ConflictItem objects for any differing values
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:detect_conflicts:'
        )
        conflicts = []

        for config_name, column_config in COLUMNS.items():
            target_column = CONFIG.get(config_name, '')
            if not target_column:
                continue

            # Skip columns that shouldn't be pushed to device
            if direction == 'to_device' and not column_config.get('push_to_device', True):
                continue

            # Skip calculated columns for conflict detection
            data_location = column_config.get('data_location', [])
            if data_location and data_location[0] == 'calculated':
                continue

            # Get device value via data_location
            device_value = sidecar_contents
            for key in data_location:
                if isinstance(device_value, dict) and key in device_value:
                    device_value = device_value[key]
                else:
                    device_value = None
                    break

            # Apply transform for comparison (device -> calibre format)
            if device_value is not None and 'transform' in column_config:
                try:
                    device_value_transformed = column_config['transform'](device_value)
                except Exception as e:
                    debug_print(f'Transform failed for {config_name}: {e}')
                    device_value_transformed = device_value
            else:
                device_value_transformed = device_value

            # Get Calibre value
            calibre_value = calibre_metadata.get(target_column)
            debug_print(f'  {config_name}: target_column={target_column}, calibre_value={calibre_value}, device_value={device_value_transformed}')

            # Normalize for comparison (handle None, type differences)
            def normalize_value(val):
                if val is None:
                    return None
                if isinstance(val, float):
                    return round(val, 6)  # Avoid floating point comparison issues
                return val

            calibre_normalized = normalize_value(calibre_value)
            device_normalized = normalize_value(device_value_transformed)

            # Compare values - only add conflict if they differ
            if calibre_normalized != device_normalized:
                # Don't report conflict if both are None/empty
                if calibre_normalized is None and device_normalized is None:
                    continue
                # Don't report conflict if one is empty string and other is None
                if (calibre_normalized == '' and device_normalized is None) or \
                   (calibre_normalized is None and device_normalized == ''):
                    continue

                conflicts.append(ConflictItem(
                    book_uuid=book_uuid,
                    book_title=book_title,
                    sidecar_path=sidecar_path,
                    field_name=config_name,
                    field_display_name=column_config.get('column_heading', config_name),
                    calibre_value=calibre_value,
                    device_value=device_value_transformed,
                ))
                debug_print(f'Conflict found: {book_title} - {config_name}: '
                           f'Calibre={calibre_value}, Device={device_value_transformed}')

        return conflicts

    def update_metadata(self, book_id_or_uuid, db, keys_values_to_update):
        """Update multiple metadata columns for the given book.

        :param book_id_or_uuid: either a numeric book_id or a UUID string
        :param keys_values_to_update: a dict of keys to update with values
        :return: a dict of values that can be used to report back to the user
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:update_metadata:'
        )

        # If we got a numeric book_id, use it directly; otherwise look up by UUID
        # Handle int, numpy int, or string that looks like an int
        try:
            book_id = int(book_id_or_uuid)
            debug_print(f'Using book_id directly: {book_id}')
        except (ValueError, TypeError):
            # It's a UUID string, look it up
            try:
                debug_print('Looking for uuid in calibre db: ', book_id_or_uuid)
                book_id = db.lookup_by_uuid(book_id_or_uuid)
            except:
                book_id = None

        if not book_id:
            debug_print(f'could not find {book_id_or_uuid} in calibre\'s library')
            return OperationStatus.SKIP, {
                'result': 'could not find book in calibre\'s library, have you deleted this book from library?'}

        # Get the current metadata for the book from the library
        metadata = db.get_metadata(book_id)

        # Dict for use in logging
        updateLog = {}

        read_percent_key = CONFIG['column_percent_read'] or CONFIG['column_percent_read_int']

        # Check config to sync only if data is more recent
        if CONFIG['checkbox_sync_if_more_recent']:
            date_modified_key = CONFIG['column_date_sidecar_modified']
            current_date_modified = metadata.get(date_modified_key)
            new_date_modified = keys_values_to_update.get(date_modified_key)
            if current_date_modified is not None and new_date_modified is not None:
                if current_date_modified.timestamp() >= new_date_modified.timestamp():
                    debug_print(
                        f'book {book_id} date_modified {new_date_modified} older than current {current_date_modified}')
                    return OperationStatus.SKIP, {
                        'result': 'skipped, data in calibre is newer',
                    }
            # Fallback if no 'Date Modified Column' is set or not obtainable (wireless)
            elif new_date_modified is None:
                current_read_percent = metadata.get(read_percent_key)
                new_read_percent = keys_values_to_update.get(read_percent_key)
                if current_read_percent is not None and new_read_percent is not None:
                    if current_read_percent >= new_read_percent:
                        debug_print(
                            f'book {book_id} read_percent {new_read_percent} lower or equal than current {current_read_percent}')
                        return OperationStatus.SKIP, {
                            'result': 'skipped, read Percent is lower or equal to the one stored in calibre',
                            'book_id': book_id,
                        }
                elif current_read_percent is not None and new_read_percent is None:
                    debug_print(
                        f'book {book_id} read_percent is None but existing is {current_read_percent}')
                    return OperationStatus.SKIP, {
                        'result': 'skipped, no new read percent found',
                    }

        # Check config to sync only if the book is not yet finished
        status_key = CONFIG['column_status']
        if CONFIG['checkbox_no_sync_if_finished']:
            current_read_percent = metadata.get(read_percent_key)
            current_status = metadata.get(status_key)
            if current_read_percent is not None and current_read_percent >= 100 \
                    or current_status is not None and current_status == "complete":
                debug_print(f'book {book_id} was already finished')
                return OperationStatus.SKIP, {
                    'result': 'skipped, book already finished',
                }

        # Check and correct reading status if required
        if status_key:
            new_status = keys_values_to_update.get(status_key)
            if not new_status:
                new_read_percent = keys_values_to_update.get(read_percent_key)
                current_status = metadata.get(status_key)
                if new_read_percent and current_status != "abandoned":
                    if new_read_percent > 0 and new_read_percent < 100 and current_status != "reading":
                        debug_print(
                            f'book {book_id} set column_status to reading')
                        keys_values_to_update[status_key] = "reading"
                        status_bool_key = CONFIG['column_status_bool']
                        if status_bool_key:
                            keys_values_to_update[status_bool_key] = False
                    elif new_read_percent >= 100 and current_status != "complete":
                        debug_print(
                            f'book {book_id} set column_status to complete')
                        keys_values_to_update[status_key] = "complete"
                        status_bool_key = CONFIG['column_status_bool']
                        if status_bool_key:
                            keys_values_to_update[status_bool_key] = True

        # Call the extension callback if it exists
        if self.extension_callback:
            try:
                updateLog = self.extension_callback(
                    self=self,
                    metadata=metadata,
                    keys_values_to_update=keys_values_to_update,
                    updateLog=updateLog,
                    CONFIG=CONFIG,
                    book_id=book_id
                )
            except Exception as e:
                debug_print(f'Error in extension onItemUpdate: {e}')

        updates = []
        # Update that metadata locally
        for key, new_value in keys_values_to_update.items():
            old_value = metadata.get(key)

            if new_value != old_value:
                updates.append(key)
                metadata.set(key, new_value)
                updateLog[key] = f'{old_value} >> {new_value}'
            else:
                if DEBUG:
                    updateLog[key] = f'{old_value} -- {new_value}'

        # Write the updated metadata back to the library
        if len(updates) == 0:
            updateLog['result'] = 'no updates needed'
            debug_print(
                'no changed metadata for book_id = ', book_id
            )
        elif DEBUG and DRY_RUN:
            debug_print(
                'would have updated the following fields for book_id = ',
                book_id, ': ', updates
            )
        else:
            try:
                db.set_metadata(
                    book_id, metadata, set_title=False,
                    set_authors=False
                )
                debug_print(
                    'updated the following fields for book_id = ', book_id,
                    ': ', updates
                )
            except Exception as e:
                debug_print(f'Failed to set metadata for book_id {book_id}: {e}')
                return OperationStatus.FAIL, {
                    'result': f'Failed to update: {str(e)}',
                    'error': True
                }

        return OperationStatus.PASS, {
            'result': 'success',
            **updateLog
        }

    def check_device(self, device):
        """Return .

        :param device: The connected device.
        :return: False if device is specifically not supported,
        otherwise True
        """

        debug_print = partial(
            module_debug_print,
            'KoreaderAction:check_device:'
        )

        if not device:
            return False

        device_class = device.__class__.__name__

        if device_class in UNSUPPORTED_DEVICES:
            debug_print('unsupported device, device_class = ', device_class)
            error_dialog(
                self.gui,
                'Device not supported',
                f'Devices of the type {device_class} are not supported by this plugin. I '
                f'have tried to get it working, but couldn’t. Sorry.',
                det_msg='',
                show=True,
                show_copy_button=False
            )
            return False
        elif device_class in SUPPORTED_DEVICES:
            return True
        else:
            debug_print(
                'not yet supported device, device_class = ',
                device_class
            )
            warning_dialog(
                self.gui,
                'Device not yet supported',
                f'Devices of the type {device_class} are not yet supported by this plugin. '
                f'Please check if there already is a feature request for this '
                f'<a href="https://github.com/harmtemolder/koreader-calibre-plugin/issues">'
                f'here</a>. If not, feel free to create one. I\'ll try to sync anyway.',
                det_msg='',
                show=True,
                show_copy_button=False
            )
            return True

    def push_metadata_to_koreader_sidecar(self, book_uuid, path):
        """Create a sidecar file for the given book.

        :param book_uuid: Calibre's uuid for the book
        :param path: path to sidecar file to create
        :return: tuple of bool and result dict
        """

        debug_print = partial(
            module_debug_print,
            'KoreaderAction:push_metadata_to_koreader_sidecar:'
        )

        try:
            db = self.gui.current_db.new_api
            book_id = db.lookup_by_uuid(book_uuid)
            debug_print(f"Book id is {book_id}")
        except:
            book_id = None

        if not book_id:
            debug_print(f'could not find {book_uuid} in calibre’s library')
            return "failure", {
                'result': f"Could not find uuid {book_uuid} in Calibre's "
                f"library."
            }

        # Get the current metadata for the book from the library
        metadata = db.get_metadata(book_id)
        sidecar_metadata = metadata.get(CONFIG["column_sidecar"])
        if not sidecar_metadata:
            return "no_metadata", {
                'result': f'No KOReader metadata for book_id {book_id}, no '
                f'need to push.'
            }
        sidecar_dict = json.loads(sidecar_metadata)
        sidecar_lua = lua.encode(sidecar_dict)
        # Lua -> JSON -> Lua conversion is lossy, because JSON does not support integer
        # keys. This means that a key like [1] will end up as ["1"] after the round
        # trip. The following regex strips the quotes from any Lua object key that consists of
        # only digits. This is not entirely correct because it now converts keys with
        # only digits that were originally string keys as well, but it doesn't seem that
        # KOReader uses those.
        sidecar_lua = re.sub(r'\["(\d+)"\]', r'[\1]', sidecar_lua)
        sidecar_lua_formatted = f"-- we can read Lua syntax here!\nreturn {sidecar_lua}\n"
        try:
            os.makedirs(os.path.dirname(path))
        except FileExistsError:
            # dir exists, so we're fine
            pass
        except PermissionError as perm_e:
            return "failure", {
                'result': f'Unable to create directory at: '
                f'{path} due to {perm_e}',
            }
        except OSError as os_e:
            return "failure", {
                'result': f'Unexpectable exception is occurred, '
                f'please report: {os_e}',
            }

        with open(path, "w", encoding="utf-8") as f:
            debug_print(f"Writing to {path}")
            f.write(sidecar_lua_formatted)

        return "success", {
            'result': 'success',
        }

    def update_sidecar_fields(self, sidecar_path: str, current_sidecar: dict,
                              fields_to_update: Dict[str, Any]) -> Tuple[str, dict]:
        """Update specific fields in an existing sidecar file.

        :param sidecar_path: Path to sidecar Lua file on device
        :param current_sidecar: Already-parsed sidecar dict (without 'calculated' key)
        :param fields_to_update: Dict mapping config_name -> calibre_value
        :return: Tuple of (status, details)
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:update_sidecar_fields:'
        )

        # Make a deep copy to avoid modifying the original
        import copy
        modified_sidecar = copy.deepcopy(current_sidecar)

        # Remove 'calculated' key if present (it's not part of the real sidecar)
        if 'calculated' in modified_sidecar:
            del modified_sidecar['calculated']

        fields_updated = []

        for config_name, calibre_value in fields_to_update.items():
            column_config = COLUMNS.get(config_name)
            if not column_config:
                debug_print(f'Unknown config_name: {config_name}')
                continue

            # Skip columns that shouldn't be pushed to device
            if not column_config.get('push_to_device', True):
                debug_print(f'Skipping {config_name} - not pushable to device')
                continue

            # Apply reverse transform if available
            if 'reverse_transform' in column_config and calibre_value is not None:
                try:
                    device_value = column_config['reverse_transform'](calibre_value)
                except Exception as e:
                    debug_print(f'Reverse transform failed for {config_name}: {e}')
                    device_value = calibre_value
            else:
                device_value = calibre_value

            # Set value at data_location path
            data_location = column_config.get('data_location', [])
            if not data_location:
                debug_print(f'No data_location for {config_name}')
                continue

            # Navigate to parent dict and set the value
            target = modified_sidecar
            for i, key in enumerate(data_location[:-1]):
                if key not in target:
                    target[key] = {}
                target = target[key]

            # Set the final key
            final_key = data_location[-1]
            target[final_key] = device_value
            fields_updated.append(config_name)
            debug_print(f'Set {config_name} ({data_location}) = {device_value}')

        if not fields_updated:
            return "no_updates", {
                'result': 'No fields to update',
            }

        # Encode to Lua and write
        try:
            sidecar_lua = lua.encode(modified_sidecar)
            # Fix integer keys (JSON uses string keys)
            sidecar_lua = re.sub(r'\["(\d+)"\]', r'[\1]', sidecar_lua)
            sidecar_lua_formatted = f"-- we can read Lua syntax here!\nreturn {sidecar_lua}\n"

            with open(sidecar_path, "w", encoding="utf-8") as f:
                debug_print(f"Writing updated sidecar to {sidecar_path}")
                f.write(sidecar_lua_formatted)

            return "success", {
                'result': 'success',
                'fields_updated': fields_updated,
            }
        except PermissionError as perm_e:
            return "failure", {
                'result': f'Permission denied writing to {sidecar_path}: {perm_e}',
            }
        except OSError as os_e:
            return "failure", {
                'result': f'Error writing sidecar: {os_e}',
            }

    def sync_missing_sidecars_to_koreader(self, silent=False):
        """Push metadata from Calibre to KOReader sidecars.

        For books WITHOUT sidecars: creates new sidecar from raw sidecar column.
        For books WITH sidecars: detects conflicts and updates individual fields
        based on user resolution.

        :return:
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:sync_missing_sidecars_to_koreader:'
        )

        # Check if we have at least the raw sidecar column or some pushable columns
        has_sidecar_column = CONFIG.get("column_sidecar", '') != ''
        has_pushable_columns = any(
            CONFIG.get(config_name, '') != '' and column_config.get('push_to_device', True)
            for config_name, column_config in COLUMNS.items()
        )

        if not has_sidecar_column and not has_pushable_columns:
            error_dialog(
                self.gui,
                'Failure',
                'No pushable columns are mapped. Please configure at least one column in plugin settings.',
                show=True,
                show_copy_button=False
            )
            return None

        device = self.get_connected_device()

        if not self.check_device(device):
            return None

        db = self.gui.current_db.new_api
        book_info_dict = self.get_paths(device)
        debug_print('book_info_dict: ', book_info_dict)

        # Separate existing vs missing sidecars
        books_with_sidecar = {}    # book_key -> book_info
        books_without_sidecar = {} # book_key -> book_info
        for book_key, book_info in book_info_dict.items():
            sidecar_path = book_info['sidecar_path']
            if os.path.exists(sidecar_path):
                books_with_sidecar[book_key] = book_info
            else:
                books_without_sidecar[book_key] = book_info

        debug_print(
            f"Sidecars not present on device: {len(books_without_sidecar)}",
            f"Sidecars present on device: {len(books_with_sidecar)}"
        )

        # Collect conflicts for existing sidecars
        all_conflicts = []
        sidecar_cache = {}  # Cache: book_key -> (sidecar_contents, book_id, calibre_uuid)

        for book_key, book_info in books_with_sidecar.items():
            try:
                sidecar_path = book_info['sidecar_path']
                book_uuid = book_info['uuid']
                app_id = book_info['application_id']

                # Use Calibre's matching (application_id)
                # Convert to int if it's a string
                book_id = None
                if app_id is not None:
                    try:
                        book_id = int(app_id)
                    except (ValueError, TypeError):
                        book_id = app_id

                if not book_id:
                    debug_print(f'Book not matched by Calibre: app_id={app_id}, uuid={book_uuid}')
                    continue
                debug_print(f'Using Calibre application_id: app_id={app_id}, book_id={book_id}')

                metadata = db.get_metadata(book_id)
                title = metadata.get('title', 'Unknown')
                calibre_uuid = metadata.get('uuid', 'NO UUID')

                # Get sidecar contents
                sidecar_contents = self.get_sidecar(device, sidecar_path)
                if isinstance(sidecar_contents, GetSidecarStatus):
                    debug_print(f'Could not read sidecar for {title}: {sidecar_contents}')
                    continue

                # Cache the sidecar for later use
                sidecar_cache[book_key] = (sidecar_contents, book_id, calibre_uuid)

                # Detect conflicts - use calibre_uuid for consistency
                conflicts = self.detect_conflicts(
                    calibre_uuid, title, sidecar_path, metadata, sidecar_contents, 'to_device'
                )
                all_conflicts.extend(conflicts)

            except Exception as e:
                debug_print(f'Error processing {book_key}: {e}')
                continue

        # Show conflict resolution dialog if there are conflicts
        resolved_conflicts = []
        if all_conflicts and not silent:
            dialog = ConflictResolutionDialog(self.gui, all_conflicts, 'to_device')
            if dialog.exec_() == QDialog.Accepted:
                resolved_conflicts = dialog.get_resolved_conflicts()
            else:
                # User cancelled
                info_dialog(
                    self.gui,
                    'Cancelled',
                    'Sync to KOReader was cancelled.',
                    show=True,
                    show_copy_button=False
                )
                return None

        # Process results
        results = []
        num_new_sidecars = 0
        num_updated_sidecars = 0
        num_no_metadata = 0
        num_fail = 0
        num_skipped = 0

        # Process books WITHOUT sidecars (create new from raw sidecar column)
        if has_sidecar_column:
            for book_key, book_info in books_without_sidecar.items():
                sidecar_path = book_info['sidecar_path']
                book_uuid = book_info['uuid']
                app_id = book_info['application_id']

                # Use Calibre's matching (application_id)
                # Convert to int if it's a string
                book_id = None
                if app_id is not None:
                    try:
                        book_id = int(app_id)
                    except (ValueError, TypeError):
                        book_id = app_id
                calibre_uuid = None

                if book_id:
                    metadata = db.get_metadata(book_id)
                    calibre_uuid = metadata.get('uuid')
                    title = metadata.get('title', 'Unknown')
                else:
                    title = 'Unknown'

                # Use calibre_uuid for push_metadata_to_koreader_sidecar
                result, details = self.push_metadata_to_koreader_sidecar(calibre_uuid or book_uuid, sidecar_path)

                if result == "success":
                    num_new_sidecars += 1

                    # After restoring sidecar, also update with current Calibre column values
                    if book_id and has_pushable_columns:
                        # Read the just-created sidecar
                        sidecar_contents = self.get_sidecar(device, sidecar_path)
                        if not isinstance(sidecar_contents, GetSidecarStatus):
                            # Collect fields to update from Calibre columns
                            fields_to_update = {}
                            for config_name, column_config in COLUMNS.items():
                                if not column_config.get('push_to_device', True):
                                    continue
                                target_column = CONFIG.get(config_name, '')
                                if not target_column or config_name == 'column_sidecar':
                                    continue
                                calibre_value = metadata.get(target_column)
                                if calibre_value is not None:
                                    fields_to_update[config_name] = calibre_value

                            if fields_to_update:
                                update_result, update_details = self.update_sidecar_fields(
                                    sidecar_path, sidecar_contents, fields_to_update
                                )
                                if update_result == "success":
                                    debug_print(f'Also updated {len(fields_to_update)} field(s) after restore')

                    results.append({
                        'title': title,
                        'result': 'New sidecar created',
                        'book_uuid': calibre_uuid or book_uuid,
                        'sidecar_path': sidecar_path,
                    })
                elif result == "failure":
                    num_fail += 1
                    results.append({
                        'title': title,
                        **details,
                        'book_uuid': calibre_uuid or book_uuid,
                        'sidecar_path': sidecar_path,
                    })
                elif result == "no_metadata":
                    num_no_metadata += 1
                    results.append({
                        'title': title,
                        **details,
                        'book_uuid': calibre_uuid or book_uuid,
                        'sidecar_path': sidecar_path,
                    })

        # Process resolved conflicts (update existing sidecars)
        # Build lookup from calibre_uuid to book_key (since conflicts use calibre_uuid)
        uuid_to_book_key = {}
        for book_key, (sidecar_contents, book_id, calibre_uuid) in sidecar_cache.items():
            uuid_to_book_key[calibre_uuid] = book_key

        if resolved_conflicts:
            # Group conflicts by book (using calibre_uuid)
            conflicts_by_uuid = {}
            for conflict in resolved_conflicts:
                if conflict.book_uuid not in conflicts_by_uuid:
                    conflicts_by_uuid[conflict.book_uuid] = []
                conflicts_by_uuid[conflict.book_uuid].append(conflict)

            for calibre_uuid, book_conflicts in conflicts_by_uuid.items():
                # Find the book_key from calibre_uuid
                book_key = uuid_to_book_key.get(calibre_uuid)
                if not book_key:
                    debug_print(f'Could not find book_key for uuid {calibre_uuid}')
                    continue

                book_info = books_with_sidecar.get(book_key)
                if not book_info:
                    continue
                sidecar_path = book_info['sidecar_path']

                # Get cached sidecar
                cached = sidecar_cache.get(book_key)
                if not cached:
                    continue
                sidecar_contents, book_id, _ = cached

                metadata = db.get_metadata(book_id) if book_id else None
                title = metadata.get('title', 'Unknown') if metadata else 'Unknown'

                # Collect fields to update (where resolution is 'calibre')
                fields_to_update = {}
                for conflict in book_conflicts:
                    if conflict.resolution == 'calibre':
                        fields_to_update[conflict.field_name] = conflict.calibre_value
                    elif conflict.resolution == 'skip':
                        num_skipped += 1

                if fields_to_update:
                    result, details = self.update_sidecar_fields(
                        sidecar_path, sidecar_contents, fields_to_update
                    )
                    if result == "success":
                        num_updated_sidecars += 1
                        results.append({
                            'title': title,
                            'result': f'Updated {len(fields_to_update)} field(s)',
                            'book_uuid': calibre_uuid,
                            'sidecar_path': sidecar_path,
                            **details,
                        })
                    else:
                        num_fail += 1
                        results.append({
                            'title': title,
                            **details,
                            'book_uuid': calibre_uuid,
                            'sidecar_path': sidecar_path,
                        })

        # Show results dialog
        if not silent:
            results_message = (
                f'Books on device without sidecars: {len(books_without_sidecar)}\n'
                f'Books on device with sidecars: {len(books_with_sidecar)}\n\n'
                f'New sidecars created: {num_new_sidecars}\n'
                f'Existing sidecars updated: {num_updated_sidecars}\n'
                f'Fields skipped: {num_skipped}\n'
                f'Failed: {num_fail}\n'
                f'No metadata to push: {num_no_metadata}\n'
            )

            total_success = num_new_sidecars + num_updated_sidecars
            if total_success > 0 and num_fail == 0:
                SyncCompletionDialog(
                    self.gui,
                    'Success',
                    results_message,
                    results,
                    'info'
                )
            elif total_success > 0 and num_fail > 0:
                SyncCompletionDialog(
                    self.gui,
                    'Partial Success',
                    results_message,
                    results,
                    'warn'
                )
            elif num_fail > 0:
                SyncCompletionDialog(
                    self.gui,
                    'Failure',
                    results_message,
                    results,
                    'error'
                )
            else:
                # No updates needed (nothing to sync or all skipped)
                SyncCompletionDialog(
                    self.gui,
                    'No Updates Needed',
                    results_message,
                    results,
                    'info'
                )

    def sync_progress_from_progresssync(self, silent=False):
        """Use KOReader's ProgressSync Server to update Calibre metadata rather than a manual sync.

        Intended to easily update Calibre with the latest reading progress from KOReader.

        :return:
        """

        debug_print = partial(
            module_debug_print,
            'KoreaderAction:sync_progress_from_progresssync:'
        )

        md5_column = CONFIG["column_md5"]
        if md5_column == '':
            error_dialog(
                self.gui,
                'Failure',
                'MD5 column not mapped, impossible to get metadata from Progress Sync Server',
                show=True,
                show_copy_button=False
            )
            return None

        if CONFIG["progress_sync_password"] == '':
            error_dialog(
                self.gui,
                'Failure',
                'Progress Sync Account is not logged in, add credentials in plugin settings',
                show=True,
                show_copy_button=False
            )
            return None

        status_key = CONFIG['column_status']
        read_percent_key = CONFIG['column_percent_read_int'] or CONFIG['column_percent_read']
        if read_percent_key == '' or status_key == '':
            error_dialog(
                self.gui,
                'Failure',
                'This feature needs a KOReader Progress (int or float) and Status Text column.\n'
                'Add those in plugin settings and try again.',
                show=True,
                show_copy_button=False
            )
            return None

        'Get list of books with MD5 column'
        db = self.gui.current_db.new_api
        books_with_md5 = db.search(f'{md5_column}:!''')

        results = []
        num_success = 0
        num_skip = 0

        headers = {
            'x-auth-user': CONFIG["progress_sync_username"],
            'x-auth-key': CONFIG["progress_sync_password"],
            'Accept': 'application/vnd.koreader.v1+json',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache',
            'User-Agent': f'CalibreKOReaderSync/{self.version}'
        }

        for book_id in books_with_md5:
            metadata = db.get_metadata(book_id)
            md5_value = metadata.get(md5_column)
            book_uuid = metadata.get('uuid')
            title = metadata.get('title')

            # Only get sync status if curr progress < 100 and status = reading or if curr_progress/status is not set yet
            metadata_status = metadata.get(status_key)
            metadata_read_percent = metadata.get(read_percent_key)
            if (metadata_status is None or metadata_status == "reading") and (metadata_read_percent is None or metadata_read_percent < 100):
                try:
                    url = f'{CONFIG["progress_sync_url"]}/syncs/progress/{md5_value}'
                    request = Request(url, headers=headers)
                    with urlopen(request, timeout=20) as response:
                        response_data = response.read()
                        if response_data == b'{}':
                            results.append({
                                'md5_value': md5_value,
                                'error': 'No ProgressSync entry for md5 hash'
                            })
                            num_skip += 1
                            continue
                        progress_data = json.loads(response_data.decode('utf-8'))

                    # Kinda Janky edge case handling
                    if len(str(progress_data)) < 8:
                        continue

                    # List of keys to check
                    ProgressSync_Columns = [
                        'column_percent_read', 'column_percent_read_int', 'column_last_read_location', 'column_date_synced']

                    # Map of progress_data keys to match each config key
                    progress_mapping = {
                        'column_percent_read': progress_data['percentage'] if not CONFIG["checkbox_percent_read_100"] else progress_data['percentage']*100,
                        'column_percent_read_int': round(progress_data['percentage']*100),
                        'column_last_read_location': progress_data['progress'],
                        'column_date_synced': datetime.fromtimestamp(progress_data['timestamp']/1000, tz=local_tz)
                        # Device and Device ID could also be added
                    }
                    # Change percentage to be human readable on summary screen
                    if CONFIG["checkbox_percent_read_100"]:
                        progress_data['percentage']*=100

                    # Dictionary to store values to be updated
                    keys_values_to_update = {}

                    for key in ProgressSync_Columns:
                        # Get internal column name from CONFIG
                        internal_column = CONFIG.get(key, '')
                        if not internal_column:  # Skip if internal column name is blank
                            continue

                        # Get current value from metadata
                        current_value = metadata.get(internal_column)
                        remote_value = progress_mapping[key]

                        # Compare current and remote values
                        if current_value != remote_value:
                            keys_values_to_update[internal_column] = remote_value
                        # TODO This is redundant isn't it? I can remove a whole chunk of this ngl.

                    # Update only if there are differences
                    if keys_values_to_update:
                        operation_status, result = self.update_metadata(
                            book_id, db, keys_values_to_update)
                    else:
                        result = {}

                    results.append({
                        **result,
                        'title': title,
                        'book_uuid': book_uuid,
                        'md5_value': md5_value,
                        **progress_data
                    })
                    num_success += 1

                except (HTTPError, URLError) as e:
                    msg = f'Failed to make progress sync query: {url}, error: {str(e)}'
                    debug_print(msg)
                    results.append({
                        'title': title,
                        'book_uuid': book_uuid,
                        'md5_value': md5_value,
                        'error': 'No data received'
                    })
                    num_skip += 1

            else:
                results.append({
                    'title': title,
                    'book_uuid': book_uuid,
                    'md5_value': md5_value,
                    'error': 'Book has already been read'
                })
                num_skip += 1

        if not silent:
            results_message = (
                f'Total books with MD5 values: {len(books_with_md5)}\n\n'
                f'Successful syncs: {num_success}\n'
                f'Failed/Skipped syncs: {num_skip}\n\n'
            )

            if num_success > 0 and num_skip == 0:
                SyncCompletionDialog(
                    self.gui,
                    'Progress sync finished',
                    results_message + 'All looks good!\n\n',
                    results,
                    'info'
                )
            elif num_skip > 0:
                SyncCompletionDialog(
                    self.gui,
                    'Some syncs failed',
                    results_message + 'There were some errors during the sync process!\n'
                    'Please investigate and report if it looks like a bug\n\n',
                    results,
                    'warn'
                )
            else:
                SyncCompletionDialog(
                    self.gui,
                    'No successful syncs',
                    results_message + 'No successful syncs\n'
                    'Please investigate and report if it looks like a bug\n\n',
                    results,
                    'error'
                )

    def scheduled_progress_sync(self):
        def scheduledTask():
            # Set another timer for the next day and order sync
            QTimer.singleShot(24 * 3600 * 1000, scheduledTask)
            self.sync_progress_from_progresssync(
                silent=True if not DEBUG else False)

        def main():
            # Get current local time
            currentTime = QTime.currentTime()

            # Set target time to user inputted time
            targetTime = QTime(
                CONFIG["scheduleSyncHour"], CONFIG["scheduleSyncMinute"])

            # Calculate the time difference
            timeDiff = currentTime.msecsTo(targetTime)

            # If target time has already passed today, set the target time for tomorrow
            if timeDiff < 0:
                timeDiff = timeDiff + 86400000

            # Create a QTimer to trigger the task at the desired time
            QTimer.singleShot(timeDiff, scheduledTask)

        main()  # Runs scheduled_progress_sync

    def sync_to_calibre(self, silent=False):
        """This plugin's main purpose. It syncs the contents of
        KOReader's metadata sidecar files into calibre's metadata.

        Now includes conflict detection: before syncing, compares device
        and Calibre values and shows a resolution dialog for conflicts.

        :return:
        """
        debug_print = partial(
            module_debug_print,
            'KoreaderAction:sync_to_calibre:'
        )

        device = self.get_connected_device()

        if not self.check_device(device):
            return None

        db = self.gui.current_db.new_api
        debug_print(f'Current library path: {self.gui.current_db.library_path}')
        debug_print(f'All book IDs in library: {sorted(db.all_book_ids())}')
        book_info_dict = self.get_paths(device)
        debug_print('book_info_dict:', book_info_dict)

        # Phase 1: Collect sidecars and detect conflicts
        all_conflicts = []
        sidecar_cache = {}  # Cache: book_key -> (sidecar_contents, title, metadata, sidecar_path)

        if not silent:
            # Show a simple progress message while collecting sidecars
            progress = ProgressDialog(self.gui, "Reading sidecars...", len(book_info_dict))
            progress.show()
            QApplication.processEvents()

        for idx, (book_key, book_info) in enumerate(book_info_dict.items()):
            sidecar_path = book_info['sidecar_path']
            book_uuid = book_info['uuid']
            app_id = book_info['application_id']
            device_title = book_info['title']

            debug_print(f'Phase 1 - Checking [{idx+1}/{len(book_info_dict)}]: {sidecar_path}')
            debug_print(f'  Device title: {device_title}')
            debug_print(f'  Device UUID: {book_uuid}')
            debug_print(f'  Application ID (Calibre book_id): {app_id}')

            sidecar_contents = self.get_sidecar(device, sidecar_path)
            if isinstance(sidecar_contents, GetSidecarStatus):
                debug_print(f'  SKIP: Sidecar status = {sidecar_contents}')
                continue

            # Use Calibre's matching (application_id) - this matches what Calibre shows in "On Device"
            book_id = None

            if app_id is not None:
                # application_id is Calibre's book_id for matched books
                # Convert to int if it's a string
                try:
                    book_id = int(app_id)
                except (ValueError, TypeError):
                    book_id = app_id
                debug_print(f'  Using Calibre application_id: app_id={app_id} (type={type(app_id).__name__}), book_id={book_id}')
            else:
                debug_print(f'  No application_id - book not matched by Calibre')

            if not book_id:
                debug_print(f'  SKIP: Book not found in Calibre library!')
                debug_print(f'  Hint: Neither application_id ({app_id}) nor UUID ({book_uuid}) matched')
                continue

            metadata = db.get_metadata(book_id)
            debug_print(f'  Raw metadata type: {type(metadata)}')
            debug_print(f'  Raw metadata title attr: {getattr(metadata, "title", "NO ATTR")}')
            title = metadata.get('title', 'Unknown')
            calibre_uuid = metadata.get('uuid', 'NO UUID')
            debug_print(f'  Found in Calibre: "{title}" (book_id={book_id}, Calibre UUID: {calibre_uuid})')

            # Cache for later use - use book_key for consistency
            sidecar_cache[book_key] = (sidecar_contents, title, metadata, sidecar_path, book_id)
            debug_print(f'  Added to cache')

            # Detect conflicts - use calibre_uuid for conflict tracking
            conflicts = self.detect_conflicts(
                calibre_uuid, title, sidecar_path, metadata, sidecar_contents, 'to_calibre'
            )
            all_conflicts.extend(conflicts)
            debug_print(f'  Conflicts found: {len(conflicts)}')

            if not silent:
                progress.setValue(idx + 1, title)
                QApplication.processEvents()

        if not silent:
            progress.close()

        debug_print(f'Phase 1 complete: {len(sidecar_cache)} books cached, {len(all_conflicts)} conflicts found')

        # Phase 2: Show conflict resolution dialog if there are conflicts
        resolved_conflicts = []
        conflict_resolutions = {}  # book_uuid -> {field_name -> resolution}

        if all_conflicts and not silent:
            dialog = ConflictResolutionDialog(self.gui, all_conflicts, 'to_calibre')
            if dialog.exec_() == QDialog.Accepted:
                resolved_conflicts = dialog.get_resolved_conflicts()
                # Build lookup: book_uuid -> {field_name -> resolution}
                for conflict in resolved_conflicts:
                    if conflict.book_uuid not in conflict_resolutions:
                        conflict_resolutions[conflict.book_uuid] = {}
                    conflict_resolutions[conflict.book_uuid][conflict.field_name] = conflict.resolution
            else:
                # User cancelled
                info_dialog(
                    self.gui,
                    'Cancelled',
                    'Sync from KOReader was cancelled.',
                    show=True,
                    show_copy_button=False
                )
                return None

        # Phase 3: Perform sync with resolved conflicts
        class KOSyncWorker(QThread):
            progress_update = pyqtSignal(int, str)
            finished_signal = pyqtSignal(dict)

            def __init__(self, action, db, book_info_dict, sidecar_cache, conflict_resolutions):
                super().__init__()
                self.action = action
                self.db = db
                self.book_info_dict = book_info_dict
                self.sidecar_cache = sidecar_cache
                self.conflict_resolutions = conflict_resolutions

            def run(self):
                results = []
                num_success = 0
                num_fail = 0
                num_skip = 0

                for idx, (book_key, book_info) in enumerate(self.book_info_dict.items()):
                    sidecar_path = book_info['sidecar_path']
                    book_uuid = book_info['uuid']
                    app_id = book_info['application_id']

                    debug_print(f'Processing sidecar [{idx+1}/{len(self.book_info_dict)}]: {sidecar_path}')
                    debug_print(f'  Book key: {book_key}, UUID: {book_uuid}, app_id: {app_id}')

                    # Use cached sidecar if available (cache uses book_key)
                    if book_key in self.sidecar_cache:
                        sidecar_contents, title, metadata, cached_path, book_id = self.sidecar_cache[book_key]
                        calibre_uuid = metadata.get('uuid', 'NO UUID')
                        debug_print(f'  Using cached sidecar for: {title} (calibre_uuid: {calibre_uuid})')
                    else:
                        debug_print(f'  Book key not in cache, skipping (not found in Calibre in Phase 1)')
                        status = 'skipped, not found in Calibre library'
                        debug_print(f'  SKIP: {status}')
                        append_results(results, None, status, book_uuid or book_key, sidecar_path)
                        num_skip += 1
                        continue

                    self.progress_update.emit(idx + 1, title)
                    if DEBUG:
                        time.sleep(.4)

                    keys_values_to_update = {}
                    # Use calibre_uuid for conflict resolution lookup (that's what detect_conflicts uses)
                    book_resolutions = self.conflict_resolutions.get(calibre_uuid, {})
                    debug_print(f'  conflict_resolutions keys: {list(self.conflict_resolutions.keys())}')
                    debug_print(f'  Looking up calibre_uuid: {calibre_uuid}')
                    debug_print(f'  book_resolutions: {book_resolutions}')

                    for config_name, column in COLUMNS.items():
                        target = CONFIG[config_name]

                        if target == '':
                            continue

                        # Check if this field has a resolution
                        if config_name in book_resolutions:
                            resolution = book_resolutions[config_name]
                            debug_print(f'  Field {config_name}: resolution={resolution}')
                            if resolution == 'calibre':
                                # Keep Calibre value - don't update
                                debug_print(f'    -> Keeping Calibre value')
                                continue
                            elif resolution == 'skip':
                                # Skip this field
                                debug_print(f'    -> Skipping')
                                continue
                            # resolution == 'device' means use device value (continue normally)
                            debug_print(f'    -> Using device value')

                        # Special handling for date started/finished
                        if config_name == 'column_date_book_started':
                            if metadata.get(target) is None and sidecar_contents.get('summary', {}).get('status') == 'reading':
                                sidecar_contents.setdefault('calculated', {})['date_book_started'] = sidecar_contents.get('calculated', {}).get('date_status_changed')
                        if config_name == 'column_date_book_finished':
                            if metadata.get(target) is None and sidecar_contents.get('summary', {}).get('status') == 'complete':
                                sidecar_contents.setdefault('calculated', {})['date_book_finished'] = sidecar_contents.get('calculated', {}).get('date_status_changed')

                        data_location = column['data_location']
                        value = sidecar_contents

                        for subproperty in data_location:
                            if isinstance(value, dict) and subproperty in value:
                                value = value[subproperty]
                            else:
                                debug_print(f'subproperty "{subproperty}" not found')
                                value = None
                                break

                        if value is None:
                            continue

                        # Transform value if required
                        if 'transform' in column:
                            debug_print('transforming value for ', target)
                            try:
                                value = column['transform'](value)
                            except Exception as e:
                                debug_print(f'Transform failed: {e}')
                                continue

                        keys_values_to_update[target] = value

                    debug_print(f'  keys_values_to_update: {keys_values_to_update}')
                    operation_status, result = self.action.update_metadata(
                        book_id, self.db, keys_values_to_update
                    )

                    results.append({
                        **result,
                        'title': title,
                        'book_uuid': calibre_uuid,
                        'sidecar_path': sidecar_path,
                        **({'updated': json.dumps(keys_values_to_update, default=str)} if DEBUG else {})
                    })

                    if operation_status == OperationStatus.PASS:
                        num_success += 1
                    elif operation_status == OperationStatus.FAIL:
                        num_fail += 1
                    elif operation_status == OperationStatus.SKIP:
                        num_skip += 1

                self.finished_signal.emit({
                    'results': results,
                    'num_success': num_success,
                    'num_fail': num_fail,
                    'num_skip': num_skip
                })

        startTime = time.perf_counter()
        self.koSyncWorker = KOSyncWorker(self, db, book_info_dict, sidecar_cache, conflict_resolutions)
        progress_dialog = None
        if not silent and len(book_info_dict) > 10:
            progress_dialog = ProgressDialog(
                self.gui, "Syncing Sidecars...", len(book_info_dict))
            progress_dialog.show()
            self.koSyncWorker.progress_update.connect(progress_dialog.setValue)

        def on_finished(res):
            if not silent:
                if progress_dialog:
                    progress_dialog.close()
                results_message = (
                    f"Total targets found: {len(book_info_dict)}\n\n"
                    f"Metadata sync succeeded for: {res['num_success']}\n"
                    f"Metadata sync skipped for: {res['num_skip']}\n"
                    f"Metadata sync failed for: {res['num_fail']}\n"
                    f"Time taken: {time.perf_counter() - startTime:.4f} seconds.\n\n"
                )
                res['results'].sort(key=lambda row: (
                    not row.get('error', False), -len(row)))
                if res['num_success'] > 0 and res['num_fail'] == 0:
                    SyncCompletionDialog(
                        self.gui,
                        'Metadata sync finished',
                        results_message + 'All looks good!\n\n',
                        res['results'],
                        'info'
                    )
                elif res['num_fail'] > 0:
                    SyncCompletionDialog(
                        self.gui,
                        'Some sync failed',
                        results_message + 'There was some error during sync process!\n'
                        'Please investigate and report if it looks like a bug\n\n',
                        res['results'],
                        'error'
                    )
                elif res['num_success'] == 0 and res['num_fail'] == 0:
                    SyncCompletionDialog(
                        self.gui,
                        'No errors but not successful syncs',
                        results_message + 'No errors but no successful syncs\n'
                        'Do you have book(s) which are ready to be sync?\n'
                        'Please investigate and report if it looks like a bug\n\n',
                        res['results'],
                        'warn'
                    )
                else:
                    error_dialog(
                        self.gui,
                        'Edge case',
                        results_message + 'Seems like a bug, please report ASAP\n\n',
                        det_msg=json.dumps(res['results'], indent=2),
                        show=True,
                        show_copy_button=False
                    )
        self.koSyncWorker.finished_signal.connect(on_finished)
        self.koSyncWorker.start()


class ProgressDialog(QDialog):
    def __init__(self, parent, title: str, count: int):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModal)
        layout = QVBoxLayout(self)
        self.progressBar = QProgressBar(self)
        self.progressBar.setMinimum(0)
        self.progressBar.setMaximum(count)
        self.progressBar.setFormat("%v of %m")
        layout.addWidget(self.progressBar)
        self.currBook = QLabel('Beginning Sync')
        layout.addWidget(self.currBook)

    def setValue(self, idx: int, bookTitle: str):
        self.progressBar.setValue(idx)
        self.currBook.setText(bookTitle)


class SyncCompletionDialog(QDialog):
    def __init__(self, parent=None, title="", msg="", results=None, type=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(800)
        self.setMinimumHeight(800)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Main Message Area
        mainMessageLayout = QHBoxLayout()
        type_icon = {
            'info': 'dialog_information',
            'error': 'dialog_error',
            'warn': 'dialog_warning',
        }.get(type)
        if type_icon is not None:
            icon = QIcon.ic(f'{type_icon}.png')
            self.setWindowIcon(icon)
            icon_widget = QLabel(self)
            icon_widget.setPixmap(icon.pixmap(64, 64))
            mainMessageLayout.addWidget(icon_widget)
        message_label = QLabel(msg)
        mainMessageLayout.addWidget(message_label)
        mainMessageLayout.addStretch()  # Left align the message/text
        layout.addLayout(mainMessageLayout)

        # Table in scrollable area if results are provided
        if results:
            self.table_area = QScrollArea(self)
            self.table_area.setWidgetResizable(True)
            table = self.create_results_table(results)
            self.table_area.setWidget(table)
            layout.addWidget(self.table_area)

        # Bottom Buttons
        bottomButtonLayout = QHBoxLayout()
        if results:
            copy_button = QPushButton("COPY", self)
            copy_button.setFixedWidth(200)
            copy_button.setIcon(QIcon.ic('edit-copy.png'))
            copy_button.clicked.connect(lambda: (
                QApplication.clipboard().setText(str(results)),
                copy_button.setText('Copied')
            ))
            bottomButtonLayout.addWidget(copy_button)
        bottomButtonLayout.addStretch()  # Right align the rest of this layout
        ok_button = QPushButton("OK", self)
        ok_button.setFixedWidth(200)
        ok_button.setIcon(QIcon.ic('ok.png'))
        ok_button.clicked.connect(self.accept)
        ok_button.setDefault(True)
        bottomButtonLayout.addWidget(ok_button)
        layout.addLayout(bottomButtonLayout)

        self.show()

    def create_results_table(self, results):
        # Get all possible headers from results and save as set
        all_headers = {key for result in results for key in result.keys()}

        headers = []
        custom_columns = sorted(h for h in all_headers
                                if h not in ('title', 'book_uuid', 'result', 'error'))

        if 'title' in all_headers:
            headers.append('title')
        if 'book_uuid' in all_headers:
            headers.append('book_uuid')
        if 'result' in all_headers:
            headers.append('result')
        if 'error' in all_headers:
            headers.append('error')
        if custom_columns:
            headers.extend(custom_columns)

        table = QTableWidget()
        table.setRowCount(len(results))
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)

        for row, result in enumerate(results):
            for col, header in enumerate(headers):
                item = QTableWidgetItem(str(result.get(header, "")))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                # Set the tooltip to the full text
                item.setToolTip(item.text())
                table.setItem(row, col, item)

        max_lines = 1
        for col, header in enumerate(headers):
            words, line, lines, col_len_limit = header.split(
            ), "", [], max(table.columnWidth(col) // 7, 10)
            for word in words:
                line = f"{line} {word}".strip()
                if len(line) > col_len_limit:
                    lines.append(line.rsplit(' ', 1)[0])
                    line = word if ' ' in line else ''
            lines.append(line)
            max_lines = max(len(lines), max_lines)
            wrapped = '\n'.join(lines)
            table.setHorizontalHeaderItem(col, QTableWidgetItem(wrapped))
        table.horizontalHeader().setFixedHeight(20 * max_lines)  # Default = 20

        return table


class ConflictResolutionDialog(QDialog):
    """Dialog for resolving conflicts between Calibre and device metadata.

    Shows a table of all conflicts with per-row resolution dropdowns.
    Provides bulk action buttons and returns the resolved conflicts list.
    """

    def __init__(self, parent, conflicts: List[ConflictItem], direction: str):
        super().__init__(parent)
        self.conflicts = conflicts
        self.direction = direction
        self.resolution_combos = []

        direction_label = "Calibre" if direction == "to_calibre" else "Device"
        self.setWindowTitle(f'Conflict Resolution - Sync to {direction_label}')
        self.setMinimumWidth(900)
        self.setMinimumHeight(600)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Header message
        header_msg = (
            f"The following books have values that differ between Calibre and the device.\n"
            f"Choose which value to keep for each field. Direction: syncing to {direction_label}."
        )
        header_label = QLabel(header_msg)
        header_label.setWordWrap(True)
        layout.addWidget(header_label)

        # Bulk action buttons
        bulk_layout = QHBoxLayout()
        keep_calibre_all = QPushButton("Keep All Calibre")
        keep_calibre_all.clicked.connect(lambda: self.set_all_resolutions('calibre'))
        bulk_layout.addWidget(keep_calibre_all)

        keep_device_all = QPushButton("Keep All Device")
        keep_device_all.clicked.connect(lambda: self.set_all_resolutions('device'))
        bulk_layout.addWidget(keep_device_all)

        skip_all = QPushButton("Skip All")
        skip_all.clicked.connect(lambda: self.set_all_resolutions('skip'))
        bulk_layout.addWidget(skip_all)

        bulk_layout.addStretch()
        layout.addLayout(bulk_layout)

        # Create table
        self.table = QTableWidget()
        self.table.setRowCount(len(conflicts))
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            'Title', 'Field', 'Calibre Value', 'Device Value', 'Action'
        ])
        # Handle both PyQt5 and PyQt6 API differences
        try:
            select_rows = QAbstractItemView.SelectionBehavior.SelectRows
        except AttributeError:
            select_rows = QAbstractItemView.SelectRows
        self.table.setSelectionBehavior(select_rows)
        try:
            stretch_mode = QHeaderView.ResizeMode.Stretch
            fixed_mode = QHeaderView.ResizeMode.Fixed
        except AttributeError:
            stretch_mode = QHeaderView.Stretch
            fixed_mode = QHeaderView.Fixed
        self.table.horizontalHeader().setSectionResizeMode(stretch_mode)
        self.table.horizontalHeader().setSectionResizeMode(4, fixed_mode)
        self.table.setColumnWidth(4, 150)

        for row, conflict in enumerate(conflicts):
            # Title
            title_item = QTableWidgetItem(conflict.book_title or 'Unknown')
            title_item.setFlags(title_item.flags() & ~Qt.ItemIsEditable)
            title_item.setToolTip(conflict.book_title or 'Unknown')
            self.table.setItem(row, 0, title_item)

            # Field name
            field_item = QTableWidgetItem(conflict.field_display_name)
            field_item.setFlags(field_item.flags() & ~Qt.ItemIsEditable)
            field_item.setToolTip(conflict.field_name)
            self.table.setItem(row, 1, field_item)

            # Calibre value
            calibre_str = self._format_value(conflict.calibre_value)
            calibre_item = QTableWidgetItem(calibre_str)
            calibre_item.setFlags(calibre_item.flags() & ~Qt.ItemIsEditable)
            calibre_item.setToolTip(calibre_str)
            self.table.setItem(row, 2, calibre_item)

            # Device value
            device_str = self._format_value(conflict.device_value)
            device_item = QTableWidgetItem(device_str)
            device_item.setFlags(device_item.flags() & ~Qt.ItemIsEditable)
            device_item.setToolTip(device_str)
            self.table.setItem(row, 3, device_item)

            # Resolution dropdown
            combo = QComboBox()
            combo.addItems(['Skip', 'Keep Calibre', 'Keep Device'])
            # Default based on sync direction:
            # to_calibre (from device) -> Keep Device (index 2)
            # to_device (from calibre) -> Keep Calibre (index 1)
            default_idx = 2 if self.direction == 'to_calibre' else 1
            combo.setCurrentIndex(default_idx)
            self.table.setCellWidget(row, 4, combo)
            self.resolution_combos.append(combo)

        layout.addWidget(self.table)

        # Bottom buttons
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()

        cancel_button = QPushButton("Cancel")
        cancel_button.setIcon(QIcon.ic('dialog_close.png'))
        cancel_button.clicked.connect(self.reject)
        bottom_layout.addWidget(cancel_button)

        apply_button = QPushButton("Apply")
        apply_button.setIcon(QIcon.ic('ok.png'))
        apply_button.clicked.connect(self.accept)
        apply_button.setDefault(True)
        bottom_layout.addWidget(apply_button)

        layout.addLayout(bottom_layout)

    def _format_value(self, value) -> str:
        """Format a value for display in the table."""
        if value is None:
            return "(empty)"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, float):
            if value <= 1.0:
                return f"{value:.2%}"
            return f"{value:.2f}"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        return str(value)

    def set_all_resolutions(self, resolution: str):
        """Set all dropdowns to the specified resolution."""
        index_map = {'skip': 0, 'calibre': 1, 'device': 2}
        idx = index_map.get(resolution, 0)
        for combo in self.resolution_combos:
            combo.setCurrentIndex(idx)

    def get_resolved_conflicts(self) -> List[ConflictItem]:
        """Get the list of conflicts with their resolutions set."""
        resolution_map = {0: 'skip', 1: 'calibre', 2: 'device'}
        for i, conflict in enumerate(self.conflicts):
            combo = self.resolution_combos[i]
            conflict.resolution = resolution_map.get(combo.currentIndex(), 'skip')
        return self.conflicts
