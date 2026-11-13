"""Read YouTube's public card metadata before permitting a player-card share."""
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

from features.x_accounts.youtube_share_text import canonical_url

MAX_BYTES = 2 * 1024 * 1024


class CardMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.conflict = False

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        attrs = dict(attrs)
        key = attrs.get('name') or attrs.get('property')
        if key in ('twitter:card', 'twitter:player', 'og:url'):
            value = attrs.get('content') or ''
            if key in self.values and self.values[key] != value:
                self.conflict = True
            self.values[key] = value


def inspect_player_card(video_id, *, session_factory=requests.Session):
    url = canonical_url(video_id)
    result = {'eligible': False, 'state': 'unavailable', 'card_type': '',
              'message': '暂时无法确认 YouTube 播放卡片，请重新检查后再转发。',
              'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    session = session_factory()
    session.trust_env = False
    try:
        # Fixed public host/URL, no credentials, redirects or user-supplied URLs.
        with session.get(url, headers={'User-Agent': 'Twitterbot/1.0'},
                         timeout=(5, 12), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return result
            data = bytearray()
            for part in response.iter_content(32 * 1024):
                data.extend(part)
                if len(data) > MAX_BYTES:
                    return result
                if b'</head>' in data:
                    break
        parser = CardMetadata()
        parser.feed(data.decode('utf-8', 'replace'))
        tags = parser.values
        if parser.conflict or tags.get('og:url') != url or not tags.get('twitter:card'):
            return result
        result['card_type'] = tags['twitter:card']
        if tags['twitter:card'] != 'player':
            result.update(state='not_player', message='此视频目前未提供 YouTube 播放卡片，已阻止转发。请稍后重新检查或选择其他视频。')
        elif tags.get('twitter:player') != 'https://www.youtube.com/embed/' + video_id:
            result.update(state='invalid_player', message='YouTube 播放器与当前视频不一致，暂不能转发。')
        else:
            result.update(eligible=True, state='ready', message='已确认 YouTube 提供此视频的播放卡片。')
        return result
    except (requests.RequestException, ValueError, UnicodeError):
        return result
    finally:
        session.close()
