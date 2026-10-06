import csv
import sqlite3
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from grid_tools import selected_csv, write_cell, write_csv
from wal_review import (checksum, committed_frames, export_selected, export_snapshot,
                        inspect_wal, modify_database)


class ExportTests(unittest.TestCase):
    def test_sparse_selection_and_csv_escaping(self):
        rows = [('a,b', 'line\n"quote"'), (None, b'\x00\xff')]
        selected = {(0, 0), (1, 0), (1, 1)}
        headings, values = selected_csv(['A', 'B'], rows, selected)
        self.assertEqual(values, [['a,b', ''], ['NULL', '0x00ff']])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'out.csv'
            write_csv(path, ['A', 'B'], rows, {(0, 0), (0, 1)})
            with path.open(newline='') as source:
                self.assertEqual(list(csv.reader(source)), [['A', 'B'], list(rows[0])])

    def test_binary_cell_and_unicode_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cell'
            write_cell(path, b'\x00\xff\n')
            self.assertEqual(path.read_bytes(), b'\x00\xff\n')
            write_cell(path, 'é\ntext')
            self.assertEqual(path.read_text(), 'é\ntext')


class WalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'database.sqlite'
        self.connection = sqlite3.connect(self.path)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA wal_autocheckpoint=0')
        self.connection.executescript('''
            CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT, data BLOB);
            CREATE TABLE keyed (a TEXT, b INTEGER, value TEXT, PRIMARY KEY (a, b)) WITHOUT ROWID;
            INSERT INTO items VALUES (1, 'old', X'00FF'), (2, 'delete me', NULL);
            INSERT INTO keyed VALUES ('key', 1, 'before');
        ''')
        self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.connection.execute("UPDATE items SET value='new' WHERE id=1")
        self.connection.commit()
        self.connection.execute("INSERT INTO items VALUES (3, 'inserted', X'0102')")
        self.connection.commit()
        self.connection.execute('DELETE FROM items WHERE id=2')
        self.connection.execute("UPDATE keyed SET value='after'")
        self.connection.commit()
        self.review = inspect_wal(self.path)

    def tearDown(self):
        self.review.close()
        self.connection.close()
        self.directory.cleanup()

    def output(self, name):
        return Path(self.directory.name) / name

    def values(self, path):
        connection = sqlite3.connect(path)
        try:
            return connection.execute('SELECT * FROM items ORDER BY id').fetchall()
        finally:
            connection.close()

    def test_diff_and_full_snapshots(self):
        self.assertEqual(self.review.transactions, 3)
        self.assertEqual(len(self.review.changes), 4)
        self.assertEqual({change.kind for change in self.review.changes}, {'Inserted', 'Updated', 'Deleted'})
        self.assertTrue(self.review.changed_pages)
        before, after = self.output('before.db'), self.output('after.db')
        export_snapshot(self.review, before, False)
        export_snapshot(self.review, after, True)
        self.assertEqual(self.values(before), [(1, 'old', b'\x00\xff'), (2, 'delete me', None)])
        self.assertEqual(self.values(after), [(1, 'new', b'\x00\xff'), (3, 'inserted', b'\x01\x02')])
        with self.assertRaises(FileExistsError):
            export_snapshot(self.review, after, False)

    def test_selected_export_and_reverse(self):
        change = next(change for change in self.review.changes if change.table == 'items' and change.kind == 'Updated')
        selected, reverted = self.output('selected.db'), self.output('reverted.db')
        export_selected(self.review, selected, [change], True)
        export_selected(self.review, reverted, [change], False)
        self.assertEqual(self.values(selected), [(1, 'new', b'\x00\xff'), (2, 'delete me', None)])
        self.assertEqual(self.values(reverted), [(1, 'old', b'\x00\xff'), (3, 'inserted', b'\x01\x02')])

    def test_source_revert_reapply_backup_and_idempotence(self):
        backup = self.output('backup.db')
        count = modify_database(self.path, self.review.changes, False, backup)
        self.assertEqual(count, 4)
        self.assertEqual(self.values(backup), [(1, 'new', b'\x00\xff'), (3, 'inserted', b'\x01\x02')])
        self.assertEqual(self.values(self.path), [(1, 'old', b'\x00\xff'), (2, 'delete me', None)])
        self.assertEqual(modify_database(self.path, self.review.changes, True, self.output('backup2.db')), 4)
        self.assertEqual(modify_database(self.path, self.review.changes, True, self.output('backup3.db')), 0)

    def test_conflict_rolls_back_every_change(self):
        self.connection.execute("UPDATE items SET value='someone else' WHERE id=1")
        self.connection.commit()
        before = self.values(self.path)
        with self.assertRaisesRegex(ValueError, 'data changed'):
            modify_database(self.path, self.review.changes, False, self.output('conflict-backup.db'))
        self.assertEqual(self.values(self.path), before)

    def test_trigger_rejection(self):
        self.connection.execute('CREATE TRIGGER audit AFTER INSERT ON items BEGIN SELECT 1; END')
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, 'triggers'):
            modify_database(self.path, self.review.changes, False, self.output('trigger-backup.db'))

    def test_checksum_corruption_and_incomplete_tail(self):
        wal = Path(str(self.path) + '-wal').read_bytes()
        page_size, frames, commits, reason = committed_frames(wal)
        self.assertEqual(len(commits), 3)
        broken = bytearray(wal)
        broken[24] ^= 1
        with self.assertRaisesRegex(ValueError, 'header checksum'):
            committed_frames(broken)
        broken = bytearray(wal)
        broken[-1] ^= 1
        _, recovered, valid_commits, reason = committed_frames(broken)
        self.assertEqual(len(valid_commits), 2)
        self.assertIn('invalid checksum', reason)
        _, recovered, valid_commits, reason = committed_frames(wal + b'partial')
        self.assertEqual(len(valid_commits), 3)
        self.assertIn('Incomplete', reason)

    def test_uncommitted_tail_is_not_replayed(self):
        wal = bytearray(Path(str(self.path) + '-wal').read_bytes())
        page_size, frames, commits, reason = committed_frames(wal)
        offset = len(wal) - page_size - 24
        wal[offset + 4:offset + 8] = b'\x00' * 4
        order = '<' if struct.unpack('>I', wal[:4])[0] == 0x377f0682 else '>'
        previous = struct.unpack('>2I', wal[offset - page_size - 8:offset - page_size]) if offset > 32 else struct.unpack('>2I', wal[24:32])
        state = checksum(wal[offset:offset + 8], order, previous)
        state = checksum(wal[offset + 24:], order, state)
        wal[offset + 16:offset + 24] = struct.pack('>2I', *state)
        _, recovered, valid_commits, reason = committed_frames(wal)
        self.assertEqual(len(valid_commits), 2)
        self.assertEqual(len(recovered), commits[1][0])
        self.assertIn('Uncommitted', reason)

    def test_both_checksum_byte_orders(self):
        for order, magic in [('<', 0x377f0682), ('>', 0x377f0683)]:
            header = struct.pack('>6I', magic, 3007000, 512, 0, 11, 22)
            state = checksum(header, order)
            header += struct.pack('>2I', *state)
            content = b'\x01' * 512
            frame = struct.pack('>4I', 1, 1, 11, 22)
            state = checksum(frame[:8], order, state)
            state = checksum(content, order, state)
            frame += struct.pack('>2I', *state) + content
            page_size, frames, commits, reason = committed_frames(header + frame)
            self.assertEqual((page_size, frames, commits), (512, [(1, content)], [(1, 1)]))

    def test_checkpointed_wal_has_no_remaining_differences(self):
        self.connection.execute('PRAGMA wal_checkpoint(FULL)')
        review = inspect_wal(self.path)
        try:
            self.assertEqual(review.changed_pages, ())
            self.assertEqual(review.changes, [])
        finally:
            review.close()

    def test_schema_changes_exported_but_not_selectively_replayed(self):
        self.connection.execute('CREATE TABLE new_table (value)')
        self.connection.commit()
        review = inspect_wal(self.path)
        try:
            self.assertTrue(any('new_table: schema changed' in warning for warning in review.warnings))
            path = self.output('schema.db')
            export_snapshot(review, path, True)
            connection = sqlite3.connect(path)
            try:
                connection.execute('SELECT * FROM new_table')
            finally:
                connection.close()
        finally:
            review.close()

    def test_cancelled_review(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            inspect_wal(self.path, cancel)

    def test_foreign_keys_do_not_cascade_unselected_rows(self):
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript('''
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES items(id) ON DELETE CASCADE);
            INSERT INTO child VALUES (1, 1);
        ''')
        change = next(change for change in self.review.changes if change.table == 'items' and change.kind == 'Updated')
        modify_database(self.path, [change], False, self.output('fk-backup.db'))
        self.assertEqual(self.connection.execute('SELECT * FROM child').fetchall(), [(1, 1)])

    def test_foreign_key_failure_rolls_back_and_does_not_publish_export(self):
        self.connection.executescript('''
            CREATE TABLE child (parent INTEGER REFERENCES items(id));
            INSERT INTO child VALUES (3);
        ''')
        change = next(change for change in self.review.changes if change.table == 'items' and change.kind == 'Inserted')
        current = self.values(self.path)
        with self.assertRaisesRegex(ValueError, 'foreign keys'):
            modify_database(self.path, [change], False, self.output('invalid-fk-backup.db'))
        self.assertEqual(self.values(self.path), current)
        # Capture the new schema in both snapshots to test a selective export failure.
        self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.connection.execute("INSERT INTO items VALUES (4, 'another', NULL)")
        self.connection.execute('INSERT INTO child VALUES (4)')
        self.connection.commit()
        review = inspect_wal(self.path)
        try:
            selected = next(change for change in review.changes if change.table == 'items')
            output = self.output('invalid-export.db')
            with self.assertRaisesRegex(ValueError, 'foreign keys'):
                export_selected(review, output, [selected], False)
            self.assertFalse(output.exists())
        finally:
            review.close()

    def test_replace_constraints_cannot_delete_unselected_rows(self):
        path = self.output('replace.sqlite')
        connection = sqlite3.connect(path)
        review = None
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE rows (id INTEGER PRIMARY KEY, value TEXT UNIQUE ON CONFLICT REPLACE);
                INSERT INTO rows VALUES (1, 'a'), (2, 'b');
            ''')
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            connection.execute('DELETE FROM rows WHERE id=2')
            connection.execute("UPDATE rows SET value='b' WHERE id=1")
            connection.commit()
            review = inspect_wal(path)
            change = next(change for change in review.changes if change.kind == 'Deleted')
            with self.assertRaises(sqlite3.IntegrityError):
                modify_database(path, [change], False, self.output('replace-backup.db'))
            self.assertEqual(connection.execute('SELECT * FROM rows').fetchall(), [(1, 'b')])
        finally:
            if review:
                review.close()
            connection.close()


if __name__ == '__main__':
    unittest.main()
