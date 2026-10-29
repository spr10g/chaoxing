import json
from queue import Empty
import tkinter as tk
from tkinter import ttk

from .ui_base import BaseApp
from .ui_tables import CheckedTable
from .ui_picker import TaskPicker
from .review import rows_for, summary, can_submit, needs_sync, needs_review, remote_label, FINAL, UNCERTAIN


ACTIONS = {'verify': '读取平台状态', 'save': '同步草稿', 'submit': '提交测验', 'redo': 'AI 重做'}
ACTIVE = {'checking', 'queued', 'running', 'removing', 'blocked'}
EXECUTION = {'checking': '读取进度', 'queued': '等待执行', 'running': '执行中', 'removing': '正在取消',
             'removed': '已取消', 'interrupted': '已取消', 'failed': '失败', 'blocked': '未开放',
             'paused': '连接异常', 'done': '已完成'}


def execution_state(record):
    state = record.get('execution_state', record.get('status', ''))
    return 'done' if state in ('draft', 'local_saved', 'review', 'submitted', 'platform_done', 'unknown', 'submitting') else state


def paper_state(record):
    if record.get('status') in UNCERTAIN: return '待确认'
    if record.get('status') in FINAL: return '已交卷'
    quality = summary(record)
    if not quality['total']: return '待答题'
    return '待处理' if quality['errors'] else '可提交'


def question_state(row):
    if not row['error']: return '正常'
    return '未答' if row['error'] in ('尚未处理', '题目更新或尚未处理') else '异常'


def location(record):
    chapter = record.get('point', {}).get('title', '')
    job = record.get('job', {})
    return chapter + (' / ' + str(job.get('name') or job.get('jobid') or '') if job else '')


