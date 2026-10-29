import json
from pathlib import Path
import time
import traceback


def redact(text, settings):
    for key in ('username', 'password', 'key'):
        value = settings.get(key)
        if isinstance(value, str) and value:
            text = text.replace(value, '<REDACTED>')
    return text


def capture_error(directory, operation, exc, settings):
    reason = redact(str(exc) or type(exc).__name__, settings)
    report = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'operation': operation,
              'type': type(exc).__name__, 'reason': reason,
              'traceback': redact(''.join(traceback.format_exception(exc)), settings)}
    try:
        (Path(directory) / 'last-error.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass
    return report
