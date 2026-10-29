"""In-process layout checks on an isolated test instance, without user UI access."""
import json
from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desktop.storage import Store
from desktop.ui import App

directory = Path(__file__).resolve().parent.parent / 'diagnostics' / 'layout'
root = tk.Tk()
app = App(root, Store(directory))
report = {}
for size in ('1180x820', '980x700'):
    root.geometry(size)
    root.update()
    for tab in range(3):
        app.notebook.select(tab)
        root.update()
        panel = root.nametowidget(app.notebook.tabs()[tab])
        violations = []
        def walk(widget):
            # Canvas children are intentionally clipped and reachable by scrolling.
            if isinstance(widget, tk.Canvas):
                region = widget.bbox('all')
                if region and region[3] > widget.winfo_height():
                    widget.yview_moveto(1.0)
                    root.update_idletasks()
                    assert widget.yview()[1] >= .99, 'settings content cannot be reached'
                    widget.yview_moveto(0.0)
                return
            for child in widget.winfo_children():
                if child.winfo_ismapped():
                    x = child.winfo_rootx() - root.winfo_rootx()
                    y = child.winfo_rooty() - root.winfo_rooty()
                    if x < 0 or y < 0 or x + child.winfo_width() > root.winfo_width() + 2 or y + child.winfo_height() > root.winfo_height() + 2:
                        violations.append({'widget': str(child), 'bounds': [x, y, child.winfo_width(), child.winfo_height()]})
                walk(child)
        walk(panel)
        report[f'{size}-tab{tab}'] = {'root': [root.winfo_width(), root.winfo_height()], 'overflow': violations}
        names = ['course_tree', 'chapter_tree', 'task_tree'] if tab == 0 else ['review_tree', 'question_tree'] if tab == 1 else []
        report[f'{size}-tab{tab}']['table_sizes'] = {name: [getattr(app, name).winfo_width(), getattr(app, name).winfo_height()] for name in names}
        for name in names:
            assert getattr(app, name).winfo_ismapped(), f'{size}: {name} is hidden by other controls'
            assert getattr(app, name).winfo_height() >= 60, f'{size}: {name} has no usable table viewport'
app.vars['video_workers'].set('6')
app.vars['answer_workers'].set('3')
assert app.save()
app.course = {'courseId': 'fixture', 'clazzId': 'test', 'cpi': 'test'}
app.events.put(('chapters', ('fixture:test:test', [{'id': '1', 'title': 'chapter 1'}, {'id': '2', 'title': 'chapter 2'}])))
app.poll()
app.select_all(True)
app.close()
reopened = Store(directory)
assert reopened.settings['video_workers'] == 6
assert reopened.settings['answer_workers'] == 3
assert reopened.settings['selections']['fixture:test:test'] == ['1', '2']
reopened.db.close()
(directory / 'layout-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report))
if any(value['overflow'] for value in report.values()):
    raise SystemExit(1)
