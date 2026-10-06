"""Best-effort deleted-record recovery, implemented with the standard library.

References: SQLite src/btree.c freeSpace(), dropCell(), freePage2(), and
https://www.sqlite.org/fileformat2.html#record_format . Source files are only
read as bytes. Suspect records are never inserted into the source database.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import struct
import tempfile

from wal_review import (MAX_FILE_BYTES, check_cancel, check_integrity, committed_frames,
                        immutable_connection, quoted, read_limited, table_rows)


MAX_RECOVERED = 50000
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_HISTORY = 64


def varint(data, position, end=None):
    """SQLite's 1–9 byte varint (the ninth byte contributes all eight bits)."""
    end = len(data) if end is None else min(end, len(data))
    value = 0
    for index in range(9):
        if position >= end:
            raise ValueError('Truncated varint.')
        byte = data[position]
        position += 1
        if index == 8:
            return (value << 8) | byte, position
        value = (value << 7) | (byte & 127)
        if byte < 128:
            return value, position
    raise ValueError('Invalid varint.')


def serial_size(code):
    if code in (0, 8, 9):
        return 0
    if 1 <= code <= 7:
        return (1, 2, 3, 4, 6, 8, 8)[code - 1]
    if code < 12:
        raise ValueError('Reserved serial type.')
    return (code - 12) // 2


def decode_record(data, start, end, encoding, expected_columns=None):
    header_size, position = varint(data, start, end)
    header_end = start + header_size
    if header_size < 2 or header_size > 1024 or header_end > end or position >= header_end:
        raise ValueError('Invalid record header.')
    serials = []
    while position < header_end:
        code, position = varint(data, position, header_end)
        serial_size(code)
        serials.append(code)
        if len(serials) > 256:
            raise ValueError('Too many fields.')
    if expected_columns is not None and len(serials) != expected_columns:
        raise ValueError('Column count does not match.')
    values, position = [], header_end
    for code in serials:
        size = serial_size(code)
        if position + size > end:
            raise ValueError('Incomplete record body or overflow payload.')
        raw = data[position:position + size]
        position += size
        if code == 0:
            value = None
        elif code in (8, 9):
            value = code - 8
        elif code <= 6:
            value = int.from_bytes(raw, 'big', signed=True)
        elif code == 7:
            value = struct.unpack('>d', raw)[0]
        elif code % 2:
            value = raw.decode(encoding, errors='strict')
            if any(ord(char) < 32 and char not in '\n\r\t' for char in value):
                raise ValueError('Text has control bytes; reject a weak carving candidate.')
        else:
            value = bytes(raw)
        values.append(value)
    return tuple(values), position, tuple(serials)


@dataclass
class TableSpec:
    name: str
    root: int
    columns: tuple
    ipk: object


@dataclass
class RecoveredRecord:
    status: str
    evidence: str
    table: object
    columns: tuple
    values: tuple
    source: str
    page: object = None
    offset: object = None
    rowid: object = None
    missing: tuple = ()
    candidate_tables: tuple = ()
    notes: str = ''
    raw: object = None
    identity: str = ''


@dataclass
class RecoveryReport:
    source_path: str
    source_sha256: str
    records: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    scanned_bytes: int = 0
    wal_sha256: object = None
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    page_size: int = 0


def specs_from_database(connection, report):
    specs = []
    kinds = {row[1]: (row[2], row[4]) for row in connection.execute('PRAGMA table_list')}
    for name, root, sql in connection.execute(
            "SELECT name, rootpage, sql FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name"):
        kind, without_rowid = kinds.get(name, ('table', 'WITHOUT ROWID' in (sql or '').upper()))
        info = connection.execute('PRAGMA table_xinfo(' + quoted(name) + ')').fetchall()
        if kind in ('virtual', 'shadow') or without_rowid or not root or any(row[6] for row in info):
            report.warnings.append(f'{name}: free-space carving skipped (virtual/shadow, WITHOUT ROWID, or generated columns). Valid snapshot recovery can still include ordinary historical rows.')
            continue
        columns = tuple(row[1] for row in info)
        pk = [index for index, row in enumerate(info) if row[5]]
        # INTEGER PRIMARY KEY DESC may have an index and is not a rowid alias.
        pk_index = any(row[3] == 'pk' for row in connection.execute('PRAGMA index_list(' + quoted(name) + ')'))
        ipk = pk[0] if len(pk) == 1 and info[pk[0]][2].upper() == 'INTEGER' and not pk_index else None
        specs.append(TableSpec(name, root, columns, ipk))
    return specs


