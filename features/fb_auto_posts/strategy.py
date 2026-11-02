"""Pure Page frequency, stagger and diversity policy. No network or publishing."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta


def bucket(value: str, size: int = 100) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest()[:16], 16) % size


def frequency(config):
    schedule = config['schedule']
    return len(schedule['times']) if schedule['mode'] == 'fixed' else int(schedule['daily_count'])


def daily_limit(config, page_id):
    maximum = frequency(config)
    overrides = {str(p['page_id']): int(p['daily_count']) for p in config.get('page_daily_limits', [])}
    return min(maximum, overrides.get(str(page_id), int(config.get('default_daily_count', maximum))))


def daily_capacity(config, pages):
    return sum(daily_limit(config, p.page_id) for p in pages if p.eligible_token_count > 0)


def slot_indices(config, page_id):
    total, count = frequency(config), daily_limit(config, page_id)
    if not count:
        return set()
    # Spread reduced schedules over the whole day, then rotate by Page to
    # distribute both skipped slots and demand. This never increases frequency.
    offset = bucket(str(page_id), total)
    return {(int(i * total / count) + offset) % total for i in range(count)}


def in_slot(config, page_id, slot_key, *, manual=False, times=None):
    if not daily_limit(config, page_id):
        return False
    if manual or not config.get('page_daily_limits') and 'default_daily_count' not in config:
        return True
    ordered = times if times is not None else config['schedule'].get('times', [])
    minute = slot_key[-5:]
    return minute in ordered and ordered.index(minute) in slot_indices(config, page_id)


def planned_time(config, page_id, slot_key, publish_at, *, manual=False):
    minutes = 0 if manual else int(config.get('stagger_minutes', 0))
    if not minutes:
        return publish_at
    offset = bucket(f'{page_id}:{slot_key}', minutes * 60 + 1)
    return (datetime.fromisoformat(publish_at) + timedelta(seconds=offset)).isoformat(timespec='seconds')


def diverse_candidates(candidates, limit, per_drama):
    """Retain ranked alternatives across dramas instead of one drama's copies."""
    if not per_drama:
        return list(candidates[:limit])
    counts, result = {}, []
    for item in candidates:
        key = item.content_id
        if counts.get(key, 0) >= per_drama:
            continue
        counts[key] = counts.get(key, 0) + 1
        result.append(item)
        if len(result) >= limit:
            break
    return result


def cooldown_content_ids(conn, page_id, publish_at, hours, now):
    if not hours:
        return set()
    target = datetime.fromisoformat(publish_at)
    lower = (target - timedelta(hours=hours)).isoformat(timespec='seconds')
    upper = (target + timedelta(hours=hours)).isoformat(timespec='seconds')
    # Check both directions: planners may prepare future slots out of order.
    # Confirmed publication uses completion time; an unresolved outcome holds
    # the content regardless of its intended time until reconciled.
    rows = conn.execute("""
        SELECT DISTINCT content_id FROM fb_auto_task
        WHERE page_id=? AND content_id<>'' AND (
          status IN ('running','submitted','unknown') OR
          (status IN ('planned','preparing','ready') AND planned_publish_at_utc>? AND planned_publish_at_utc<?) OR
          (status IN ('published','failed_without_retry') AND
            COALESCE(NULLIF(completed_at_utc,''),created_at_utc)>? AND
            COALESCE(NULLIF(completed_at_utc,''),created_at_utc)<?))
    """, (str(page_id), lower, upper, lower, upper))
    return {str(row[0]) for row in rows}
