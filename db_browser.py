#!/usr/bin/env python3
"""SQLite browser using only Python's standard library. Run: python3 db_browser.py."""

import argparse
import queue
import re
import sqlite3
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from grid_tools import CellGrid, write_cell, write_csv
from wal_review import inspect_wal, export_snapshot, export_selected, modify_database


PAGE_SIZE = 200
RESULT_LIMIT = 10000


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def connect_database(path, writable=False):
    uri = Path(path).resolve().as_uri() + ('?mode=rw' if writable else '?mode=ro')
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    return connection


def display_value(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bytes):
        return '0x' + value.hex()
    return str(value)


def database_objects(connection):
    return connection.execute(
        "SELECT name, type FROM sqlite_schema WHERE type IN ('table', 'view') "
        "ORDER BY name"
    ).fetchall()


def search_database(connection, pattern, ignore_case=False, cancel=None, progress=None):
    """Yield each matching cell, including NULL and BLOB values, in all tables/views.

    Row numbers are scan positions, not persistent database identifiers. BLOBs are
    searched as both UTF-8 text (with replacement) and their 0x-prefixed hex form.
    """
    regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    for name, kind in database_objects(connection):
        if cancel and cancel.is_set():
            return
        if progress:
            progress(name)
        cursor = connection.execute('SELECT * FROM ' + quote_identifier(name))
        columns = [item[0] for item in cursor.description]
        for row_number, row in enumerate(cursor, 1):
            if cancel and cancel.is_set():
                return
            for column, value in zip(columns, row):
                text = display_value(value)
                matched = regex.search(text) is not None
                if isinstance(value, bytes) and not matched:
                    matched = regex.search(value.decode('utf-8', errors='replace')) is not None
                if matched:
                    yield name, row_number, column, text


