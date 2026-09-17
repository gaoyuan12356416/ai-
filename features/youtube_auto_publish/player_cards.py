"""Bounded, asynchronous, read-only Player Card status for task lists."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
import threading
import time

import requests

from features.drama_synthesis.youtube import YouTubeHTTPClient
from .x_card import inspect_player_card

TTL = 15 * 60
ERROR_TTL = 2 * 60
SOURCE_KEYS = ('app_id', 'channel_local_id', 'youtube_account_id', 'channel_id', 'video_id')
MESSAGES = {
    'ready': '已确认公开视频提供播放器卡片，未发现地区或年龄限制。X 的实际展示与播放仍以平台为准。',
    'restricted': '此视频提供播放器卡片，但存在地区或年龄播放限制，请核实目标观众是否可观看。',
    'not_player': '此视频当前提供图片等非播放器卡片。',
    'not_public': 'YouTube 当前尚未公开或处理完成，不能按公开视频转发。',
    'not_embeddable': 'YouTube 当前不允许此视频在外站嵌入播放。',
    'unavailable': '暂时无法确认视频的播放器卡片或公开状态，稍后自动重查。',
    'checking': '正在后台检查，结果会自动更新。',
    'pending': '等待后台检查，结果会自动更新。',
}


def result(state, **extra):
    return dict(state=state, eligible=state == 'ready', message=MESSAGES[state], **extra)


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class YouTubeCardProbe:
    """Only background display checks may reuse these short-lived credentials."""
    def __init__(self, repository, *, client=None, card_check=inspect_player_card,
                 session_factory=requests.Session, clock=time.monotonic):
        self.repository = repository
        self.client = client or YouTubeHTTPClient(timeout=20)
        self.card_check, self.session_factory, self.clock = card_check, session_factory, clock
        self._lock = threading.Lock()
        self._tokens = OrderedDict()

    def _token(self, source):
        key = tuple(source[k] for k in SOURCE_KEYS[:-1])
        # This lock is used only by the two background workers, never an HTTP handler.
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] > self.clock():
                self._tokens.move_to_end(key)
                return cached[0]
            credential = self.repository.credential(
                app_id=source['app_id'], channel_local_id=source['channel_local_id'],
                account_id=source['youtube_account_id'], expected_channel_id=source['channel_id'])
            token = self.client.refresh_access_token(credential)
            self.client.verify_channel_identity(token, source['channel_id'])
            self._tokens[key] = (token, self.clock() + 300)
            self._tokens.move_to_end(key)
            while len(self._tokens) > 64:
                self._tokens.popitem(last=False)
            return token

    def __call__(self, source):
        card = self.card_check(source['video_id'])
        if card.get('state') == 'not_player':
            return result('not_player', card_type=str(card.get('card_type') or ''))
        if card.get('eligible') is not True:
            return result('unavailable')
        token = self._token(source)
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.get('https://www.googleapis.com/youtube/v3/videos',
                params={'part': 'snippet,status,contentDetails', 'id': source['video_id']},
                headers={'Authorization': 'Bearer ' + token}, timeout=(5, 15), allow_redirects=False)
            if response.status_code != 200:
                if response.status_code in (401, 403):
                    with self._lock:
                        self._tokens.pop(tuple(source[k] for k in SOURCE_KEYS[:-1]), None)
                return result('unavailable')
            data = response.json()
            items = data.get('items') if isinstance(data, dict) else None
            item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else {}
            if item.get('id') != source['video_id'] or (item.get('snippet') or {}).get('channelId') != source['channel_id']:
                return result('unavailable')
            status = item.get('status') or {}
            if status.get('privacyStatus') != 'public' or status.get('uploadStatus') != 'processed':
                return result('not_public', card_type='player')
            if status.get('embeddable') is not True:
                return result('not_embeddable', card_type='player')
            details = item.get('contentDetails') or {}
            region = details.get('regionRestriction') or {}
            age_limited = (details.get('contentRating') or {}).get('ytRating') == 'ytAgeRestricted'
            if 'allowed' in region or bool(region.get('blocked')) or age_limited:
                return result('restricted', card_type='player')
            return result('ready', card_type='player')
        finally:
            session.close()


class PlayerCardStatusCache:
    """No I/O in get(); expired successes immediately lose their positive badge."""
    def __init__(self, probe, *, clock=time.time, max_pending=200, max_entries=512):
        self.probe, self.clock = probe, clock
        self.max_pending, self.max_entries = max_pending, max_entries
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._pending = set()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='youtube-card-status')

    def get(self, ledger):
        source = {key: str(ledger.get(key) or '') for key in SOURCE_KEYS}
        if (not re.fullmatch(r'[A-Za-z0-9_-]{11}', source['video_id'])
                or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', source['channel_id'])):
            return result('unavailable', checked_at='', expires_at='')
        key = tuple(source[key] for key in SOURCE_KEYS)
        with self._lock:
            entry = self._entries.get(key)
            if entry:
                self._entries.move_to_end(key)
                if key not in self._pending and entry['expires'] > self.clock():
                    return dict(entry['value'])
            if key in self._pending:
                return dict(entry['value'])
            pending = result('pending', checked_at=(entry or {}).get('value', {}).get('checked_at', ''), expires_at='')
            if len(self._pending) >= self.max_pending:
                return pending
            while key not in self._entries and len(self._entries) >= self.max_entries:
                evict = next((old for old in self._entries if old not in self._pending), None)
                if evict is None:
                    return pending
                del self._entries[evict]
            pending.update(state='checking', message=MESSAGES['checking'])
            self._entries[key] = {'value': pending, 'expires': 0}
            self._pending.add(key)
            try:
                self._pool.submit(self._check, key, source)
            except RuntimeError:
                self._pending.discard(key)
                self._entries.pop(key, None)
                return result('unavailable', checked_at='', expires_at='')
            return dict(pending)

    def _check(self, key, source):
        try:
            value = self.probe(source)
            state = value.get('state')
            if state not in MESSAGES or state in ('pending', 'checking'):
                raise ValueError('invalid probe result')
            # Only fixed messages and a known card type enter the public DTO.
            safe = result(state)
            if value.get('card_type') in ('player', 'summary', 'summary_large_image'):
                safe['card_type'] = value['card_type']
        except Exception:
            # Upstream exceptions may include credential-bearing request details.
            safe = result('unavailable')
        checked = self.clock()
        expires = checked + (ERROR_TTL if safe['state'] == 'unavailable' else TTL)
        safe.update(checked_at=iso(checked), expires_at=iso(expires))
        with self._lock:
            self._entries[key] = {'value': safe, 'expires': expires}
            self._pending.discard(key)

    def close(self):
        self._pool.shutdown(wait=True)
