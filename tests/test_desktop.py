import copy
import json
import tempfile
import threading
import time
import unittest
import uuid
import shutil
from types import SimpleNamespace
from pathlib import Path

from desktop.answers import AnswerService, validate
from desktop.backend import Engine, Platform
from desktop.scheduler import Lane, LaneFault, Cancelled
from desktop.storage import Store


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('condition did not become true')


def question(qid='q1', title='下列哪个数字是偶数？'):
    return {'id': qid, 'title': title, 'options': 'A.1\nB.2\nC.3', 'type': 'single',
            'answerField': {'answer' + qid: '', 'answertype' + qid: '0'}}


class FakePlatform:
    def __init__(self):
        self.video_gate = threading.Event()
        self.video_gate.set()
        self.videos = []
        self.writes = []
        self.completed = set()
        self.documents = {}
        self.raise_write = False
        self.local = threading.local()

    def bind(self, checkpoint, progress=None):
        self.local.checkpoint = checkpoint

    def jobs(self, course, point):
        pid = str(point['id'])
        jobs = [{'type': 'video', 'jobid': 'video-' + pid, 'name': 'video'},
                {'type': 'workid', 'jobid': 'work-' + pid, 'enc': 'fresh'}]
        return [j for j in jobs if j['jobid'] not in self.completed], {'knowledgeid': pid, 'ktoken': 'token', 'cpi': '1'}

    def fetch_work(self, task):
        return copy.deepcopy(self.documents.get(task['point']['id'], {'questions': [question()]}))

    def video(self, task, checkpoint, progress):
        while not self.video_gate.wait(.01):
            checkpoint()
        self.videos.append(task['point']['id'])
        return 'done'

    def write_work(self, task, doc, rows, submit=False):
        if self.raise_write:
            raise TimeoutError('simulated timeout')
        self.writes.append((task['id'], submit, copy.deepcopy(rows)))


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parent.parent / 'diagnostics' / 'test-tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        directory = temporary_root / uuid.uuid4().hex
        directory.mkdir()
        self.tmp = SimpleNamespace(name=str(directory))
        self.store = Store(self.tmp.name)
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.stop()
            for lane in engine.lanes.values():
                lane.wait_stopped()
        self.store.db.close()
        if 'api.logger' in __import__('sys').modules:
            from api.logger import logger
            logger.remove()
        directory = Path(self.tmp.name).resolve()
        expected_root = (Path(__file__).resolve().parent.parent / 'diagnostics' / 'test-tmp').resolve()
        if directory.is_relative_to(expected_root):
            shutil.rmtree(directory)

    def engine(self, platform=None):
        engine = Engine(self.store, platform=platform or FakePlatform())
        self.engines.append(engine)
        return engine

    def selections(self, n=4):
        return [({'courseId': 'course', 'clazzId': 'class', 'cpi': '1'},
                 [{'id': str(i), 'title': f'Chapter {i}'} for i in range(n)])]

    def success(self, q, checkpoint):
        checkpoint()
        return {'payload': {'answers': ['B']}, 'answer': 'B', 'source': 'fixture', 'error': ''}

    def test_answers_finish_while_video_is_blocked(self):
        platform = FakePlatform()
        platform.video_gate.clear()
        engine = self.engine(platform)
        engine.ai.query = self.success
        engine.start(self.selections())
        until(lambda: len(platform.writes) == 4)
        self.assertEqual(platform.videos, [])
        self.assertTrue(all(not submit for _, submit, _ in platform.writes))
        platform.video_gate.set()
        until(lambda: len(platform.videos) == 4)

    def test_videos_finish_while_answer_api_is_blocked(self):
        engine = self.engine()
        gate = threading.Event()
        def slow(q, checkpoint):
            while not gate.wait(.01):
                checkpoint()
            return self.success(q, checkpoint)
        engine.ai.query = slow
        engine.start(self.selections())
        until(lambda: len(engine.platform.videos) == 4)
        self.assertEqual(engine.platform.writes, [])
        gate.set()
        until(lambda: len(engine.platform.writes) == 4)

    def test_global_api_fault_ends_answers_and_history_can_be_requeued(self):
        engine = self.engine()
        def fault(q, checkpoint):
            raise LaneFault('API Key 无效')
        engine.ai.query = fault
        engine.start(self.selections())
        until(lambda: engine.lanes['answer'].cancelled.is_set())
        until(lambda: len(engine.platform.videos) == 4)
        self.assertEqual(engine.platform.writes, [])
        until(lambda: not engine.lanes['answer'].alive() and not engine.busy())
        engine.ai.query = self.success
        ids = [r['id'] for r in self.store.tasks() if r.get('lane') == 'answer']
        engine.requeue(ids)
        until(lambda: len(engine.platform.writes) == 4)

    def test_failed_question_is_preserved_and_does_not_submit_or_block_next_chapter(self):
        self.store.save_settings({'auto_submit': True})
        platform = FakePlatform()
        platform.documents['0'] = {'questions': [question('bad', 'bad'), question('good')]}
        engine = self.engine(platform)
        def query(q, checkpoint):
            if q['id'] == 'bad':
                raise ValueError('选项解析失败')
            return self.success(q, checkpoint)
        engine.ai.query = query
        engine.start(self.selections(2))
        until(lambda: len(platform.writes) == 2)
        bad = next(w for w in platform.writes if w[0].endswith(':work-0:answer'))
        good = next(w for w in platform.writes if w[0].endswith(':work-1:answer'))
        self.assertFalse(bad[1])
        self.assertTrue(good[1])
        self.assertEqual(len(bad[2]), 2)
        self.assertTrue(bad[2][0]['error'])
        self.assertEqual(bad[2][1]['answer'], 'B')

    def test_unknown_post_result_not_automatically_retried(self):
        self.store.save_settings({'auto_submit': True, 'video': False})
        platform = FakePlatform()
        platform.raise_write = True
        engine = self.engine(platform)
        engine.ai.query = self.success
        engine.start(self.selections(1))
        until(lambda: any(r.get('status') == 'unknown' for r in self.store.tasks()))
        until(lambda: not engine.busy())
        platform.raise_write = False
        engine.start(self.selections(1))
        until(lambda: not engine.busy())
        self.assertEqual(platform.writes, [])

    def test_changed_question_cannot_reuse_old_answer(self):
        self.store.save_settings({'video': False})
        platform = FakePlatform()
        engine = self.engine(platform)
        engine.ai.query = self.success
        engine.start(self.selections(1))
        until(lambda: not engine.busy())
        first = next(r for r in self.store.tasks() if r.get('lane') == 'answer')
        platform.documents['0'] = {'questions': [question(title='全新题目')]}
        called = []
        def new_answer(q, checkpoint):
            called.append(q['title'])
            return self.success(q, checkpoint)
        engine.ai.query = new_answer
        engine.start(self.selections(1))
        until(lambda: not engine.busy())
        self.assertEqual(called, ['全新题目'])
        self.assertNotEqual(first['document'], self.store.task(first['id'])['document'])

    def test_cache_separates_option_order_and_rejects_invalid_answer(self):
        import httpx
        service = AnswerService(self.store, dict(self.store.settings, key='test-key', min_interval=0))
        calls = []
        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answers":["B"]}'}}]})
        service.local.client = httpx.Client(transport=httpx.MockTransport(respond), base_url='https://example.test/')
        service.local.version = service.version
        q = question()
        service.query(q, lambda: None)
        self.assertEqual(service.query(q, lambda: None)['source'], '本地缓存')
        service.query(dict(q, options='A.2\nB.1\nC.3'), lambda: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['response_format'], {'type': 'json_object'})
        self.assertEqual(calls[0]['thinking'], {'type': 'disabled'})
        for payload in ({'answers': ['Z']}, {'answers': ['A', 'B']}, {'answers': []}, {'answers': ['A'], 'extra': 1}):
            with self.assertRaises(ValueError):
                validate(q, payload)
        with self.assertRaises(ValueError):
            validate(dict(q, type='judgement'), {'answers': ['maybe']})

    def test_independent_concurrency_caps_and_stop_releases_workers(self):
        lock = threading.Lock()
        active = 0
        peak = 0
        def handler(task, checkpoint):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                for _ in range(5):
                    checkpoint()
                    time.sleep(.01)
            finally:
                with lock:
                    active -= 1
        lane = Lane('test', 2, handler)
        for i in range(8):
            lane.put({'id': str(i)})
        until(lambda: lane.counts().get('done') == 8)
        self.assertEqual(peak, 2)
        lane.pause()
        lane.put({'id': 'paused'})
        lane.stop()
        lane.wait_stopped()
        self.assertFalse(lane.alive())
        self.assertEqual(lane.states['paused'], 'interrupted')

    def test_settings_migration_retains_unknown_fields_and_old_files(self):
        folder = Path(self.tmp.name) / 'old'
        folder.mkdir()
        raw = '[common]\nusername=123\nspeed=1.5\ncustom_keep=hello\n[tiku]\nkey=secret\nmodel=custom\n'
        (folder / 'config.ini').write_text(raw, encoding='utf-8')
        (folder / 'cookies.txt').write_text('_uid=123;token=abc=def', encoding='utf-8')
        (folder / 'cache.json').write_text('{"old title":"B"}', encoding='utf-8')
        self.store.import_legacy(folder)
        self.store.save_settings({'video_workers': 7, 'selections': {'course': ['1', '2']}})
        reopened = Store(self.tmp.name)
        self.assertEqual(reopened.settings['video_workers'], 7)
        self.assertEqual(reopened.settings['legacy']['common']['custom_keep'], 'hello')
        self.assertEqual(reopened.settings['selections']['course'], ['1', '2'])
        self.assertEqual((folder / 'config.ini').read_text(encoding='utf-8'), raw)
        self.assertEqual(reopened.db.execute('SELECT count(*) FROM cache').fetchone()[0], 0)
        reopened.db.close()

    def test_restart_during_submit_requires_platform_verification(self):
        self.store.save_task({'id': 'uncertain', 'status': 'submitting'})
        reopened = Store(self.tmp.name)
        self.assertEqual(reopened.task('uncertain')['status'], 'unknown')
        reopened.db.close()

    def test_real_html_parser_preserves_malformed_and_image_questions(self):
        from api.decode import decode_questions_info
        html = '''<form><input name="workAnswerId" value="fresh">
        <div class="singleQuesId" data="1"><div class="TiMu" data="0"></div>
        <div class="Zy_TItle">测试题</div><ul><li aria-label="A.甲"></li><li aria-label="B.乙"></li></ul></div>
        <div class="singleQuesId" data="2"><img src="x"></div></form>'''
        doc = decode_questions_info(html)
        self.assertEqual(len(doc['questions']), 2)
        self.assertEqual(doc['workAnswerId'], 'fresh')
        self.assertTrue(doc['questions'][1]['unsupported'])
        with self.assertRaises(ValueError):
            decode_questions_info('<html>登录过期</html>')

    def test_video_delegates_to_original_video_and_audio_with_current_progress(self):
        from api.base import StudyResult, SessionManager
        platform = Platform(self.store)
        self.assertIs(SessionManager.get_session(), SessionManager.get_session())
        calls = []
        platform.jobs = lambda c, p: ([{'jobid': 'v', 'playTime': 180000}], {})
        def study(course, job, info, **kwargs):
            calls.append((job['playTime'], kwargs['_type'], kwargs['_speed']))
            return StudyResult.ERROR if kwargs['_type'] == 'Video' else StudyResult.SUCCESS
        platform.client.study_video = study
        result = platform.video({'course': {}, 'point': {}, 'job': {'jobid': 'v', 'playTime': 0}}, lambda: None, lambda *args: None)
        self.assertEqual(result, 'done')
        self.assertEqual(calls, [(180000, 'Video', 1.0), (180000, 'Audio', 1.0)])

    def test_platform_submit_uses_fresh_fields_and_never_posts_changed_questions(self):
        from types import SimpleNamespace
        platform = Platform(self.store)
        q = question()
        old = {'questions': [q], 'token': 'expired'}
        fresh = {'questions': [copy.deepcopy(q)], 'token': 'new-token'}
        platform.fetch_work = lambda task: copy.deepcopy(fresh)
        requests = []
        class Session:
            def post(self, url, **kwargs):
                requests.append(kwargs)
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'status': True})
        platform.SessionManager = SimpleNamespace(get_session=lambda: Session())
        row = {'payload': {'answers': ['B']}, 'error': ''}
        platform.write_work({}, old, [row], submit=True)
        self.assertEqual(requests[0]['data']['token'], 'new-token')
        self.assertEqual(requests[0]['data']['answerq1'], 'B')
        self.assertEqual(requests[0]['data']['pyFlag'], '')
        fresh['questions'][0]['options'] = 'A.2\nB.1'
        with self.assertRaises(ValueError):
            platform.write_work({}, old, [row], submit=True)
        self.assertEqual(len(requests), 1)
        with self.assertRaises(ValueError):
            platform.write_work({}, old, [], submit=True)
        self.assertEqual(len(requests), 1)

    def test_key_error_classification_and_invalid_json_never_cached(self):
        import httpx
        service = AnswerService(self.store, dict(self.store.settings, key='test-key', min_interval=0))
        service.local.version = service.version
        service.local.client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={'error': 'invalid'})), base_url='https://example.test/')
        with self.assertRaises(LaneFault):
            service.query(question(), lambda: None)
        service.local.client.close()
        service.local.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answers":["Z"]}'}}]})), base_url='https://example.test/')
        with self.assertRaises(ValueError):
            service.query(question(), lambda: None)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM cache').fetchone()[0], 0)
        service.local.client.close()

    def test_locked_chapter_rechecks_are_bounded(self):
        platform = FakePlatform()
        calls = []
        platform.jobs = lambda c, p: (calls.append(p['id']) or [], {'notOpen': True})
        engine = self.engine(platform)
        engine.start(self.selections(1))
        until(lambda: not engine.busy())
        for _ in range(8):
            engine.recheck_blocked()
            engine.poll_recheck()
            until(lambda: not engine.busy())
        self.assertEqual(len(calls), 4)  # initial discovery + at most 3 checks

    def test_unknown_result_can_be_checked_without_any_post(self):
        engine = self.engine()
        task = {'id': 'uncertain', 'lane': 'answer', 'course': {}, 'point': {'id': '1'},
                'job': {'jobid': 'work-1'}, 'status': 'unknown',
                'document': {'questions': [question()]}, 'rows': []}
        self.store.save_task(task)
        engine.verify('uncertain')
        until(lambda: not engine.manual)
        self.assertEqual(self.store.task('uncertain')['status'], 'local_saved')
        self.assertEqual(engine.platform.writes, [])
        engine.platform.completed.add('work-1')
        engine.verify('uncertain')
        until(lambda: not engine.manual)
        self.assertEqual(self.store.task('uncertain')['status'], 'platform_done')
        self.assertEqual(engine.platform.writes, [])

    def test_original_video_loop_releases_slot_when_final_reports_never_pass(self):
        from api.base import StudyResult, SessionManager
        from unittest.mock import patch
        platform = Platform(self.store)
        platform.client.get_fid = lambda: 'test-fid'
        attempts = []
        def report(*args, **kwargs):
            attempts.append(1)
            return False, 200
        platform.client.video_progress_log = report
        response = SimpleNamespace(json=lambda: {'status': 'success', 'duration': 0,
                                                   'dtoken': 'test', 'crc': '', 'key': ''})
        session = SimpleNamespace(get=lambda *args, **kwargs: response)
        with patch.object(SessionManager, 'get_session', return_value=session), patch('api.base.time.sleep', return_value=None):
            result = platform.client.study_video({}, {'objectid': 'test', 'playTime': 0, 'name': 'test-video'}, {})
        self.assertEqual(result, StudyResult.TIMEOUT)
        self.assertLessEqual(len(attempts), 8)


if __name__ == '__main__':
    unittest.main()
