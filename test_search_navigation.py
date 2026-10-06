import sqlite3
import unittest

from db_browser import search_database
from search_navigation import locate_cell


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute('CREATE TABLE data (id INTEGER PRIMARY KEY, value TEXT)')
        self.connection.executemany('INSERT INTO data VALUES (?, ?)', [(i, f'value {i}') for i in range(1, 451)])

    def tearDown(self):
        self.connection.close()

    def locator(self, pattern):
        locators = []
        list(search_database(self.connection, pattern, capture=locators.append))
        return locators[-1]

    def test_page_context_and_exact_column(self):
        locator = self.locator('^value 345$')
        columns, rows, offset, row, column = locate_cell(self.connection, locator, 200)
        self.assertEqual((offset, row, column), (200, 144, 1))
        self.assertEqual(rows[row], (345, 'value 345'))
        self.assertEqual(rows[row - 1][0], 344)
        self.assertEqual(rows[row + 1][0], 346)

    def test_row_identity_survives_shifted_scan_position(self):
        locator = self.locator('^value 345$')
        self.connection.execute('DELETE FROM data WHERE id < 30')
        columns, rows, offset, row, column = locate_cell(self.connection, locator, 200)
        self.assertEqual(rows[row], (345, 'value 345'))
        self.assertEqual(offset + row, 315)

    def test_changed_or_deleted_row_is_rejected(self):
        locator = self.locator('^value 345$')
        self.connection.execute("UPDATE data SET value='changed' WHERE id=345")
        with self.assertRaisesRegex(ValueError, 'changed'):
            locate_cell(self.connection, locator, 200)
        self.connection.execute('DELETE FROM data WHERE id=345')
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            locate_cell(self.connection, locator, 200)

    def test_ambiguous_view_rows_are_rejected(self):
        self.connection.execute("CREATE VIEW duplicated AS SELECT 'same match' AS value FROM data")
        locator = self.locator('^same match$')
        with self.assertRaisesRegex(ValueError, 'identical rows'):
            locate_cell(self.connection, locator, 200)


if __name__ == '__main__':
    unittest.main()
