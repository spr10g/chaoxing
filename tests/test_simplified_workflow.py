import copy
import threading
import tkinter as tk
import unittest

import test_desktop as fixtures
from test_desktop import FakePlatform, question, until
from desktop.ui import App
from desktop.review import needs_sync
from desktop.scheduler import Lane


class ResumeTests(unittest.TestCase):
    setUp = fixtures.DesktopTests.setUp
    tearDown = fixtures.DesktopTests.tearDown
    engine = fixtures.DesktopTests.engine
    selections = fixtures.DesktopTests.selections
    success = fixtures.DesktopTests.success
    def test_cancel_inflight_answer_preserves_result_and_requeue_only_fills_remaining(self):
        self.store.save_settings({'video': False, 'answer_workers': 1})
        platform = FakePlatform(); platform.documents['0'] = {'questions': [question('one'), question('two')]}
        engine = self.engine(platform)
        entered, release = threading.Event(), threading.Event(); calls = []
        def answer(q, checkpoint):
            calls.append(q['id']); entered.set(); release.wait(2)
            return {'payload': {'answers': ['B']}, 'answer': 'B', 'error': '', 'source': 'test'}
        engine.ai.query = answer; engine.start(self.selections(1))
        self.assertTrue(entered.wait(2))
        tid = next(r['id'] for r in self.store.tasks() if r.get('lane') == 'answer')
        engine.remove_tasks([tid]); release.set()
        until(lambda: self.store.task(tid)['status'] == 'removed')
        until(lambda: tid not in engine.lanes['answer'].pending)
        self.assertEqual(platform.writes, [])
        engine.requeue([tid]); until(lambda: len(platform.writes) == 1)
        self.assertEqual(calls, ['one', 'two'])
        self.assertIn('progress_checked_at', self.store.task(tid))

    def test_completed_platform_task_is_checked_and_never_answered_again(self):
        self.store.save_settings({'video': False})
        engine = self.engine(); engine.ai.query = self.success
        engine.start(self.selections(1)); until(lambda: not engine.busy())
        tid = next(r['id'] for r in self.store.tasks() if r.get('lane') == 'answer')
        engine.platform.completed.add('work-0')
        checked = []; jobs = engine.platform.jobs
        engine.platform.jobs = lambda c, p: (checked.append(p['id']) or jobs(c, p))
        engine.ai.query = lambda *args, **kwargs: self.fail('Completed work must not call AI')
        engine.requeue([tid]); until(lambda: not engine.busy())
        self.assertEqual(checked, ['0'])
        self.assertEqual(self.store.task(tid)['status'], 'platform_done')
        self.assertEqual(len(engine.platform.writes), 1)

    def test_add_tasks_while_other_video_is_running_does_not_restart_it(self):
        self.store.save_settings({'answer': False, 'video_workers': 1})
        engine = self.engine(); engine.platform.video_gate.clear()
        first = self.selections(1); engine.add(first)
        until(lambda: engine.lanes['video'].counts().get('running') == 1)
        engine.add([(first[0][0], [{'id': '1', 'title': 'second'}])])
        until(lambda: engine.lanes['video'].counts().get('queued') == 1)
        lane = engine.lanes['video']; engine.platform.video_gate.set()
        until(lambda: len(engine.platform.videos) == 2)
        self.assertIs(engine.lanes['video'], lane)
        self.assertEqual(engine.platform.videos, ['0', '1'])

    def test_restarting_unchanged_draft_reads_progress_without_another_save(self):
        self.store.save_settings({'video': False})
        engine = self.engine(); engine.ai.query = self.success
        engine.start(self.selections(1)); until(lambda: not engine.busy())
        engine.start(self.selections(1)); until(lambda: not engine.busy())
        self.assertEqual(len(engine.platform.writes), 1)

    def test_retry_discovery_uses_original_task_types(self):
        self.store.save_settings({'video': False})
        engine = self.engine(); jobs = engine.platform.jobs
        engine.platform.jobs = lambda *args: (_ for _ in ()).throw(ValueError('读取失败'))
        engine.add(self.selections(1)); until(lambda: not engine.busy())
        tid = next(r['id'] for r in self.store.tasks() if r['lane'] == 'discovery')
        self.assertEqual(self.store.task(tid)['status'], 'failed')
        engine.platform.jobs = jobs; engine.ai.query = self.success
        self.store.save_settings({'video': True, 'answer': False})
        engine.requeue([tid]); until(lambda: not engine.busy())
        self.assertEqual(len(engine.platform.writes), 1)
        self.assertEqual(engine.platform.videos, [])

    def test_blocked_history_retry_keeps_scope_and_can_be_cancelled(self):
        self.store.save_settings({'answer': False})
        engine = self.engine(); engine.start(self.selections(1)); until(lambda: not engine.busy())
        tid = next(r['id'] for r in self.store.tasks() if r['lane'] == 'video')
        jobs = engine.platform.jobs
        engine.platform.jobs = lambda *args: ([], {'notOpen': True})
        engine.requeue([tid]); until(lambda: not engine.busy())
        self.assertEqual(self.store.task(tid)['execution_state'], 'blocked')
        engine.remove_tasks([tid]); engine.platform.jobs = jobs
        engine.recheck_blocked(); engine.poll_recheck(); until(lambda: not engine.busy())
        self.assertEqual(self.store.task(tid)['status'], 'removed')
        self.assertEqual(len(engine.platform.videos), 1)