class Browser(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title('SQLite Database Browser')
        self.geometry('1150x760')
        self.minsize(800, 500)
        self.path = None
        self.busy = False
        self.closing = False
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.offset = 0
        self.table = None
        self.review = None
        self.writable = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Open a SQLite database to begin.')
        self.build_ui()
        self.after(80, self.poll_events)
        self.protocol('WM_DELETE_WINDOW', self.close)
        if path:
            self.after(0, lambda: self.open_database(path))

    def build_ui(self):
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill='x')
        ttk.Button(toolbar, text='Open database…', command=self.choose_database).pack(side='left')
        ttk.Checkbutton(toolbar, text='Allow database changes', variable=self.writable,
                        command=self.change_mode).pack(side='left', padx=15)
        ttk.Button(toolbar, text='Cancel operation', command=self.cancel.set).pack(side='right')
        self.filename = ttk.Label(toolbar, text='No database open')
        self.filename.pack(side='left', padx=8)
        notebook = ttk.Notebook(self)
        notebook.pack(fill='both', expand=True, padx=8)
        browse = ttk.Frame(notebook)
        sql = ttk.Frame(notebook)
        search = ttk.Frame(notebook)
        wal = ttk.Frame(notebook)
        notebook.add(browse, text='Tables & views')
        notebook.add(sql, text='SQL editor')
        notebook.add(search, text='Search all data')
        notebook.add(wal, text='WAL changes')

        pane = ttk.Panedwindow(browse, orient='horizontal')
        pane.pack(fill='both', expand=True)
        left = ttk.Frame(pane, padding=5)
        right = ttk.Frame(pane, padding=5)
        pane.add(left, weight=1)
        pane.add(right, weight=5)
        self.objects = ttk.Treeview(left, columns=('type',), show='tree headings', selectmode='browse')
        self.objects.heading('#0', text='Name')
        self.objects.heading('type', text='Type')
        self.objects.column('#0', width=180)
        self.objects.column('type', width=60)
        self.objects.pack(fill='both', expand=True)
        self.objects.bind('<<TreeviewSelect>>', self.select_table)
        ttk.Button(left, text='Refresh database', command=self.refresh).pack(fill='x', pady=5)
        nav = ttk.Frame(right)
        nav.pack(fill='x')
        ttk.Button(nav, text='Previous', command=lambda: self.page(-1)).pack(side='left')
        ttk.Button(nav, text='Next', command=lambda: self.page(1)).pack(side='left', padx=5)
        self.page_label = ttk.Label(nav, text='')
        self.page_label.pack(side='left', padx=8)
        self.table_grid = self.make_grid(right)
        self.schema = tk.Text(right, height=6, wrap='word', state='disabled')
        self.schema.pack(fill='x', pady=5)

        ttk.Label(sql, text='Enter one SQL statement. Ctrl+Enter runs it. Changes commit on success when editing is enabled.').pack(anchor='w', padx=8, pady=5)
        self.editor = tk.Text(sql, height=9, wrap='none', undo=True)
        self.editor.pack(fill='x', padx=8)
        self.editor.insert('1.0', 'SELECT name, type FROM sqlite_schema ORDER BY name;')
        self.editor.bind('<Control-Return>', lambda event: self.run_sql())
        ttk.Button(sql, text='Run SQL', command=self.run_sql).pack(anchor='w', padx=8, pady=5)
        self.sql_grid = self.make_grid(sql)

        controls = ttk.Frame(search, padding=8)
        controls.pack(fill='x')
        ttk.Label(controls, text='Regular expression:').pack(side='left')
        self.pattern = ttk.Entry(controls)
        self.pattern.pack(side='left', fill='x', expand=True, padx=8)
        self.pattern.bind('<Return>', lambda event: self.run_search())
        self.ignore_case = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text='Ignore case', variable=self.ignore_case).pack(side='left')
        ttk.Button(controls, text='Search', command=self.run_search).pack(side='left', padx=8)
        ttk.Label(search, text='Searches every cell in all tables and views. BLOBs: UTF-8 and hex; NULL: “NULL”. Row numbers are scan positions.\n'
                  'Results display the first 10,000 matches; the scan continues to count all matches. Double-click any cell to inspect its full value.').pack(anchor='w', padx=8, pady=5)
        self.search_grid = self.make_grid(search)
        ttk.Label(wal, text='Review an offline database and its adjacent -wal file. Close other applications using the files first.\n'
                  'Green = inserted; yellow = updated; red = deleted. Only differences from the current main file can be recovered.').pack(anchor='w', padx=8, pady=5)
        wal_controls = ttk.Frame(wal, padding=8)
        wal_controls.pack(fill='x')
        self.offline = tk.BooleanVar(value=False)
        ttk.Checkbutton(wal_controls, text='Files are not in use by other applications', variable=self.offline).pack(side='left')
        ttk.Button(wal_controls, text='Inspect WAL', command=self.run_wal_review).pack(side='left', padx=8)
        export_controls = ttk.Frame(wal, padding=8)
        export_controls.pack(fill='x')
        for text, callback in [
                ('Export with all WAL changes', lambda: self.export_wal(True)),
                ('Export main file only', lambda: self.export_wal(False)),
                ('Export with selected changes', lambda: self.export_wal(True, True)),
                ('Export without selected changes', lambda: self.export_wal(False, True))]:
            ttk.Button(export_controls, text=text, command=callback).pack(side='left', padx=3)
        edit_controls = ttk.Frame(wal, padding=8)
        edit_controls.pack(fill='x')
        ttk.Button(edit_controls, text='Apply selected to open database', command=lambda: self.edit_wal(True)).pack(side='left')
        ttk.Button(edit_controls, text='Revert selected in open database', command=lambda: self.edit_wal(False)).pack(side='left', padx=8)
        ttk.Label(edit_controls, text='Creates a backup and checks for conflicts. Select any cell in a change row.').pack(side='left')
        self.wal_summary = tk.Text(wal, height=5, wrap='word', state='disabled')
        self.wal_summary.pack(fill='x', padx=8)
        self.wal_grid = self.make_grid(wal)
        self.wal_grid.tag_configure('Inserted', background='#d9f3df')
        self.wal_grid.tag_configure('Updated', background='#fff1bc')
        self.wal_grid.tag_configure('Deleted', background='#f8d5d5')
        ttk.Label(self, text='Select cells by dragging or Shift-clicking; Ctrl/Command-click toggles cells. Right-click to export.', padding=(8, 3)).pack(fill='x')
        ttk.Label(self, textvariable=self.status, padding=8).pack(fill='x')

    def make_grid(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, padx=5, pady=5)
        tree = CellGrid(frame, show='headings')
        vertical = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        def scrolled(scrollbar, first, last):
            scrollbar.set(first, last)
            tree.after_idle(tree.paint)
        tree.configure(yscrollcommand=lambda first, last: scrolled(vertical, first, last),
                       xscrollcommand=lambda first, last: scrolled(horizontal, first, last))
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree.bind('<Double-1>', self.inspect_cell)
        tree.bind('<Button-3>', self.cell_menu)
        tree.bind('<Button-2>', self.cell_menu)
        return tree

    def cell_menu(self, event):
        tree = event.widget
        cell = tree.location(event)
        if cell is None:
            return 'break'
        if cell not in tree.cells:
            tree.cells = {cell}
            tree.anchor_cell = cell
            tree.paint()
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='Export cell data…', state='normal' if len(tree.cells) == 1 else 'disabled',
                         command=lambda: self.export_cells(tree, False))
        menu.add_command(label=f'Export {len(tree.cells)} selected cells as CSV…',
                         command=lambda: self.export_cells(tree, True))
        menu.add_command(label='Select all displayed cells', command=tree.select_all)
        menu.add_command(label='Clear selection', command=tree.clear_selection)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def protect_source(self, destination):
        candidate = Path(destination).resolve()
        if self.path and candidate in {Path(self.path + suffix).resolve() for suffix in ('', '-wal', '-shm', '-journal')}:
            raise ValueError('Choose an export file other than the open database or its sidecars.')
        if self.path and candidate.exists():
            for suffix in ('', '-wal', '-shm', '-journal'):
                source = Path(self.path + suffix)
                if source.exists() and candidate.samefile(source):
                    raise ValueError('Export path refers to the open database or a sidecar.')

    def export_cells(self, tree, as_csv):
        if not tree.cells:
            return
        path = filedialog.asksaveasfilename(title='Export selected cells' if as_csv else 'Export cell data',
                                          defaultextension='.csv' if as_csv else '.txt',
                                          filetypes=[('CSV', '*.csv'), ('All files', '*')] if as_csv else [('All files', '*')])
        if not path:
            return
        try:
            self.protect_source(path)
            if as_csv:
                columns, rows, cells = tree.export_selection()
                write_csv(path, columns, rows, cells)
            else:
                item, column = next(iter(tree.cells))
                write_cell(path, tree.raw_rows[item][column])
            self.status.set(f'Exported to {path}')
        except (OSError, ValueError) as error:
            messagebox.showerror('Export failed', str(error))

    def inspect_cell(self, event):
        tree = event.widget
        item = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if not item or not column or column == '#0':
            return
        value = tree.item(item, 'values')[int(column[1:]) - 1]
        window = tk.Toplevel(self)
        window.title('Cell value')
        window.geometry('700x400')
        text = tk.Text(window, wrap='word')
        text.pack(fill='both', expand=True)
        text.insert('1.0', value)
        text.configure(state='disabled')

    def fill_grid(self, tree, columns, rows):
        tree.clear_selection()
        tree.raw_rows.clear()
        tree.delete(*tree.get_children())
        ids = [str(i) for i in range(len(columns))]
        tree.configure(columns=ids)
        for ident, heading in zip(ids, columns):
            tree.heading(ident, text=heading)
            tree.column(ident, width=180, minwidth=80, stretch=False)
        for row in rows:
            item = tree.insert('', 'end', values=[display_value(value) for value in row])
            tree.raw_rows[item] = tuple(row)

    def choose_database(self):
        path = filedialog.askopenfilename(title='Open SQLite database', filetypes=[
            ('SQLite databases', '*.db *.sqlite *.sqlite3'), ('All files', '*')])
        if path:
            self.open_database(path)

    def open_database(self, path):
        if self.busy:
            messagebox.showinfo('Operation in progress', 'Cancel or wait for the current operation first.')
            return
        try:
            with connect_database(path) as connection:
                objects = database_objects(connection)
            connection.close()
        except (sqlite3.Error, OSError) as error:
            messagebox.showerror('Cannot open database', str(error))
            return
        self.path = str(Path(path).resolve())
        if self.review:
            self.review.close()
            self.review = None
        self.offline.set(False)
        self.writable.set(False)
        self.filename.configure(text=self.path)
        self.title(f'SQLite Database Browser — {Path(path).name}')
        self.set_objects(objects)
        self.fill_grid(self.table_grid, [], [])
        self.fill_grid(self.sql_grid, [], [])
        self.fill_grid(self.search_grid, [], [])
        self.fill_grid(self.wal_grid, [], [])
        self.set_wal_summary('Inspect WAL to review this database.')
        self.table = None
        self.status.set(f'Opened {len(objects)} tables/views in read-only mode.')

    def set_objects(self, objects):
        self.objects.delete(*self.objects.get_children())
        for name, kind in objects:
            self.objects.insert('', 'end', text=name, values=(kind,))

    def change_mode(self):
        if self.busy:
            self.writable.set(not self.writable.get())
            return
        self.status.set('Database changes enabled.' if self.writable.get() else 'Read-only mode enabled.')

    def start_job(self, work, done, use_connection=True):
        if not self.path:
            messagebox.showinfo('Open a database', 'Choose a database file first.')
            return
        if self.busy:
            messagebox.showinfo('Operation in progress', 'Cancel or wait for the current operation first.')
            return
        self.busy = True
        self.cancel.clear()
        path, writable = self.path, self.writable.get()
        self.status.set('Working…')

        def worker():
            connection = None
            try:
                if use_connection:
                    connection = connect_database(path, writable)
                    connection.set_progress_handler(lambda: int(self.cancel.is_set()), 1000)
                result = work(connection)
                if self.cancel.is_set() and (use_connection or hasattr(result, 'close')):
                    if connection:
                        connection.rollback()
                    if hasattr(result, 'close'):
                        result.close()
                    raise RuntimeError('Operation cancelled.')
                if connection:
                    connection.commit()
                self.events.put(('done', done, result))
            except Exception as error:
                if connection:
                    connection.set_progress_handler(None, 0)
                    connection.rollback()
                self.events.put(('error', str(error)))
            finally:
                if connection:
                    connection.close()

        threading.Thread(target=worker, daemon=True).start()

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'progress':
                    self.status.set(event[1])
                elif event[0] == 'done':
                    self.busy = False
                    if self.closing:
                        if hasattr(event[2], 'close'):
                            event[2].close()
                        self.close()
                        return
                    event[1](event[2])
                elif event[0] == 'error':
                    self.busy = False
                    if self.closing:
                        self.close()
                        return
                    self.status.set('Operation cancelled.' if self.cancel.is_set() else 'Operation failed.')
                    if not self.cancel.is_set():
                        messagebox.showerror('Database operation failed', event[1])
        except queue.Empty:
            pass
        self.after(80, self.poll_events)

    def refresh(self):
        self.start_job(database_objects, lambda objects: (self.set_objects(objects), self.status.set('Database list refreshed.')))

    def select_table(self, event=None):
        selected = self.objects.selection()
        if selected and not self.busy:
            self.table = self.objects.item(selected[0], 'text')
            self.offset = 0
            self.load_table()

    def page(self, direction):
        if self.table and not self.busy:
            if direction > 0 and not self.has_next:
                return
            self.offset = max(0, self.offset + direction * PAGE_SIZE)
            self.load_table()

    def load_table(self):
        table, offset = self.table, self.offset

        def work(connection):
            cursor = connection.execute('SELECT * FROM ' + quote_identifier(table) + ' LIMIT ? OFFSET ?', (PAGE_SIZE + 1, offset))
            rows = cursor.fetchall()
            schema = connection.execute('SELECT sql FROM sqlite_schema WHERE name = ?', (table,)).fetchone()
            return [c[0] for c in cursor.description], rows, schema[0] if schema else ''

        def done(result):
            columns, rows, schema = result
            self.has_next = len(rows) > PAGE_SIZE
            self.fill_grid(self.table_grid, columns, rows[:PAGE_SIZE])
            self.schema.configure(state='normal')
            self.schema.delete('1.0', 'end')
            self.schema.insert('1.0', schema or 'No schema available.')
            self.schema.configure(state='disabled')
            count = min(len(rows), PAGE_SIZE)
            self.page_label.configure(text=f'Rows {offset + 1 if count else 0}–{offset + count}')
            self.status.set(f'{table}: {count} rows displayed. Use SQL with ORDER BY for a stable row order.')

        self.start_job(work, done)

    def run_sql(self):
        statement = self.editor.get('1.0', 'end').strip()
        if not statement:
            return

        def work(connection):
            cursor = connection.execute(statement)
            if cursor.description:
                columns = [c[0] for c in cursor.description]
                rows = cursor.fetchmany(RESULT_LIMIT + 1)
                truncated = len(rows) > RESULT_LIMIT
                # Drain RETURNING statements so their changes can commit.
                if truncated:
                    for _ in cursor:
                        if self.cancel.is_set():
                            break
                return columns, rows[:RESULT_LIMIT], truncated, cursor.rowcount
            return [], [], False, cursor.rowcount

        def done(result):
            columns, rows, truncated, affected = result
            self.fill_grid(self.sql_grid, columns, rows)
            self.status.set(f'{len(rows)} rows displayed' + (' (limited to 10,000).' if truncated else '.') if columns
                            else f'Statement completed. Rows affected: {max(affected, 0)}.')

        self.start_job(work, done)

    def run_search(self):
        pattern, ignore_case = self.pattern.get(), self.ignore_case.get()
        try:
            re.compile(pattern)
        except re.error as error:
            messagebox.showerror('Invalid regular expression', str(error))
            return

        def work(connection):
            matches, total = [], 0
            for match in search_database(connection, pattern, ignore_case, self.cancel,
                    lambda name: self.events.put(('progress', f'Searching {name}…'))):
                total += 1
                if len(matches) < RESULT_LIMIT:
                    matches.append(match)
            return matches, total

        def done(result):
            matches, total = result
            self.fill_grid(self.search_grid, ['Table / view', 'Row position', 'Column', 'Value'], matches)
            self.status.set(f'{total:,} matching cells. {len(matches):,} displayed.')

        self.start_job(work, done)

    def close(self):
        self.cancel.set()
        if self.busy:
            self.closing = True
            self.status.set('Cancelling the current operation before closing…')
            return
        if self.review:
            self.review.close()
        self.destroy()

    def set_wal_summary(self, text):
        self.wal_summary.configure(state='normal')
        self.wal_summary.delete('1.0', 'end')
        self.wal_summary.insert('1.0', text)
        self.wal_summary.configure(state='disabled')

    def run_wal_review(self):
        if not self.offline.get():
            messagebox.showinfo('Offline files required', 'Close other applications using the database, then tick the offline-files checkbox. '
                                'For forensic captures, use an offline copy of the database and its WAL. Closing the last SQLite client may checkpoint and remove its WAL.')
            return
        path = self.path

        def done(review):
            if self.review:
                self.review.close()
            self.review = review
            rows = []
            for change in review.changes[:RESULT_LIMIT]:
                changed_columns = [name for index, name in enumerate(change.columns)
                                   if change.before is None or change.after is None or change.before[index] != change.after[index]]
                rows.append((change.kind, change.table, repr(change.key), ', '.join(changed_columns),
                             repr(dict(zip(change.columns, change.before))) if change.before is not None else '(absent)',
                             repr(dict(zip(change.columns, change.after))) if change.after is not None else '(absent)'))
            self.fill_grid(self.wal_grid, ['Change', 'Table', 'Row identity', 'Changed columns', 'Before', 'After'], rows)
            for item, change in zip(self.wal_grid.get_children(), review.changes):
                self.wal_grid.item(item, tags=(change.kind,))
            pages = ', '.join(map(str, review.changed_pages[:100]))
            if len(review.changed_pages) > 100:
                pages += ', …'
            self.set_wal_summary(f'{review.transactions} committed transactions; {review.frames} valid committed frames; '
                                 f'{len(review.changed_pages)} pages differ from the main file. Pages: {pages or "none"}\n'
                                 f'{len(review.changes):,} row differences; {len(rows):,} displayed. '
                                 'This is a net comparison, not a full transaction history.\n' + '\n'.join(review.warnings))
            self.status.set('WAL review complete. Select change rows to apply, revert, or export.')

        self.start_job(lambda connection: inspect_wal(path, self.cancel), done, use_connection=False)

    def selected_changes(self):
        if not self.review:
            raise ValueError('Inspect a WAL file first.')
        items = list(self.wal_grid.get_children())
        selected = {item for item, column in self.wal_grid.cells}
        changes = [self.review.changes[index] for index, item in enumerate(items) if item in selected]
        if not changes:
            raise ValueError('Select one or more cells in the change rows first.')
        return changes

    def export_wal(self, include_wal, selected=False):
        if self.busy:
            return
        try:
            if not self.review:
                raise ValueError('Inspect a WAL file first.')
            changes = self.selected_changes() if selected else None
            if not include_wal and not self.review.base_valid:
                raise ValueError('The main file is not a valid baseline.')
            if selected and not self.review.base_valid:
                raise ValueError('Selective export needs a valid main-file baseline.')
        except ValueError as error:
            messagebox.showinfo('WAL export', str(error))
            return
        destination = filedialog.asksaveasfilename(title='Export WAL review to a new database', defaultextension='.sqlite',
                                                 filetypes=[('SQLite database', '*.sqlite'), ('All files', '*')])
        if not destination:
            return
        try:
            self.protect_source(destination)
            if Path(destination).exists() or any(Path(destination + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                raise ValueError('Choose a new filename with no existing database or sidecars.')
        except ValueError as error:
            messagebox.showerror('Export failed', str(error))
            return
        review = self.review

        def work(connection):
            if selected:
                return export_selected(review, destination, changes, include_wal, self.cancel)
            export_snapshot(review, destination, include_wal)
            return None

        self.start_job(work, lambda count: self.status.set(f'Exported database to {destination}'), use_connection=False)

    def edit_wal(self, include_wal):
        if self.busy:
            return
        try:
            if not self.writable.get():
                raise ValueError('Enable “Allow database changes” first.')
            changes = self.selected_changes()
        except ValueError as error:
            messagebox.showinfo('WAL changes', str(error))
            return
        action = 'Apply' if include_wal else 'Revert'
        if not messagebox.askyesno(f'{action} selected changes',
                                  f'{action} {len(changes)} selected row changes in {self.path}?\n'
                                  'A full backup is required. Conflicts or constraint violations cancel the entire edit.'):
            return
        backup_path = filedialog.asksaveasfilename(title='Save pre-edit backup to a new file', defaultextension='.sqlite',
                                                 filetypes=[('SQLite database', '*.sqlite'), ('All files', '*')])
        if not backup_path:
            return
        try:
            self.protect_source(backup_path)
            if Path(backup_path).exists() or any(Path(backup_path + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                raise ValueError('Choose a new backup filename with no existing sidecars.')
        except ValueError as error:
            messagebox.showerror('Backup failed', str(error))
            return
        path = self.path
        self.start_job(lambda connection: modify_database(path, changes, include_wal, backup_path, self.cancel),
                       lambda count: self.status.set(f'{action}: {count} rows changed. Backup: {backup_path}. Refresh tables to see changes.'),
                       use_connection=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', nargs='?', help='SQLite file to open')
    args = parser.parse_args()
    Browser(args.database).mainloop()


if __name__ == '__main__':
    main()
