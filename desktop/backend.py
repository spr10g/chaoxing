import copy
import os
import threading
import time

from .answers import AnswerService, fingerprint, validate
from .scheduler import Lane, LaneFault, Cancelled
from .review import rows_for, FormConflict, SubmissionRejected, SubmissionUncertain


def course_key(course):
    return ':'.join(str(course.get(k, '')) for k in ('courseId', 'clazzId', 'cpi'))


class Platform:
    """Reuse the original video implementation, with no AI dependency in this path."""
    def __init__(self, store):
        os.environ['CHAOXING_DESKTOP_DATA'] = str(store.directory)
        from api.base import Chaoxing, Account, SessionManager
        from api.config import GlobalConst
        GlobalConst.COOKIES_PATH = str(store.directory / 'cookies.txt')
        self.store, self.SessionManager = store, SessionManager
        self.local = threading.local()
        self.client = Chaoxing(Account(store.settings['username'], store.settings['password']),
                               discovery_only=True, checkpoint=self.checkpoint,
                               progress=self.progress)
        # Suppress old HTTP dumps and cookie traces, which can contain credentials.
        from api.logger import logger
        logger.remove()
        logger.add(str(store.directory / 'chaoxing.log'), rotation='10 MB', level='INFO',
                   filter=lambda record: not any(word in record['message'] for word in ('请求头', '请求url', 'Logged in with cookies')))

    def checkpoint(self):
        getattr(self.local, 'checkpoint', lambda: None)()

    def bind(self, checkpoint, progress=None):
        self.local.checkpoint = checkpoint
        self.local.progress = progress or (lambda *args: None)
        self.SessionManager.set_checkpoint(checkpoint)

    def progress(self, current, total):
        getattr(self.local, 'progress', lambda *args: None)(current, total)

    def login(self):
        from api.base import Account
        s = self.store.settings
        self.client.account = Account(s['username'], s['password'])
        result = self.client.login(login_with_cookies=s['use_cookies'])
        if not result.get('status'):
            raise ValueError(result.get('msg', '登录失败'))
        return self.client.get_course_list()

    def chapters(self, course):
        return self.client.get_course_point(course['courseId'], course['clazzId'], course['cpi'])['points']

    @property
    def uid(self):
        return str(self.client.get_uid())

    def check_owner(self, task):
        if task.get('owner') and task['owner'] != self.uid:
            raise ValueError('此任务属于其他登录账号，请切回原账号处理')

    def jobs(self, course, point):
        jobs, info = self.client.get_job_list(course, point)
        if not info:
            raise ValueError('未读取到章节任务信息；请检查登录状态或页面变化')
        return jobs, info

    def video(self, task, checkpoint, progress):
        self.check_owner(task)
        self.bind(checkpoint, progress)
        # Re-read the platform's current playTime / completion / token before resuming.
        jobs, info = self.jobs(task['course'], task['point'])
        if info.get('notOpen'):
            return 'blocked'
        job = next((j for j in jobs if j.get('jobid') == task['job'].get('jobid')), None)
        if job is None:
            return 'done'
        speed = min(2.0, max(1.0, float(self.store.settings['speed'])))
        result = self.client.study_video(task['course'], job, info, _speed=speed, _type='Video')
        if result.is_failure():
            checkpoint()
            result = self.client.study_video(task['course'], job, info, _speed=speed, _type='Audio')
        if result.is_failure():
            raise ValueError('视频/音频处理失败：' + result.name)
        return 'done'

    def fetch_work(self, task):
        self.check_owner(task)
        from api.decode import decode_questions_info
        self.checkpoint()
        course, job, info = task['course'], task['job'], task['info']
        response = self.SessionManager.get_session().get('https://mooc1.chaoxing.com/mooc-ans/api/work', params={
            'api': '1', 'workId': job['jobid'].replace('work-', ''), 'jobid': job['jobid'],
            'originJobId': job['jobid'], 'needRedirect': 'true', 'skipHeader': 'true',
            'knowledgeid': str(info['knowledgeid']), 'ktoken': info['ktoken'], 'cpi': info['cpi'],
            'ut': 's', 'clazzId': course['clazzId'], 'type': '', 'enc': job['enc'],
            'mooc2': '1', 'courseid': course['courseId'],
        })
        response.raise_for_status()
        if 'passport2.chaoxing.com' in response.url:
            raise ValueError('学习通登录已过期，请重新登录')
        if '教师未创建完成该测验' in response.text:
            raise ValueError('教师未创建完成该测验')
        questions = decode_questions_info(response.text)
        if not questions.get('questions'):
            raise ValueError('没有读取到题目；保留任务，不尝试提交空表单')
        ids = [q['id'] for q in questions['questions']]
        if not all(ids) or len(set(ids)) != len(ids):
            raise ValueError('题目编号缺失或重复')
        return questions

    def write_work(self, task, document, rows, submit=True, allow_blank=False):
        if not submit:
            raise ValueError('草稿自动保存在本地，仅交卷时上传学习通')
        # Always use fresh platform tokens, never persist and replay an old POST form.
        fresh = self.fetch_work(task)
        if [fingerprint(q) for q in fresh['questions']] != [fingerprint(q) for q in document['questions']]:
            raise FormConflict('平台题目或选项已变化，请重新读取或让 AI 重做')
        by_id = {row['question']['id']: row for row in rows if 'question' in row}
        # Compatibility for callers that already supply complete rows in document order.
        if rows and not by_id and len(rows) == len(fresh['questions']):
            by_id = {q['id']: dict(row, question=q) for q, row in zip(fresh['questions'], rows)}
        normalized = rows_for({'document': fresh, 'rows': list(by_id.values())})
        if not any(not row['error'] for row in normalized):
            raise FormConflict('没有格式检查通过的答案，不能交空白测验')
        if submit and not allow_blank and any(row['error'] for row in normalized):
            raise FormConflict('该测验仍有异常题；请先处理，或明确选择允许含空题交卷')
        form = {k: v for k, v in fresh.items() if k != 'questions'}
        form['pyFlag'] = ''
        for question, row in zip(fresh['questions'], normalized):
            qid = question['id']
            if row.get('payload') and not row.get('error'):
                value = validate(question, row['payload'])
            else:
                value = ''
            form['answer' + qid] = value
            type_code = question['answerField'].get('answertype' + qid, '')
            if not type_code:
                raise FormConflict('题型字段缺失，无法生成有效表单，请重新读取题目')
            form['answertype' + qid] = type_code
        self.checkpoint()
        try:
            response = self.SessionManager.get_session().post(
                'https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew', data=form,
                headers={'X-Requested-With': 'XMLHttpRequest'}, timeout=(8, 20))
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            if submit:
                raise SubmissionUncertain('交卷请求已发出，但未取得明确结果，请核验平台状态') from exc
            raise
        if not isinstance(result, dict) or result.get('status') is None:
            raise SubmissionUncertain('已发送交卷，但响应未包含明确结果，请核实交卷状态')
        if result.get('status') not in (True, 1, 'true'):
            raise SubmissionRejected('平台拒绝交卷：' + str(result.get('msg', '未知原因'))[:200])



from .engine import Engine  # public compatibility entry
