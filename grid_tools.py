"""Cell selection and export helpers for the Tkinter browser."""

import csv
import reprlib
import tkinter as tk
from tkinter import ttk


def cell_preview(value, limit=500):
    """Bound the text sent to Tk while retaining the original value for export."""
    if isinstance(value, RecordText):
        return value.preview()[:limit]
    if isinstance(value, bytes):
        return '0x' + value[:limit // 2].hex() + ('…' if len(value) > limit // 2 else '')
    text = text_value(value)
    return text[:limit] + ('…' if len(text) > limit else '')


class RecordText:
    """Defer large record formatting until export or detailed inspection."""

    def __init__(self, values):
        self.values = values

    def __str__(self):
        return repr(self.values)

    def preview(self):
        formatter = reprlib.Repr()
        formatter.maxstring = 200
        formatter.maxother = 200
        formatter.maxdict = 12
        formatter.repr_bytes = lambda value, level: repr(value[:100]) + ('…' if len(value) > 100 else '')
        return formatter.repr(self.values)


def text_value(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bytes):
        return '0x' + value.hex()
    return str(value)


def selected_csv(columns, rows, selected):
    """Return a rectangular CSV projection; unselected intersections are blank."""
    row_numbers = sorted({row for row, column in selected})
    column_numbers = sorted({column for row, column in selected})
    return ([columns[column] for column in column_numbers],
            [[text_value(rows[row][column]) if (row, column) in selected else ''
              for column in column_numbers] for row in row_numbers])


def write_csv(path, columns, rows, selected):
    headings, values = selected_csv(columns, rows, selected)
    with open(path, 'w', encoding='utf-8', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(headings)
        writer.writerows(values)


def write_cell(path, value):
    if isinstance(value, bytes):
        with open(path, 'wb') as output:
            output.write(value)
    else:
        with open(path, 'w', encoding='utf-8', newline='') as output:
            output.write(text_value(value))


class CellGrid(ttk.Treeview):
    """Treeview with real cell selection and visible selection overlays."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, selectmode='none', **kwargs)
        self.cells = set()
        self.anchor_cell = None
        self.raw_rows = {}
        self.overlays = {}
        self.command_mask = 0x8 if self.tk.call('tk', 'windowingsystem') == 'aqua' else 0
        self.dragging = False
        self.drag_base = set()
        self.paint_pending = None
        self.bind('<Button-1>', self.click)
        self.bind('<B1-Motion>', self.drag)
        self.bind('<ButtonRelease-1>', self.release)
        self.bind('<Configure>', lambda event: self.request_paint())
        self.bind('<Control-a>', self.select_all)
        self.bind('<Command-a>', self.select_all)
        self.bind('<Escape>', self.clear_selection)

    def location(self, event):
        # Overlay labels relay their events using root coordinates.
        x = event.x_root - self.winfo_rootx()
        y = event.y_root - self.winfo_rooty()
        item, column = self.identify_row(y), self.identify_column(x)
        if not item or not column or column == '#0':
            return None
        return item, int(column[1:]) - 1

    def rectangle(self, first, last):
        items = list(self.get_children())
        a, b = sorted((items.index(first[0]), items.index(last[0])))
        c, d = sorted((first[1], last[1]))
        return {(item, column) for item in items[a:b + 1] for column in range(c, d + 1)}

    def click(self, event):
        cell = self.location(event)
        if cell is None:
            self.dragging = False
            return
        self.focus_set()
        additive = bool(event.state & (0x4 | self.command_mask))
        if event.state & 0x1 and self.anchor_cell:
            self.cells = self.rectangle(self.anchor_cell, cell)
        elif additive:
            self.cells.symmetric_difference_update({cell})
            self.anchor_cell = cell
        else:
            self.cells = {cell}
            self.anchor_cell = cell
        self.drag_base = set(self.cells) if additive else set()
        self.dragging = True
        self.paint()
        return 'break'

    def drag(self, event):
        if not self.dragging:
            # Let Treeview's class binding handle header-divider resizing.
            # Redraw overlays after the native binding changes column widths.
            self.request_paint()
            return
        cell = self.location(event)
        if self.dragging and cell and self.anchor_cell:
            self.cells = self.drag_base | self.rectangle(self.anchor_cell, cell)
            self.paint()
        return 'break'

    def release(self, event):
        self.dragging = False
        self.request_paint()

    def select_all(self, event=None):
        column_count = len(self['columns'])
        self.cells = {(item, column) for item in self.get_children()
                      for column in range(column_count)}
        self.paint()
        return 'break'

    def clear_selection(self, event=None):
        self.cells.clear()
        self.anchor_cell = None
        self.paint()
        return 'break'

    def paint(self):
        if self.paint_pending is not None:
            self.after_cancel(self.paint_pending)
        self.paint_pending = None
        visible_items = {self.identify_row(y) for y in range(0, self.winfo_height(), 3)}
        visible_columns = set()
        for x in list(range(0, self.winfo_width(), 5)) + [self.winfo_width() - 1]:
            column = self.identify_column(x)
            if column and column != '#0':
                visible_columns.add(int(column[1:]) - 1)
        visible_cells = set()
        columns = self['columns']
        for item, column in ((item, column) for item in visible_items for column in visible_columns):
            if (item, column) not in self.cells or not self.exists(item):
                continue
            box = self.bbox(item, columns[column])
            if not box:
                continue
            x, y, width, height = box
            if x + width <= 0 or x >= self.winfo_width():
                continue
            cell = (item, column)
            visible_cells.add(cell)
            label = self.overlays.get(cell)
            if label is None:
                label = tk.Label(self, text=cell_preview(self.raw_rows[item][column]),
                                 bg='#245b9e', fg='white', anchor='w', padx=3)
                for event_name, callback in [('<Button-1>', self.click), ('<B1-Motion>', self.drag),
                                              ('<ButtonRelease-1>', self.release)]:
                    label.bind(event_name, callback)
                for event_name in ('<Button-3>', '<Button-2>', '<Double-1>', '<MouseWheel>', '<Button-4>', '<Button-5>'):
                    label.bind(event_name, lambda event, name=event_name: self.relay(event, name))
                self.overlays[cell] = label
            label.place(x=x, y=y, width=width, height=height)
        for cell in self.overlays.keys() - visible_cells:
            self.overlays.pop(cell).destroy()

    def request_paint(self):
        if self.paint_pending is None:
            self.paint_pending = self.after(16, self.paint)

    def relay(self, event, name):
        options = dict(x=event.x_root - self.winfo_rootx(),
                       y=event.y_root - self.winfo_rooty(), state=event.state)
        if name == '<MouseWheel>':
            options['delta'] = event.delta
        self.event_generate(name, **options)

    def export_selection(self):
        items = list(self.get_children())
        indices = {item: index for index, item in enumerate(items)}
        columns = [self.heading(column, 'text') for column in self['columns']]
        rows = [self.raw_rows[item] for item in items]
        cells = {(indices[item], column) for item, column in self.cells if item in indices}
        return columns, rows, cells
