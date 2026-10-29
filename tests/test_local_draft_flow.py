import json
import tkinter as tk
import threading
import unittest

import test_desktop as fixtures
import test_batch_workflow as batches
from desktop.review import SubmissionRejected, SubmissionUncertain, rows_for
from desktop.ui import App
from desktop.ui_workspace import paper_state
from desktop.storage import Store


class LocalDraftTests(unittest.TestCase):
    setUp = fixtures.DesktopTests.setUp
    tearDown = fixtures.DesktopTests.tearDown
    engine = fixtures.DesktopTests.engine
    selections = fixtures.DesktopTests.selections
    success = fixtures.DesktopTests.success

    def paper(self, engine, **kwargs):
        return batches.BatchWorkflowTests.paper(self, engine, 'a', **kwargs)

    def test_answering_editing_and_redo_save_locally_without_platform_posts(self):
        self.store.save_settings({'video': False})
        engine = self.engine(); engine.ai.query = lambda q, checkpoint, **kw: self.success(q, checkpoint)
        engine.add(self.selections(1)); fixtures.until(lambda: not engine.busy())
        record = next(r for r in self.store.tasks() if r.get('lane') == 'answer')
        self.assertEqual(record['status'], 'local_saved')
        self.assertEqual(engine.platform.writes, [])
        engine.edit_answer(record['id'], 'q1', {'answers': ['A']})
        engine.batch('redo', [record['id']], {record['id']: ['q1']})
        fixtures.until(lambda: not engine.batch_threads)
        self.assertEqual(engine.platform.writes, [])
        self.assertEqual(self.store.task(record['id'])['rows'][0]['answer'], 'B')
        with self.assertRaises(ValueError): engine.batch('save', [record['id']])

    def test_one_submit_post_purges_draft_and_history_but_retains_receipt_and_shared_cache(self):
        engine = self.engine(); original = self.paper(engine)
        original['document_history'] = [{'document': original['document'], 'rows': original['rows']}]
        self.store.save_task(original)
        self.store.set('other-paper-cache', {'payload': {'answers': ['A']}}, 'cache')
        engine.batch('submit', ['a']); fixtures.until(lambda: not engine.batch_threads)
        self.assertEqual(len(engine.platform.writes), 1)
        self.assertTrue(engine.platform.writes[0][1])
        receipt = self.store.task('a')
        for field in ('document', 'rows', 'document_history', 'info', 'draft_saved_at'):
            self.assertNotIn(field, receipt)
        self.assertEqual(receipt['status'], 'submitted')
        self.assertTrue(receipt['submission_receipt']['confirmed_at'])
        self.assertEqual(receipt['course'], original['course'])
        self.assertEqual(receipt['point'], original['point'])
        self.assertIsNotNone(self.store.get('other-paper-cache', 'cache'))
        engine.record(original, reason='late callback')
        self.assertNotIn('document', self.store.task('a'))

    def test_rejected_and_unknown_submissions_retain_draft_and_use_distinct_states(self):
        for failure, expected in ((SubmissionRejected('平台拒绝交卷'), '需处理'),
                                  (ValueError('读取题目失败，请重新读取'), '需处理'),
                                  (SubmissionUncertain('响应丢失'), '待确认')):
            with self.subTest(expected=expected):
                engine = self.engine(); original = self.paper(engine)
                def write(*args, **kwargs): raise failure
                engine.platform.write_work = write
                engine.batch('submit', ['a']); fixtures.until(lambda: not engine.batch_threads)
                current = self.store.task('a')
                self.assertEqual(current['rows'], original['rows'])
                self.assertEqual(current['document'], original['document'])
                self.assertEqual(paper_state(current), expected)

    def test_missing_job_is_uncertain_and_does_not_purge_local_answers(self):
        engine = self.engine(); original = self.paper(engine)
        engine.platform.jobs = lambda *args: ([], {'knowledgeid': 'a'})
        engine.verify('a'); fixtures.until(lambda: not engine.batch_threads)
        current = self.store.task('a')
        self.assertEqual(paper_state(current), '待确认')
        self.assertEqual(current['rows'], original['rows'])
        self.assertEqual(engine.platform.writes, [])

    def test_restart_preserves_unknown_submit_even_before_worker_has_finished(self):
        engine = self.engine(); record = self.paper(engine, status='unknown')
        record.update(remote_state='unknown', execution_state='running')
        self.store.save_task(record)
        reopened = Store(self.store.directory)
        try:
            recovered = reopened.task('a')
            self.assertEqual(paper_state(recovered), '待确认')
            self.assertEqual(recovered['rows'], record['rows'])
        finally:
            reopened.db.close()

    def test_explicit_completion_marker_confirms_and_cleans_local_draft(self):
        engine = self.engine(); self.paper(engine)
        engine.platform.jobs = lambda *args: ([], {'knowledgeid': 'a', 'completed_jobs': ['work-a']})
        engine.verify('a'); fixtures.until(lambda: not engine.batch_threads)
        current = self.store.task('a')
        self.assertEqual(paper_state(current), '已交卷')
        self.assertNotIn('rows', current)
        self.assertTrue(current['submission_receipt']['confirmed_at'])

    def test_legacy_completion_claim_is_rechecked_and_receipt_is_never_refilled(self):
        engine = self.engine(); original = self.paper(engine, status='platform_done')
        engine.verify('a'); fixtures.until(lambda: not engine.batch_threads)
        self.assertEqual(paper_state(self.store.task('a')), '待交卷')
        engine.batch('submit', ['a']); fixtures.until(lambda: not engine.batch_threads)
        # A contradictory outstanding platform task cannot be reported as submitted.
        engine.platform.completed.clear()
        engine.verify('a'); fixtures.until(lambda: not engine.batch_threads)
        current = self.store.task('a')
        self.assertEqual(paper_state(current), '待确认')
        self.assertNotIn('document', current)
        self.assertEqual(len(engine.platform.writes), 1)

    def test_processing_takes_priority_over_answer_completeness(self):
        engine = self.engine(); record = self.paper(engine)
        self.assertEqual(paper_state(record), '待交卷')
        for execution in ('queued', 'running', 'checking', 'removing'):
            self.assertEqual(paper_state(dict(record, execution_state=execution)), '处理中')
        self.assertEqual(paper_state(dict(record, status='submitting')), '处理中')
        self.assertEqual(paper_state(dict(record, status='unknown'), busy=True), '处理中')

    def test_busy_waits_for_final_local_draft_to_be_persisted(self):
        self.store.save_settings({'video': False})
        engine = self.engine(); engine.ai.query = self.success
        entered, release = threading.Event(), threading.Event()
        original = engine.state
        def delayed(task, status, reason):
            if status == 'local_saved':
                entered.set(); release.wait(2)
            original(task, status, reason)
        engine.state = delayed
        try:
            engine.add(self.selections(1)); self.assertTrue(entered.wait(2))
            self.assertTrue(engine.busy())
        finally:
            release.set()
        fixtures.until(lambda: not engine.busy())
        self.assertEqual(next(r for r in self.store.tasks() if r.get('lane') == 'answer')['status'], 'local_saved')

    def test_merged_column_filters_and_actions_reflect_local_workflow(self):
        engine = self.engine(); self.paper(engine)
        app = App(tk.Tk(), self.store)
        try:
            columns = app.paper_table.tree['columns']
            self.assertNotIn('sync', columns)
            self.assertEqual(app.paper_table.tree.heading('state', 'text'), '测验状态')
            self.assertFalse(hasattr(app, 'sync_button'))
            self.assertEqual(app.submit_button.cget('text'), '交卷到学习通')
            app.paper_filter.set('待交卷'); app.render_review()
            self.assertEqual(app.paper_table.tree.get_children(), ('a',))
            app.paper_filter.set('需处理'); app.render_review()
            self.assertEqual(app.paper_table.tree.get_children(), ())
        finally:
            app.root.update_idletasks(); app.close()

    def test_card_decoder_keeps_explicit_completion_evidence(self):
        from api.decode import decode_course_card
        html = 'mArg=' + json.dumps({'defaults': {'knowledgeid': 'a'}, 'attachments': [
            {'type': 'workid', 'jobid': 'work-a', 'isPassed': True},
            {'type': 'workid', 'jobid': 'work-b', 'isPassed': False, 'job': True}
        ]}) + ';'
        _, info = decode_course_card(html)
        self.assertEqual(info['completed_jobs'], ['work-a'])
        self.assertEqual(info['pending_jobs'], ['work-b'])
