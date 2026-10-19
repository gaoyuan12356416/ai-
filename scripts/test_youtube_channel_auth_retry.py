"""Regression tests for refreshed-token channel reads; no platform writes."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_synthesis.youtube import YouTubeCredential, YouTubeHTTPClient, YouTubeHTTPError
from features.youtube_auto_publish.channels import ChannelProbe

CRED = YouTubeCredential(account_id='1', channel_local_id='2', channel_id='UC' + 'a' * 22,
    channel_name='Example', channel_status=1, scopes=frozenset({'youtube.force-ssl'}),
    refresh_token='SECRET_REFRESH', client_id='SECRET_CLIENT', client_secret='SECRET_PASSWORD')


def response(status, payload):
    return Mock(status_code=status, json=Mock(return_value=payload))


def error(status=401, reason='authError', domain='global'):
    return response(status, {'error': {'message': 'SECRET_UPSTREAM_TEXT',
        'errors': [{'domain': domain, 'reason': reason}]}})


def allowed(channel=CRED.channel_id, long_status='allowed'):
    return response(200, {'items': [{'id': channel, 'status': {'longUploadsStatus': long_status}}]})


class ChannelAuthRetryTests(unittest.TestCase):
    def session(self, replies):
        session = Mock()
        session.post.return_value = response(200, {'access_token': 'SECRET_ACCESS'})
        session.get.side_effect = replies
        return session

    def probe(self, session):
        with patch('features.drama_synthesis.youtube.time.sleep') as sleep:
            result = ChannelProbe(lambda: session)(CRED)
        self.assertNotIn('SECRET', json.dumps(result))
        return result, sleep

    def test_transient_401_recovers_with_same_token_and_one_refresh(self):
        session = self.session([error(), error(), allowed()])
        result, sleep = self.probe(session)
        self.assertTrue(result['eligible'])
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])
        self.assertTrue(all(c.kwargs['headers'] == {'Authorization': 'Bearer SECRET_ACCESS'}
            and c.kwargs['allow_redirects'] is False for c in session.get.call_args_list))
        session.close.assert_called_once()

    def test_exhausted_401_is_unknown_and_never_eligible(self):
        session = self.session([error() for _ in range(8)])
        result, sleep = self.probe(session)
        self.assertEqual(result['auth_status'], 'unknown')
        self.assertFalse(result['eligible'])
        self.assertEqual(result['auth_error_code'], 'authError')
        self.assertNotIn('重新鉴权', result['reason'])
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.get.call_count, 8)
        self.assertEqual(sleep.call_args_list, [call(1), call(2), call(4)] * 2)

    def test_replaces_rejected_access_token_once_and_recovers(self):
        session = self.session([error() for _ in range(4)] + [allowed()])
        session.post.side_effect = [response(200, {'access_token': 'SECRET_FIRST'}),
                                   response(200, {'access_token': 'SECRET_SECOND'})]
        result, _ = self.probe(session)
        self.assertTrue(result['eligible'])
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.get.call_count, 5)
        self.assertEqual(session.get.call_args.kwargs['headers']['Authorization'], 'Bearer SECRET_SECOND')

    def test_replacement_token_scope_loss_or_revocation_remains_blocked(self):
        for replacement in (response(200, {'access_token': 'SECRET_SECOND', 'scope': 'youtube.readonly'}),
                            response(400, {'error': 'invalid_grant'})):
            session = self.session([error() for _ in range(4)])
            session.post.side_effect = [response(200, {'access_token': 'SECRET_FIRST'}), replacement]
            result, _ = self.probe(session)
            self.assertFalse(result['eligible'])
            self.assertEqual(result['auth_status'], 'blocked')
            self.assertEqual(session.get.call_count, 4)

    def test_recovery_still_checks_identity_and_long_video_eligibility(self):
        for success in (allowed(channel='different'), allowed(long_status='eligible')):
            with self.subTest(success=success):
                result, _ = self.probe(self.session([error(), success]))
                self.assertFalse(result['eligible'])
                self.assertEqual(result['auth_status'], 'blocked')

    def test_permission_and_quota_403_are_distinct_and_not_retried(self):
        for reason, expected in [('insufficientPermissions', 'blocked'), ('quotaExceeded', 'unknown')]:
            session = self.session([error(403, reason)])
            result, sleep = self.probe(session)
            self.assertEqual(result['auth_status'], expected)
            self.assertFalse(result['eligible'])
            self.assertEqual(session.get.call_count, 1)
            sleep.assert_not_called()

    def test_unrecognized_401_and_malformed_errors_do_not_retry(self):
        for reply in (error(reason='required'), error(domain='other'),
                      response(401, {'error': {'errors': 'authError'}}),
                      response(401, {'error': {'errors': []}})):
            session = self.session([reply])
            result, sleep = self.probe(session)
            self.assertFalse(result['eligible'])
            self.assertEqual(session.get.call_count, 1)
            sleep.assert_not_called()

    def test_revoked_refresh_is_blocked_without_channel_request(self):
        session = self.session([])
        session.post.return_value = response(400, {'error': 'invalid_grant'})
        result, sleep = self.probe(session)
        self.assertEqual(result['auth_status'], 'blocked')
        session.get.assert_not_called()
        sleep.assert_not_called()

    def test_publish_identity_read_recovers_without_writes(self):
        session = self.session([error(), allowed()])
        with patch('features.drama_synthesis.youtube.time.sleep'):
            YouTubeHTTPClient(session_factory=lambda: session).verify_channel_identity('SECRET_ACCESS', CRED.channel_id)
        session.post.assert_not_called()
        session.put.assert_not_called()
        self.assertEqual(session.get.call_count, 2)

    def test_publish_identity_exhaustion_is_retryable_before_writes(self):
        session = self.session([error() for _ in range(4)])
        with patch('features.drama_synthesis.youtube.time.sleep'), self.assertRaises(YouTubeHTTPError) as raised:
            YouTubeHTTPClient(session_factory=lambda: session).verify_channel_identity('SECRET_ACCESS', CRED.channel_id)
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(raised.exception.unknown)
        self.assertNotIn('SECRET', str(raised.exception))
        session.post.assert_not_called()
        session.put.assert_not_called()

    def test_recovered_publish_identity_mismatch_remains_blocked(self):
        session = self.session([error(), allowed(channel='different')])
        with patch('features.drama_synthesis.youtube.time.sleep'), self.assertRaises(YouTubeHTTPError) as raised:
            YouTubeHTTPClient(session_factory=lambda: session).verify_channel_identity('SECRET_ACCESS', CRED.channel_id)
        self.assertEqual(raised.exception.code, 'youtube_channel_identity_mismatch')
        self.assertFalse(raised.exception.retryable)


if __name__ == '__main__':
    unittest.main()