def table_leaf_pages(image, spec, page_size, usable, cancel):
    pending, seen, leaves = [spec.root], set(), []
    page_count = len(image) // page_size
    while pending:
        check_cancel(cancel)
        number = pending.pop()
        if number in seen or not 1 <= number <= page_count:
            raise ValueError('Invalid or cyclic table page tree.')
        seen.add(number)
        page = image[(number - 1) * page_size:number * page_size]
        header = 100 if number == 1 else 0
        kind = page[header]
        count = int.from_bytes(page[header + 3:header + 5], 'big')
        if kind == 13:
            if header + 8 + 2 * count > usable:
                raise ValueError('Invalid cell pointer array.')
            leaves.append(number)
        elif kind == 5:
            if header + 12 + 2 * count > usable:
                raise ValueError('Invalid interior page.')
            pending.append(int.from_bytes(page[header + 8:header + 12], 'big'))
            for index in range(count):
                pointer = int.from_bytes(page[header + 12 + 2 * index:header + 14 + 2 * index], 'big')
                if pointer < header + 12 + 2 * count or pointer + 4 > usable:
                    raise ValueError('Invalid interior cell pointer.')
                pending.append(int.from_bytes(page[pointer:pointer + 4], 'big'))
        else:
            raise ValueError('Not a rowid table b-tree.')
    return leaves


def free_regions(page, header, usable):
    """Only return the unallocated gap and validated freeblocks of a leaf page."""
    count = int.from_bytes(page[header + 3:header + 5], 'big')
    pointer_end = header + 8 + 2 * count
    content = int.from_bytes(page[header + 5:header + 7], 'big') or 65536
    if pointer_end > content or content > usable:
        raise ValueError('Invalid leaf-page free space.')
    live_starts = [int.from_bytes(page[header + 8 + 2 * i:header + 10 + 2 * i], 'big') for i in range(count)]
    if any(pointer < content or pointer >= usable for pointer in live_starts):
        raise ValueError('Invalid live cell pointer.')
    regions = [(pointer_end, content, False)] if content > pointer_end else []
    block, previous_end = int.from_bytes(page[header + 1:header + 3], 'big'), content
    seen = set()
    while block:
        if block in seen or block < previous_end or block + 4 > usable:
            raise ValueError('Invalid or cyclic freeblock chain.')
        seen.add(block)
        next_block, size = struct.unpack('>HH', page[block:block + 4])
        if size < 4 or block + size > usable or any(block <= pointer < block + size for pointer in live_starts):
            raise ValueError('Freeblock overlaps live data or reserved bytes.')
        regions.append((block, block + size, True))
        previous_end = block + size
        block = next_block
    return regions


def freelist_regions(image, page_size, usable):
    count, trunk = int.from_bytes(image[36:40], 'big'), int.from_bytes(image[32:36], 'big')
    seen, regions = set(), []
    while trunk:
        if trunk in seen or not 2 <= trunk <= len(image) // page_size:
            raise ValueError('Invalid freelist trunk.')
        seen.add(trunk)
        page = image[(trunk - 1) * page_size:trunk * page_size]
        following, leaves = struct.unpack('>II', page[:8])
        if 8 + 4 * leaves > usable:
            raise ValueError('Invalid freelist leaf count.')
        # Trunk metadata has overwritten the old page header; scan only its remainder.
        regions.append((trunk, 8 + 4 * leaves, usable, False))
        for offset in range(8, 8 + 4 * leaves, 4):
            leaf = int.from_bytes(page[offset:offset + 4], 'big')
            if leaf in seen or not 2 <= leaf <= len(image) // page_size:
                raise ValueError('Invalid or duplicate freelist leaf.')
            seen.add(leaf)
            old_page = image[(leaf - 1) * page_size:leaf * page_size]
            # A freelist leaf may retain a complete old table leaf page.
            if old_page[0] == 13:
                cell_count = int.from_bytes(old_page[3:5], 'big')
                start = 8 + 2 * cell_count
                if start > usable:
                    start = 0
            else:
                start = 0
            regions.append((leaf, start, usable, False))
        trunk = following
        if len(seen) > count:
            raise ValueError('Freelist is larger than its declared count.')
    if len(seen) != count:
        raise ValueError('Freelist count does not match.')
    return regions


