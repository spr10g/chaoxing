"""Layout checks with an isolated account-free instance."""
import json
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desktop.storage import Store
from desktop.ui import App

directory = Path(__file__).resolve().parent.parent / 'diagnostics' / 'layout'
root = tk.Tk(); store = Store(directory); app = App(root, store)
report = {}
for size in ('1180x820', '980x700'):
    root.geometry(size); root.update()
    for tab, subtab in ((0, None), (1, 0), (1, 1), (2, None)):
        app.notebook.select(tab)
        if subtab is not None: app.answer_tabs.select(subtab)
        root.update()
        panel = root.nametowidget(app.notebook.tabs()[tab])
        violations, tables, controls = [], [], []
        def walk(widget):
            if isinstance(widget, tk.Canvas):
                region = widget.bbox('all')
                if region and region[3] > widget.winfo_height():
                    widget.yview_moveto(1); root.update_idletasks()
                    assert widget.yview()[1] >= .99
                    widget.yview_moveto(0)
                return
            for child in widget.winfo_children():
                if not child.winfo_ismapped(): continue
                x, y = child.winfo_rootx()-root.winfo_rootx(), child.winfo_rooty()-root.winfo_rooty()
                w, h = child.winfo_width(), child.winfo_height()
                if x < 0 or y < 0 or x+w > root.winfo_width()+2 or y+h > root.winfo_height()+2:
                    violations.append({'widget': str(child), 'bounds': [x, y, w, h]})
                if isinstance(child, ttk.Treeview): tables.append([w, h])
                if isinstance(child, ttk.Button): controls.append(child.cget('text'))
                walk(child)
        walk(panel)
        if tab != 2:
            assert len(tables) == 1, f'{size} tab {tab}/{subtab} has {len(tables)} tables'
            assert tables[0][1] >= 200, f'Table is too short: {tables}'
            assert not any('暂停' in c or '恢复' in c for c in controls)
        report[f'{size}-{tab}-{subtab}'] = {'overflow': violations, 'tables': tables, 'controls': controls}
app.vars['video_workers'].set('6'); app.vars['answer_workers'].set('3')
assert app.save()
app.close(); store.db.close()
(directory/'layout-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=True))
assert not any(r['overflow'] for r in report.values())
