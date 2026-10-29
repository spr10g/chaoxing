import copy
from contextlib import contextmanager
import threading
import time
import uuid

from .answers import AnswerService, fingerprint, validate
from .review import (rows_for, summary, can_submit, changed_row, dirty_remote, remote_state,
                     FINAL, UNCERTAIN, FormConflict, SubmissionRejected)
from .scheduler import Lane, LaneFault, Cancelled, TaskRemoved


def course_key(course):
    return ':'.join(str(course.get(key, '')) for key in ('courseId', 'clazzId', 'cpi'))


class Engine:
    def __init__(self, store, emit=lambda *args: None, platform=None):
        self.store, self.emit, self.platform = store, emit, platform
        self.ai = AnswerService(store, store.settings)
        self.lanes = {}
        self.discovery = None
        self.discovery_stop = threading.Event()
        self.lock = threading.RLock()
        self.discovery_guard = threading.RLock()
        self.refresh_requested = threading.Event()
        self.blocked = {}
        self.excluded = set()
        self.manual = set()
        self.manual_cancel = {}
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
        self.record(task, status=status, execution_state=status, reason=reason)
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
                self.lanes[key] = Lane(key, int(s[key + '_workers']), handler, self.state, self.task_guard)
        self.discovery = threading.Thread(target=self._discover_all, args=(copy.deepcopy(selections),), daemon=True)
        self.discovery.start()

    def _discovery_checkpoint(self):
        if self.discovery_stop.is_set():
            raise Cancelled('章节扫描已停止')

    def _discover_all(self, selections):
        try:
            self.platform.bind(self._discovery_checkpoint)
            for course, points in selections:
                for point in points:
                    self._discovery_checkpoint()
                    self._discover(course, point)
        except Cancelled:
            pass
        except Exception as exc:
            self.emit('error', '发现任务失败：' + str(exc))
        finally:
            self.emit('discovery_done', None)

    def _discover(self, course, point, retry=0):
        key = course_key(course) + ':' + str(point['id'])
        task = {'id': key + ':discovery', 'lane': 'discovery', 'course': course, 'point': point}
        if task['id'] in self.excluded:
            return
        try:
            jobs, info = self.platform.jobs(course, point)
            if task['id'] in self.excluded:
                return
            if info.get('notOpen'):
                with self.discovery_guard:
                    self.blocked[key] = (course, point, retry)
                self.record(task, status='blocked', reason='章节未开放或等待前置任务')
                return
            with self.discovery_guard:
                self.blocked.pop(key, None)
            self.record(task, status='done', reason='任务列表已读取')
            for job in jobs:
                lane_name = {'video': 'video', 'workid': 'answer'}.get(job['type'])
                if lane_name not in self.lanes:
                    continue
                work = {'id': key + ':' + str(job.get('jobid') or job.get('objectid')) + ':' + lane_name,
                        'lane': lane_name, 'course': course, 'point': point, 'job': job, 'info': info,
                        'owner': getattr(self.platform, 'uid', '')}
                with self.lock:
                    old = self.store.task(work['id'])
                    if work['id'] in self.excluded:
                        continue
                    if old and old.get('status') in FINAL | UNCERTAIN:
                        self.emit('task', old)
                        continue
                    self.record(work, queue_removed=False)
                    self.lanes[lane_name].put(work)
        except Cancelled:
            raise
        except Exception as exc:
            self.record(task, status='failed', reason=str(exc))

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
                for course, point, retry in blocked:
                    self._discovery_checkpoint()
                    self._discover(course, point, retry + 1)
            except Cancelled:
                pass
        self.discovery = threading.Thread(target=work, daemon=True)
        self.discovery.start()

    def run_video(self, task, checkpoint):
        status = self.platform.video(task, checkpoint, lambda current, total: self.emit('progress', (task['id'], current, total)))
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
        fresh = self.fresh_task(task)
        if fresh is None:
            self.record(task, remote_state='completed')
            return 'platform_done'
        document = self.platform.fetch_work(fresh)
        old = self.archive_document(old, document)
        rows = self.refreshed_document(old, document)
        self.record(task, document=document, rows=rows, job=fresh['job'], info=fresh['info'], reason='')
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
            self.record(task, rows=rows, remote_state=dirty_remote(old))
            checkpoint()
        ready = summary({'document': document, 'rows': rows})['ready']
        auto = self.auto_submit and ready and not old.get('require_review')
        return self.save_or_submit(fresh, document, rows, auto)

    def save_or_submit(self, task, document, rows, submit, allow_blank=False):
        old = self.store.task(task['id']) or task
        if submit:
            self.record(task, status='submitting', remote_state='submitting')
        try:
            if allow_blank:
                self.platform.write_work(task, document, rows, submit=submit, allow_blank=True)
            else:
                self.platform.write_work(task, document, rows, submit=submit)
        except Cancelled:
            if submit:
                self.record(task, remote_state=remote_state(old))
            raise
        except (FormConflict, SubmissionRejected) as exc:
            self.record(task, remote_state=remote_state(old) if submit else 'draft_failed', reason=str(exc))
            return 'local_saved'
        except Exception as exc:
            self.record(task, remote_state='unknown' if submit else 'draft_failed',
                        reason=('交卷结果需核验：' if submit else '平台草稿同步失败，本地结果保留：') + str(exc))
            return 'unknown' if submit else 'local_saved'
        if submit:
            self.record(task, remote_state='submitted', submitted_at=time.time(), require_review=False,
                        reason='整份测验已交卷' + ('（含留空题）' if allow_blank else ''))
            self.recheck_blocked()
            return 'submitted'
        self.record(task, remote_state='draft', draft_saved_at=time.time(),
                    reason='正常答案已同步平台草稿；' + ('仍有需处理题目' if summary({'document': document, 'rows': rows})['errors'] else '等待审核交卷'))
        return 'draft'

    def _editable(self, record):
        if not record or record.get('lane') != 'answer':
            raise ValueError('请选择答题章节')
        if record.get('status') in FINAL:
            raise ValueError('该章节已交卷或平台已完成，不能修改或重复交卷')
        if record.get('status') in UNCERTAIN:
            raise ValueError('该章节交卷结果尚不明确，请先核验这个章节')

    def edit_answer(self, task_id, question_id, payload):
        mutex = self.mutex(task_id)
        if not mutex.acquire(blocking=False):
            raise ValueError('该章节正在处理，请稍后编辑；其他章节不受影响')
        try:
            if task_id in self.manual:
                raise ValueError('该章节正在批量操作，请稍后编辑')
            record = self.store.task(task_id)
            self._editable(record)
            rows = rows_for(record)
            index = next(i for i, r in enumerate(rows) if r['question']['id'] == question_id)
            value = validate(rows[index]['question'], payload)
            rows[index] = changed_row(rows[index], rows[index]['question'], {'payload': payload, 'answer': value, 'source': '人工修正', 'error': ''})
            self.record(record, rows=rows, status='local_saved', require_review=True,
                        remote_state=dirty_remote(record), reason='人工修正已保留，尚未同步平台')
        finally:
            mutex.release()

    def batch(self, action, task_ids, question_ids=None, allow_blank=False):
        if not self.connected:
            raise ValueError('尚未登录，请先登录后进行平台操作')
        if action not in ('verify', 'save', 'submit', 'redo'):
            raise ValueError('未知批量操作')
        ids = list(dict.fromkeys(task_ids))
        if not ids:
            raise ValueError('请先选择章节或题目')
        batch_id = uuid.uuid4().hex
        result = {'id': batch_id, 'action': action, 'time': time.time(), 'items': [], 'done': False}
        reserved = []
        with self.lock:
            for tid in ids:
                if tid in self.manual:
                    result['items'].append({'task_id': tid, 'result': 'skipped', 'reason': '这个章节已有批量操作'})
                else:
                    self.manual.add(tid)
                    self.manual_cancel[tid] = threading.Event()
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
                            raise ValueError('该章节正在执行；可等待或只移出该任务，其他章节继续')
                        def checkpoint():
                            if self.manual_cancel[tid].is_set():
                                raise TaskRemoved('选中的批量任务已停止，已有结果保留')
                        checkpoint()
                        self.platform.bind(checkpoint)
                        record = self.store.task(tid)
                        if not record or record.get('lane') != 'answer':
                            raise ValueError('不是答题章节，已跳过')
                        item['label'] = record.get('course', {}).get('title', '') + ' / ' + record.get('point', {}).get('title', '')
                        if action == 'verify':
                            status = self._verify(record)
                        else:
                            self._editable(record)
                            if action == 'redo':
                                if api_fault:
                                    raise ValueError('API 已暂停，未再次调用：' + api_fault)
                                selected = (question_ids or {}).get(tid, [])
                                if not selected:
                                    raise ValueError('本章节没有选中的重做题目')
                                status = self._redo(record, set(selected), checkpoint)
                            else:
                                if action == 'submit' and not can_submit(record, allow_blank):
                                    raise ValueError('本章节仍有异常题或没有可用答案；默认只提交完整正常的章节')
                                fresh = self.fresh_task(record)
                                if fresh is None:
                                    self.record(record, status='platform_done', remote_state='completed', reason='平台显示任务已完成，无需重交')
                                    status = 'platform_done'
                                else:
                                    status = self.save_or_submit(fresh, record['document'], rows_for(record), action == 'submit', allow_blank and action == 'submit')
                                    self.record(record, status=status)
                        final = self.store.task(tid)
                        item.update(result='success' if status in ('draft', 'submitted', 'platform_done', 'review') else 'failed', reason=final.get('reason', ''), status=status)
                        if action == 'redo' and status == 'review' and any(row['error'] for row in rows_for(final) if row['question']['id'] in selected):
                            item['result'] = 'partial'
                    except (Cancelled, TaskRemoved) as exc:
                        self.record({'id': tid}, status='removed', execution_state='removed', reason=str(exc))
                        item.update(result='cancelled', reason=str(exc))
                    except LaneFault as exc:
                        api_fault = str(exc)
                        if 'answer' in self.lanes:
                            self.lanes['answer'].pause()
                        self.record({'id': tid}, reason='API 整体故障，答题暂停：' + api_fault)
                        item.update(result='failed', reason=api_fault)
                    except Exception as exc:
                        item.update(result='failed' if acquired else 'skipped', reason=str(exc))
                    finally:
                        if acquired:
                            mutex.release()
                        with self.lock:
                            self.manual.discard(tid)
                            self.manual_cancel.pop(tid, None)
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
            self.record(record, status='platform_done', remote_state='completed', reason='平台任务列表显示任务已完成')
            return 'platform_done'
        document = self.platform.fetch_work(fresh)
        record = self.archive_document(record, document)
        same = [fingerprint(q) for q in record.get('document', {}).get('questions', [])] == [fingerprint(q) for q in document['questions']]
        state = remote_state(record)
        status = record.get('status', 'local_saved')
        if status in UNCERTAIN:
            status, state = 'local_saved', 'unknown_draft'
        if status in ('interrupted', 'removed', 'failed'):
            status = 'local_saved'
        changes = dict(status=status, platform_state='incomplete', remote_state=state,
                       job=fresh['job'], info=fresh['info'], reason='平台任务仍未完成；本次只核验，未保存或交卷')
        if not same:
            changes.update(status='local_saved', document=document, rows=self.refreshed_document(record, document),
                           remote_state=dirty_remote(record), require_review=True, reason='平台题目有更新，请处理变化题目；原结果保留在历史中')
        self.record(record, **changes)
        return 'review'

    def _redo(self, record, selected, checkpoint):
        fresh = self.fresh_task(record)
        if fresh is None:
            self.record(record, status='platform_done', remote_state='completed', reason='平台任务已完成，未再调用 AI')
            return 'platform_done'
        document = self.platform.fetch_work(fresh)
        record = self.archive_document(record, document)
        rows = self.refreshed_document(record, document)
        available = {q['id'] for q in document['questions']}
        if not selected <= available:
            raise ValueError('选中题目的编号已变化，请重新核验后选择题目')
        self.record(record, document=document, rows=rows, status='review', require_review=True, remote_state=dirty_remote(record))
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
            self.record(record, rows=rows, status='review', require_review=True, remote_state=dirty_remote(record),
                        reason='AI 重做结果已本地保留，等待审核；未自动交卷')
            checkpoint()
        self.record(record, reason=f'AI 重做 {len(selected)} 题，{failures} 题仍需处理；结果待审核，未自动保存或交卷')
        return 'review'

    def approve(self, task_id):
        record = self.store.task(task_id)
        self._editable(record)
        if not can_submit(record):
            raise ValueError('该章节仍有异常题；请保存正常题、重做或明确选择含空题交卷')
        return self.batch('submit', [task_id])

    def verify(self, task_id):
        return self.batch('verify', [task_id])

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
                            reason='已请求停止选中任务；正在结束当前请求' if running or event else '已移出当前队列，题目和答案保留')
            else:
                self.record(record, queue_removed=True)

    def busy(self):
        if self.discovery and self.discovery.is_alive():
            return True
        return any(any(state in ('queued', 'running', 'removing', 'paused') for state in lane.states.values())
                   for lane in self.lanes.values() if not lane.cancelled.is_set())

    def stop(self):
        self.discovery_stop.set()
        for lane in self.lanes.values():
            lane.stop()
        with self.lock:
            for event in self.manual_cancel.values():
                event.set()
