# Countries demo walkthrough

Launch from this project directory:

```sh
python3 db_browser.py demo_data/test_database.sqlite
```

Keep `test_database.sqlite` and `test_database.sqlite-wal` together. They are a
deliberately preserved **offline capture**; no demo server needs to run. Population
figures, observations, contacts, and notes are illustrative rather than current
statistics. A second database, `checkpointed_database.sqlite`, contains the same
latest live data with its WAL changes already incorporated.

1. **Browse.** In **Tables & views**, select `countries`: 17 countries include
   accents, numbers, NULLs, and BLOBs. `country_overview` and `continent_summary`
   demonstrate views. Select `observations` and use Next/Previous across its 576
   rows. Drag column dividers to resize them. Double-click cells for full values.

2. **Query.** In **SQL editor**, run:

   ```sql
   SELECT continent, COUNT(*) AS countries,
          SUM(population_sample) AS illustrative_population
   FROM countries GROUP BY continent ORDER BY countries DESC;
   ```

   ```sql
   SELECT c.name, o.month, o.sample_temperature, o.sample_visitors
   FROM observations AS o JOIN countries AS c USING (country_id)
   WHERE c.code = 'JP' ORDER BY o.month;
   ```

3. **Search and export.** In **Search all data**, try `^Tokyo$`,
   `@example\.com$`, `2023-0[1-3]`, or `WAL-(UPDATED|INSERTED)`.
   Right-click Tokyo and choose **Go to table cell** to see the highlighted cell
   among the other countries. Select cells with dragging/Shift-click, or toggle
   them with Ctrl/Command-click. Right-click to export a cell or selected CSV.

4. **Inspect WAL.** In **WAL changes**, tick **Files are not in use by other
   applications**, then **Inspect WAL**. Expect three committed transactions and
   three net row differences: Japan updated, Greece inserted, and Iceland's draft
   in `retired_profiles` deleted. The Brazil visit existed only between WAL
   commits, so it is absent from this net comparison. Export **main file only**
   to a new filename to see the 16-country baseline; export **with all WAL
   changes** to see the 17-country latest state. Open either export in the browser.

5. **Recover deleted data.** Reopen the original demo pair. In **Deleted data
   recovery**, tick the offline checkbox, leave both recovery methods enabled,
   and run recovery. Look for:
   - `DELETED-WAL`: the Iceland draft, recovered from the main-file baseline.
   - `DELETED-HISTORY`: the Brazil visit, recovered from an earlier WAL commit.
   - `DELETED-FREEBLOCK`: the Japan itinerary, usually a partial candidate with
     an unknown original ID because SQLite overwrote its cell header.
   - `DELETED-FREELIST`: archived journal candidates on freed pages; ownership
     is unknown and suggested table names are unverified.

   Purple rows are snapshot deletions; amber/orange rows are uncertain/partial
   candidates. Candidate counts can vary with SQLite version and page reuse.
   Filter by evidence/table and double-click values or notes to inspect details.

6. **Export recovery.** Choose **Export all recovered data…**, saving to a new
   file such as `countries_recovered.sqlite`. Open it and inspect
   `recovered_data_001` and the other recovered groups. `recovered_records` links
   each row to its status, original table, missing fields, and source evidence.
   Recovery exports contain recovered records separately, rather than mixing
   uncertain data into the original country tables.

7. **Compare a checkpointed database.** Open `demo_data/checkpointed_database.sqlite`.
   Browsing shows the same 17 live countries. WAL inspection reports no WAL file:
   its changes have already been incorporated. Recovery can still find retained
   free-space candidates, but earlier WAL snapshots are unavailable there.

   To perform a checkpoint yourself, copy the original database/WAL pair to a
   separate folder, open that copy, enable **Allow database changes**, and run
   `PRAGMA wal_checkpoint(TRUNCATE);` in the SQL editor. A successful checkpoint
   incorporates the committed changes and truncates the WAL. The live rows stay
   the same, while WAL history is no longer available for inspection/recovery.

For apply/revert experiments, first copy the original database **and its WAL** to
a separate folder. Enable editing in that copy and select the Japan update in
the WAL review. **Revert selected** requires a new backup file; Japan returns to
the baseline sample population of 125,000,000. **Apply selected** returns it to
124,000,000. These are ordinary new transactions and can change/remove the demo
WAL when the last writable connection closes. Preserve the supplied capture for
repeatable recovery exercises. Avoid VACUUM, checkpoint commands, and writes on
the original demo pair before recovery.

To generate a fresh set without overwriting an existing one:

```sh
python3 create_demo_data.py --output-dir demo_data_fresh
```
