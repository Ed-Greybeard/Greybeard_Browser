# SQLite Database Browser

A desktop database browser written in pure Python with `sqlite3` and Tkinter.
No third-party packages are required. Use Python 3.9 or newer with Tk support.

```sh
python3 db_browser.py
# Or open a file directly:
python3 db_browser.py /path/to/database.sqlite
```

- **Tables & views:** select an object, inspect its schema, and browse 200 rows
  at a time. Drag the divider between column headings to resize a column.
  Double-click a cell to view and copy its full value.
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
  Right-click a result and choose **Go to table cell** to open its table page
  with surrounding rows and highlight the exact matching cell. Navigation uses
  rowid where available and verifies the saved row values; changed/deleted rows
  require a fresh search. Objects without rowid use a unique full-row match;
  ambiguous identical view rows are reported instead of highlighting a guess.
- Database jobs, opening files, and cell exports run in background workers.
  Regex search runs in a separate process so even an expression with excessive
  backtracking can be cancelled without locking the UI. Use **Cancel operation**
  to request cancellation. Database/recovery cancellation remains cooperative;
  cell-file exports finish their current write before returning.
- The bottom activity bar animates while work is active and shows elapsed time
  with **Working**, **Displaying results**, or **Cancelling**. It returns to
  **Ready** after the worker and result display have finished. Large result sets
  render in short batches so the window can process input between batches.
  Cancelling display leaves only the rows already displayed available for cell
  selection/export. Recovery's complete underlying results remain available for
  its all-record database export.
- Grid cells show bounded previews for large values. Double-click to load the
  full value into a detail window in small chunks, or export it. Exports keep
  the original full values. Selection redraw requests are coalesced to avoid
  building up a queue of redundant paint operations.
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
- **Deleted data recovery:** open an offline capture and choose the **Deleted
  data recovery** tab. Tick the offline-files checkbox, choose unused-space
  scanning and/or WAL history, then click **Recover deleted data**. A WAL file
  is optional for unused-space scanning. Recovery does not edit source files.
  Filter results by table or evidence, and double-click values or notes for
  details. Select any cell in a row to include that record in a selected export.
  Existing cell/CSV export is also available through the right-click menu.

Deleted-data results use these labels and colours:

| Colour | Status | What the evidence establishes |
| --- | --- | --- |
| Purple | Recovered deletion | A row existed in a valid earlier main-file/WAL snapshot and its identity is absent from the latest committed state. |
| Amber | Candidate | Unreferenced bytes decode as an intact table-leaf cell. They may be a deleted row, an older update, a stale copy, or a false positive. |
| Orange | Partial candidate | Surviving bytes fit a supported small-cell layout, but the header is inferred and the original rowid/INTEGER PRIMARY KEY is unknown. |

Unknown fields display as `<UNKNOWN: overwritten>`, separately from actual
NULL values. Freelist pages lose reliable table ownership: such records show
**unknown** table names and generic `column_1`, `column_2`, etc. Possible table
names are compatibility hints, not recovered ownership. A known table name on
an unused-space candidate is based on the page's current b-tree ownership.

Choose **Export all recovered data…** or **Export selected records…** to create
a new SQLite database containing recovered records only. All-record export
includes results hidden by filters and the display limit. The export contains:

- `recovered_data_001`, `recovered_data_002`, etc.: recovered values, grouped by
  original table and column layout, with a unique `_recovery_id` linking each
  record to its metadata. Column names and native SQLite value types are kept.
  The ID column receives an extra underscore if its name conflicts with a
  recovered column. Unknown fields are stored as NULL and listed explicitly in
  the metadata so they remain distinguishable from recovered NULLs.
- `recovered_records`: status, evidence, original table/identity, possible tables,
  source snapshot or free-space location, page number, page-relative byte offset,
  missing column names, notes, and raw carved bytes where available. `data_table`
  and `identity_column` identify the corresponding recovered row table and ID.
- `recovery_info`: source file/WAL SHA-256 hashes, capture timestamp, page size,
  scan warnings, and format information.

The export uses unconstrained recovery tables so missing fields, duplicate keys,
and uncertain records are preserved for examination. It does not merge suspect
records into the original tables. It is built and integrity-checked in temporary
storage before the new destination is created. Source files and existing output
files are protected from overwriting; cancelled/failed publication removes the
new partial output.

Deleted-record recovery is **best effort**, not a complete forensic recovery
tool. SQLite's deletion code removes cell references, writes freeblock metadata,
coalesces free space, and may release entire pages. New writes can reuse these
bytes; `secure_delete` can zero them, and `VACUUM` rebuilds the database. Preserve
an offline copy, including a matching WAL if one exists, before attempting
recovery. Earlier valid WAL snapshots can still contain records erased from the
latest state. Repeated source-byte comparison checks for movement but does not
replace the offline requirement.

The scanner reads unused regions of ordinary rowid-table leaf pages and the
freelist. It decodes complete local cells with surviving headers and a supported
partial layout: a first-column INTEGER PRIMARY KEY, one-byte payload size, and
one- or two-byte original rowid. It does not guess missing primary keys. Carving
requires a text/BLOB value at least three characters/bytes long; numeric-only
records, incomplete overflow chains, records with altered column counts, and
text containing unusual control characters are omitted to reduce false positives.
It does not carve index, virtual/shadow, or WITHOUT ROWID table records. Valid
snapshot comparison can recover WITHOUT ROWID rows and complete overflow values
when their schema, identity, and columns match the latest table. Dropped tables and
changed schemas/column/identity layouts are not classified as snapshot deletions. Surviving
versions of the same deleted row may appear separately if their values differ.

Recovery limits: 256 MiB per input/reconstructed file, 64 MiB of scanned unused
space, the latest 64 earlier WAL commits plus a valid main-file baseline, and
50,000 recovered records/candidates. The UI displays at most 10,000 matching
results at once. Live/historical row comparison uses the existing 1,000,000-cell
per-table limit. Omitted regions/tables/history are reported in scan warnings.
The latest reconstructed database must pass an integrity check; this feature is
for deleted-data recovery from a readable database, rather than arbitrary
corruption repair. Free-space candidates remain suspect even after export.

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

Deletion/recovery references: SQLite's [b-tree source](https://github.com/sqlite/sqlite/blob/master/src/btree.c)
(`dropCell`, `freeSpace`, `freePage2`), [record format](https://www.sqlite.org/fileformat2.html#record_format),
[secure-delete pragma](https://www.sqlite.org/pragma.html#pragma_secure_delete), and
[recovery documentation](https://www.sqlite.org/recovery.html).

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
