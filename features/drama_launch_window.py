"""Signed elapsed-day ranges for template selection and exact release scheduling."""
from datetime import datetime, timezone
import re

POLICY = 'template_release_time_v1'
LIMIT = 3650


def normalize(value):
    """Keep legacy 0/N values; canonicalize an inclusive signed A~B range."""
    text = str(value).strip().replace('～', '~')
    for left, right in (('[', ']'), ('【', '】')):
        if text.startswith(left) and text.endswith(right):
            text = text[1:-1].strip()
            break
    match = re.fullmatch(r'(-?\d+)\s*~\s*(-?\d+)', text)
    if match:
        low, high = map(int, match.groups())
        if -LIMIT <= low <= high <= LIMIT:
            return f'{low}~{high}'
    elif re.fullmatch(r'\d+', text) and int(text) <= LIMIT:
        return int(text)
    raise ValueError('上线窗口请填 0、正整数天数或下限~上限（如 -1~0）；范围为 -3650 至 3650，且下限不能大于上限')


def bounds(value, now_epoch):
    value = normalize(value)
    if isinstance(value, str):
        low, high = map(int, value.split('~'))
        return max(1, now_epoch - high * 86400), now_epoch - low * 86400
    return (max(1, now_epoch - value * 86400) if value else 1), now_epoch


def is_range(value):
    return isinstance(normalize(value), str)


def release_timing(release, now):
    release = int(release)
    if release <= 0:
        raise ValueError('目标剧缺少有效可投放时间')
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    due = datetime.fromtimestamp(release, timezone.utc)
    return dict(policy=POLICY, release_at_utc=due.isoformat(),
                publish_at_utc=max(now, due).isoformat(), prepare_at_utc=now.isoformat(),
                advance_seconds=0, future_release=due > now)


def frozen_release_times(task):
    """Only extend hydration for the exact drama/language frozen by selection."""
    from features.hit_publish import target_language
    selection = task.selection
    timing = selection.get('hit_publish_timing') or {}
    if timing.get('policy') != POLICY:
        return {}
    drama = selection.get('drama') or {}
    if str(drama.get('content_id')) != str(task.content_id):
        raise ValueError('预约剧目与任务不一致')
    release = int(datetime.fromisoformat(timing['release_at_utc']).timestamp())
    return {str(task.content_id) + ':' + target_language(drama.get('language')): release}
