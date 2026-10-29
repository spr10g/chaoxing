import copy
import json
from pathlib import Path
import threading
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_desktop as fixtures
from test_desktop import FakePlatform, question, until
from desktop.backend import Platform, course_key
from desktop.review import rows_for, summary, can_submit, remote_label
from desktop.ui import App
from desktop.answers import AnswerService


class BatchWorkflowTests(unittest.TestCase):
    setUp = fixtures.DesktopTests.setUp
    tearDown = fixtures.DesktopTests.tearDown
    engine = fixtures.DesktopTests.engine
    selections = fixtures.DesktopTests.selections
    success = fixtures.DesktopTests.success
    def paper(self, engine, name='p1', bad=False, status='draft', course='course'):
        q1, q2 = question('q1'), question('q2', '另外一道题')
        rows = [{'question': q1, **self.success(q1, lambda: None)},
                {'question': q2, 'error': '模拟失败', 'answer': '', 'source': ''} if bad else
                {'question': q2, **self.success(q2, lambda: None)}]
        record = {'id': name, 'lane': 'answer', 'status': status, 'remote_state': 'draft',
                  'course': {'courseId': course, 'clazzId': 'class', 'cpi': '1', 'title': course},
                  'point': {'id': name, 'title': name}, 'job': {'jobid': 'work-' + name},
                  'info': {}, 'document': {'questions': [q1, q2]}, 'rows': rows}
        engine.platform.documents[name] = copy.deepcopy(record['document'])
        self.store.save_task(record)
        return record

    def finish_batch(self, engine):
        until(lambda: not engine.batch_threads)
        return self.store.get('batch_history')[-1]

    def test_multicourse_dispatch_runs_both_courses(self):
        engine = self.engine()
        engine.ai.query = self.success
        a = self.selections(2)
        b = self.selections(2)
        b[0][0]['courseId'] = 'second'
        engine.start(a + b)
        until(lambda: len(engine.platform.writes) == 4)
        self.assertEqual(len({tid.split(':')[0] for tid, _, _ in engine.platform.writes}), 2)

    def test_review_other_paper_while_video_running(self):
        engine = self.engine()
        engine.platform.video_gate.clear()
        self.store.save_settings({'answer': False})
        engine.start(self.selections(1))
        until(lambda: engine.lanes['video'].counts().get('running') == 1)
        self.paper(engine, 'unrelated')
        engine.batch('submit', ['unrelated'])
        result = self.finish_batch(engine)
        self.assertEqual(result['items'][0]['result'], 'success')
        self.assertTrue(engine.busy())
        self.assertEqual(self.store.task('unrelated')['status'], 'submitted')
        engine.platform.video_gate.set()

    def test_batch_submits_clean_papers_skips_error_without_blocking_others(self):
        engine = self.engine()
        self.paper(engine, 'a', course='one')
        self.paper(engine, 'b', bad=True, course='two')
        self.paper(engine, 'c', course='three')
        engine.batch('submit', ['a', 'b', 'c'])
        result = self.finish_batch(engine)
        self.assertEqual([item['result'] for item in result['items']], ['success', 'failed', 'success'])
        self.assertEqual([tid for tid, submit, rows in engine.platform.writes if submit], ['a', 'c'])
        self.assertEqual(self.store.task('b')['status'], 'draft')

    def test_save_normal_answers_even_when_paper_contains_error(self):
        engine = self.engine()
        self.paper(engine, 'a', bad=True)
        engine.batch('save', ['a'])
        self.finish_batch(engine)
        self.assertFalse(engine.platform.writes[0][1])
        self.assertEqual(self.store.task('a')['remote_state'], 'draft')
        self.assertEqual(summary(self.store.task('a'))['errors'], 1)

    def test_partial_final_submit_requires_explicit_flag_and_sends_blank(self):
        platform = Platform(self.store)
        q1, q2 = question('one'), question('two')
        document = {'questions': [q1, q2], 'token': 'fresh'}
        platform.fetch_work = lambda task: copy.deepcopy(document)
        posts = []
        class Session:
            def post(self, url, **kwargs):
                posts.append(kwargs['data'])
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'status': True})
        platform.SessionManager = SimpleNamespace(get_session=lambda: Session())
        rows = [{'question': q1, **self.success(q1, lambda: None)}, {'question': q2, 'error': 'bad', 'payload': {'answers': ['A']}}]
        with self.assertRaises(ValueError): platform.write_work({}, document, rows, submit=True)
        self.assertEqual(posts, [])
        platform.write_work({}, document, rows, submit=True, allow_blank=True)
        self.assertEqual(posts[0]['answerone'], 'B')
        self.assertEqual(posts[0]['answertwo'], '')
        self.assertEqual(posts[0]['pyFlag'], '')

    def test_force_redo_selected_error_bypasses_cache_keeps_other_answer_and_history(self):
        engine = self.engine()
        original = self.paper(engine, 'a', bad=True)
        calls = []
        def redo(q, checkpoint, force=False):
            calls.append((q['id'], force))
            return self.success(q, checkpoint)
        engine.ai.query = redo
        engine.batch('redo', ['a'], {'a': ['q2']})
        self.finish_batch(engine)
        record = self.store.task('a')
        self.assertEqual(calls, [('q2', True)])
        self.assertEqual(record['rows'][0], original['rows'][0])
        self.assertEqual(record['rows'][1]['history'][-1]['error'], '模拟失败')
        self.assertTrue(record['require_review'])
        self.assertTrue(summary(record)['ready'])
        self.assertEqual(record['remote_state'], 'draft_old')
        self.assertEqual(engine.platform.writes, [])

    def test_remove_running_video_keeps_other_video_running_and_suppresses_rediscovery(self):
        engine = self.engine()
        self.store.save_settings({'answer': False, 'video_workers': 1})
        engine.platform.video_gate.clear()
        engine.start(self.selections(2))
        until(lambda: engine.lanes['video'].counts().get('running') == 1)
        tid = next(r['id'] for r in self.store.tasks() if r.get('lane') == 'video' and r.get('status') == 'running')
        record = self.store.task(tid)
        engine.remove_tasks([tid])
        until(lambda: self.store.task(tid)['status'] == 'removed')
        engine.platform.bind(lambda: None)
        engine._discover(record['course'], record['point'])
        self.assertEqual(self.store.task(tid)['status'], 'removed')
        engine.platform.video_gate.set()
        until(lambda: len(engine.platform.videos) == 1)
        self.assertNotIn(record['point']['id'], engine.platform.videos)

    def test_removing_paused_queued_task_does_not_need_resume(self):
        engine = self.engine()
        self.paper(engine, 'a')
        engine.remove_tasks(['a'])
        self.assertEqual(self.store.task('a')['status'], 'removed')
        self.assertEqual(len(self.store.task('a')['rows']), 2)

    def test_verify_preserves_platform_draft_evidence(self):
        engine = self.engine()
        self.paper(engine, 'a', bad=True)
        engine.verify('a')
        self.finish_batch(engine)
        record = self.store.task('a')
        self.assertEqual(record['remote_state'], 'draft')
        self.assertIn('平台草稿已保存', remote_label(record))
        self.assertIn('任务未完成', remote_label(record))
        self.assertEqual(engine.platform.writes, [])

    def test_missing_questions_are_shown_and_block_default_final_submission(self):
        engine = self.engine()
        record = self.paper(engine, 'a')
        record['rows'].pop()
        self.store.save_task(record)
        self.assertEqual(len(rows_for(record)), 2)
        self.assertEqual(summary(record)['errors'], 1)
        self.assertFalse(can_submit(record))
        self.assertTrue(can_submit(record, allow_blank=True))

    def test_selected_task_lock_rejects_only_that_task(self):
        engine = self.engine()
        self.paper(engine, 'a')
        self.paper(engine, 'b')
        engine.mutex('a').acquire()
        try:
            engine.batch('verify', ['a', 'b'])
            result = self.finish_batch(engine)
            self.assertEqual(result['items'][0]['result'], 'skipped')
            self.assertNotIn('登录', result['items'][0]['reason'])
            self.assertEqual(result['items'][1]['result'], 'success')
        finally: engine.mutex('a').release()

    def test_unrelated_task_does_not_block_local_edit(self):
        engine = self.engine()
        self.paper(engine, 'a')
        engine.manual.add('unrelated')
        engine.edit_answer('a', 'q1', {'answers': ['A']})
        self.assertEqual(self.store.task('a')['rows'][0]['answer'], 'A')
        engine.manual.clear()

    def test_multicourse_ui_resolves_independent_chapter_selections(self):
        root = tk.Tk()
        app = App(root, self.store)
        try:
            c1 = {'courseId': 'one', 'clazzId': 'class', 'cpi': '1', 'title': 'one'}
            c2 = dict(c1, courseId='two', title='two')
            app.courses = [c1, c2]
            app.engine.connected = True
            app.engine.chapters = lambda course: [{'id': '1', 'title': '1'}, {'id': '2', 'title': '2'}]
            captured = []
            app.engine.start = lambda plan: captured.extend(plan)
            app.choose_courses(True)
            app.all_selected_chapters()
            app.start()
            deadline = time.monotonic() + 2
            while not captured and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            self.assertEqual(len(captured), 2)
            self.assertEqual([len(points) for _, points in captured], [2, 2])
            self.assertEqual(set(self.store.settings['selected_courses']), {course_key(c1), course_key(c2)})
        finally: app.close()

    def test_review_ui_filters_and_batches_across_chapters(self):
        engine = self.engine()
        self.paper(engine, 'a', course='one')
        self.paper(engine, 'b', bad=True, course='two')
        root = tk.Tk(); app = App(root, self.store)
        try:
            self.assertEqual(len(app.review_tree.get_children()), 2)
            self.assertEqual(len(app.question_tree.get_children()), 4)
            app.question_filter.set('需处理'); app.render_review()
            self.assertEqual(len(app.question_tree.get_children()), 1)
            called = []
            app.engine.batch = lambda action, ids, *args: called.append((action, list(ids), args))
            app.submit_all_ready()
            self.assertEqual(called[-1][1], ['a'])
            app.redo_errors()
            self.assertEqual(called[-1][1], ['b'])
            self.assertEqual(called[-1][2][0], {'b': ['q2']})
        finally: app.close()

    def test_force_query_really_ignores_valid_cache(self):
        import httpx
        service = AnswerService(self.store, dict(self.store.settings, key='fixture', min_interval=0))
        calls = []
        def respond(request):
            calls.append(request)
            answer = 'A' if len(calls) == 1 else 'B'
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': [answer]})}}]})
        client = httpx.Client(transport=httpx.MockTransport(respond), base_url='https://example.test/')
        service._client = lambda *args: client
        q = question()
        self.assertEqual(service.query(q, lambda: None)['answer'], 'A')
        self.assertEqual(service.query(q, lambda: None)['answer'], 'A')
        self.assertEqual(service.query(q, lambda: None, force=True)['answer'], 'B')
        self.assertEqual(len(calls), 2)
        client.close()

    def test_manual_and_automatic_ai_calls_share_concurrency_cap(self):
        import httpx
        service = AnswerService(self.store, dict(self.store.settings, key='fixture', min_interval=0, answer_workers=2))
        lock = threading.Lock()
        active, peak = 0, 0
        errors = []
        def respond(request):
            nonlocal active, peak
            with lock: active += 1; peak = max(peak, active)
            time.sleep(.04)
            with lock: active -= 1
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answers":["B"]}'}}]})
        client = httpx.Client(transport=httpx.MockTransport(respond), base_url='https://example.test/')
        service._client = lambda *args: client
        def invoke(i):
            try: service.query(question(title=str(i)), lambda: None, force=bool(i % 2))
            except Exception as exc: errors.append(type(exc).__name__)
        threads = [threading.Thread(target=invoke, args=(i,)) for i in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(peak, 2)
        self.assertFalse(any(t.is_alive() for t in threads))
        client.close()

    def test_server_rejection_does_not_become_uncertain_submit(self):
        from desktop.review import SubmissionRejected
        engine = self.engine()
        self.paper(engine, 'a')
        def reject(*args, **kwargs): raise SubmissionRejected('平台不允许留空题交卷')
        engine.platform.write_work = reject
        engine.batch('submit', ['a'])
        self.finish_batch(engine)
        self.assertNotIn(self.store.task('a')['status'], ('unknown', 'submitting', 'submitted'))
        self.assertEqual(self.store.task('a')['remote_state'], 'draft')

    def test_manual_partial_submit_flag_is_not_implicitly_enabled(self):
        engine = self.engine()
        self.paper(engine, 'a', bad=True)
        flags = []
        def write(task, doc, rows, submit=False, allow_blank=False):
            flags.append((submit, allow_blank))
        engine.platform.write_work = write
        engine.batch('submit', ['a'])
        self.finish_batch(engine)
        self.assertEqual(flags, [])
        engine.batch('submit', ['a'], allow_blank=True)
        self.finish_batch(engine)
        self.assertEqual(flags, [(True, True)])
        self.assertEqual(self.store.task('a')['status'], 'submitted')

    def test_removing_submitted_record_keeps_final_state_and_history(self):
        engine = self.engine()
        original = self.paper(engine, 'a', status='submitted')
        original['remote_state'] = 'submitted'
        self.store.save_task(original)
        engine.remove_tasks(['a'])
        record = self.store.task('a')
        self.assertEqual(record['status'], 'submitted')
        self.assertEqual(record['remote_state'], 'submitted')
        self.assertTrue(record['queue_removed'])
        self.assertEqual(record['rows'], original['rows'])
