"""Offline WAL inspection and row-level changes through SQLite's public API.

Format/checksum algorithm: https://github.com/sqlite/sqlite/blob/master/src/wal.c
Never rewrites or removes the source database's WAL.
"""

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile


MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TABLE_CELLS = 1000000


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def immutable_connection(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)


def checksum(data, order, state=(0, 0)):
    a, b = state
    for x, y in struct.iter_unpack(order + 'II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def committed_frames(wal, cancel=None):
    """Validate header and cumulative frame checksums; ignore any invalid tail.

    Like SQLite recovery, only frames through the last valid commit count.
    """
    if len(wal) < 32:
        raise ValueError('WAL has no complete header.')
    magic, version, page_size, sequence, salt1, salt2, a, b = struct.unpack('>8I', wal[:32])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000:
        raise ValueError('Unsupported WAL header or format version.')
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise ValueError('Invalid WAL page size.')
    order = '<' if magic == 0x377f0682 else '>'
    state = checksum(wal[:24], order)
    if state != (a, b):
        raise ValueError('Invalid WAL header checksum.')
    frames, commits, last_commit = [], [], 0
    reason = ''
    for offset in range(32, len(wal), page_size + 24):
        if cancel and cancel.is_set():
            raise RuntimeError('Operation cancelled.')
        frame = wal[offset:offset + 24 + page_size]
        if len(frame) < 24 + page_size:
            reason = 'Incomplete trailing frame ignored.'
            break
        page, size, fs1, fs2, fa, fb = struct.unpack('>6I', frame[:24])
        if not page or (fs1, fs2) != (salt1, salt2):
            reason = 'Trailing frames from another WAL generation ignored.'
            break
        next_state = checksum(frame[:8], order, state)
        next_state = checksum(frame[24:], order, next_state)
        if next_state != (fa, fb):
            reason = 'Trailing frame with invalid checksum ignored.'
            break
        state = next_state
        frames.append((page, frame[24:]))
        if size:
            last_commit = len(frames)
            commits.append((last_commit, size))
    if len(frames) > last_commit:
        reason = (reason + ' Uncommitted frames ignored.').strip()
    return page_size, frames[:last_commit], commits, reason


def read_limited(path):
    with open(path, 'rb') as source:
        value = source.read(MAX_FILE_BYTES + 1)
    if len(value) > MAX_FILE_BYTES:
        raise ValueError('Database/WAL inspection supports files up to 256 MiB each.')
    return value


def check_integrity(path):
    connection = immutable_connection(path)
    try:
        results = connection.execute('PRAGMA integrity_check').fetchall()
        if results != [('ok',)]:
            raise ValueError('Snapshot integrity check failed: ' + str(results[:3]))
    finally:
        connection.close()


@dataclass
class RowChange:
    table: str
    columns: tuple
    key_columns: tuple
    key: tuple
    before: object
    after: object
    schema: str
    editable: bool

    @property
    def kind(self):
        return 'Inserted' if self.before is None else 'Deleted' if self.after is None else 'Updated'


@dataclass
class WalReview:
    directory: object
    base: Path
    latest: Path
    frames: int
    transactions: int
    changed_pages: tuple
    changes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    base_valid: bool = True

    def close(self):
        self.directory.cleanup()


def table_rows(connection, table, cancel=None):
    info = connection.execute('PRAGMA table_xinfo(' + quoted(table) + ')').fetchall()
    columns = tuple(row[1] for row in info)
    editable = all(row[6] == 0 for row in info)
    lowered = {name.lower() for name in columns}
    alias = next((name for name in ('_rowid_', 'rowid', 'oid') if name not in lowered), None)
    rowid = False
    if alias:
        try:
            connection.execute('SELECT ' + quoted(alias) + ' FROM ' + quoted(table) + ' LIMIT 0')
            # SQLite's double-quoted string fallback must not masquerade as rowid.
            connection.execute('SELECT ' + alias + ' FROM ' + quoted(table) + ' LIMIT 0')
            rowid = True
        except sqlite3.OperationalError:
            pass
    keys = (alias,) if rowid else tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
    if not keys:
        raise ValueError('No unambiguous row identity available.')
    select_columns = ', '.join(quoted(name) for name in columns)
    cursor = connection.execute('SELECT ' + (alias + ', ' if rowid else '') + select_columns + ' FROM ' + quoted(table))
    rows = {}
    for record in cursor:
        if cancel and cancel.is_set():
            raise RuntimeError('Operation cancelled.')
        values = tuple(record[1:] if rowid else record)
        key = (record[0],) if rowid else tuple(values[columns.index(name)] for name in keys)
        if any(value is None for value in key) or key in rows:
            raise ValueError('Ambiguous or NULL row identity.')
        rows[key] = values
        if len(rows) * max(1, len(columns)) > MAX_TABLE_CELLS:
            raise ValueError('Table exceeds the 1,000,000-cell review limit.')
    return columns, keys, rows, editable


def compare_rows(review, cancel=None):
    before, after = immutable_connection(review.base), immutable_connection(review.latest)
    try:
        schemas = []
        for connection in (before, after):
            schemas.append(dict(connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")))
        shadow_tables = {row[1] for row in after.execute('PRAGMA table_list') if row[2] == 'shadow'}
        for name in sorted(set(schemas[0]) | set(schemas[1])):
            if cancel and cancel.is_set():
                raise RuntimeError('Operation cancelled.')
            if name not in schemas[0] or name not in schemas[1] or schemas[0][name] != schemas[1][name]:
                review.warnings.append(f'{name}: schema changed; use a whole-database export.')
                continue
            schema = schemas[0][name]
            if 'VIRTUAL TABLE' in schema.upper() or name in shadow_tables:
                review.warnings.append(f'{name}: virtual table; use a whole-database export.')
                continue
            try:
                columns, keys, old_rows, editable = table_rows(before, name, cancel)
                new_columns, new_keys, new_rows, new_editable = table_rows(after, name, cancel)
                if (columns, keys) != (new_columns, new_keys):
                    raise ValueError('Row identity changed.')
            except (ValueError, sqlite3.Error) as error:
                review.warnings.append(f'{name}: {error}')
                continue
            for key in old_rows.keys() | new_rows.keys():
                old, new = old_rows.get(key), new_rows.get(key)
                if old != new:
                    review.changes.append(RowChange(name, columns, keys, key, old, new,
                                                   schema, editable and new_editable))
    finally:
        before.close()
        after.close()


def inspect_wal(path, cancel=None):
    """Inspect an offline DB/WAL pair without opening either source in SQLite.

    The caller must ensure other applications are not using the files. Repeated
    byte comparison detects movement but is not a substitute for that condition.
    """
    path = Path(path)
    wal_path = Path(str(path) + '-wal')
    journal = Path(str(path) + '-journal')
    if journal.exists() and journal.stat().st_size:
        raise ValueError('A rollback journal is present. Recover a separate copy with SQLite first.')
    main, wal = read_limited(path), read_limited(wal_path)
    if main != read_limited(path) or wal != read_limited(wal_path):
        raise ValueError('Database/WAL changed during capture. Close other applications and try again.')
    if main[:16] != b'SQLite format 3\x00':
        raise ValueError('Not a SQLite database.')
    page_size, frames, commits, reason = committed_frames(wal, cancel)
    database_page_size = struct.unpack('>H', main[16:18])[0]
    database_page_size = 65536 if database_page_size == 1 else database_page_size
    if database_page_size != page_size:
        raise ValueError('Database and WAL page sizes differ.')
    if not commits:
        raise ValueError('WAL has no valid committed transactions.')
    if commits[-1][1] * page_size > MAX_FILE_BYTES:
        raise ValueError('Reconstructed database exceeds the 256 MiB review limit.')
    directory = tempfile.TemporaryDirectory(prefix='sqlite-wal-review-')
    try:
        base, latest = Path(directory.name) / 'base.db', Path(directory.name) / 'latest.db'
        base.write_bytes(main)
        latest.write_bytes(main)
        with open(latest, 'r+b') as output:
            for page, content in frames:
                if cancel and cancel.is_set():
                    raise RuntimeError('Operation cancelled.')
                if page <= commits[-1][1]:
                    output.seek((page - 1) * page_size)
                    output.write(content)
            output.truncate(commits[-1][1] * page_size)
        check_integrity(latest)
        final_pages = dict(frames)
        changed = tuple(sorted(page for page, content in final_pages.items()
                               if page <= commits[-1][1] and main[(page - 1) * page_size:page * page_size] != content))
        review = WalReview(directory, base, latest, len(frames), len(commits), changed)
        if reason:
            review.warnings.append(reason)
        try:
            check_integrity(base)
        except (ValueError, sqlite3.Error) as error:
            review.base_valid = False
            review.warnings.append('Main file alone is not a valid baseline (possibly partially checkpointed). '
                                   'Only the complete WAL-inclusive export is available. ' + str(error))
        if review.base_valid:
            compare_rows(review, cancel)
        return review
    except Exception:
        directory.cleanup()
        raise


def export_snapshot(review, destination, include_wal):
    if not include_wal and not review.base_valid:
        raise ValueError('The main-file baseline failed its integrity check.')
    # Exclusive creation prevents overwriting a live database or the reviewed source.
    with open(destination, 'xb') as output, open(review.latest if include_wal else review.base, 'rb') as source:
        shutil.copyfileobj(source, output)


def apply_row_changes(connection, changes, include_wal):
    """Apply/revert reviewed differences in an existing transaction, with conflicts checked.

    Deletes happen before inserts to support unique-value swaps. Triggers and
    generated columns are refused; their side effects cannot be selectively undone.
    """
    if connection.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise ValueError('Selective replay requires foreign-key actions disabled; integrity is checked before commit.')
    pending = []
    for change in changes:
        if not change.editable:
            raise ValueError(f'{change.table}: generated/hidden columns require whole-database export.')
        schema = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (change.table,)).fetchone()
        if not schema or schema[0] != change.schema:
            raise ValueError(f'{change.table}: schema no longer matches the review.')
        if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (change.table,)).fetchone():
            raise ValueError(f'{change.table}: triggers require whole-database export.')
        where = ' AND '.join(quoted(name) + ' IS ?' for name in change.key_columns)
        select = ', '.join(quoted(name) for name in change.columns)
        found = connection.execute('SELECT ' + select + ' FROM ' + quoted(change.table) + ' WHERE ' + where, change.key).fetchall()
        if len(found) > 1:
            raise ValueError(f'{change.table}: row identity is ambiguous.')
        current = tuple(found[0]) if found else None
        desired = change.after if include_wal else change.before
        expected = change.before if include_wal else change.after
        if current == desired:
            continue
        if current != expected:
            raise ValueError(f'{change.table} {change.key}: data changed since review; refresh before editing.')
        pending.append((change, where, current, desired))
    for change, where, current, desired in pending:
        if current is not None:
            connection.execute('DELETE FROM ' + quoted(change.table) + ' WHERE ' + where, change.key)
    for change, where, current, desired in pending:
        if desired is not None:
            columns, values = list(change.columns), list(desired)
            for key_column, key_value in zip(change.key_columns, change.key):
                if key_column not in columns:
                    columns.insert(0, key_column)
                    values.insert(0, key_value)
            connection.execute('INSERT OR ABORT INTO ' + quoted(change.table) + ' (' + ', '.join(map(quoted, columns)) +
                               ') VALUES (' + ', '.join('?' for _ in values) + ')', values)
    violations = connection.execute('PRAGMA foreign_key_check').fetchmany(5)
    if violations:
        raise ValueError('Selected changes violate foreign keys: ' + str(violations))
    return len(pending)


def modify_database(path, changes, include_wal, backup_path, cancel=None):
    """Back up the locked database, then make an atomic, conflict-checked edit."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
    backup = None
    try:
        # Avoid cascading deletes during delete/reinsert; validate the final state.
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('PRAGMA defer_foreign_keys=ON')
        # A second reader backs up the committed state while our writer lock prevents edits.
        with open(backup_path, 'xb'):
            pass
        backup = sqlite3.connect(backup_path)
        reader = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            reader.backup(backup, pages=128, progress=lambda status, remaining, total:
                          check_cancel(cancel))
        finally:
            reader.close()
            backup.close()
            backup = None
        connection.set_progress_handler(lambda: int(bool(cancel and cancel.is_set())), 1000)
        count = apply_row_changes(connection, changes, include_wal)
        check_cancel(cancel)
        connection.commit()
        return count
    except Exception:
        connection.set_progress_handler(None, 0)
        connection.rollback()
        raise
    finally:
        if backup:
            backup.close()
        connection.close()


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise RuntimeError('Operation cancelled.')


def export_selected(review, destination, changes, include_wal, cancel=None):
    """Start from baseline/latest and apply/revert only the selected row changes."""
    if not review.base_valid:
        raise ValueError('Selective export requires a valid baseline.')
    working_directory = tempfile.TemporaryDirectory(prefix='sqlite-wal-export-')
    working_path = Path(working_directory.name) / 'export.db'
    export_snapshot(review, working_path, include_wal=not include_wal)
    # These copies have no sidecars. Disable WAL before starting the transaction.
    connection = sqlite3.connect(working_path)
    try:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('PRAGMA defer_foreign_keys=ON')
        connection.set_progress_handler(lambda: int(bool(cancel and cancel.is_set())), 1000)
        count = apply_row_changes(connection, changes, include_wal)
        check_cancel(cancel)
        connection.commit()
        with open(destination, 'xb') as output, open(working_path, 'rb') as source:
            shutil.copyfileobj(source, output)
        return count
    except Exception:
        connection.set_progress_handler(None, 0)
        connection.rollback()
        raise
    finally:
        connection.close()
        working_directory.cleanup()
