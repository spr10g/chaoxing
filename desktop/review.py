"""Question validation and independent platform/execution state presentation."""
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


def remote_state(record):
    if record.get('remote_state'):
        return record['remote_state']
    return {'draft': 'draft', 'submitted': 'submitted', 'platform_done': 'completed',
            'unknown': 'unknown', 'submitting': 'submitting'}.get(record.get('status'), 'unknown_draft')


def remote_label(record):
    labels = {'draft': '平台草稿已保存', 'draft_old': '平台有草稿 · 本地有更新',
              'draft_failed': '草稿同步失败 · 本地保留', 'unknown_draft': '平台草稿未核实',
              'dirty': '本地有更新 · 未同步', 'submitted': '已交卷', 'completed': '平台任务已完成',
              'submitting': '交卷请求处理中', 'unknown': '交卷结果待核验'}
    text = labels.get(remote_state(record), '平台状态待核实')
    if record.get('platform_state') == 'incomplete' and remote_state(record) not in ('submitted', 'completed'):
        text += ' · 任务未完成'
    return text


def needs_review(record):
    return record.get('lane') == 'answer' and record.get('status') not in FINAL | UNCERTAIN


def can_submit(record, allow_blank=False):
    quality = summary(record)
    return needs_review(record) and bool(quality['good']) and (quality['ready'] or allow_blank)


def changed_row(previous, question, result):
    history = copy.deepcopy(previous.get('history', [])) if previous else []
    if previous and (previous.get('payload') or previous.get('error')):
        history.append({'time': time.time(), **{k: copy.deepcopy(v) for k, v in previous.items() if k != 'history'}})
    return {'question': copy.deepcopy(question), 'source': '', 'answer': '', 'error': '',
            **result, 'history': history, 'changed_at': time.time()}


def dirty_remote(record):
    return 'draft_old' if remote_state(record) in ('draft', 'draft_old') else 'dirty'
