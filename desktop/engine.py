import copy
from contextlib import contextmanager
import threading
import time
import uuid

from .answers import AnswerService, fingerprint, validate
from .review import (rows_for, summary, can_submit, changed_row,
                     confirmed_submission, FINAL, UNCERTAIN, FormConflict, SubmissionRejected, SubmissionUncertain)
from .scheduler import Lane, LaneFault, Cancelled, TaskRemoved


def course_key(course):
    return ':'.join(str(course.get(key, '')) for key in ('courseId', 'clazzId', 'cpi'))


class Engine:
    def __init__(self, store, emit=lambda *args: None, platform=None):
        self.store, self.emit, self.platform = store, emit, platform
        self.ai = AnswerService(store, store.settings)
        self.lanes = {}
        self.discovery = None
        self.discovery_threads = set()
        self.scan_lock = threading.Lock()
        self.discovery_stop = threading.Event()
        self.lock = threading.RLock()
        self.discovery_guard = threading.RLock()
        self.refresh_requested = threading.Event()
        self.blocked = {}
        self.excluded = set()
        self.manual = set()
        self.manual_cancel = {}
        self.manual_action = {}
        self.task_locks = {}
        self.batch_threads = set()
        self.connected = platform is not None
        self.auto_submit = False

    def connect(self):
        self.connected = False
        if self.platform is None:
            from .backend import Platform
            self.platform = Platform(self.store)
        self.platform.bind(lambda: None)
        courses = self.platform.login()
        self.connected = True
        return courses

    def chapters(self, course):
        if not self.connected:
            raise ValueError('尚未登录，请先登录后读取章节')
        return self.platform.chapters(course)

    def record(self, task, **changes):
        with self.lock:
            record = self.store.task(task['id']) or dict(task)
            record.update(changes)
            record['revision'] = record.get('revision', 0) + 1
            record['updated'] = time.time()
            self.store.save_task(record)
            self.emit('task', copy.deepcopy(record))
        return record

    def mutex(self, task_id):
        with self.lock:
            return self.task_locks.setdefault(task_id, threading.Lock())

    @contextmanager
    def task_guard(self, task, checkpoint):
        mutex = self.mutex(task['id'])
        while True:
            checkpoint()
            with self.lock:
                reserved = task['id'] in self.manual
                acquired = not reserved and mutex.acquire(blocking=False)
            if acquired:
                break
            time.sleep(.05)
        try:
            yield
        finally:
            mutex.release()

    def state(self, task, status, reason):
        old = self.store.task(task['id']) or {}
        if not reason and status not in ('queued', 'running'):
            reason = old.get('reason', '')
        if status == 'blocked' and not reason:
            reason = '章节未开放或等待前置任务'
        execution = status if status in ('queued', 'running', 'checking', 'removing', 'removed', 'failed', 'blocked', 'interrupted') else 'done'
        self.record(task, status=status, execution_state=execution, reason=reason)
        self.emit('counts', {key: lane.counts() for key, lane in self.lanes.items()})

    def configure(self):
        self.ai.configure(self.store.settings)

    def start(self, selections):
        if not self.connected:
            raise ValueError('尚未登录，请先登录后启动任务')
        if self.busy():
            raise ValueError('任务队列正在运行，请先停止当前队列或等待完成，再启动新一轮')
        if self.manual:
            raise ValueError('所选任务正在批量处理，请等待批量处理完成后启动新一轮')
        self.stop()
        for lane in self.lanes.values():
            lane.wait_stopped(.25)
            if lane.alive():
                raise ValueError('上轮请求正在结束，请稍后启动；登录状态正常')
        self.discovery_stop = threading.Event()
        self.refresh_requested.clear()
        self.blocked, self.excluded = {}, set()
        self.configure()
        s = self.store.settings
        self.ai.limit = int(s['answer_workers'])
        self.auto_submit = bool(s['auto_submit'])
        self.lanes = {}
        for key, handler in (('video', self.run_video), ('answer', self.run_answer)):
            if s[key]:
                self.lanes[key] = Lane(key, int(s[key + '_workers']), handler, self.state, self.task_guard, self.api_fault)
        self.discovery = threading.Thread(target=self._discover_all, args=(copy.deepcopy(selections), self.auto_submit), daemon=True)
        self.discovery.start()

    def _ensure_lanes(self, names):
        self.configure()
        self.ai.limit = int(self.store.settings['answer_workers'])
        self.auto_submit = bool(self.store.settings['auto_submit'])
        for name in names:
            old = self.lanes.get(name)
            if old and old.cancelled.is_set():
                old.wait_stopped(.1)
                if old.alive():
                    raise ValueError('上次请求正在结束，请稍后重新加入')
            if old is None or old.cancelled.is_set():
                handler = self.run_video if name == 'video' else self.run_answer
                self.lanes[name] = Lane(name, int(self.store.settings[name + '_workers']), handler, self.state, self.task_guard, self.api_fault)

    def api_fault(self, name, reason):
        if name != 'answer': return
        lane = self.lanes.get('answer')
        if lane:
            lane.fault_reason = reason
            lane.stop()
        with self.lock:
            for tid, action in self.manual_action.items():
                if action == 'redo': self.manual_cancel[tid].set()

    def add(self, selections):
        """Append selected courses without restarting independent active queues."""
        if not self.connected:
            raise ValueError('请先登录并加载课程')
        names = {name for name in ('video', 'answer') if self.store.settings[name]}
        if not names:
            raise ValueError('请选择视频或 AI 答题')
        self._ensure_lanes(names)
        chosen = {(course_key(course), str(point['id'])) for course, points in selections for point in points}
        with self.lock:
            records = [r for r in self.store.tasks()
                       if (course_key(r.get('course', {})), str(r.get('point', {}).get('id'))) in chosen
                       and r.get('lane') in names | {'discovery'}]
            for record in records:
                lane = self.lanes.get(record['lane'])
                if ((lane and lane.states.get(record['id']) == 'removing') or
                        (record['id'] in self.manual_cancel and self.manual_cancel[record['id']].is_set())):
                    raise ValueError('所选任务正在取消，请等待当前请求结束后再添加')
            for record in records:
                self.excluded.discard(record['id'])
                if record.get('list_hidden') or record.get('queue_removed'):
                    self.record(record, list_hidden={}, queue_removed=False)
        self.discovery_stop.clear()
        self._scan_async(copy.deepcopy(selections), names, auto_submit=bool(self.store.settings['auto_submit']))

    def _scan_async(self, selections, names, targets=None, auto_submit=None):
        mode = self.auto_submit if auto_submit is None else auto_submit
        def scan():
            try:
                with self.scan_lock:
                    self.platform.bind(self._discovery_checkpoint)
                    for course, points in selections:
                        for point in points:
                            self._discovery_checkpoint()
                            self._discover(course, point, allowed=names, targets=targets, auto_submit=mode)
            except Cancelled:
                pass
            finally:
                with self.lock:
                    self.discovery_threads.discard(threading.current_thread())
                self.emit('discovery_done', None)
        thread = threading.Thread(target=scan, name='progress-check', daemon=True)
        with self.lock:
            self.discovery_threads.add(thread)
        thread.start()

    def requeue(self, task_ids):
        if not self.connected:
            raise ValueError('请先登录，读取平台进度后再重新加入')
        records = [self.store.task(tid) for tid in dict.fromkeys(task_ids)]
        records = [r for r in records if r and r.get('lane') in ('video', 'answer', 'discovery')]
        if not records:
            raise ValueError('请勾选历史任务')
        for record in records:
            if hasattr(self.platform, 'check_owner'):
                self.platform.check_owner(record)
            lane = self.lanes.get(record['lane'])
            if record['id'] in self.manual or (lane and record['id'] in lane.pending):
                raise ValueError('所选任务还有请求正在结束，请稍后重新加入')
        names = set()
        for record in records:
            names.update(record.get('requested_lanes', ('video', 'answer')) if record['lane'] == 'discovery' else (record['lane'],))
        self._ensure_lanes(names)
        groups = {}
        for record in records:
            key = course_key(record['course']) + ':' + str(record['point']['id'])
            groups.setdefault(key, (record['course'], record['point']))
            self.excluded.discard(record['id'])
            self.record(record, queue_removed=False, list_hidden={}, execution_state='checking', reason='正在读取平台进度')
        self.discovery_stop.clear()
        self._scan_async([(course, [point]) for course, point in groups.values()],
                         names, {r['id'] for r in records})

    def _discovery_checkpoint(self):
        if self.discovery_stop.is_set():
            raise Cancelled('章节扫描已停止')

    def _discover_all(self, selections, auto_submit=None):
        mode = self.auto_submit if auto_submit is None else auto_submit
        try:
            self.platform.bind(self._discovery_checkpoint)
            for course, points in selections:
                for point in points:
                    self._discovery_checkpoint()
                    self._discover(course, point, auto_submit=mode)
        except Cancelled:
            pass
        except Exception as exc:
            self.emit('error', '发现任务失败：' + str(exc))
        finally:
            self.emit('discovery_done', None)

    def _discover(self, course, point, retry=0, allowed=None, targets=None, auto_submit=None):
        key = course_key(course) + ':' + str(point['id'])
        task = {'id': key + ':discovery', 'lane': 'discovery', 'course': course, 'point': point}
        previous_scan = self.store.task(task['id']) or {}
        point_selected = targets is not None and task['id'] in targets
        if point_selected:
            allowed = set(previous_scan.get('requested_lanes', allowed or self.lanes))
        limited_targets = None if point_selected else targets
        if task['id'] in self.excluded:
            return
        try:
            self.record(task, status='running', execution_state='running', requested_lanes=sorted(allowed or self.lanes), reason='正在读取平台进度')
            jobs, info = self.platform.jobs(course, point)
            self._discovery_checkpoint()
            if task['id'] in self.excluded:
                return
            if info.get('notOpen'):
                with self.discovery_guard:
                    self.blocked[key] = (course, point, retry, allowed, targets, auto_submit)
                self.record(task, status='blocked', execution_state='blocked', reason='章节未开放或等待前置任务')
                for tid in targets or ():
                    record = self.store.task(tid)
                    if record and course_key(record['course']) == course_key(course) and str(record['point']['id']) == str(point['id']):
                        self.record(record, status='blocked', execution_state='blocked', reason='章节未开放，保留历史结果')
                return
            with self.discovery_guard:
                self.blocked.pop(key, None)
            self.record(task, status='done', execution_state='done', reason='任务列表已读取')
            for job in jobs:
                lane_name = {'video': 'video', 'workid': 'answer'}.get(job['type'])
                if lane_name not in self.lanes or (allowed is not None and lane_name not in allowed):
                    continue
                work = {'id': key + ':' + str(job.get('jobid') or job.get('objectid')) + ':' + lane_name,
                        'lane': lane_name, 'course': course, 'point': point, 'job': job, 'info': info,
                        'owner': getattr(self.platform, 'uid', '')}
                with self.lock:
                    old = self.store.task(work['id'])
                    if old and hasattr(self.platform, 'check_owner'):
                        try:
                            self.platform.check_owner(old)
                        except ValueError as exc:
                            self.emit('error', str(exc))
                            continue
                    mode = old.get('auto_submit', self.auto_submit) if old and targets is not None else self.auto_submit if auto_submit is None else auto_submit
                    work['auto_submit'] = mode
                    if limited_targets is not None and work['id'] not in limited_targets:
                        continue
                    if work['id'] in self.excluded or (old and old.get('queue_removed') and (targets is None or work['id'] not in targets)):
                        continue
                    if old and old.get('status') in FINAL | UNCERTAIN:
                        if old['status'] == 'platform_done' and not confirmed_submission(old):
                            self.record(old, status='local_saved', remote_state='local')
                        else:
                            changes = dict(execution_state='done', progress_checked_at=time.time(), queue_removed=False)
                            if confirmed_submission(old):
                                changes.update(status='unknown', remote_state='unknown',
                                               reason='平台仍返回待做任务，与交卷成功记录不一致，请稍后核实')
                            self.record(old, **changes)
                            continue
                    self.record(work, queue_removed=False, progress_checked_at=time.time(), job=job, info=info, auto_submit=mode)
                    lane = self.lanes[lane_name]
                    if lane.cancelled.is_set():
                        self.record(work, status='failed', execution_state='failed', reason=lane.fault_reason or '本轮执行已结束，请重新加入')
                    else:
                        lane.put(work)
            if targets:
                present = {str(j.get('jobid') or j.get('objectid')) for j in jobs}
                for tid in targets:
                    record = self.store.task(tid)
                    if not record or record.get('lane') == 'discovery' or course_key(record['course']) != course_key(course) or str(record['point']['id']) != str(point['id']):
                        continue
                    if tid in self.excluded:
                        continue
                    job_id = str(record['job'].get('jobid') or record['job'].get('objectid'))
                    if job_id not in present:
                        if record['lane'] == 'answer':
                            if job_id in info.get('completed_jobs', []) or confirmed_submission(record):
                                self.complete_submission(record, 'platform_passed')
                            else:
                                self.record(record, status='unknown', remote_state='unknown', execution_state='done',
                                            queue_removed=False, reason='任务列表未找到该测验，交卷结果待确认；本地草稿保留')
                        else:
                            self.record(record, status='done', execution_state='done', queue_removed=False,
                                        progress_checked_at=time.time(), reason='平台已完成，进度已更新')
        except Cancelled:
            raise
        except Exception as exc:
            self.record(task, status='failed', execution_state='failed', reason=str(exc))
            for tid in targets or ():
                record = self.store.task(tid)
                if record and course_key(record['course']) == course_key(course) and str(record['point']['id']) == str(point['id']):
                    self.record(record, status='failed', execution_state='failed', reason='读取平台进度失败：' + str(exc))

    def recheck_blocked(self):
        if not self.discovery_stop.is_set():
            self.refresh_requested.set()

    def poll_recheck(self):
        if not self.refresh_requested.is_set() or (self.discovery and self.discovery.is_alive()):
            return
        self.refresh_requested.clear()
        with self.discovery_guard:
            blocked = [v for v in self.blocked.values() if v[2] < 3]
        if not blocked or self.discovery_stop.is_set():
            return
        def work():
            self.platform.bind(self._discovery_checkpoint)
            try:
                for course, point, retry, allowed, targets, auto_submit in blocked:
                    self._discovery_checkpoint()
                    self._discover(course, point, retry + 1, allowed, targets, auto_submit)
            except Cancelled:
                pass
        self.discovery = threading.Thread(target=work, daemon=True)
        self.discovery.start()

    def run_video(self, task, checkpoint):
        def progress(current, total):
            self.record(task, progress={'current': current, 'total': total})
            self.emit('progress', (task['id'], current, total))
        status = self.platform.video(task, checkpoint, progress)
        if status == 'done':
            self.recheck_blocked()
        return status

    def fresh_task(self, task):
        if hasattr(self.platform, 'check_owner'):
            self.platform.check_owner(task)
        jobs, info = self.platform.jobs(task['course'], task['point'])
        if info.get('notOpen'):
            raise FormConflict('该章节未开放，无法读取或提交')
        job = next((j for j in jobs if j.get('jobid') == task['job'].get('jobid')), None)
        if job is None and task.get('lane') == 'answer':
            if str(task['job']['jobid']) not in info.get('completed_jobs', []) and not confirmed_submission(task):
                raise SubmissionUncertain('任务列表未找到该测验，尚不能确认已交卷；本地草稿保留，请稍后核实')
        return dict(task, job=job, info=info) if job else None

    def refreshed_document(self, record, document):
        old_rows = {r['question']['id']: r for r in record.get('rows', [])}
        rows = []
        for q in document['questions']:
            old = old_rows.get(q['id'])
            if old and fingerprint(old['question']) == fingerprint(q):
                rows.append(copy.deepcopy(old))
            else:
                rows.append(changed_row(old, q, {'error': '题目更新或尚未处理'}))
        return rows

    def archive_document(self, record, document):
        old = record.get('document')
        if old and [fingerprint(q) for q in old['questions']] != [fingerprint(q) for q in document['questions']]:
            history = record.get('document_history', []) + [{'time': time.time(), 'document': old, 'rows': record.get('rows', [])}]
            return self.record(record, document_history=history)
        return record

    def run_answer(self, task, checkpoint):
        self.platform.bind(checkpoint)
        checkpoint()
        old = self.store.task(task['id']) or task
        if old.get('status') in FINAL | UNCERTAIN:
            return old['status']
        try:
            fresh = self.fresh_task(task)
        except SubmissionUncertain as exc:
            self.record(task, remote_state='unknown', reason=str(exc))
            return 'unknown'
        if fresh is None:
            return self.complete_submission(task, 'platform_passed')
        document = self.platform.fetch_work(fresh)
        old = self.archive_document(old, document)
        rows = self.refreshed_document(old, document)
        self.record(task, document=document, rows=rows, job=fresh['job'], info=fresh['info'], submission_error='', reason='')
        for index, q in enumerate(document['questions']):
            checkpoint()
            previous = rows[index]
            try:
                if previous.get('payload') and not previous.get('error'):
                    validate(q, previous['payload'])
                    continue
                result = self.ai.query(q, checkpoint)
            except (LaneFault, Cancelled):
                raise
            except Exception as exc:
                result = {'error': str(exc)}
            rows[index] = changed_row(previous, q, result)
            self.record(task, rows=rows, remote_state='local')
            checkpoint()
        ready = summary({'document': document, 'rows': rows})['ready']
        auto = bool(task.get('auto_submit', self.auto_submit)) and ready and not old.get('require_review')
        if auto:
            return self.submit_work(fresh, document, rows)
        errors = summary({'document': document, 'rows': rows})['errors']
        self.record(task, remote_state='local', reason=f'本地草稿已保存，{errors} 道题需要处理' if errors else '本地草稿已保存，等待审核交卷')
        return 'local_saved'

    def complete_submission(self, task, evidence):
        """Replace the draft with a small receipt in the same durable transaction."""
        with self.lock:
            old = self.store.task(task['id']) or task
            now = time.time()
            receipt = old.get('submission_receipt') or {
                'confirmed_at': now, 'evidence': evidence,
            }
            keep = ('id', 'lane', 'course', 'point', 'owner', 'auto_submit', 'list_hidden', 'progress_checked_at')
            record = {key: copy.deepcopy(old[key]) for key in keep if key in old}
            record.update(job={key: value for key, value in old.get('job', {}).items() if key in ('jobid', 'name')},
                          status='submitted', remote_state='submitted', execution_state='done',
                          submitted_at=old.get('submitted_at') or now, submission_receipt=receipt,
                          revision=old.get('revision', 0) + 1, updated=now,
                          reason='交卷成功，本地草稿已清理')
            self.store.save_task(record)
            self.emit('task', copy.deepcopy(record))
        self.recheck_blocked()
        return 'submitted'

    def submit_work(self, task, document, rows, allow_blank=False):
        self.record(task, status='submitting', remote_state='submitting', submission_error='')
        try:
            if allow_blank:
                self.platform.write_work(task, document, rows, submit=True, allow_blank=True)
            else:
                self.platform.write_work(task, document, rows, submit=True)
        except Cancelled:
            self.record(task, status='local_saved', remote_state='local')
            raise
        except (FormConflict, SubmissionRejected) as exc:
            self.record(task, status='local_saved', remote_state='local', submission_error=str(exc), reason=str(exc))
            return 'local_saved'
        except SubmissionUncertain as exc:
            self.record(task, status='unknown', remote_state='unknown', reason='交卷结果待确认：' + str(exc))
            return 'unknown'
        except Exception as exc:
            self.record(task, status='local_saved', remote_state='local', submission_error=str(exc), reason=str(exc))
            return 'local_saved'
        return self.complete_submission(task, 'submit_response')

    def _editable(self, record):
        if not record or record.get('lane') != 'answer':
            raise ValueError('请选择一份测验')
        if record.get('status') in FINAL:
            raise ValueError('该测验已交卷或平台已完成')
        if record.get('status') in UNCERTAIN:
            raise ValueError('该测验交卷结果待确认，请先读取平台状态')

    def edit_answer(self, task_id, question_id, payload):
        mutex = self.mutex(task_id)
        if not mutex.acquire(blocking=False):
            raise ValueError('该测验正在处理，请稍后编辑')
        try:
            if task_id in self.manual:
                raise ValueError('该测验正在批量操作，请稍后编辑')
            record = self.store.task(task_id)
            self._editable(record)
            rows = rows_for(record)
            index = next(i for i, r in enumerate(rows) if r['question']['id'] == question_id)
            value = validate(rows[index]['question'], payload)
            rows[index] = changed_row(rows[index], rows[index]['question'], {'payload': payload, 'answer': value, 'source': '人工修正', 'error': ''})
            self.record(record, rows=rows, status='local_saved', require_review=True,
                        remote_state='local', submission_error='', reason='修改已自动保存到本地草稿')
        finally:
            mutex.release()

    def batch(self, action, task_ids, question_ids=None, allow_blank=False):
        if not self.connected:
            raise ValueError('尚未登录，请先登录后进行平台操作')
        if action not in ('verify', 'submit', 'redo'):
            raise ValueError('未知批量操作')
        ids = list(dict.fromkeys(task_ids))
        if not ids:
            raise ValueError('请先勾选测验或题目')
        batch_id = uuid.uuid4().hex
        result = {'id': batch_id, 'action': action, 'time': time.time(), 'items': [], 'done': False}
        reserved = []
        with self.lock:
            for tid in ids:
                if tid in self.manual:
                    result['items'].append({'task_id': tid, 'result': 'skipped', 'reason': '这份测验已有批量操作'})
                else:
                    self.manual.add(tid)
                    self.manual_cancel[tid] = threading.Event()
                    self.manual_action[tid] = action
                    reserved.append(tid)
            self.batch_threads.add(batch_id)
        self.emit('batch', copy.deepcopy(result))
        self.configure()
        def work():
            api_fault = None
            try:
                for tid in reserved:
                    item = {'task_id': tid, 'result': 'skipped', 'reason': ''}
                    mutex = self.mutex(tid)
                    acquired = mutex.acquire(blocking=False)
                    try:
                        if not acquired:
                            raise ValueError('该测验正在执行，已跳过；可在任务页取消后处理')
                        def checkpoint():
                            if self.manual_cancel[tid].is_set():
                                raise TaskRemoved('选中的批量任务已停止，已有结果保留')
                        checkpoint()
                        self.platform.bind(checkpoint)
                        record = self.store.task(tid)
                        if not record or record.get('lane') != 'answer':
                            raise ValueError('不是测验任务，已跳过')
                        item['label'] = record.get('course', {}).get('title', '') + ' / ' + record.get('point', {}).get('title', '')
                        if action == 'verify':
                            status = self._verify(record)
                        else:
                            self._editable(record)
                            if action == 'redo':
                                if api_fault:
                                    raise ValueError('本轮 AI 操作已结束：' + api_fault)
                                selected = (question_ids or {}).get(tid, [])
                                if not selected:
                                    raise ValueError('这份测验没有选中的重做题目')
                                status = self._redo(record, set(selected), checkpoint)
                            else:
                                if action == 'submit' and not can_submit(record, allow_blank):
                                    raise ValueError('这份测验仍有异常题或未答题，已跳过')
                                fresh = self.fresh_task(record)
                                if fresh is None:
                                    status = self.complete_submission(record, 'platform_passed')
                                else:
                                    status = self.submit_work(fresh, record['document'], rows_for(record), allow_blank)
                                    self.record(record, status=status)
                        final = self.store.task(tid)
                        item.update(result='success' if status in ('submitted', 'review') else 'failed',
                                    reason=final.get('reason', ''), status=status)
                        if action == 'redo' and status == 'review' and any(row['error'] for row in rows_for(final) if row['question']['id'] in selected):
                            item['result'] = 'partial'
                    except (Cancelled, TaskRemoved) as exc:
                        self.record({'id': tid}, status='removed', execution_state='removed', reason=str(exc))
                        item.update(result='cancelled', reason=str(exc))
                    except LaneFault as exc:
                        api_fault = str(exc)
                        self.api_fault('answer', api_fault)
                        self.record({'id': tid}, reason='本轮 AI 操作已结束：' + api_fault)
                        item.update(result='failed', reason=api_fault)
                    except SubmissionUncertain as exc:
                        self.record({'id': tid}, status='unknown', remote_state='unknown', execution_state='done', reason=str(exc))
                        item.update(result='failed', reason=str(exc), status='unknown')
                    except Exception as exc:
                        if action == 'submit' and acquired:
                            current = self.store.task(tid)
                            if current and current.get('status') not in FINAL | UNCERTAIN:
                                self.record(current, submission_error=str(exc), reason=str(exc))
                        item.update(result='failed' if acquired else 'skipped', reason=str(exc))
                    finally:
                        if acquired:
                            mutex.release()
                        with self.lock:
                            self.manual.discard(tid)
                            self.manual_cancel.pop(tid, None)
                            self.manual_action.pop(tid, None)
                    result['items'].append(item)
                    self.emit('batch', copy.deepcopy(result))
            finally:
                with self.lock:
                    result['done'] = True
                    batches = self.store.get('batch_history') or []
                    self.store.set('batch_history', (batches + [result])[-30:])
                    self.emit('batch', copy.deepcopy(result))
                    self.batch_threads.discard(batch_id)
        threading.Thread(target=work, name='batch-' + action, daemon=True).start()
        return batch_id

    def _verify(self, record):
        fresh = self.fresh_task(record)
        if fresh is None:
            return self.complete_submission(record, 'platform_passed')
        if record.get('submission_receipt') or confirmed_submission(record):
            self.record(record, status='unknown', remote_state='unknown', execution_state='done',
                        reason='平台仍返回待做任务，与交卷成功记录不一致，请稍后核实')
            return 'unknown'
        document = self.platform.fetch_work(fresh)
        record = self.archive_document(record, document)
        same = [fingerprint(q) for q in record.get('document', {}).get('questions', [])] == [fingerprint(q) for q in document['questions']]
        uncertain = record.get('status') in UNCERTAIN and str(record['job']['jobid']) not in fresh['info'].get('pending_jobs', [])
        changes = dict(status='unknown' if uncertain else 'local_saved', platform_state='incomplete',
                       remote_state='unknown' if uncertain else 'local', execution_state='done',
                       job=fresh['job'], info=fresh['info'], submission_error='',
                       reason='平台仍返回任务，但未取得明确交卷结果，请稍后核实' if uncertain else '已读取平台状态，测验尚未交卷；本地草稿保留')
        if not same:
            changes.update(document=document, rows=self.refreshed_document(record, document), require_review=True,
                           reason=changes['reason'] if uncertain else '平台题目有更新，请处理变化题目；原结果保留在本地')
        self.record(record, **changes)
        return 'unknown' if uncertain else 'review'

    def _redo(self, record, selected, checkpoint):
        fresh = self.fresh_task(record)
        if fresh is None:
            return self.complete_submission(record, 'platform_passed')
        document = self.platform.fetch_work(fresh)
        record = self.archive_document(record, document)
        rows = self.refreshed_document(record, document)
        available = {q['id'] for q in document['questions']}
        if not selected <= available:
            raise ValueError('选中题目的编号已变化，请重新核验后选择题目')
        self.record(record, document=document, rows=rows, status='review', require_review=True, remote_state='local', submission_error='')
        failures = 0
        for index, row in enumerate(rows):
            if row['question']['id'] not in selected:
                continue
            checkpoint()
            try:
                answer = self.ai.query(row['question'], checkpoint, force=True)
            except Cancelled:
                raise
            except LaneFault:
                raise
            except Exception as exc:
                answer = {'error': str(exc)}
                failures += 1
            rows[index] = changed_row(row, row['question'], answer)
            self.record(record, rows=rows, status='review', require_review=True, remote_state='local',
                        reason='AI 重做结果已本地保留，等待审核；未自动交卷')
            checkpoint()
        self.record(record, reason=f'AI 重做 {len(selected)} 题，{failures} 题仍需处理；已保存到本地草稿')
        return 'review'

    def approve(self, task_id):
        record = self.store.task(task_id)
        self._editable(record)
        if not can_submit(record):
            raise ValueError('该测验仍有异常题；请修改、重做或明确选择含空题交卷')
        return self.batch('submit', [task_id])

    def verify(self, task_id):
        return self.batch('verify', [task_id])

    def delete_list_items(self, view, task_ids, question_ids=None):
        """Remove selected list entries while retaining full forms and saved answers."""
        if view not in ('tasks', 'papers', 'questions'):
            raise ValueError('未知列表')
        ids = list(dict.fromkeys(task_ids))
        if view == 'tasks':
            self.remove_tasks(ids)
        changed = []
        with self.lock:
            for tid in ids:
                record = self.store.task(tid)
                if not record:
                    continue
                hidden = copy.deepcopy(record.get('list_hidden', {}))
                if view == 'questions':
                    valid = {r['question']['id'] for r in rows_for(record)}
                    selected = set((question_ids or {}).get(tid, ())) & valid
                    if not selected:
                        continue
                    hidden[view] = list(dict.fromkeys([*hidden.get(view, []), *sorted(selected)]))
                else:
                    hidden[view] = True
                changed.append(self.record(record, list_hidden=hidden))
        return changed

    def remove_tasks(self, task_ids):
        for tid in dict.fromkeys(task_ids):
            record = self.store.task(tid)
            if not record:
                continue
            with self.lock:
                self.excluded.add(tid)
                event = self.manual_cancel.get(tid)
                if event:
                    event.set()
            running = False
            for lane in self.lanes.values():
                if lane.remove(tid):
                    running = lane.states.get(tid) in ('running', 'removing') or running
            if record.get('status') not in FINAL | UNCERTAIN:
                self.record(record, status='removing' if running or event else 'removed',
                            queue_removed=True, previous_execution_state=record.get('execution_state', record.get('status')),
                            execution_state='removing' if running or event else 'removed',
                            reason='正在取消，等待当前请求结束' if running or event else '任务已取消，题目和答案保留')
            else:
                self.record(record, queue_removed=True)

    def busy(self):
        if (self.discovery and self.discovery.is_alive()) or any(t.is_alive() for t in list(self.discovery_threads)):
            return True
        for lane in list(self.lanes.values()):
            with lane.lock:
                if lane.pending:
                    return True
        return False

    def stop(self):
        self.discovery_stop.set()
        for lane in self.lanes.values():
            lane.stop()
        with self.lock:
            for event in self.manual_cancel.values():
                event.set()
