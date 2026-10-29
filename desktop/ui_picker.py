"""Course and chapter selection lives in a task-creation window."""
import copy
import tkinter as tk
from tkinter import ttk
from .engine import course_key


class TaskPicker:
    def __init__(self, app):
        self.app = app
        self.root = tk.Toplevel(app.root)
        self.root.title('添加任务')
        self.root.geometry('800x600')
        self.root.minsize(620, 460)
        self.root.transient(app.root)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.loading = set()
        self.points = {}
        self.courses = {course_key(c): c for c in app.courses}
        self.all_courses = set(app.store.settings.get('all_chapter_courses', [])) & self.courses.keys()
        self.selected = copy.deepcopy(app.store.settings.get('selections', {}))
        self.active = set(app.store.settings.get('selected_courses', [])) & self.courses.keys()
        outer = ttk.Frame(self.root, padding=16); outer.pack(fill='both', expand=True)
        bar = ttk.Frame(outer); bar.pack(fill='x', pady=(0, 8))
        ttk.Label(bar, text='勾选课程选择全部章节；展开课程可选择部分章节。').pack(side='left')
        ttk.Button(bar, text='全选 / 清空', command=self.select_all).pack(side='right')
        area = ttk.Frame(outer); area.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(area, columns=('state',), show='tree headings', selectmode='none')
        self.tree.heading('#0', text='课程 / 章节'); self.tree.heading('state', text='进度')
        self.tree.column('#0', width=530); self.tree.column('state', width=120)
        scroll = ttk.Scrollbar(area, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Button-1>', self.click)
        self.tree.bind('<<TreeviewOpen>>', self.expand)
        for key, course in self.courses.items():
            self.tree.insert('', 'end', iid=key, text=course['title'])
            self.tree.insert(key, 'end', iid=key+'|loading', text='展开后读取章节')
        self.render()
        kind = ttk.Frame(outer); kind.pack(fill='x', pady=12)
        ttk.Checkbutton(kind, text='视频 / 音频', variable=app.variable('video', True)).pack(side='left', padx=(0, 20))
        ttk.Checkbutton(kind, text='AI 答题', variable=app.variable('answer', True)).pack(side='left')
        mode = ttk.Frame(outer); mode.pack(fill='x')
        ttk.Label(mode, text='答题完成后').pack(side='left', padx=(0, 12))
        ttk.Radiobutton(mode, text='保存草稿，等待审核', variable=app.variable('auto_submit', True), value=False).pack(side='left')
        ttk.Radiobutton(mode, text='自动交卷完整测验', variable=app.variable('auto_submit', True), value=True).pack(side='left', padx=12)
        self.summary = ttk.Label(outer, style='Muted.TLabel'); self.summary.pack(anchor='w', pady=10)
        self.start_button = ttk.Button(outer, text='添加并开始', style='Accent.TButton', command=self.start)
        self.start_button.pack(anchor='e')
        self.render()

    def alive(self):
        return bool(self.root.winfo_exists())

    def render(self):
        if not self.alive(): return
        for key, course in self.courses.items():
            marked = key in self.active
            text = '☑' if marked and key in self.all_courses else '▣' if marked else '☐'
            state = '全部章节' if marked and key in self.all_courses else f"已选 {len(self.selected.get(key, []))} 章" if marked else ''
            self.tree.item(key, text=text+'  '+course['title'], values=(state,))
            for point in self.points.get(key, []):
                pid = str(point['id']); iid = key+'|'+pid
                chosen = marked and (key in self.all_courses or pid in self.selected.get(key, []))
                self.tree.item(iid, text=('☑' if chosen else '☐')+'  '+point['title'])
        if hasattr(self, 'summary'): self.summary.configure(text=f'已选 {len(self.active)} 门课程')

    def select_all(self):
        if self.active == set(self.courses) and self.all_courses == set(self.courses):
            self.active.clear(); self.all_courses.clear()
        else:
            self.active = set(self.courses); self.all_courses = set(self.courses)
        self.render()

    def click(self, event):
        if self.tree.identify('element', event.x, event.y) == 'Treeitem.indicator': return
        iid = self.tree.identify_row(event.y)
        if not iid: return
        if iid in self.courses:
            if iid in self.active:
                self.active.discard(iid); self.all_courses.discard(iid)
            else:
                self.active.add(iid); self.all_courses.add(iid)
        elif '|loading' not in iid:
            key, pid = iid.rsplit('|', 1)
            chosen = {str(p['id']) for p in self.points.get(key, [])} if key in self.all_courses else set(self.selected.get(key, []))
            chosen.symmetric_difference_update({pid})
            self.all_courses.discard(key); self.selected[key] = sorted(chosen)
            if chosen: self.active.add(key)
            else: self.active.discard(key)
        self.render()
        return 'break'

    def expand(self, event=None):
        key = self.tree.focus()
        if key not in self.courses or key in self.points or key in self.loading: return
        self.loading.add(key)
        course = copy.deepcopy(self.courses[key])
        def load(): return self, key, self.app.engine.chapters(course)
        self.app.background(load, 'picker_chapters')

    def chapters_loaded(self, key, points):
        self.loading.discard(key)
        if not self.alive(): return
        self.points[key] = points
        self.tree.delete(*self.tree.get_children(key))
        for point in points:
            self.tree.insert(key, 'end', iid=key+'|'+str(point['id']), text=point['title'],
                             values=('已完成' if point.get('has_finished') else '未完成',))
        self.render()

    def start(self):
        if not self.active:
            self.app.notice.set('请勾选课程或章节'); return
        if not self.app.save(True): return
        if not (self.app.store.settings['video'] or self.app.store.settings['answer']):
            self.app.notice.set('请选择视频或 AI 答题'); return
        settings = {'selected_courses': sorted(self.active), 'all_chapter_courses': sorted(self.all_courses), 'selections': self.selected}
        self.app.store.save_settings(settings)
        courses = [copy.deepcopy(self.courses[k]) for k in sorted(self.active)]
        all_courses, selected = set(self.all_courses), copy.deepcopy(self.selected)
        self.start_button.configure(state='disabled')
        def resolve():
            plan, skipped = [], []
            for course in courses:
                key = course_key(course)
                try:
                    points = self.app.engine.chapters(course)
                    chosen = points if key in all_courses else [p for p in points if str(p['id']) in selected.get(key, [])]
                    if chosen: plan.append((course, chosen))
                except Exception as exc: skipped.append(course['title']+'：'+str(exc))
            return self, plan, skipped
        self.app.background(resolve, 'selection_plan')

    def close(self):
        self.app.picker = None
        self.root.destroy()