def meaningful(values):
    return any(isinstance(value, (str, bytes)) and len(value) >= 3 for value in values)


def cell_at(page, start, end, encoding, usable, expected_columns=None):
    payload_size, position = varint(page, start, end)
    rowid, position = varint(page, position, end)
    if payload_size < 2 or payload_size > usable - 35 or position + payload_size > end:
        raise ValueError('Not a complete, local table-leaf cell.')
    values, record_end, serials = decode_record(page, position, position + payload_size, encoding, expected_columns)
    if record_end != position + payload_size:
        raise ValueError('Payload length mismatch.')
    if rowid >= 1 << 63:
        rowid -= 1 << 64
    return values, rowid, record_end, serials


def partial_candidates(page, start, end, spec, encoding, number, source):
    if spec is None or spec.ipk != 0 or len(spec.columns) < 2:
        return
    # Common small-cell layout: [payload][rowid][header-size][NULL-IPK].
    # A deleted cell's first four bytes may become freeblock linkage and size.
    for rowid_width in (1, 2):
        serial_start = start + 3 + rowid_width
        if rowid_width == 2 and (serial_start > end or page[start + 4] != 0):
            continue
        try:
            position, serials = serial_start, []
            for _ in spec.columns[1:]:
                code, position = varint(page, position, end)
                serial_size(code)
                serials.append(code)
            header_size = 2 + position - serial_start
            payload_size = header_size + sum(serial_size(code) for code in serials)
            cell_size = 1 + rowid_width + payload_size
            if header_size >= 128 or payload_size >= 128 or start + cell_size > end:
                continue
            body_end = position + sum(serial_size(code) for code in serials)
            reconstructed = bytes((header_size, 0)) + page[serial_start:body_end]
            values, record_end, codes = decode_record(reconstructed, 0, len(reconstructed), encoding, len(spec.columns))
            if record_end != len(reconstructed) or not meaningful(values):
                continue
            yield RecoveredRecord('Partial candidate', 'Inferred small-cell header; rowid lost', spec.name,
                                  spec.columns, values, source, number, start, missing=(0,),
                                  candidate_tables=(spec.name,), raw=page[start:start + cell_size],
                                  notes='Header layout is a hypothesis. INTEGER PRIMARY KEY/rowid bytes were overwritten; '
                                        'that field is unknown, not a recovered NULL. Other fields are decoded from surviving bytes.')
        except (ValueError, UnicodeError, struct.error):
            continue


