"""Timezone-explicit scheduling at the HTTP/preparation boundary."""
from datetime import datetime, timezone
from .templates import WorkflowError


def normalize_publish_at(value, *, future=False):
    if value is None or value == '':
        return ''
    if not isinstance(value, str) or len(value) > 40:
        raise WorkflowError('invalid_publish_at', '请选择有效的预约时间')
    try:
        value = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError('timezone required')
        value = value.astimezone(timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError):
        raise WorkflowError('invalid_publish_at', '预约时间必须包含时区') from None
    if future and value <= datetime.now(timezone.utc):
        raise WorkflowError('publish_at_past', '预约时间已过去，请选择未来时间')
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


def is_due(value):
    return bool(value and datetime.fromisoformat(value.replace('Z', '+00:00')) <= datetime.now(timezone.utc))