class App(BaseApp):
    def __init__(self, root, store=None):
        self.picker = None
        self.question_refs = {}
        self.review_dirty = True
        self.tasks_dirty = True
        self.batch_window = None
        self.batches = {}
        super().__init__(root, store)
        self.batches = {b['id']: b for b in self.store.get('batch_history') or []}
        self.render_all_tasks(); self.render_review()

    def button(self, parent, text, command, accent=False):
        button = ttk.Button(parent, text=text, command=command, style='Accent.TButton' if accent else 'TButton')
        button.pack(side='left', padx=(0, 6))
        return button

    def combo(self, parent, variable, values, command, width=15):
        box = ttk.Combobox(parent, textvariable=variable, values=values, state='readonly', width=width)
        box.pack(side='left', padx=(0, 10)); box.bind('<<ComboboxSelected>>', lambda event: command())
        return box

    def _run(self, parent):
        bar = ttk.Frame(parent); bar.pack(fill='x', pady=(0, 12))
        self.add_button = self.button(bar, '添加任务', self.open_picker, True)
        self.cancel_button = self.button(bar, '取消已选', self.remove_selected)
        self.requeue_button = self.button(bar, '重新加入', self.requeue_selected)
        self.task_detail_button = self.button(bar, '查看详情', self.show_task)
        self.task_scope = tk.StringVar(value='当前任务')
        self.task_kind = tk.StringVar(value='全部类型')
        filters = ttk.Frame(parent); filters.pack(fill='x', pady=(0, 8))
        self.combo(filters, self.task_scope, ['当前任务', '历史记录', '全部任务'], self.render_all_tasks)
        self.combo(filters, self.task_kind, ['全部类型', '视频', '答题', '读取'], self.render_all_tasks, 10)
        self.task_delete_button = self.button(filters, '删除', lambda: self.delete_selected('tasks'))
        self.task_count = ttk.Label(filters, style='Muted.TLabel'); self.task_count.pack(side='right')
        self.task_table = CheckedTable(parent, ('course', 'content', 'kind', 'state', 'progress'),
                                      ('课程', '章节 / 内容', '类型', '执行状态', '进度'), self.task_selection,
                                      {'course': 150, 'content': 310, 'kind': 80, 'state': 110, 'progress': 130})
        self.task_table.pack(fill='both', expand=True); self.task_tree = self.task_table.tree
        self.queue_summary = ttk.Label(parent, style='Muted.TLabel'); self.queue_summary.pack(anchor='w', pady=(10, 0))
        self.task_selection()

    def _review(self, parent):
        top = ttk.Frame(parent); top.pack(fill='x', pady=(0, 10))
        self.review_course = tk.StringVar(value='全部课程')
        self.review_course_box = self.combo(top, self.review_course, ['全部课程'], self.render_review, 26)
        ttk.Button(top, text='操作记录', command=self.show_batches).pack(side='right')
        self.answer_tabs = ttk.Notebook(parent); self.answer_tabs.pack(fill='both', expand=True)
        questions, papers = ttk.Frame(self.answer_tabs, padding=(0, 10)), ttk.Frame(self.answer_tabs, padding=(0, 10))
        self.answer_tabs.add(questions, text='题目处理'); self.answer_tabs.add(papers, text='测验提交')
        qbar = ttk.Frame(questions); qbar.pack(fill='x', pady=(0, 8))
        self.question_filter = tk.StringVar(value='需处理')
        self.combo(qbar, self.question_filter, ['需处理', '全部题目', '正常题', '异常题', '未答题'], self.render_review, 12)
        self.redo_button = self.button(qbar, 'AI 重做', self.redo_selected, True)
        self.edit_button = self.button(qbar, '查看 / 修改', self.show_question)
        self.question_delete_button = self.button(qbar, '删除', lambda: self.delete_selected('questions'))
        self.question_count = ttk.Label(qbar, style='Muted.TLabel'); self.question_count.pack(side='right')
        self.question_table = CheckedTable(questions, ('course', 'paper', 'title', 'answer', 'state'),
                                          ('课程', '章节 / 测验', '题目', '答案', '状态'), self.question_selection,
                                          {'course': 125, 'paper': 230, 'title': 300, 'answer': 80, 'state': 80})
        self.question_table.pack(fill='both', expand=True); self.question_tree = self.question_table.tree
        pbar = ttk.Frame(papers); pbar.pack(fill='x', pady=(0, 8))
        self.paper_filter = tk.StringVar(value='待处理')
        self.combo(pbar, self.paper_filter, ['待处理', '全部测验', '待同步', '可提交', '有异常', '待确认', '已交卷'], self.render_review, 12)
        self.sync_button = self.button(pbar, '同步草稿', lambda: self.paper_batch('save'))
        self.submit_button = self.button(pbar, '提交测验', lambda: self.paper_batch('submit'), True)
        self.paper_more = ttk.Menubutton(pbar, text='更多'); self.paper_more.pack(side='left', padx=(0, 6))
        menu = tk.Menu(self.paper_more, tearoff=False); self.paper_more.configure(menu=menu)
        menu.add_command(label='读取平台状态', command=lambda: self.paper_batch('verify'))
        menu.add_command(label='含未答题交卷', command=lambda: self.paper_batch('submit', True))
        self.paper_delete_button = self.button(pbar, '删除', lambda: self.delete_selected('papers'))
        self.paper_count = ttk.Label(pbar, style='Muted.TLabel'); self.paper_count.pack(side='right')
        self.paper_table = CheckedTable(papers, ('course', 'paper', 'counts', 'state', 'sync'),
                                       ('课程', '章节 / 测验', '正常 / 异常 / 总数', '答题状态', '平台草稿'), self.paper_selection,
                                       {'course': 140, 'paper': 300, 'counts': 135, 'state': 90, 'sync': 130})
        self.paper_table.pack(fill='both', expand=True); self.review_tree = self.paper_table.tree
        self.answer_tabs.bind('<<NotebookTabChanged>>', lambda event: self.selection_counts())
        self.selection_counts()

    def open_picker(self):
        if not self.engine.connected:
            self.notice.set('请先在“设置”登录并加载课程'); self.notebook.select(2); return
        if self.picker and self.picker.alive(): self.picker.root.lift(); return
        self.picker = TaskPicker(self)

    def task_selection(self):
        if not hasattr(self, 'task_table'): return
        ids = self.task_table.selected()
        active = [tid for tid in ids if self.is_active(self.records[tid])]
        history = [tid for tid in ids if not self.is_active(self.records[tid]) and self.records[tid].get('lane') in ('video', 'answer', 'discovery')]
        self.task_count.configure(text=f'已选 {len(ids)} 项 / {len(self.task_tree.get_children())} 项')
        self.cancel_button.configure(state='normal' if active else 'disabled')
        self.requeue_button.configure(state='normal' if history and len(history) == len(ids) else 'disabled')
        self.task_detail_button.configure(state='normal' if len(ids) == 1 else 'disabled')
        self.task_delete_button.configure(state='normal' if ids else 'disabled')

    def question_selection(self):
        if not hasattr(self, 'question_table'): return
        ids = self.question_table.selected()
        self.question_count.configure(text=f'已选 {len(ids)} 题')
        editable = any(needs_review(self.records[self.question_refs[i][0]]) for i in ids if i in self.question_refs)
        self.redo_button.configure(state='normal' if editable else 'disabled')
        self.edit_button.configure(state='normal' if len(ids) == 1 else 'disabled')
        self.question_delete_button.configure(state='normal' if ids else 'disabled')

    def paper_selection(self):
        if not hasattr(self, 'paper_table'): return
        records = [self.records[i] for i in self.paper_table.selected()]
        self.paper_count.configure(text=f'已选 {len(records)} 份')
        self.sync_button.configure(state='normal' if any(needs_sync(r) for r in records) else 'disabled')
        self.submit_button.configure(state='normal' if any(can_submit(r) for r in records) else 'disabled')
        self.paper_more.configure(state='normal' if records else 'disabled')
        self.paper_delete_button.configure(state='normal' if records else 'disabled')

    def selection_counts(self):
        self.question_selection(); self.paper_selection()

    def is_active(self, record):
        return record['id'] in self.engine.manual or execution_state(record) in ACTIVE

    def render_task(self, record):
        self.tasks_dirty = True

    def render_all_tasks(self):
        self.tasks_dirty = False
        rows = []
        for record in sorted(self.records.values(), key=lambda r: r.get('updated', 0), reverse=True):
            if record.get('list_hidden', {}).get('tasks'): continue
            if record.get('lane') == 'discovery' and record.get('status') == 'done': continue
            active = self.is_active(record)
            if self.task_scope.get() == '当前任务' and not active: continue
            if self.task_scope.get() == '历史记录' and active: continue
            kind = {'video': '视频', 'answer': '答题', 'discovery': '读取'}.get(record.get('lane'), '')
            if self.task_kind.get() not in ('全部类型', kind): continue
            state = '处理中' if record['id'] in self.engine.manual else EXECUTION.get(execution_state(record), '已结束')
            progress = record.get('progress', {})
            text = f"{int(progress['current'])} / {int(progress['total'])} 秒" if progress.get('total') else ''
            if record.get('lane') == 'answer' and record.get('document'):
                q = summary(record); text = f"{q['good']} 正常 / {q['errors']} 待处理"
            rows.append((record['id'], (record.get('course', {}).get('title', ''), location(record), kind, state, text)))
        self.task_table.set_rows(rows)

    def remove_selected(self):
        ids = [tid for tid in self.task_table.selected() if self.is_active(self.records[tid])]
        if not ids: return
        self.engine.remove_tasks(ids)
        self.notice.set(f'正在取消 {len(ids)} 项任务，结果保留在历史记录')

    def delete_selected(self, view):
        table = {'tasks': self.task_table, 'questions': self.question_table, 'papers': self.paper_table}[view]
        selected = table.selected()
        if not selected: return
        questions = {}
        if view == 'questions':
            for iid in selected:
                tid, qid = self.question_refs[iid]
                questions.setdefault(tid, []).append(qid)
            ids = list(questions)
        else:
            ids = selected
        for record in self.engine.delete_list_items(view, ids, questions):
            self.records[record['id']] = record
        self.render_all_tasks(); self.render_review()
        label = {'tasks': '任务', 'questions': '题目', 'papers': '测验'}[view]
        self.notice.set(f'已从{label}列表删除 {len(selected)} 项，题目和答案已保留')

    def requeue_selected(self):
        ids = self.task_table.selected()
        if not ids or not self.save(): return
        try:
            self.engine.requeue(ids)
            self.task_scope.set('当前任务'); self.tasks_dirty = True
            self.notice.set(f'已重新加入 {len(ids)} 项任务，正在读取平台进度')
        except ValueError as exc: self.notice.set(str(exc))

    def course_matches(self, record):
        c = record.get('course', {})
        name = f"{c.get('title', '')} · {c.get('courseId', '')}"
        return self.review_course.get() in ('全部课程', name)

    def render_review(self):
        self.review_dirty = False
        papers = [r for r in self.records.values() if r.get('lane') == 'answer']
        names = sorted({f"{r.get('course', {}).get('title', '')} · {r.get('course', {}).get('courseId', '')}" for r in papers})
        self.review_course_box['values'] = ['全部课程', *names]
        if self.review_course.get() not in self.review_course_box['values']: self.review_course.set('全部课程')
        p_rows, q_rows, refs = [], [], {}
        for record in papers:
            if not self.course_matches(record): continue
            tid = record['id']; quality = summary(record); state = paper_state(record)
            scope = self.paper_filter.get()
            include = (scope == '全部测验' or (scope == '待处理' and state != '已交卷') or
                       (scope == '待同步' and needs_sync(record)) or (scope == '可提交' and can_submit(record)) or
                       (scope == '有异常' and quality['errors']) or scope == state)
            hidden = record.get('list_hidden', {})
            if include and not hidden.get('papers'):
                sync = '已同步' if record.get('remote_state') == 'draft' else '待同步' if needs_sync(record) else '—'
                p_rows.append((tid, (record.get('course', {}).get('title', ''), location(record),
                                    f"{quality['good']} / {quality['errors']} / {quality['total']}", state, sync)))
            for row in rows_for(record):
                if row['question']['id'] in hidden.get('questions', []): continue
                qstate = question_state(row); qscope = self.question_filter.get()
                if qscope == '需处理' and (not row['error'] or record.get('status') in FINAL): continue
                if qscope in ('正常题', '异常题', '未答题') and qstate != qscope[:-1]: continue
                qid = row['question']['id']; iid = json.dumps([tid, qid], ensure_ascii=True)
                refs[iid] = tid, qid
                q_rows.append((iid, (record.get('course', {}).get('title', ''), location(record),
                                     row['question']['title'], row.get('answer', ''), qstate)))
        self.question_refs = refs
        self.paper_table.set_rows(p_rows); self.question_table.set_rows(q_rows)

    def selected_question_map(self):
        selected = {}
        for iid in self.question_table.selected():
            if iid not in self.question_refs: continue
            tid, qid = self.question_refs[iid]
            if needs_review(self.records[tid]): selected.setdefault(tid, []).append(qid)
        return selected

    def begin_batch(self, action, task_ids, question_ids=None, allow_blank=False):
        if not task_ids: return
        if not self.save(): return
        try:
            self.engine.batch(action, task_ids, question_ids, allow_blank)
            self.tasks_dirty = True
            self.notice.set(f"{ACTIONS[action]}已开始，范围：{sum(map(len, question_ids.values()))} 道题" if question_ids else f'{ACTIONS[action]}已开始，范围：{len(task_ids)} 份测验')
        except ValueError as exc: self.notice.set(str(exc))

    def paper_batch(self, action, allow_blank=False):
        ids = self.paper_table.selected()
        if action == 'save': ids = [tid for tid in ids if needs_sync(self.records[tid])]
        self.begin_batch(action, ids, allow_blank=allow_blank)

    def redo_selected(self):
        selected = self.selected_question_map()
        self.begin_batch('redo', list(selected), selected)

    def dialog(self, title, text):
        window = tk.Toplevel(self.root); window.title(title); window.transient(self.root)
        window.geometry('820x570'); window.minsize(620, 400)
        area = ttk.Frame(window, padding=16); area.pack(fill='both', expand=True)
        box = tk.Text(area, wrap='word', font=('Microsoft YaHei UI', 10), relief='flat', padx=12, pady=10)
        scroll = ttk.Scrollbar(area, orient='vertical', command=box.yview)
        box.configure(yscrollcommand=scroll.set); scroll.pack(side='right', fill='y'); box.pack(fill='both', expand=True)
        box.insert('1.0', text); box.configure(state='disabled')
        return window

    def show_question(self):
        ids = self.question_table.selected()
        if len(ids) != 1: return
        tid, qid = self.question_refs[ids[0]]; record = self.records[tid]
        row = next(r for r in rows_for(record) if r['question']['id'] == qid); q = row['question']
        history = '\n'.join(f"{i+1}. {r.get('source', '')}：{r.get('answer', '')} {r.get('error', '')}" for i, r in enumerate(row.get('history', [])))
        window = self.dialog('题目详情', f"{record.get('course', {}).get('title', '')} / {location(record)}\n\n{q['title']}\n\n{q.get('options', '')}\n\n当前答案：{row.get('answer', '')}\n处理状态：{row['error'] or '正常'}\n\n答案历史\n{history or '暂无'}")
        bar = ttk.Frame(window, padding=16); bar.pack(fill='x', side='bottom', before=window.winfo_children()[0])
        ttk.Label(bar, text='答案').pack(side='left', padx=(0, 8))
        value = tk.StringVar(value=row.get('answer', ''))
        ttk.Entry(bar, textvariable=value).pack(side='left', fill='x', expand=True)
        def save():
            answer = value.get().strip()
            payload = {'answers': [answer.lower()] if q['type'] == 'judgement' else list(answer.upper())}
            try:
                self.engine.edit_answer(tid, qid, payload)
                self.notice.set('答案已修改，测验草稿待同步'); window.destroy()
            except ValueError as exc: self.notice.set(str(exc))
        button = ttk.Button(bar, text='保存修改', command=save); button.pack(side='left', padx=(10, 0))
        button.configure(state='normal' if needs_review(record) else 'disabled')

    def show_task(self):
        ids = self.task_table.selected()
        if len(ids) != 1: return
        record = self.records[ids[0]]
        self.dialog('任务详情', f"{record.get('course', {}).get('title', '')}\n{location(record)}\n\n执行状态：{EXECUTION.get(execution_state(record), '已结束')}\n说明：{record.get('reason', '')}\n\n" + (remote_label(record) if record.get('lane') == 'answer' else ''))

    def show_batches(self):
        if self.batch_window and self.batch_window.winfo_exists(): self.batch_window.lift(); return
        window = tk.Toplevel(self.root); self.batch_window = window
        window.title('操作记录'); window.geometry('900x480'); window.transient(self.root)
        area = ttk.Frame(window, padding=16); area.pack(fill='both', expand=True)
        self.batch_table = CheckedTable(area, ('action', 'paper', 'result', 'reason'), ('操作', '测验', '结果', '说明'))
        self.batch_table.pack(fill='both', expand=True)
        self.render_batches()

    def render_batches(self):
        if not self.batch_window or not self.batch_window.winfo_exists(): return
        rows = []
        for batch in reversed(list(self.batches.values())):
            for index, item in enumerate(batch['items']):
                rows.append((batch['id']+':'+str(index), (ACTIONS[batch['action']], item.get('label', item['task_id']),
                             {'success': '成功', 'failed': '失败', 'partial': '仍需处理', 'skipped': '已跳过', 'cancelled': '已取消'}.get(item['result'], item['result']), item.get('reason', ''))))
        self.batch_table.set_rows(rows)

    def poll(self):
        if self.closed: return
        for _ in range(200):
            try: kind, value = self.events.get_nowait()
            except Empty: break
            if kind == 'courses':
                self.courses = value; self.connection.configure(text=f'已登录 · {len(value)} 门课程')
                self.notice.set('点击“添加任务”选择课程与章节'); self.notebook.select(0)
            elif kind == 'connect_finished':
                self.connecting = False; self.login_button.configure(state='normal')
            elif kind == 'picker_chapters':
                picker, key, points = value
                if picker == self.picker and picker.alive(): picker.chapters_loaded(key, points)
            elif kind == 'selection_plan':
                picker, plan, skipped = value
                if picker != self.picker or not picker.alive(): continue
                try:
                    if not plan: raise ValueError('没有可添加的章节。'+'；'.join(skipped))
                    self.engine.add(plan)
                    self.task_scope.set('当前任务'); self.tasks_dirty = True
                    self.notice.set(f'已添加 {len(plan)} 门课程，正在读取平台进度'+('；'+'；'.join(skipped) if skipped else ''))
                    if picker.alive(): picker.close()
                except ValueError as exc:
                    self.notice.set(str(exc))
                    if picker.alive(): picker.start_button.configure(state='normal')
            elif kind == 'task':
                if value.get('revision', 0) >= self.records.get(value['id'], {}).get('revision', 0):
                    self.records[value['id']] = value; self.tasks_dirty = True; self.review_dirty = True
            elif kind == 'progress': self.tasks_dirty = True
            elif kind == 'batch':
                self.batches[value['id']] = value; self.render_batches(); self.review_dirty = True; self.tasks_dirty = True
                if value['done']:
                    counts = {state: sum(i['result'] == state for i in value['items']) for state in ('success', 'failed', 'partial', 'skipped', 'cancelled')}
                    self.notice.set(f"{ACTIONS[value['action']]}完成：成功 {counts['success']}，需处理 {counts['failed'] + counts['partial']}，跳过 {counts['skipped']}，取消 {counts['cancelled']}")
            elif kind == 'error':
                if isinstance(value, dict): self.last_error = value; reason, operation = value['reason'], value['operation']
                else: reason, operation = str(value), ''; self.last_error = {'reason': reason, 'operation': ''}
                self.error_details.configure(state='normal')
                self.notice.set(('登录失败：' if self.connecting or operation == 'courses' else '操作失败：')+reason[:500])
                if self.connecting or operation == 'courses': self.connection.configure(text='登录失败：'+reason[:90])
                if operation == 'selection_plan' and self.picker and self.picker.alive(): self.picker.start_button.configure(state='normal')
                if operation == 'picker_chapters' and self.picker: self.picker.loading.clear()
            elif kind == 'discovery_done': self.tasks_dirty = True
        if self.tasks_dirty: self.render_all_tasks()
        if self.review_dirty: self.render_review()
        parts = []
        for key, title in (('video', '视频'), ('answer', '答题')):
            lane = self.engine.lanes.get(key); counts = lane.counts() if lane else {}
            parts.append(f"{title}：执行 {counts.get('running', 0)} · 等待 {counts.get('queued', 0)}")
        self.queue_summary.configure(text='     '.join(parts))
        self.engine.poll_recheck(); self.root.after(200, self.poll)
