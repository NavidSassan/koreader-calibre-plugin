
import os
import sqlite3
from unittest.mock import MagicMock

from action import KoreaderAction


def test_dummy_data_consistency():
    # Verify dummy_library metadata.db
    conn = sqlite3.connect('dummy_library/metadata.db')
    cursor = conn.cursor()
    cursor.execute('SELECT title, uuid FROM books')
    db_books = {title: uuid for title, uuid in cursor.fetchall()}
    conn.close()

    assert "Alice's Adventures in Wonderland" in db_books
    assert "Walden, and On The Duty Of Civil Disobedience" in db_books

    # Verify dummy_device paths
    alice_path = "Carroll, Lewis/Alice's Adventures in Wonderland - Lewis Carroll.epub"
    thoreau_path = "Thoreau, Henry David/Walden, and On The Duty Of Civil Disobedience - Henry David Thoreau.epub"
    
    assert os.path.exists(os.path.join('dummy_device', alice_path))
    assert os.path.exists(os.path.join('dummy_device', thoreau_path))

def test_get_paths_with_dummy_device():
    # Mock book objects as Calibre's GUI would annotate them in
    # gui.memory_view.model().db (set_books_in_library() sets in_library /
    # application_id) - this is get_paths()'s primary source, not
    # device.books() directly. See SYNC_ARCHITECTURE.md.
    class MockBook:
        def __init__(self, uuid, path, application_id, title):
            self.uuid = uuid
            self.path = path
            self.application_id = application_id
            self.in_library = 'UUID'
            self.db_id = None
            self.title = title

    alice_book = MockBook(
        uuid='43bd8264-96fa-461a-a05e-1d1cb245d34f',
        path="Carroll, Lewis/Alice's Adventures in Wonderland - Lewis Carroll.epub",
        application_id=1,
        title="Alice's Adventures in Wonderland",
    )
    thoreau_book = MockBook(
        uuid='3393747a-f0d8-44e1-bfaf-5fad857da3eb',
        path="Thoreau, Henry David/Walden, and On The Duty Of Civil Disobedience - Henry David Thoreau.epub",
        application_id=2,
        title="Walden, and On The Duty Of Civil Disobedience",
    )

    # Instantiate action with mocks for parent and site_customization
    mock_parent = MagicMock()
    mock_site_customization = MagicMock()
    mock_site_customization.name = 'KOReader Sync'
    mock_site_customization.version = (0, 8, 0)

    action = KoreaderAction(mock_parent, mock_site_customization)
    action.gui.memory_view.model.return_value.db = [alice_book, thoreau_book]

    paths = action.get_paths()

    assert len(paths) == 2
    # Keyed by application_id; verify Alice's sidecar path generation and
    # that the in_library annotation survived into the result.
    alice_entry = paths[1]
    assert alice_entry['sidecar_path'] == "Carroll, Lewis/Alice's Adventures in Wonderland - Lewis Carroll.sdr/metadata.epub.lua"
    assert alice_entry['uuid'] == alice_book.uuid
    assert alice_entry['in_library'] == 'UUID'
