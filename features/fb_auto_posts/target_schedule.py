"""Page-specific daily schedules; native task identities remain authoritative."""
from __future__ import annotations

import random
from bisect import bisect_left

from features import social_random_schedule as daily
from .strategy import daily_limit


def source_key(template_id):
    return f"fb:auto:{template_id}"


def run_slot(day, ordinal):
    return f"auto:target:{day}:{int(ordinal)}"


def parse_slot(value):
    parts = str(value).split(":")
    if len(parts) != 4 or parts[:2] != ["auto", "target"]:
        return None
    return parts[2], int(parts[3])


def page_times(config, page_id, day, version, template_id):
    count = daily_limit(config, page_id)
    if not count:
        return []
    schedule = config["schedule"]
    start = sum(n * m for n, m in zip(map(int, schedule["start"].split(":")), (60, 1)))
    end = sum(n * m for n, m in zip(map(int, schedule["end"].split(":")), (60, 1)))
    choices = [minute for minute in range(start, end + 1) if minute % 60]
    size = len(choices)
    next_index = [bisect_left(choices, minute + 60) for minute in choices]
    ways = [[0] * (count + 1) for _ in range(size + 1)]
    ways[size][0] = 1
    for index in range(size - 1, -1, -1):
        ways[index][0] = 1
        for remaining in range(1, count + 1):
            ways[index][remaining] = ways[index + 1][remaining] + ways[next_index[index]][remaining - 1]
    if not ways[0][count]:
        raise ValueError("fb_auto_schedule_window_too_small")
    rng = random.Random(f"{daily.POLICY}:{template_id}:{version}:{day}:{page_id}")
    result, index, remaining = [], 0, count
    while remaining:
        take = ways[next_index[index]][remaining - 1]
        skip = ways[index + 1][remaining]
        if rng.randrange(take + skip) < take:
            minute = choices[index]
            result.append(f"{minute // 60:02d}:{minute % 60:02d}")
            index, remaining = next_index[index], remaining - 1
        else:
            index += 1
    return result


def ensure_plan(conn, template_id, version, config, day, page_ids, *, now):
    return daily.ensure_day_plan(conn, source_key(template_id), day, version, config, page_ids,
        lambda page_id: page_times(config, page_id, day, version, template_id), now=now)
