import copy
import tkinter as tk
import unittest

import test_desktop as fixtures
import test_batch_workflow as batches
from desktop.review import can_submit, rows_for
from desktop.storage import Store
from desktop.ui import App


class ListDeletionTests(unittest.TestCase):
    setUp = fixtures.DesktopTests.setUp
    tearDown = fixtures.DesktopTests.tearDown
    engine = fixtures.DesktopTests.engine
    success = fixtures.DesktopTests.success
    selections = fixtures.DesktopTests.selections

    def open_app(self):
        engine = self.engine()
        batches.BatchWorkflowTests.paper(self, engine, 'a')
        batches.BatchWorkflowTests.paper(self, engine, 'b')
        app = App(tk.Tk(), self.store)
        app.root.update_idletasks()
        app.task_scope.set('全部任务'); app.render_all_tasks()
        app.question_filter.set('全部题目'); app.render_review()
        return app

    def test_delete_task_button_removes_only_selected_task_listing(self):
        app = self.open_app()
        try:
            self.assertTrue(app.task_delete_button.instate(['disabled']))
            app.task_table.set_checked(['a'])
            app.task_delete_button.invoke()
            self.assertEqual(app.task_table.tree.get_children(), ('b',))
            self.assertEqual(set(app.paper_table.tree.get_children()), {'a', 'b'})
            self.assertEqual(len(rows_for(self.store.task('a'))), 2)
            # Late worker updates must not reinsert a deleted listing.
            app.engine.record(self.store.task('a'), reason='late progress')
            app.poll()
            self.assertEqual(app.task_table.tree.get_children(), ('b',))
        finally:
            app.close()

    def test_delete_question_preserves_complete_paper_and_survives_reopening(self):
        app = self.open_app()
        try:
            iid = next(i for i, ref in app.question_refs.items() if ref == ('a', 'q1'))
            before = copy.deepcopy(self.store.task('a')['rows'])
            app.question_table.set_checked([iid]); app.question_delete_button.invoke()
            self.assertEqual(len(app.question_tree.get_children()), 3)
            self.assertEqual(self.store.task('a')['rows'], before)
            self.assertTrue(can_submit(self.store.task('a')))
            self.assertEqual(len(rows_for(self.store.task('a'))), 2)
        finally:
            app.close()
        reopened = Store(self.store.directory)
        app = App(tk.Tk(), reopened)
        app.root.update_idletasks()
        try:
            app.question_filter.set('全部题目'); app.render_review()
            self.assertNotIn(('a', 'q1'), app.question_refs.values())
            self.assertEqual(len(app.question_tree.get_children()), 3)
        finally:
            app.close(); reopened.db.close()

    def test_delete_paper_keeps_task_and_question_lists(self):
        app = self.open_app()
        try:
            app.paper_table.set_checked(['a']); app.paper_delete_button.invoke()
            self.assertEqual(app.paper_table.tree.get_children(), ('b',))
            self.assertEqual(len(app.question_tree.get_children()), 4)
            self.assertEqual(len(app.task_tree.get_children()), 2)
            app.paper_filter.set('全部测验'); app.render_review()
            self.assertEqual(app.paper_table.tree.get_children(), ('b',))
        finally:
            app.close()

    def test_no_selection_never_deletes_other_records(self):
        app = self.open_app()
        try:
            before = self.store.tasks()
            for button in (app.task_delete_button, app.question_delete_button, app.paper_delete_button):
                self.assertEqual(button.cget('text'), '删除')
                self.assertTrue(button.instate(['disabled']))
                button.invoke()
            self.assertEqual(self.store.tasks(), before)
        finally:
            app.close()

    def test_deleting_running_task_cancels_only_selected_and_allows_explicit_readd(self):
        self.store.save_settings({'answer': False, 'video_workers': 1})
        engine = self.engine(); engine.platform.video_gate.clear()
        engine.add(self.selections(2))
        fixtures.until(lambda: engine.lanes['video'].counts().get('running') == 1)
        tid = next(r['id'] for r in self.store.tasks() if r.get('lane') == 'video' and r['point']['id'] == '0')
        engine.delete_list_items('tasks', [tid])
        fixtures.until(lambda: tid not in engine.lanes['video'].pending)
        self.assertTrue(self.store.task(tid)['list_hidden']['tasks'])
        engine.platform.video_gate.set()
        fixtures.until(lambda: not engine.busy())
        self.assertEqual(engine.platform.videos, ['1'])
        engine.add(self.selections(1)); fixtures.until(lambda: not engine.busy())
        self.assertEqual(engine.platform.videos, ['1', '0'])
        self.assertEqual(self.store.task(tid)['list_hidden'], {})

    def test_explicit_readd_restores_answer_lists_and_reuses_saved_answers(self):
        self.store.save_settings({'video': False})
        engine = self.engine(); engine.ai.query = self.success
        engine.add(self.selections(1)); fixtures.until(lambda: not engine.busy())
        tid = next(r['id'] for r in self.store.tasks() if r.get('lane') == 'answer')
        engine.delete_list_items('papers', [tid])
        engine.delete_list_items('questions', [tid], {tid: ['q1']})
        engine.ai.query = lambda *args, **kwargs: self.fail('Saved answer must be reused')
        engine.add(self.selections(1)); fixtures.until(lambda: not engine.busy())
        self.assertEqual(self.store.task(tid)['list_hidden'], {})
        self.assertEqual(len(rows_for(self.store.task(tid))), 1)
        self.assertEqual(engine.platform.writes, [])
