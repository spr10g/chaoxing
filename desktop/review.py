"""Local draft validation and the user-facing submission lifecycle."""
import copy
import time

from .answers import validate


FINAL = {'submitted', 'platform_done'}
UNCERTAIN = {'submitting', 'unknown'}


class FormConflict(ValueError):
    pass


class SubmissionRejected(ValueError):
    pass


class SubmissionUncertain(RuntimeError):
    pass


def rows_for(record):
    saved = {r['question']['id']: r for r in record.get('rows', [])}
    result = []
    for question in record.get('document', {}).get('questions', []):
        row = copy.deepcopy(saved.get(question['id'], {}))
        row['question'] = copy.deepcopy(question)
        row.setdefault('source', '')
        row.setdefault('answer', '')
        row.setdefault('error', '' if row.get('payload') else '尚未处理')
        if not row['error']:
            try:
                row['answer'] = validate(question, row.get('payload'))
            except (ValueError, TypeError, KeyError) as exc:
                row['error'] = str(exc)
        result.append(row)
    return result


def summary(record):
    rows = rows_for(record)
    good = sum(not r['error'] for r in rows)
    return {'total': len(rows), 'good': good, 'errors': len(rows) - good,
            'ready': bool(rows) and good == len(rows)}


def confirmed_submission(record):
    return record.get('status') in FINAL and record.get('platform_state') != 'incomplete' and bool(record.get('submission_receipt') or
                                                  (record.get('status') == 'submitted' and record.get('submitted_at')))


def paper_state(record, busy=False):
    if busy or record.get('status') == 'submitting': return '处理中'
    if record.get('status') in UNCERTAIN: return '待确认'
    if confirmed_submission(record): return '已交卷'
    if record.get('status') in FINAL: return '待确认'
    if record.get('execution_state', record.get('status')) in ('queued', 'running', 'checking', 'removing'):
        return '处理中'
    if record.get('submission_error') or record.get('status') in ('failed', 'blocked', 'paused'):
        return '需处理'
    return '待交卷' if summary(record)['ready'] else '需处理'


def paper_reason(record, busy=False):
    state = paper_state(record, busy)
    if state == '已交卷': return '交卷成功，已清理本地草稿' if record.get('submission_receipt') else '历史交卷成功记录'
    if state == '待确认':
        if record.get('status') in FINAL: return '历史完成状态尚需核实，请读取平台状态'
        return record.get('reason') or '交卷结果尚未明确，请读取平台状态'
    if state == '处理中': return record.get('reason') or '程序正在处理，请等待完成'
    if record.get('submission_error'): return record['submission_error']
    if state == '待交卷': return '本地草稿已保存，可审核后交卷'
    return record.get('reason') or (f"{summary(record)['errors']} 道题尚未完成或有异常" if summary(record)['total'] else '尚未读取题目，请从任务页重新加入')


def needs_review(record):
    return record.get('lane') == 'answer' and record.get('status') not in FINAL | UNCERTAIN


def can_submit(record, allow_blank=False):
    quality = summary(record)
    executing = record.get('execution_state', record.get('status')) in ('queued', 'running', 'checking', 'removing')
    return needs_review(record) and not executing and bool(quality['good']) and (quality['ready'] or allow_blank)


def changed_row(previous, question, result):
    history = copy.deepcopy(previous.get('history', [])) if previous else []
    if previous and (previous.get('payload') or previous.get('error')):
        history.append({'time': time.time(), **{k: copy.deepcopy(v) for k, v in previous.items() if k != 'history'}})
    return {'question': copy.deepcopy(question), 'source': '', 'answer': '', 'error': '',
            **result, 'history': history, 'changed_at': time.time()}