class SelectionTests:
    setUp = fixtures.DesktopTests.setUp
    tearDown = fixtures.DesktopTests.tearDown


class UiSelectionTests(SelectionTests, unittest.TestCase):
    def test_no_selection_never_becomes_all_and_filter_drops_hidden_checks(self):
        from test_batch_workflow import BatchWorkflowTests
        engine = fixtures.DesktopTests.engine(self)
        BatchWorkflowTests.paper(self, engine, 'good')
        BatchWorkflowTests.paper(self, engine, 'bad', bad=True)
        root = tk.Tk(); app = App(root, self.store)
        try:
            calls = []; app.engine.batch = lambda action, ids, *args: calls.append((action, ids, args))
            app.redo_selected(); app.paper_batch('submit')
            self.assertEqual(calls, [])
            app.question_filter.set('全部题目'); app.render_review(); app.question_table.toggle_all()
            self.assertEqual(len(app.question_table.selected()), 4)
            app.question_filter.set('异常题'); app.render_review()
            self.assertEqual(len(app.question_table.selected()), 1)
            app.redo_selected()
            self.assertEqual(calls[-1][1], ['bad'])
            self.assertEqual(calls[-1][2][0], {'bad': ['q2']})
            app.question_table.set_checked([]); app.redo_selected()
            self.assertEqual(len(calls), 1)
        finally: app.close()

    success = fixtures.DesktopTests.success

    def test_sync_is_available_only_after_changes_and_skips_unchanged_records(self):
        from test_batch_workflow import BatchWorkflowTests
        engine = fixtures.DesktopTests.engine(self)
        clean = BatchWorkflowTests.paper(self, engine, 'clean')
        changed = BatchWorkflowTests.paper(self, engine, 'changed')
        engine.edit_answer('changed', 'q1', {'answers': ['A']})
        self.assertFalse(needs_sync(clean)); self.assertTrue(needs_sync(self.store.task('changed')))
        engine.batch('save', ['clean', 'changed']); until(lambda: not engine.batch_threads)
        batch = self.store.get('batch_history')[-1]
        self.assertEqual([r['result'] for r in batch['items']], ['skipped', 'success'])
        self.assertEqual([r[0] for r in engine.platform.writes], ['changed'])
        self.assertFalse(needs_sync(self.store.task('changed')))
