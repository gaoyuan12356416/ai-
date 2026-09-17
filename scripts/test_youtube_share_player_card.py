"""Public metadata eligibility: no platform writes or real network calls."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.x_card import inspect_player_card, MAX_BYTES
from features.x_accounts.youtube_share_text import canonical_url, preview

VIDEO = 'AbcdEFG_123'
URL = canonical_url(VIDEO)


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        for offset in range(0, len(self.body), size): yield self.body[offset:offset + size]


class PlayerCardTests(unittest.TestCase):
    def inspect(self, *, card='player', player=None, og=None, status=200, extra=''):
        html = '<head><meta property="og:url" content="%s"><meta name="twitter:card" content="%s"><meta name="twitter:player" content="%s">%s</head>' % (
            URL if og is None else og, card, 'https://www.youtube.com/embed/' + VIDEO if player is None else player, extra)
        session = Mock()
        session.get.return_value = Response(html.encode(), status)
        result = inspect_player_card(VIDEO, session_factory=lambda: session)
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertEqual(session.get.call_args.kwargs['headers'], {'User-Agent': 'Twitterbot/1.0'})
        session.close.assert_called_once()
        return result

    def test_only_matching_player_is_eligible(self):
        self.assertTrue(self.inspect()['eligible'])
        self.assertFalse(self.inspect(card='summary_large_image')['eligible'])
        self.assertFalse(self.inspect(player='https://www.youtube.com/embed/OtherID_123')['eligible'])
        self.assertFalse(self.inspect(player='https://evil.invalid/embed/' + VIDEO)['eligible'])
        self.assertFalse(self.inspect(og=URL + 'different')['eligible'])
        self.assertFalse(self.inspect(extra='<meta name="twitter:card" content="summary">')['eligible'])

    def test_redirect_missing_metadata_and_network_errors_fail_closed(self):
        self.assertFalse(self.inspect(status=302)['eligible'])
        self.assertFalse(self.inspect(card='')['eligible'])
        session = Mock()
        session.get.side_effect = requests.Timeout('do not expose transport internals')
        result = inspect_player_card(VIDEO, session_factory=lambda: session)
        self.assertFalse(result['eligible'])
        self.assertNotIn('transport', result['message'])
        session.close.assert_called_once()

    def test_response_size_is_bounded(self):
        session = Mock()
        session.get.return_value = Response(b'x' * (MAX_BYTES + 1))
        self.assertFalse(inspect_player_card(VIDEO, session_factory=lambda: session)['eligible'])

    def test_video_url_takes_precedence_without_discarding_custom_text(self):
        values = {'youtube_url': URL, 'short_url': 'https://example.invalid/promo', 'title': 'Test'}
        result = preview('{short_url}\n\n{title}', values)
        self.assertTrue(result['valid'])
        self.assertEqual(result['text'], URL + '\n\nhttps://example.invalid/promo\n\nTest')
        result = preview('{short_url}\n{youtube_url}', values)
        self.assertFalse(result['valid'])
        self.assertIn('请将 {youtube_url} 放在其他链接之前', result['errors'][0])
        self.assertTrue(preview('{title}\n{youtube_url}\n{short_url}', values)['valid'])


if __name__ == '__main__':
    unittest.main()
