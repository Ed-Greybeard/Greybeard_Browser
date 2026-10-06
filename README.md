# SQLite Database Browser

A desktop database browser written in pure Python with `sqlite3` and Tkinter.
No third-party packages are required. Use Python 3.9 or newer with Tk support.

```sh
python3 db_browser.py
# Or open a file directly:
python3 db_browser.py /path/to/database.sqlite
```

- **Tables & views:** select an object, inspect its schema, and browse 200 rows
  at a time. Double-click a cell to view and copy its full value.
- **Cell export:** drag across cells or Shift-click to select a rectangle.
  Ctrl-click (Command-click on macOS) toggles individual cells; Ctrl/Command+A
  selects all displayed cells. Right-click (or secondary-click on macOS) opens
  the export menu. A single cell exports as UTF-8 text or raw bytes for a BLOB.
  Multiple cells export as CSV with column headings, in displayed row/column
  order. Unselected intersections in sparse selections are empty CSV fields.
  NULL is written as `NULL`, and BLOBs as `0x`-prefixed hex in CSV.
  Exports contain the displayed selection, not undisplayed pages or query rows.
- **SQL editor:** execute one statement at a time with Run SQL or Ctrl+Enter.
  Results show up to 10,000 rows. Use `ORDER BY`, `WHERE`, and `LIMIT` to narrow
  results. Enable **Allow database changes** to run updates; successful statements
  commit automatically, and failed or cancelled statements roll back.
- **Search all data:** enter a Python regular expression, such as
  `example\.com`, `^Alice$`, or `\b\d{4}-\d{2}-\d{2}\b`. Every cell in every
  table and view is searched, including internal SQLite tables. Search results
  show the first 10,000 matching cells and report the full match count.
  Text and numbers use their string representation; NULL is `NULL`. Binary data
  is searched both as UTF-8 with replacement characters and as `0x`-prefixed hex.
  A result's row position is its position during that scan, not a row ID.
- Queries and searches run in a background thread. Use **Cancel operation** to
  stop them. Cancellation is cooperative; Python's regex engine cannot interrupt
  an individual pathological expression, so avoid patterns with excessive
  backtracking on large values.
- **WAL changes:** use an offline database with its matching adjacent `-wal`
  file, tick the offline-files checkbox, and choose **Inspect WAL**. For an
  existing capture, preserve both files together before opening them. Closing
  the last application using a live database may checkpoint and delete its WAL.
  The inspector reads the original files directly without opening them in SQLite,
  validates WAL header/frame checksums and salts, and reconstructs the latest
  committed state in temporary files. It ignores incomplete, stale, corrupt,
  or uncommitted trailing frames, reporting this in the review summary.
  Inserts are green, updates yellow, and deletions red, with changed column names
  and before/after values. Select any cell in a change row to include that row.
  The summary also lists page numbers whose latest WAL content differs from the
  main file. Double-click before/after cells to inspect full values.
- **Apply/revert WAL differences:** the WAL's committed changes are normally
  already visible through SQLite. **Revert selected** restores those rows to
  their main-file values; **Apply selected** brings them to their reviewed WAL
  values again. Enable editing, select change rows, choose the action, and save
  a full pre-edit backup to a new filename. A writer lock prevents concurrent
  edits during backup and replay. Every affected row and table schema must still
  match the reviewed version or the desired version; conflicts roll back the
  entire operation. Already-applied changes are skipped. This creates ordinary
  SQLite transactions; it does not delete, patch, or truncate source WAL frames.
  Refresh the table browser after editing. Re-inspect an offline capture to
  obtain a new WAL comparison.
- **WAL database export:** export the complete WAL-inclusive state, the main
  file alone, or a version with only selected row differences applied/reverted.
  Choose a new output filename with no existing sidecars. Selective exports are
  constructed and validated in temporary files before publishing the result.

WAL review compares the current main file with the latest valid committed WAL
state. It is a **net comparison**, not a transaction history: checkpointed data
may already be present in the main file, and pre-checkpoint values cannot be
recovered from WAL alone. Do not inspect actively changing files; repeated byte
comparison detects movement but does not replace the offline requirement.
Files and reconstructed images are limited to 256 MiB each, and row comparison
to 1,000,000 cells per table. The review lists any omitted tables and displays
the first 10,000 row differences; whole-database exports retain all data.

Schema changes and virtual/shadow tables use whole-database exports. Selective
edits require an unambiguous rowid or primary key, unchanged table schemas, no
table triggers, and no generated/hidden columns. SQLite constraint failures abort
the edit. Foreign-key cascade actions are disabled during replay so unselected
child rows are preserved; the complete final state must pass `foreign_key_check`
before commit. Internal SQLite bookkeeping is managed by SQLite rather than
selectively replayed. If the main file alone fails its integrity check (for
example after a partial checkpoint), only the complete WAL-inclusive export is
available. Failed source edits may still leave the completed backup on disk.

Implementation references: SQLite's [WAL source](https://github.com/sqlite/sqlite/blob/master/src/wal.c),
[file format](https://www.sqlite.org/fileformat2.html#walformat), and
[WAL documentation](https://www.sqlite.org/wal.html).

Files open read-only by default. Editing mode opens an existing file and never
creates a new database. Save a backup before making changes. Explicit transaction
control (`BEGIN`, `COMMIT`) is unnecessary because each execution commits itself.
Table pages and searches do not guarantee stable ordering during concurrent edits.
An inaccessible or invalid view causes the search to report an error.

Tkinter ships with many Python distributions. If importing it fails, install your
platform's Python Tk support (for example `python3-tk` on Debian/Ubuntu). A desktop
display is required.

Run the non-GUI checks with:

```sh
python3 -m unittest -v
```
