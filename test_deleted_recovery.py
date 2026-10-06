import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from deleted_recovery import (RecoveredRecord, RecoveryReport, decode_record,
                              export_recovered, recover_deleted, varint)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'deleted.sqlite'

    def tearDown(self):
        self.directory.cleanup()

    def create_rows(self, secure=False):
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA page_size=1024')
        connection.execute('PRAGMA secure_delete=' + ('ON' if secure else 'OFF'))
        connection.execute('CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, data BLOB, score REAL)')
        rows = [(1, 'first surviving person', 20, b'first', 1.5),
                (2, 'deleted person é', -200, b'\x00\xffrecovery', 2.25),
                (3, 'last surviving person', 30, b'last', 3.5)]
        connection.executemany('INSERT INTO people VALUES (?, ?, ?, ?, ?)', rows)
        connection.commit()
        return connection, rows

    def test_partial_freeblock_recovery_and_source_preservation(self):
        connection, rows = self.create_rows()
        connection.execute('DELETE FROM people WHERE id=2')
        connection.commit()
        connection.close()
        before = self.path.read_bytes()
        report = recover_deleted(self.path)
        matches = [record for record in report.records if rows[1][1] in record.values]
        self.assertTrue(matches)
        record = matches[0]
        self.assertEqual(record.status, 'Partial candidate')
        self.assertEqual(record.missing, (0,))
        self.assertEqual(record.values, (None,) + rows[1][1:])
        self.assertIsNone(record.rowid)
        self.assertEqual(record.table, 'people')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(report.source_sha256, hashlib.sha256(before).hexdigest())
        self.assertFalse(any(rows[0][1] in record.values or rows[2][1] in record.values for record in report.records))

    def test_intact_cell_in_unallocated_space(self):
        connection, rows = self.create_rows()
        original = self.path.read_bytes()
        connection.execute('DELETE FROM people WHERE id=3')
        connection.commit()
        connection.close()
        report = recover_deleted(self.path)
        matches = [record for record in report.records if rows[2][1] in record.values]
        self.assertTrue(matches)
        # Older SQLite builds replace the initial four bytes even in the gap.
        self.assertEqual(matches[0].values[1:], rows[2][1:])
        # Verify the fully intact layout too, using an offline page image with
        # the old cell's bytes restored but its live cell pointer still removed.
        cell_start = int.from_bytes(original[1024 + 12:1024 + 14], 'big')
        cell_end = int.from_bytes(original[1024 + 10:1024 + 12], 'big')
        image = bytearray(self.path.read_bytes())
        image[1024 + cell_start:1024 + cell_end] = original[1024 + cell_start:1024 + cell_end]
        self.path.write_bytes(image)
        report = recover_deleted(self.path)
        matches = [record for record in report.records if rows[2][1] in record.values]
        self.assertTrue(matches)
        self.assertEqual(matches[0].values, rows[2])
        self.assertEqual(matches[0].status, 'Candidate')
        self.assertEqual(matches[0].rowid, 3)

    def test_secure_delete_and_vacuum_remove_the_target(self):
        connection, rows = self.create_rows(secure=True)
        connection.execute('DELETE FROM people WHERE id=2')
        connection.commit()
        connection.close()
        report = recover_deleted(self.path)
        self.assertFalse(any(rows[1][1] in record.values for record in report.records))
        # A separate deletion without scrubbing is removed by rebuilding the file.
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA secure_delete=OFF')
        connection.execute('DELETE FROM people WHERE id=3')
        connection.commit()
        connection.execute('VACUUM')
        connection.close()
        report = recover_deleted(self.path)
        self.assertFalse(any(rows[2][1] in record.values for record in report.records))

    def test_freelist_records_have_unknown_table_ownership(self):
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA page_size=1024')
        connection.execute('PRAGMA secure_delete=OFF')
        connection.execute('CREATE TABLE documents (id INTEGER PRIMARY KEY, content TEXT)')
        connection.executemany('INSERT INTO documents VALUES (?, ?)',
                               [(i, f'deleted-document-{i:04d}-' + 'x' * 150) for i in range(1, 151)])
        connection.commit()
        connection.execute('DELETE FROM documents')
        connection.commit()
        self.assertGreater(connection.execute('PRAGMA freelist_count').fetchone()[0], 0)
        connection.close()
        report = recover_deleted(self.path)
        matches = [record for record in report.records if record.source == 'Freelist page' and
                   any(isinstance(value, str) and value.startswith('deleted-document-') for value in record.values)]
        self.assertGreater(len(matches), 20)
        self.assertTrue(all(record.table is None and record.status == 'Candidate' for record in matches))
        self.assertTrue(all('documents' in record.candidate_tables for record in matches))

    def test_wal_history_recovers_row_created_and_deleted_after_checkpoint(self):
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT, data BLOB)')
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute("INSERT INTO items VALUES (7, 'historical deleted row', X'00FF')")
        connection.commit()
        connection.execute('DELETE FROM items WHERE id=7')
        connection.commit()
        capture = Path(self.directory.name) / 'capture.sqlite'
        shutil.copyfile(self.path, capture)
        shutil.copyfile(str(self.path) + '-wal', str(capture) + '-wal')
        connection.close()
        main_before, wal_before = capture.read_bytes(), Path(str(capture) + '-wal').read_bytes()
        report = recover_deleted(capture, carve=False)
        matches = [record for record in report.records if record.status == 'Recovered deletion']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].values, (7, 'historical deleted row', b'\x00\xff'))
        self.assertEqual(matches[0].source, 'WAL commit 1')
        self.assertEqual(matches[0].rowid, 7)
        self.assertEqual(capture.read_bytes(), main_before)
        self.assertEqual(Path(str(capture) + '-wal').read_bytes(), wal_before)
        self.assertEqual(recover_deleted(capture, carve=False, include_wal=False).records, [])

    def test_main_baseline_and_without_rowid_snapshot_deletions(self):
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE keyed (a TEXT, b INTEGER, value TEXT, PRIMARY KEY(a,b)) WITHOUT ROWID')
        connection.execute("INSERT INTO keyed VALUES ('key', 4, 'deleted composite key')")
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute('DELETE FROM keyed')
        connection.commit()
        capture = Path(self.directory.name) / 'capture.sqlite'
        shutil.copyfile(self.path, capture)
        shutil.copyfile(str(self.path) + '-wal', str(capture) + '-wal')
        connection.close()
        report = recover_deleted(capture, carve=False)
        self.assertEqual(len(report.records), 1)
        self.assertEqual(report.records[0].values, ('key', 4, 'deleted composite key'))
        self.assertEqual(report.records[0].source, 'Main file before WAL')
        self.assertEqual(report.records[0].identity, "('key', 4)")

    def test_updated_live_row_is_not_reported_as_a_snapshot_deletion(self):
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)')
        connection.execute("INSERT INTO items VALUES (1, 'old version')")
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute("UPDATE items SET value='new version' WHERE id=1")
        connection.commit()
        capture = Path(self.directory.name) / 'capture.sqlite'
        shutil.copyfile(self.path, capture)
        shutil.copyfile(str(self.path) + '-wal', str(capture) + '-wal')
        connection.close()
        report = recover_deleted(capture, carve=False)
        self.assertEqual(report.records, [])

    def test_export_preserves_types_missing_fields_and_metadata(self):
        connection, rows = self.create_rows()
        connection.execute('DELETE FROM people WHERE id=2')
        connection.commit()
        connection.close()
        report = recover_deleted(self.path)
        record = next(record for record in report.records if rows[1][1] in record.values)
        output = Path(self.directory.name) / 'recovered.sqlite'
        self.assertEqual(export_recovered(report, output, [record]), 1)
        recovered = sqlite3.connect(output)
        try:
            status, missing, table, identity = recovered.execute(
                'SELECT status, missing_columns, data_table, identity_column FROM recovered_records').fetchone()
            self.assertEqual(status, 'Partial candidate')
            self.assertEqual(json.loads(missing), ['id'])
            values = recovered.execute(f'SELECT id, name, age, data, score FROM "{table}"').fetchone()
            self.assertEqual(values, record.values)
            self.assertEqual(recovered.execute(f'SELECT typeof(age), typeof(data), typeof(score) FROM "{table}"').fetchone(),
                             ('integer', 'blob', 'real'))
            self.assertEqual(recovered.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        finally:
            recovered.close()
        with self.assertRaises(ValueError):
            export_recovered(report, output)

    def test_export_reserved_names_and_quotes(self):
        record = RecoveredRecord('Candidate', 'test evidence', 'odd" table',
                                 ('_recovery_id', 'a"b'), ('old id', b'bytes'), 'Freelist page')
        report = RecoveryReport(str(self.path), 'test hash', [record])
        output = Path(self.directory.name) / 'quoted.sqlite'
        export_recovered(report, output)
        connection = sqlite3.connect(output)
        try:
            self.assertEqual(connection.execute('SELECT * FROM recovered_data_001').fetchall(), [(1, 'old id', b'bytes')])
            self.assertEqual(connection.execute('SELECT identity_column FROM recovered_records').fetchone(), ('_recovery_id_',))
        finally:
            connection.close()

    def test_cancellation_and_invalid_file(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            recover_deleted(self.path, cancel=cancel)
        self.path.write_bytes(b'not a database')
        with self.assertRaisesRegex(ValueError, 'Not a SQLite'):
            recover_deleted(self.path)

    def test_export_cancellation_does_not_publish_a_file(self):
        record = RecoveredRecord('Candidate', 'test', None, ('value',), ('data',), 'test')
        report = RecoveryReport(str(self.path), 'test', [record])
        cancel = threading.Event()
        cancel.set()
        output = Path(self.directory.name) / 'cancelled.sqlite'
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            export_recovered(report, output, cancel=cancel)
        self.assertFalse(output.exists())

    def test_cancellation_during_publication_removes_partial_output(self):
        record = RecoveredRecord('Candidate', 'test', None, ('value',), ('data',), 'test')
        report = RecoveryReport(str(self.path), 'test', [record])
        output = Path(self.directory.name) / 'partial.sqlite'
        calls = 0

        def cancellation(cancel):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError('Operation cancelled.')

        with patch('deleted_recovery.check_cancel', cancellation):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                export_recovered(report, output)
        self.assertFalse(output.exists())

    def test_wal_snapshot_recovers_complete_overflow_payload(self):
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA page_size=1024')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE payloads (id INTEGER PRIMARY KEY, data BLOB)')
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        blob = b'\x00\xfflarge-payload' * 1000
        connection.execute('INSERT INTO payloads VALUES (?, ?)', (1, blob))
        connection.commit()
        connection.execute('DELETE FROM payloads')
        connection.commit()
        capture = Path(self.directory.name) / 'overflow.sqlite'
        shutil.copyfile(self.path, capture)
        shutil.copyfile(str(self.path) + '-wal', str(capture) + '-wal')
        connection.close()
        report = recover_deleted(capture, carve=False)
        self.assertEqual(len(report.records), 1)
        self.assertEqual(report.records[0].values, (1, blob))

    def test_scanning_and_results_limits_are_reported(self):
        connection, rows = self.create_rows()
        connection.execute('DELETE FROM people WHERE id=2')
        connection.commit()
        connection.close()
        with patch('deleted_recovery.MAX_SCAN_BYTES', 1):
            report = recover_deleted(self.path)
        self.assertTrue(any('64 MiB limit' in warning for warning in report.warnings))
        with patch('deleted_recovery.MAX_RECOVERED', 0):
            report = recover_deleted(self.path)
        self.assertTrue(any('records; additional results omitted' in warning for warning in report.warnings))


class RecordParserTests(unittest.TestCase):
    def test_nine_byte_varint(self):
        self.assertEqual(varint(b'\xff' * 9, 0), ((1 << 64) - 1, 9))
        with self.assertRaises(ValueError):
            varint(b'\x80', 0)

    def test_serial_types_signed_integer_float_blob_and_null(self):
        # Header: 6 bytes; five serial codes: NULL, int16, real, 3-byte BLOB, 3-byte text.
        data = bytes((6, 0, 2, 7, 18, 19)) + (-200).to_bytes(2, 'big', signed=True)
        import struct
        data += struct.pack('>d', 1.25) + b'\x00\xffx' + b'abc'
        values, end, serials = decode_record(data, 0, len(data), 'utf-8')
        self.assertEqual(values, (None, -200, 1.25, b'\x00\xffx', 'abc'))
        self.assertEqual(end, len(data))

    def test_utf16_and_malformed_records(self):
        data = bytes((2, 21)) + 'hé'.encode('utf-16le')
        self.assertEqual(decode_record(data, 0, len(data), 'utf-16le')[0], ('hé',))
        with self.assertRaises(ValueError):
            decode_record(bytes((2, 10)), 0, 2, 'utf-8')
        with self.assertRaises(ValueError):
            decode_record(bytes((2, 23)), 0, 2, 'utf-8')


if __name__ == '__main__':
    unittest.main()
