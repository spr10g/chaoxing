import copy
from queue import Empty
import tkinter as tk
from tkinter import ttk, simpledialog
from .ui_base import BaseApp
from .storage import Store
from .backend import course_key
from .review import rows_for, summary, can_submit, needs_review, remote_label, FINAL, UNCERTAIN

STATUS = {'queued': '等待执行', 'running': '执行中', 'done': '执行完成', 'draft': '答题结束 · 待审核',
          'local_saved': '待审核', 'review': '待审核', 'submitted': '已交卷', 'platform_done': '平台已完成',
          'failed': '失败 · 结果保留', 'blocked': '等待章节开放', 'paused': 'API 故障 · 已暂停',
          'interrupted': '已停止 · 可重新加入', 'unknown': '交卷结果待核验', 'submitting': '交卷请求处理中',
          'removed': '已移出队列', 'removing': '正在移出 · 等待请求结束'}
ACTIONS = {'verify': '核验', 'save': '保存正常题', 'submit': '整章交卷', 'redo': 'AI 重做'}


class App(BaseApp):
    def __init__(self, root, store=None):
        store = store or Store()
        self.selected_courses = set(store.settings.get('selected_courses', []))
        self.all_courses = set(store.settings.get('all_chapter_courses', []))
        self.chapter_cache, self.loading_courses = {}, set()
        self.preparing, self.review_dirty = False, True
        self.question_refs = {}
        self.batches = {b['id']: b for b in (store.get('batch_history') or [])}
        super().__init__(root, store)
        self.render_review()
        for batch in self.batches.values(): self.render_batch(batch)

    def buttons(self, parent, pairs):
        for label, command in pairs:
            ttk.Button(parent, text=label, command=command).pack(side='left', padx=(0, 4))

    def tree(self, parent, columns, headings, height=5):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=columns, show='headings', height=height, selectmode='extended')
        for col, heading in zip(columns, headings):
            tree.heading(col, text=heading)
            tree.column(col, width=150, minwidth=45)
        vertical = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        horizontal.pack(side='bottom', fill='x')
        vertical.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True)
        return tree

    def _run(self, parent):
        toolbar = ttk.Frame(parent); toolbar.pack(fill='x')
        self.buttons(toolbar, [('勾选全部课程', lambda: self.choose_courses(True)), ('清空课程', lambda: self.choose_courses(False)), ('勾选课程的全部章节', self.all_selected_chapters)])
        self.start_button = ttk.Button(toolbar, text='启动所选课程', command=self.start, style='Accent.TButton')
        self.start_button.pack(side='right')
        types = ttk.Frame(parent); types.pack(fill='x', pady=7)
        for key, label in [('video', '视频 / 音频'), ('answer', 'AI 答题'), ('auto_submit', '自动交卷完整正常章节（否则预览）')]:
            ttk.Checkbutton(types, text=label, variable=self.variable(key, True)).pack(side='left', padx=(0, 12))
        panes = ttk.Panedwindow(parent, orient='horizontal', height=125); panes.pack(fill='both', expand=True)
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(left, weight=2); panes.add(right, weight=3)
        self.course_tree = self.tree(left, ('check', 'course', 'chapters'), ('勾选', '课程（可多选）', '章节选择'), 4)
        self.course_tree.column('check', width=50, stretch=False)
        self.course_tree.column('course', width=220)
        self.course_tree.bind('<Button-1>', self.toggle_course)
        self.course_tree.bind('<<TreeviewSelect>>', self.focus_course)
        bar = ttk.Frame(right); bar.pack(fill='x')
        self.chapter_title = ttk.Label(bar, text='点击课程名称查看章节'); self.chapter_title.pack(side='left')
        ttk.Button(bar, text='本课程全选', command=lambda: self.select_all(True)).pack(side='right')
        ttk.Button(bar, text='清空', command=lambda: self.select_all(False)).pack(side='right')
        self.chapter_tree = self.tree(right, ('check', 'chapter', 'state'), ('勾选', '章节', '平台状态'), 4)
        self.chapter_tree.column('check', width=50, stretch=False)
        self.chapter_tree.bind('<Button-1>', self.toggle_chapter)
        lanes = ttk.Frame(parent); lanes.pack(fill='x', pady=8)
        self.lane_labels = {}
        for i, (key, title) in enumerate([('video', '视频队列'), ('answer', '答题队列')]):
            box = ttk.LabelFrame(lanes, text=title); box.grid(row=0, column=i, sticky='ew', padx=(0, 8))
            lanes.columnconfigure(i, weight=1)
            self.lane_labels[key] = ttk.Label(box, text='等待开始'); self.lane_labels[key].pack(anchor='w')
            bar = ttk.Frame(box); bar.pack(fill='x', pady=(4, 0))
            self.buttons(bar, [('暂停', lambda k=key: self.pause(k)), ('恢复', lambda k=key: self.resume(k))])
        ttk.Button(lanes, text='停止全部并保留', command=self.stop).grid(row=0, column=2)
        bar = ttk.Frame(parent); bar.pack(fill='x', pady=(0, 5))
        ttk.Label(bar, text='队列：Ctrl / Shift 多选', style='Muted.TLabel').pack(side='left')
        self.show_removed = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='显示移出记录', variable=self.show_removed, command=self.render_all_tasks).pack(side='left', padx=8)
        ttk.Button(bar, text='全选显示任务', command=lambda: self.task_tree.selection_set(self.task_tree.get_children())).pack(side='right')
        ttk.Button(bar, text='移出选中任务', command=self.remove_selected).pack(side='right', padx=5)
        self.task_tree = self.tree(parent, ('course', 'lane', 'chapter', 'execution', 'platform', 'reason'), ('课程', '队列', '章节 / 任务', '执行状态', '平台保存 / 交卷', '原因'), 5)
        self.task_tree.column('lane', width=55, stretch=False)
        self.task_tree.column('reason', width=300)
        self.task_tree.bind('<Double-1>', self.open_selected_review)

    def _review(self, parent):
        bar = ttk.Frame(parent); bar.pack(fill='x')
        self.review_course = tk.StringVar(value='全部课程')
        self.review_course_box = ttk.Combobox(bar, textvariable=self.review_course, state='readonly', values=['全部课程'], width=25)
        self.review_course_box.pack(side='left')
        self.review_scope = tk.StringVar(value='全部待处理')
        ttk.Combobox(bar, textvariable=self.review_scope, state='readonly', width=17, values=['全部待处理', '全部记录', '仅异常题章节', '完整可交卷', '已交卷', '交卷待核验']).pack(side='left', padx=6)
        self.question_filter = tk.StringVar(value='全部题目')
        ttk.Combobox(bar, textvariable=self.question_filter, state='readonly', width=14, values=['全部题目', '需处理', '校验通过']).pack(side='left')
        for var in [self.review_course, self.review_scope, self.question_filter]: var.trace_add('write', lambda *args: self.refresh_review())
        ttk.Label(bar, text='Ctrl / Shift 多选', style='Muted.TLabel').pack(side='right')
        bar = ttk.Frame(parent); bar.pack(fill='x', pady=6)
        self.buttons(bar, [('全选显示章节', lambda: self.review_tree.selection_set(self.review_tree.get_children())), ('核验选中章节', lambda: self.chapter_batch('verify')), ('保存选中章节正常题', lambda: self.chapter_batch('save')), ('提交选中章节', lambda: self.chapter_batch('submit')), ('提交全部可交卷', self.submit_all_ready)])
        bar = ttk.Frame(parent); bar.pack(fill='x', pady=(0, 6))
        self.allow_blank = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='本次允许含空题整章交卷（默认关闭）', variable=self.allow_blank, command=self.blank_hint).pack(side='left')
        self.review_summary = ttk.Label(bar, text='', style='Muted.TLabel'); self.review_summary.pack(side='right')
        panes = ttk.Panedwindow(parent, orient='vertical'); panes.pack(fill='both', expand=True)
        top, middle, bottom = ttk.Frame(panes), ttk.Frame(panes), ttk.Frame(panes)
        panes.add(top, weight=2); panes.add(middle, weight=3); panes.add(bottom, weight=2)
        self.review_tree = self.tree(top, ('course', 'chapter', 'quality', 'execution', 'platform'), ('课程', '章节', '通过 / 需处理 / 总数', '执行状态', '平台保存 / 交卷'), 4)
        self.review_tree.column('platform', width=230)
        self.review_tree.bind('<<TreeviewSelect>>', lambda event: self.render_questions())
        bar = ttk.Frame(middle); bar.pack(fill='x', pady=4)
        self.buttons(bar, [('全选显示题目', lambda: self.question_tree.selection_set(self.question_tree.get_children())), ('保存选中题所在章节', self.save_question_chapters), ('AI 重做选中题', self.redo_selected), ('AI 重做显示的异常题', self.redo_errors)])
        self.question_tree = self.tree(middle, ('course', 'chapter', 'title', 'answer', 'source', 'quality'), ('课程', '章节', '题目（双击修正）', '选择', '来源', '校验 / 错误原因'), 5)
        self.question_tree.column('title', width=340); self.question_tree.column('quality', width=240)
        self.question_tree.bind('<<TreeviewSelect>>', self.question_selected)
        self.question_tree.bind('<Double-1>', self.edit_answer)
        self.detail_tabs = ttk.Notebook(bottom); self.detail_tabs.pack(fill='both', expand=True)
        self.detail = tk.Text(self.detail_tabs, height=4, wrap='word', font=('Microsoft YaHei UI', 10), bg='white', relief='flat')
        batch_panel = ttk.Frame(self.detail_tabs)
        self.detail_tabs.add(self.detail, text='题目详情与历史'); self.detail_tabs.add(batch_panel, text='批量操作结果')
        self.detail.configure(state='disabled')
        self.batch_tree = self.tree(batch_panel, ('action', 'chapter', 'result', 'reason'), ('操作', '课程 / 章节', '结果', '原因'), 3)
        self.batch_tree.column('reason', width=500)

    def persist_courses(self):
        self.store.save_settings({'selected_courses': sorted(self.selected_courses), 'all_chapter_courses': sorted(self.all_courses)})
        self.render_courses()

    def render_courses(self):
        for course in self.courses:
            key = course_key(course)
            selected = '全部章节' if key in self.all_courses else f"已选 {len(self.store.settings['selections'].get(key, []))} 章"
            values = ('✓' if key in self.selected_courses else '○', course['title'], selected)
            if self.course_tree.exists(key): self.course_tree.item(key, values=values)
            else: self.course_tree.insert('', 'end', iid=key, values=values)

    def toggle_course(self, event):
        key = self.course_tree.identify_row(event.y)
        if key and self.course_tree.identify_column(event.x) == '#1':
            if key in self.selected_courses: self.selected_courses.remove(key)
            else: self.selected_courses.add(key)
            self.persist_courses()

    def choose_courses(self, selected):
        self.selected_courses = {course_key(c) for c in self.courses} if selected else set()
        self.persist_courses()

    def all_selected_chapters(self):
        if not self.selected_courses:
            self.notice.set('请先勾选一个或多个课程。'); return
        self.all_courses.update(self.selected_courses); self.persist_courses()
        if self.course and course_key(self.course) in self.selected_courses: self.select_all(True)
        self.notice.set(f'已设置 {len(self.selected_courses)} 门课程全部章节；启动时后台加载。')

    def focus_course(self, event=None):
        selected = self.course_tree.selection()
        if selected:
            self.course = next(c for c in self.courses if course_key(c) == selected[-1])
            self.load_chapters()

    def load_chapters(self, event=None):
        if not self.course: return
        key = course_key(self.course)
        self.chapter_title.configure(text=self.course['title'][:22])
        if key in self.chapter_cache: self.show_chapters(key, self.chapter_cache[key])
        elif key not in self.loading_courses:
            self.points = []; self.chapter_tree.delete(*self.chapter_tree.get_children())
            self.loading_courses.add(key); course = dict(self.course)
            def load():
                try: return key, self.engine.chapters(course)
                finally: self.events.put(('chapter_load_finished', key))
            self.background(load, 'chapters')

    def show_chapters(self, key, points):
        self.chapter_cache[key] = points
        if not self.course or key != course_key(self.course): return
        self.points = points
        self.checked = {str(p['id']) for p in points} if key in self.all_courses else set(self.store.settings['selections'].get(key, []))
        self.chapter_tree.delete(*self.chapter_tree.get_children())
        for point in points:
            pid = str(point['id'])
            state = '已完成' if point.get('has_finished') else '可能需解锁' if point.get('need_unlock') else '待处理'
            self.chapter_tree.insert('', 'end', iid=pid, values=('✓' if pid in self.checked else '○', point['title'], state))

    def persist_selection(self):
        if self.course is not None:
            key = course_key(self.course); selections = copy.deepcopy(self.store.settings['selections'])
            selections[key] = sorted(self.checked)
            if self.checked: self.selected_courses.add(key)
            self.store.save_settings({'selections': selections, 'last_course': key}); self.persist_courses()

    def toggle_chapter(self, event):
        item = self.chapter_tree.identify_row(event.y)
        if item and self.chapter_tree.identify_column(event.x) == '#1':
            self.all_courses.discard(course_key(self.course))
            if item in self.checked: self.checked.remove(item)
            else: self.checked.add(item)
            self.chapter_tree.set(item, 'check', '✓' if item in self.checked else '○'); self.persist_selection()

    def select_all(self, selected):
        if self.course is None: return
        key = course_key(self.course)
        if selected: self.all_courses.add(key); self.selected_courses.add(key)
        else: self.all_courses.discard(key)
        self.checked = set(self.chapter_tree.get_children()) if selected else set()
        for item in self.chapter_tree.get_children(): self.chapter_tree.set(item, 'check', '✓' if item in self.checked else '○')
        self.persist_selection()

    def start(self):
        if not self.save(True) or self.preparing: return
        if not self.engine.connected: self.notice.set('尚未登录，请先登录后启动任务。'); return
        if self.engine.busy(): self.notice.set('队列正在运行。可同时审核其他空闲章节；新一轮请先停止当前队列。'); return
        if not (self.store.settings['video'] or self.store.settings['answer']): self.notice.set('请至少选择视频或 AI 答题。'); return
        courses = [copy.deepcopy(c) for c in self.courses if course_key(c) in self.selected_courses]
        if not courses: self.notice.set('请先勾选一个或多个课程。'); return
        settings, all_courses = copy.deepcopy(self.store.settings), set(self.all_courses)
        self.preparing = True; self.start_button.configure(state='disabled')
        self.notice.set(f'正在后台加载 {len(courses)} 门课程的章节…')
        def resolve():
            plan, skipped = [], []
            for course in courses:
                key = course_key(course)
                try:
                    points = self.engine.chapters(course)
                    chosen = points if key in all_courses else [p for p in points if str(p['id']) in settings['selections'].get(key, [])]
                    if chosen: plan.append((course, chosen))
                    else: skipped.append(course['title'] + '：未选择章节')
                except Exception as exc: skipped.append(course['title'] + '：' + str(exc))
            return plan, skipped
        self.background(resolve, 'selection_plan')

    def remove_selected(self):
        selected = self.task_tree.selection()
        if not selected: self.notice.set('请先选择队列任务，可用 Ctrl / Shift 多选。'); return
        self.engine.remove_tasks(selected)
        self.notice.set(f'已请求移出 {len(selected)} 项任务；仅停止选中项，题目和答案保留。')

    def render_all_tasks(self):
        for record in self.records.values(): self.render_task(record)

    def execution_label(self, record):
        status = record.get('status', '')
        if status == 'interrupted':
            if record.get('reason', '').startswith('未开始'): return '未开始 · 已停止'
            if record.get('reason', '').startswith('处理中'): return '处理中 · 已停止'
        text = STATUS.get(status, status)
        if status == 'running' and record['id'] in self.progress:
            current, total = self.progress[record['id']]; text += f' {current}/{total} 秒'
        return text

    def render_task(self, record):
        tid = record['id']
        archived = record.get('status') == 'removed' or (record.get('queue_removed') and record.get('status') != 'removing')
        hidden = (record.get('lane') == 'discovery' and record.get('status') == 'done') or (archived and not self.show_removed.get())
        if hidden:
            if self.task_tree.exists(tid): self.task_tree.delete(tid)
            return
        values = (record.get('course', {}).get('title', ''), {'answer': '答题', 'video': '视频', 'discovery': '读取'}.get(record.get('lane'), ''),
                  record.get('point', {}).get('title', '') + ' / ' + record.get('job', {}).get('name', ''), self.execution_label(record),
                  remote_label(record) if record.get('lane') == 'answer' else '', record.get('reason', ''))
        if self.task_tree.exists(tid): self.task_tree.item(tid, values=values)
        else: self.task_tree.insert('', 'end', iid=tid, values=values)

    def open_selected_review(self, event=None):
        ids = [tid for tid in self.task_tree.selection() if self.records.get(tid, {}).get('lane') == 'answer']
        self.review_scope.set('全部记录'); self.review_course.set('全部课程'); self.render_review()
        self.review_tree.selection_set([tid for tid in ids if self.review_tree.exists(tid)])
        self.render_questions(); self.notebook.select(1)

    def refresh_review(self): self.review_dirty = True

    def visible_review_records(self):
        result = []
        for record in self.records.values():
            if record.get('lane') != 'answer': continue
            course = record.get('course', {}); name = f"{course.get('title', '')} · {course.get('courseId', '')}"
            if self.review_course.get() not in ('全部课程', name): continue
            quality, status, scope = summary(record), record.get('status'), self.review_scope.get()
            if scope == '全部待处理' and status in FINAL: continue
            if scope == '仅异常题章节' and not (quality['errors'] or not quality['total']): continue
            if scope == '完整可交卷' and not can_submit(record): continue
            if scope == '已交卷' and status not in FINAL: continue
            if scope == '交卷待核验' and status not in UNCERTAIN: continue
            result.append(record)
        return result

    def render_review(self):
        self.review_dirty = False
        chosen = set(self.review_tree.selection()); visible = self.visible_review_records(); ids = {r['id'] for r in visible}
        for tid in self.review_tree.get_children():
            if tid not in ids: self.review_tree.delete(tid)
        for record in visible:
            q = summary(record)
            values = (record.get('course', {}).get('title', ''), record.get('point', {}).get('title', ''), f"{q['good']} / {q['errors']} / {q['total']}", self.execution_label(record), remote_label(record))
            if self.review_tree.exists(record['id']): self.review_tree.item(record['id'], values=values)
            else: self.review_tree.insert('', 'end', iid=record['id'], values=values)
        self.review_tree.selection_set(list(chosen & ids))
        self.review_summary.configure(text=f'{len(visible)} 个章节 · {sum(can_submit(r) for r in visible)} 个完整可交卷')
        names = sorted({f"{r.get('course', {}).get('title', '')} · {r.get('course', {}).get('courseId', '')}" for r in self.records.values() if r.get('lane') == 'answer'})
        self.review_course_box['values'] = ['全部课程'] + names
        self.render_questions()

    def render_questions(self):
        old = {self.question_refs[i] for i in self.question_tree.selection() if i in self.question_refs}
        chapters = set(self.review_tree.selection()) or set(self.review_tree.get_children())
        self.question_tree.delete(*self.question_tree.get_children()); self.question_refs = {}; selected = []
        for tid in self.review_tree.get_children():
            if tid not in chapters: continue
            record = self.records[tid]
            for row in rows_for(record):
                if self.question_filter.get() == '需处理' and not row['error']: continue
                if self.question_filter.get() == '校验通过' and row['error']: continue
                q = row['question']; iid = str(len(self.question_refs)); self.question_refs[iid] = (tid, q['id'])
                self.question_tree.insert('', 'end', iid=iid, values=(record.get('course', {}).get('title', ''), record.get('point', {}).get('title', ''), q['title'], row.get('answer', ''), row.get('source', ''), row['error'] or '校验通过 · 待人工确认正确性'))
                if (tid, q['id']) in old: selected.append(iid)
        self.question_tree.selection_set(selected)

    def question_selected(self, event=None):
        selection = self.question_tree.selection()
        if not selection: return
        tid, qid = self.question_refs[selection[-1]]; record = self.records[tid]
        row = next(r for r in rows_for(record) if r['question']['id'] == qid); q = row['question']
        history = '\n'.join(f"{i+1}. {old.get('source', '')}：{old.get('answer', '')} {old.get('error', '')}" for i, old in enumerate(row.get('history', [])))
        text = f"{record.get('course', {}).get('title', '')} / {record.get('point', {}).get('title', '')}\n题目：{q['title']}\n{q.get('options', '')}\n选择：{row.get('answer', '')}  来源：{row.get('source', '')}\n错误：{row['error']}\n平台：{remote_label(record)}\n历史记录：\n{history or '无'}"
        self.detail.configure(state='normal'); self.detail.delete('1.0', 'end'); self.detail.insert('1.0', text); self.detail.configure(state='disabled')

    def edit_answer(self, event=None):
        selected = self.question_tree.selection()
        if len(selected) != 1: self.notice.set('人工修正请选择一道题；批量重做可以直接多选。'); return
        tid, qid = self.question_refs[selected[0]]
        if tid in self.engine.manual or self.engine.mutex(tid).locked(): self.notice.set('这个章节正在处理，请稍后修正；无需停止其他队列。'); return
        row = next(r for r in rows_for(self.records[tid]) if r['question']['id'] == qid)
        value = simpledialog.askstring('人工修正', '单选填 A，多选填 AC；判断填 true 或 false。', initialvalue=row.get('answer', ''), parent=self.root)
        if value is None: return
        value = value.strip(); payload = {'answers': [value.lower()] if row['question']['type'] == 'judgement' else list(value.upper())}
        try:
            self.engine.edit_answer(tid, qid, payload); self.notice.set('修正已本地保存，原结果保留在历史记录中。')
        except (ValueError, StopIteration) as exc: self.notice.set('未修改答案：' + str(exc))

    def begin_batch(self, action, task_ids, question_ids=None, allow_blank=False):
        try:
            self.engine.batch(action, task_ids, question_ids, allow_blank)
            self.detail_tabs.select(1); self.notice.set('批量' + ACTIONS[action] + '已开始；其他课程和队列继续运行。')
        except ValueError as exc: self.notice.set(str(exc))

    def chapter_batch(self, action):
        self.begin_batch(action, self.review_tree.selection(), allow_blank=self.allow_blank.get() if action == 'submit' else False)
        if action == 'submit': self.allow_blank.set(False)

    def blank_hint(self):
        if self.allow_blank.get():
            count = sum(summary(self.records[tid])['errors'] for tid in self.review_tree.selection())
            self.notice.set(f'本次选中章节有 {count} 道异常或未答题，将留空随整章交卷；平台可能拒绝或按未答处理。')

    def submit_all_ready(self): self.begin_batch('submit', [r['id'] for r in self.visible_review_records() if can_submit(r)])

    def selected_question_map(self, errors_only=False):
        ids = self.question_tree.get_children() if errors_only else self.question_tree.selection(); result = {}
        for iid in ids:
            tid, qid = self.question_refs[iid]; record = self.records[tid]
            if not needs_review(record): continue
            row = next(r for r in rows_for(record) if r['question']['id'] == qid)
            if errors_only and not row['error']: continue
            result.setdefault(tid, []).append(qid)
        return result

    def redo_selected(self):
        questions = self.selected_question_map()
        if self.save(): self.begin_batch('redo', list(questions), questions)

    def redo_errors(self):
        questions = self.selected_question_map(True)
        if self.save(): self.begin_batch('redo', list(questions), questions)

    def save_question_chapters(self): self.begin_batch('save', list(self.selected_question_map()))

    def render_batch(self, batch):
        for index, item in enumerate(batch['items']):
            iid = batch['id'] + ':' + str(index)
            values = (ACTIONS[batch['action']], item.get('label', item['task_id']), {'success': '成功', 'failed': '失败', 'partial': '已完成 · 仍有异常', 'skipped': '已跳过', 'cancelled': '已停止'}.get(item['result'], item['result']), item.get('reason', ''))
            if self.batch_tree.exists(iid): self.batch_tree.item(iid, values=values)
            else: self.batch_tree.insert('', 'end', iid=iid, values=values)

    def poll(self):
        if self.closed: return
        for _ in range(200):
            try: kind, value = self.events.get_nowait()
            except Empty: break
            if kind == 'courses':
                self.courses = value; valid = {course_key(c) for c in value}; self.selected_courses &= valid
                if not self.selected_courses and self.store.settings.get('last_course') in valid: self.selected_courses.add(self.store.settings['last_course'])
                self.render_courses(); self.connection.configure(text=f'已登录 · {len(value)} 门课程')
                self.notice.set('勾选多个课程，再选择各自章节；可一键设置全部章节。'); self.notebook.select(0)
                if value:
                    key = next(iter(self.selected_courses), course_key(value[0])); self.course_tree.selection_set(key); self.course_tree.focus(key); self.focus_course()
            elif kind == 'connect_finished': self.connecting = False; self.login_button.configure(state='normal')
            elif kind == 'chapters': self.show_chapters(*value)
            elif kind == 'chapter_load_finished': self.loading_courses.discard(value)
            elif kind == 'selection_plan':
                self.preparing = False; self.start_button.configure(state='normal'); plan, skipped = value
                try:
                    if not plan: raise ValueError('没有可启动的章节。' + '；'.join(skipped))
                    self.engine.start(plan); self.notice.set(f'已启动 {len(plan)} 门课程、{sum(len(p) for _, p in plan)} 个章节。' + (' 跳过：' + '；'.join(skipped) if skipped else ''))
                except ValueError as exc: self.notice.set(str(exc))
            elif kind == 'task':
                if value.get('revision', 0) >= self.records.get(value['id'], {}).get('revision', 0):
                    self.records[value['id']] = value; self.render_task(value); self.review_dirty = True
            elif kind == 'progress':
                tid, current, total = value; self.progress[tid] = current, total
                if tid in self.records: self.render_task(self.records[tid])
            elif kind == 'batch':
                self.batches[value['id']] = value; self.render_batch(value); self.review_dirty = True
                if value['done']:
                    counts = {s: sum(i['result'] == s for i in value['items']) for s in ['success', 'failed', 'partial', 'skipped', 'cancelled']}
                    self.notice.set(f"批量{ACTIONS[value['action']]}结束：成功 {counts['success']}，失败 {counts['failed']}，仍有异常 {counts['partial']}，跳过 {counts['skipped']}，停止 {counts['cancelled']}。详见批量操作结果。")
            elif kind == 'error':
                if isinstance(value, dict): self.last_error = value; reason, operation = value['reason'], value['operation']
                else: reason, operation = str(value), ''; self.last_error = {'reason': reason, 'operation': ''}
                self.error_details.configure(state='normal'); prefix = '登录失败' if self.connecting or operation == 'courses' else '操作失败'
                self.notice.set(prefix + '：' + reason[:500])
                if self.connecting or operation == 'courses': self.connection.configure(text='登录失败：' + reason[:90])
                if operation == 'selection_plan': self.preparing = False; self.start_button.configure(state='normal')
            elif kind == 'discovery_done': self.notice.set('章节扫描结束；两条队列各自继续，可同时审核其他空闲章节。')
        for key, lane in self.engine.lanes.items():
            counts = lane.counts(); state = '已停止' if lane.cancelled.is_set() else '已暂停' if not lane.enabled.is_set() else '运行中' if any(counts.get(s, 0) for s in ['running', 'queued']) else '当前队列已处理完'
            self.lane_labels[key].configure(text=f"{state} · 执行 {counts.get('running', 0)} · 等待 {counts.get('queued', 0)} · 移出 {counts.get('removed', 0)}")
        if self.review_dirty: self.render_review()
        self.engine.poll_recheck(); self.root.after(200, self.poll)
