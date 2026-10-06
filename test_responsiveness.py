import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from background_search import run_search_process
from db_browser import Browser
from grid_tools import RecordText, cell_preview


class FakeGrid:
    def __init__(self):
        self.raw_rows = {}
        self.rows = {}

    def clear_selection(self):
        pass

    def get_children(self):
        return tuple(self.rows)

    def delete(self, *items):
        self.rows.clear()

    def configure(self, **options):
        pass

    def heading(self, *args, **options):
        pass

    def column(self, *args, **options):
        pass

    def insert(self, *args, **options):
        item = str(len(self.rows))
        self.rows[item] = options
        return item


class RenderHarness:
    fill_grid = Browser.fill_grid

    def __init__(self):
        self.busy = False
        self.cancel = threading.Event()
        self.render_jobs = {}
        self.callbacks = []
        self.recovery_items = {}
        self.status = self

    def set(self, value):
        self.message = value

    def set_busy(self, value):
        self.busy = value

    def finish_activity(self):
        if not self.render_jobs:
            self.busy = False

    def after(self, delay, callback):
        self.callbacks.append(callback)

    def drain(self):
        while self.callbacks:
            self.callbacks.pop(0)()


class RenderingTests(unittest.TestCase):
    def test_large_results_yield_and_preserve_values_tags_and_identity(self):
        browser, tree = RenderHarness(), FakeGrid()
        rows = [(i, 'x' * 1000) for i in range(1000)]
        browser.fill_grid(tree, ['id', 'value'], rows, tags=['candidate'] * len(rows), item_indices=list(range(1000)))
        self.assertTrue(browser.busy)
        self.assertEqual(tree.rows, {})
        browser.callbacks.pop(0)()
        self.assertLessEqual(len(tree.rows), 100)
        self.assertTrue(browser.busy)
        browser.drain()
        self.assertFalse(browser.busy)
        self.assertEqual(len(tree.rows), 1000)
        self.assertEqual(tree.raw_rows['999'], rows[999])
        self.assertLess(len(tree.rows['999']['values'][1]), 510)
        self.assertEqual(tree.rows['999']['tags'], ('candidate',))
        self.assertEqual(browser.recovery_items['999'], 999)

    def test_cancel_stops_display_and_releases_busy_state(self):
        browser, tree = RenderHarness(), FakeGrid()
        browser.fill_grid(tree, ['value'], [(i,) for i in range(1000)])
        browser.callbacks.pop(0)()
        count = len(tree.rows)
        browser.cancel.set()
        browser.drain()
        self.assertEqual(len(tree.rows), count)
        self.assertFalse(browser.busy)
        self.assertIn('Display cancelled', browser.message)

    def test_empty_replacement_invalidates_pending_renderer(self):
        browser, tree = RenderHarness(), FakeGrid()
        browser.fill_grid(tree, ['value'], [(1,)] * 1000)
        browser.fill_grid(tree, [], [])
        browser.drain()
        self.assertEqual(tree.rows, {})
        self.assertFalse(browser.busy)

    def test_large_blob_and_record_have_bounded_previews(self):
        blob = b'\xff' * 1000000
        self.assertLess(len(cell_preview(blob)), 510)
        record = RecordText({'blob': blob, 'text': 'x' * 1000000})
        self.assertLessEqual(len(cell_preview(record)), 500)
        self.assertEqual(record.values['blob'], blob)


class SearchProcessTests(unittest.TestCase):
    def test_search_results_and_cancellation_of_pathological_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE data (value)')
            connection.executemany('INSERT INTO data VALUES (?)', [('Alice',), ('a' * 50000 + '! ',)])
            connection.commit()
            connection.close()
            matches, total = run_search_process(path, '^alice$', True, threading.Event())
            self.assertEqual(total, 1)
            self.assertEqual(matches[0][-1], 'Alice')
            cancel = threading.Event()
            timers = []

            def progress(message):
                timer = threading.Timer(0.2, cancel.set)
                timers.append(timer)
                timer.start()

            started = time.monotonic()
            try:
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    run_search_process(path, '(a+)+$', False, cancel, progress)
            finally:
                for timer in timers:
                    timer.cancel()
                    timer.join()
            self.assertLess(time.monotonic() - started, 5)


if __name__ == '__main__':
    unittest.main()