def recover_deleted(path, carve=True, include_wal=True, cancel=None, progress=None):
    """Scan an offline capture; raw files are never opened using SQLite."""
    check_cancel(cancel)
    path = Path(path).resolve()
    journal = Path(str(path) + '-journal')
    if journal.exists() and journal.stat().st_size:
        raise ValueError('A rollback journal is present. Recover a separate copy with SQLite first.')
    main = read_limited(path)
    wal_path = Path(str(path) + '-wal')
    wal = read_limited(wal_path) if wal_path.exists() else None
    if main != read_limited(path) or wal != (read_limited(wal_path) if wal_path.exists() else None):
        raise ValueError('Files changed during capture. Use an offline copy.')
    if len(main) < 100 or main[:16] != b'SQLite format 3\x00':
        raise ValueError('Not a SQLite database.')
    page_size = int.from_bytes(main[16:18], 'big') or 0
    page_size = 65536 if page_size == 1 else page_size
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1) or len(main) % page_size:
        raise ValueError('Invalid database page size or truncated file.')
    usable = page_size - main[20]
    if usable < 480:
        raise ValueError('Unsupported database reserved-page size.')
    encoding = {1: 'utf-8', 2: 'utf-16le', 3: 'utf-16be'}.get(int.from_bytes(main[56:60], 'big'))
    if encoding is None:
        raise ValueError('Unsupported text encoding.')
    report = RecoveryReport(str(path), hashlib.sha256(main).hexdigest(),
                            wal_sha256=hashlib.sha256(wal).hexdigest() if wal is not None else None,
                            page_size=page_size)
    report.warnings.append('Free-space candidates can be stale copies, old updates, or false positives; deletion is not proven. '
                           'Overwritten bytes, secure-delete erasure, VACUUM, and reused pages cannot be undone.')
    frames, commits = [], []
    if wal:
        wal_page_size, frames, commits, reason = committed_frames(wal, cancel)
        if wal_page_size != page_size:
            raise ValueError('Database and WAL page sizes differ.')
        if reason:
            report.warnings.append(reason)
        if commits and commits[-1][1] * page_size > MAX_FILE_BYTES:
            raise ValueError('Reconstructed database exceeds 256 MiB.')
    with tempfile.TemporaryDirectory(prefix='sqlite-deleted-recovery-') as directory:
        latest = Path(directory) / 'latest.db'
        latest.write_bytes(main)
        with latest.open('r+b') as output:
            for page, content in frames:
                check_cancel(cancel)
                if page <= commits[-1][1]:
                    output.seek((page - 1) * page_size)
                    output.write(content)
            if commits:
                output.truncate(commits[-1][1] * page_size)
        check_integrity(latest)
        connection = immutable_connection(latest)
        try:
            specs = specs_from_database(connection, report)
            current_schemas = dict(connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='table'"))
            current = {}
            for name, in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"):
                check_cancel(cancel)
                try:
                    current[name] = table_rows(connection, name, cancel)
                except (ValueError, sqlite3.Error) as error:
                    report.warnings.append(f'{name}: live-row comparison unavailable: {error}')
            seen = set()
            live_signatures = {}
            limit_warning = f'Recovery limited to {MAX_RECOVERED:,} records; additional results omitted.'

            def add(record):
                check_cancel(cancel)
                key = (record.table, record.columns, record.values, record.missing, record.rowid)
                if key in seen:
                    return
                if record.table in current and record.status != 'Recovered deletion':
                    columns, keys, rows, editable = current[record.table]
                    if columns == record.columns:
                        signature_key = (record.table, record.missing)
                        indices = tuple(i for i in range(len(columns)) if i not in record.missing)
                        if signature_key not in live_signatures:
                            live_signatures[signature_key] = {tuple(values[i] for i in indices) for values in rows.values()}
                        if tuple(record.values[i] for i in indices) in live_signatures[signature_key]:
                            return
                if len(report.records) >= MAX_RECOVERED:
                    if limit_warning not in report.warnings:
                        report.warnings.append(limit_warning)
                    return
                seen.add(key)
                report.records.append(record)

            if include_wal and commits:
                historical = Path(directory) / 'history.db'
                historical.write_bytes(main)
                # Evaluate the main baseline and the most recent prior commits.
                chosen = set(range(max(0, len(commits) - 1 - MAX_HISTORY), len(commits) - 1))
                if len(commits) - 1 > MAX_HISTORY:
                    report.warnings.append(f'WAL history limited to {MAX_HISTORY} prior commits plus the main-file baseline.')

                def history_rows(label):
                    if progress:
                        progress('Reading deleted rows from ' + label)
                    try:
                        check_integrity(historical)
                    except (ValueError, sqlite3.Error):
                        report.warnings.append(label + ': not a valid independent snapshot; skipped.')
                        return
                    old = immutable_connection(historical)
                    try:
                        for name, schema in old.execute("SELECT name, sql FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"):
                            check_cancel(cancel)
                            try:
                                columns, keys, rows, editable = table_rows(old, name, cancel)
                            except (ValueError, sqlite3.Error) as error:
                                report.warnings.append(f'{label}, {name}: skipped ({error}).')
                                continue
                            if name not in current:
                                report.warnings.append(f'{label}, {name}: current table missing or unsupported; historical rows are not classified as deletions.')
                                continue
                            if schema != current_schemas.get(name):
                                report.warnings.append(f'{label}, {name}: schema changed; historical comparison skipped.')
                                continue
                            current_columns, current_keys, live_rows, editable = current[name]
                            if (columns, keys) != (current_columns, current_keys):
                                report.warnings.append(f'{label}, {name}: columns or identity changed; historical comparison skipped.')
                                continue
                            for key, values in rows.items():
                                if key not in live_rows:
                                    rowid = key[0] if len(keys) == 1 and keys[0] in ('rowid', '_rowid_', 'oid') else None
                                    add(RecoveredRecord('Recovered deletion', 'Valid snapshot; identity absent now', name,
                                                        columns, values, label, rowid=rowid, identity=repr(key),
                                                        notes='This identity existed in a valid prior snapshot and is absent from the latest committed state.'))
                    finally:
                        old.close()

                history_rows('Main file before WAL')
                previous = 0
                for index, (end, size) in enumerate(commits):
                    check_cancel(cancel)
                    with historical.open('r+b') as output:
                        for page, content in frames[previous:end]:
                            if page <= size:
                                output.seek((page - 1) * page_size)
                                output.write(content)
                        output.truncate(size * page_size)
                    previous = end
                    if index in chosen:
                        history_rows(f'WAL commit {index + 1}')
            if carve:
                image = latest.read_bytes()
                regions = []
                for spec in specs:
                    try:
                        for number in table_leaf_pages(image, spec, page_size, usable, cancel):
                            page = image[(number - 1) * page_size:number * page_size]
                            for start, end, freeblock in free_regions(page, 100 if number == 1 else 0, usable):
                                regions.append((number, start, end, freeblock, spec))
                    except ValueError as error:
                        report.warnings.append(f'{spec.name}: page scan skipped ({error}).')
                try:
                    regions.extend((*region, None) for region in freelist_regions(image, page_size, usable))
                except ValueError as error:
                    report.warnings.append('Freelist scan skipped: ' + str(error))
                for region_index, (number, start, end, freeblock, spec) in enumerate(regions):
                    check_cancel(cancel)
                    if len(report.records) >= MAX_RECOVERED:
                        if limit_warning not in report.warnings:
                            report.warnings.append(limit_warning)
                        break
                    if progress and region_index % 64 == 0:
                        progress(f'Carving unused space on page {number}; {len(report.records):,} records found')
                    if report.scanned_bytes + end - start > MAX_SCAN_BYTES:
                        report.warnings.append('Free-space scan reached its 64 MiB limit; remaining regions omitted.')
                        break
                    report.scanned_bytes += end - start
                    page = image[(number - 1) * page_size:number * page_size]
                    if not any(page[start:end]):
                        continue
                    if freeblock:
                        for record in partial_candidates(page, start, end, spec, encoding, number, 'Freeblock'):
                            add(record)
                    position = start + 4 if freeblock else start
                    while position + 4 <= end:
                        if len(report.records) >= MAX_RECOVERED:
                            break
                        if position % 512 == 0:
                            check_cancel(cancel)
                        # Some SQLite versions leave freeblock metadata even when the
                        # cell is absorbed into the unallocated gap. Treat it as a
                        # hypothesis, never as an authoritative live freeblock.
                        if not freeblock and spec:
                            following, old_size = struct.unpack('>HH', page[position:position + 4])
                            if old_size >= 7 and position + old_size <= end and (not following or position + old_size <= following < usable):
                                for record in partial_candidates(page, position, position + old_size, spec, encoding, number,
                                                                 'Unallocated space (old freeblock)'):
                                    add(record)
                        if page[position] == 0:
                            position += 1
                            continue
                        try:
                            values, rowid, cell_end, serials = cell_at(page, position, end, encoding, usable,
                                                                    len(spec.columns) if spec else None)
                            if not meaningful(values):
                                raise ValueError('Weak candidate without substantial text/blob content.')
                            candidates = [item for item in specs if len(item.columns) == len(values)
                                          and (item.ipk is None or serials[item.ipk] == 0)]
                            if spec:
                                if spec not in candidates:
                                    raise ValueError('INTEGER PRIMARY KEY storage mismatch.')
                                table, columns = spec.name, spec.columns
                                if spec.ipk is not None:
                                    values = list(values)
                                    values[spec.ipk] = rowid
                                    values = tuple(values)
                            else:
                                # Freed pages have lost reliable ownership. Never assign a guessed table.
                                table, columns = None, tuple(f'column_{i + 1}' for i in range(len(values)))
                            add(RecoveredRecord('Candidate', 'Intact cell structure; deletion unproven', table, columns, values,
                                                'Freeblock interior' if freeblock else 'Unallocated table space' if spec else 'Freelist page',
                                                number, position, rowid=rowid,
                                                candidate_tables=tuple(item.name for item in candidates), raw=page[position:cell_end],
                                                notes='Unreferenced bytes may be from a deletion, an update, or page rebalancing. '
                                                      'Freelist table names, if listed, are compatibility hints only.'))
                            position = cell_end
                        except (ValueError, UnicodeError, struct.error):
                            position += 1
            check_cancel(cancel)
        finally:
            connection.close()
    return report


def export_recovered(report, destination, records=None, cancel=None):
    """Create an evidence database with typed recovered rows and provenance.

    Recovered groups use unconstrained tables so partial/duplicate rows survive.
    All work is built in a temporary database before exclusively creating output.
    """
    records = report.records if records is None else list(records)
    destination = Path(destination)
    if destination.exists() or any(Path(str(destination) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('Choose a new output filename with no existing sidecars.')
    if not records:
        raise ValueError('There are no recovered records to export.')
    with tempfile.TemporaryDirectory(prefix='sqlite-recovery-export-') as directory:
        working = Path(directory) / 'recovered.sqlite'
        connection = sqlite3.connect(working)
        try:
            connection.executescript('''
                CREATE TABLE recovery_info (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE recovered_records (
                    recovery_id INTEGER PRIMARY KEY, status TEXT, evidence TEXT, original_table TEXT,
                    candidate_tables TEXT, source TEXT, page_number INTEGER, byte_offset INTEGER,
                    original_rowid INTEGER, original_identity TEXT, missing_columns TEXT, notes TEXT,
                    raw_record BLOB, data_table TEXT, identity_column TEXT
                );
            ''')
            connection.executemany('INSERT INTO recovery_info VALUES (?, ?)', [
                ('source_path', report.source_path), ('source_sha256', report.source_sha256),
                ('wal_sha256', report.wal_sha256), ('captured_at_utc', report.captured_at),
                ('page_size', str(report.page_size)), ('scanned_unused_bytes', str(report.scanned_bytes)),
                ('warnings', json.dumps(report.warnings, ensure_ascii=False)),
                ('format', 'Deleted-record evidence export v1; candidates are not proven deletions'),
            ])
            groups = {}
            for index, record in enumerate(records, 1):
                check_cancel(cancel)
                key = (record.table, record.columns)
                if key not in groups:
                    table = f'recovered_data_{len(groups) + 1:03d}'
                    identity = '_recovery_id'
                    while identity.lower() in {name.lower() for name in record.columns}:
                        identity += '_'
                    groups[key] = table, identity
                    connection.execute('CREATE TABLE ' + quoted(table) + ' (' + quoted(identity) +
                                       ' INTEGER PRIMARY KEY, ' + ', '.join(quoted(name) for name in record.columns) + ')')
                table, identity = groups[key]
                connection.execute('INSERT INTO ' + quoted(table) + ' VALUES (' + ', '.join('?' for _ in range(len(record.values) + 1)) + ')',
                                   (index,) + record.values)
                connection.execute('INSERT INTO recovered_records VALUES (' + ', '.join('?' for _ in range(15)) + ')',
                                   (index, record.status, record.evidence, record.table,
                                    json.dumps(record.candidate_tables, ensure_ascii=False), record.source, record.page, record.offset,
                                    record.rowid, record.identity, json.dumps([record.columns[i] for i in record.missing], ensure_ascii=False),
                                    record.notes, record.raw, table, identity))
            connection.commit()
        finally:
            connection.close()
        check_integrity(working)
        check_cancel(cancel)
        created = False
        try:
            with destination.open('xb') as output:
                created = True
                with working.open('rb') as source:
                    while True:
                        check_cancel(cancel)
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
        except Exception:
            if created:
                destination.unlink()
            raise
    return len(records)
