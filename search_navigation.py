"""Stable row identification for search-to-table navigation."""
import sqlite3


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def rowid_alias(connection, table):
    columns = {row[1].lower() for row in connection.execute('PRAGMA table_xinfo(' + quote(table) + ')')}
    kind = connection.execute('SELECT type FROM sqlite_schema WHERE name=?', (table,)).fetchone()
    if not kind or kind[0] != 'table':
        return None
    for name in ('_rowid_', 'rowid', 'oid'):
        if name not in columns:
            try:
                connection.execute('SELECT ' + name + ' FROM ' + quote(table) + ' LIMIT 0')
                return name
            except sqlite3.OperationalError:
                pass
    return None


def locate_cell(connection, locator, page_size):
    table, expected, column, alias, identity, expected_columns = locator
    cursor = connection.execute('SELECT ' + (alias + ', ' if alias else '') + '* FROM ' + quote(table))
    columns = [item[0] for item in cursor.description][1 if alias else 0:]
    if tuple(columns) != expected_columns:
        raise ValueError('The table columns changed since the search. Run the search again.')
    position = None
    for index, record in enumerate(cursor):
        values = tuple(record[1:] if alias else record)
        if alias and record[0] == identity:
            if values != expected:
                raise ValueError('The matching row changed since the search. Run the search again.')
            position = index
            break
        if not alias and values == expected:
            if position is not None:
                raise ValueError('Several identical rows exist; this result cannot be identified uniquely.')
            position = index
    if position is None:
        raise ValueError('The matching row no longer exists. Run the search again.')
    offset = position // page_size * page_size
    cursor = connection.execute('SELECT ' + (alias + ', ' if alias else '') + '* FROM ' + quote(table) + ' LIMIT ? OFFSET ?', (page_size + 1, offset))
    rows = [tuple(row[1:] if alias else row) for row in cursor.fetchall()]
    return columns, rows, offset, position - offset, column
