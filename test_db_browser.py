import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from db_browser import connect_database, quote_identifier, search_database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'test # database.sqlite'
        self.connection = sqlite3.connect(self.path)
        self.connection.execute('CREATE TABLE "odd"" name" (text_value, number_value, empty_value, binary_value)')
        self.connection.execute('INSERT INTO "odd"" name" VALUES (?, ?, ?, ?)',
                                ('Alice@example.com', 12345, None, b'hello\xff'))
        self.connection.execute('CREATE VIEW sample_view AS SELECT text_value FROM "odd"" name"')
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def test_search_every_value_and_views(self):
        matches = list(search_database(self.connection, 'alice|12345|NULL|hello', True))
        self.assertEqual(len(matches), 5)
        self.assertEqual({match[2] for match in matches},
                         {'text_value', 'number_value', 'empty_value', 'binary_value'})
        self.assertIn('sample_view', {match[0] for match in matches})

    def test_blob_hex_and_quoted_identifier(self):
        matches = list(search_database(self.connection, '^0x68656c6c6fff$'))
        self.assertEqual(len(matches), 1)
        self.assertEqual(quote_identifier('odd" name'), '"odd"" name"')

    def test_read_only_and_missing_file(self):
        connection = connect_database(self.path)
        try:
            self.assertEqual(connection.execute('SELECT count(*) FROM sample_view').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM sample_view')
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('CREATE TABLE forbidden (x)')
        finally:
            connection.close()
        missing = self.path.parent / 'missing.sqlite'
        with self.assertRaises(sqlite3.OperationalError):
            connect_database(missing, writable=True)
        self.assertFalse(missing.exists())

    def test_cancelled_search(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(list(search_database(self.connection, '.', cancel=cancel)), [])

    def test_invalid_regex(self):
        import re
        with self.assertRaises(re.error):
            list(search_database(self.connection, '['))


if __name__ == '__main__':
    unittest.main()
