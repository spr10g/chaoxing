"""A single, visible selection rule shared by every bulk-operation table."""
import tkinter as tk
from tkinter import ttk


class CheckedTable(ttk.Frame):
    def __init__(self, parent, columns, headings, changed=lambda: None, widths=None):
        super().__init__(parent)
        self.changed = changed
        self.checked = set()
        self.anchor = None
        self.tree = ttk.Treeview(self, columns=('check', *columns), show='headings', selectmode='none')
        self.tree.heading('check', text='全选', command=self.toggle_all)
        self.tree.column('check', width=48, minwidth=48, stretch=False)
        for column, label in zip(columns, headings):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=(widths or {}).get(column, 140), minwidth=65)
        x = ttk.Scrollbar(self, orient='horizontal', command=self.tree.xview)
        y = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        self.tree.configure(xscrollcommand=x.set, yscrollcommand=y.set)
        self.rowconfigure(0, weight=1); self.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky='nsew')
        y.grid(row=0, column=1, sticky='ns'); x.grid(row=1, column=0, sticky='ew')
        self.tree.bind('<Button-1>', self.click)
        self.tree.bind('<space>', self.space)

    def selected(self):
        return [iid for iid in self.tree.get_children() if iid in self.checked]

    def set_rows(self, rows):
        ids = {iid for iid, values in rows}
        self.checked.intersection_update(ids)
        for iid in self.tree.get_children():
            if iid not in ids: self.tree.delete(iid)
        for index, (iid, values) in enumerate(rows):
            data = ('☑' if iid in self.checked else '☐', *values)
            if self.tree.exists(iid): self.tree.item(iid, values=data)
            else: self.tree.insert('', 'end', iid=iid, values=data)
            self.tree.move(iid, '', index)
        self._notify()

    def _notify(self):
        visible = set(self.tree.get_children())
        self.checked.intersection_update(visible)
        self.tree.selection_set(self.selected())
        self.changed()

    def set_checked(self, ids):
        self.checked = set(ids) & set(self.tree.get_children())
        for iid in self.tree.get_children():
            self.tree.set(iid, 'check', '☑' if iid in self.checked else '☐')
        self._notify()

    def toggle_all(self):
        ids = set(self.tree.get_children())
        self.set_checked([] if ids and ids <= self.checked else ids)

    def click(self, event):
        if self.tree.identify_region(event.x, event.y) not in ('cell', 'tree'): return
        iid = self.tree.identify_row(event.y)
        if not iid: return
        self.tree.focus_set(); self.tree.focus(iid)
        ids = list(self.tree.get_children())
        if event.state & 1 and self.anchor in ids:
            a, b = sorted((ids.index(self.anchor), ids.index(iid)))
            self.set_checked(self.checked | set(ids[a:b + 1]))
        else:
            self.anchor = iid
            self.set_checked(self.checked ^ {iid})
        return 'break'

    def space(self, event=None):
        iid = self.tree.focus()
        if iid: self.set_checked(self.checked ^ {iid})
        return 'break'
