"""Send start/end notices with durable, non-replayable uncertain outcomes."""
import json
import threading
from pathlib import Path
from . import feishu_transport
from .runtime import RuntimeLedgerError

_LOCK = threading.Lock()
_RECIPIENT = 'ou_62fed22c4918524e48ded4eab8b82b1e'


class Notifier:
    def __init__(self, runtime, config_path):
        self.runtime = runtime
        self.path = Path(config_path)

    def _config(self):
        config = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(config, dict) or config.get('receive_id_type') != 'open_id'
                or config.get('receive_id') != _RECIPIENT):
            raise ValueError('unexpected_notification_recipient')
        for key in ('app_id', 'app_secret'):
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise ValueError('notification_credential_missing')
        return config

    def send(self, run_id, phase, body):
        notice = self.runtime.claim_notice(run_id, phase, body)
        if notice['status'] == 'sent':
            return json.loads(notice['receipt'])
        if notice['status'] in ('sending', 'unknown'):
            raise RuntimeError('notification_unknown_requires_reconciliation')
        # Claim before loading config so multiple workers cannot send the phase.
        notice = self.runtime.start_notice(run_id, phase)
        if not notice['claimed']:
            if notice['status'] == 'sent':
                return json.loads(notice['receipt'])
            raise RuntimeError('notification_unknown_requires_reconciliation')
        message_attempted = False
        try:
            config = self._config()
            with _LOCK:
                old_app_id = feishu_transport.APP_ID
                old_secret = feishu_transport.APP_SECRET
                original = feishu_transport.post_json
                feishu_transport.APP_ID = config['app_id']
                feishu_transport.APP_SECRET = config['app_secret']

                def post(url, payload, token=None):
                    nonlocal message_attempted
                    if '/im/v1/messages?' in url:
                        # The durable sending state exists before network I/O.
                        message_attempted = True
                        payload = dict(payload, uuid=notice['uuid'])
                    return original(url, payload, token)

                feishu_transport.post_json = post
                try:
                    response = feishu_transport.send_text(
                        config['receive_id_type'], config['receive_id'], notice['body'])
                finally:
                    feishu_transport.post_json = original
                    feishu_transport.APP_ID = old_app_id
                    feishu_transport.APP_SECRET = old_secret
            code = response.get('code') if isinstance(response, dict) else None
            data = response.get('data') if isinstance(response, dict) else None
            mid = data.get('message_id') if isinstance(data, dict) else None
            if isinstance(code, int) and not isinstance(code, bool) and code != 0:
                self.runtime.finish_notice(run_id, phase, 'failed', {'code': code})
                raise RuntimeError('notification_rejected')
            if code != 0 or isinstance(code, bool) or not isinstance(mid, str) or not mid:
                # Missing receipt after a possible accepted request is unknown.
                self.runtime.finish_notice(run_id, phase, 'unknown',
                                           {'reason': 'invalid_message_receipt'})
                raise RuntimeError('notification_receipt_unknown')
            receipt = {'code': 0, 'message_id': mid, 'uuid': notice['uuid']}
            self.runtime.finish_notice(run_id, phase, 'sent', receipt)
            return receipt
        except RuntimeLedgerError:
            # Do not mask a failed receipt commit with another attempted write.
            raise
        except BaseException as exc:
            current = self.runtime.notice(run_id, phase)
            if current and current['status'] == 'sending':
                self.runtime.finish_notice(
                    run_id, phase, 'unknown' if message_attempted else 'failed',
                    {'exception_type': type(exc).__name__, 'request_sent': message_attempted})
            if isinstance(exc, (KeyboardInterrupt, SystemExit, InterruptedError)):
                raise
            raise RuntimeError('notification_not_confirmed') from None
