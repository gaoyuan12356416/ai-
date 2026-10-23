"""Send start/end notices with durable, non-replayable uncertain outcomes."""
import json
import threading
from pathlib import Path
from . import feishu_transport
from .runtime import RuntimeLedgerError, digest

_LOCK = threading.Lock()
_RECIPIENT = 'ou_62fed22c4918524e48ded4eab8b82b1e'
MAX_MESSAGE_BYTES = 100000


def _payload_size(text, uuid='0' * 32):
    return len(feishu_transport.encode_payload(feishu_transport.text_payload(_RECIPIENT, text, uuid)))


def _split_body(body):
    """Preserve every character; measure the real two-level JSON wire format."""
    if _payload_size(body) <= MAX_MESSAGE_BYTES:
        return [body]
    segments = []
    offset = 0
    # Header width is reserved before the final part count is known.
    reserve = '（播报分片 999999/999999）\n'
    while offset < len(body):
        low, high = 0, min(len(body) - offset, MAX_MESSAGE_BYTES)
        while low < high:
            middle = (low + high + 1) // 2
            if _payload_size(reserve + body[offset:offset + middle]) <= MAX_MESSAGE_BYTES:
                low = middle
            else:
                high = middle - 1
        if low == 0:
            raise ValueError('notification_part_cannot_fit')
        length = low
        if offset + length < len(body):
            newline = body.rfind('\n', offset, offset + length)
            if newline >= offset:
                length = newline - offset + 1
        segments.append(body[offset:offset + length])
        offset += length
        if len(segments) > 999999:
            raise ValueError('notification_part_count_exceeded')
    count = len(segments)
    return ['（播报分片 %d/%d）\n%s' % (index, count, value)
            for index, value in enumerate(segments, 1)]


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
        saved = json.loads(notice['receipt'])
        if saved.get('delivery') == 'multipart':
            parts = saved['parts']
            # Reuse the stored plan/body across retries and code releases.
            self.runtime._check_notice_parts(run_id, phase, saved)
        else:
            texts = _split_body(notice['body'])
            if len(texts) == 1:
                return self._send_single(run_id, phase, notice['body'])
            if '-part-' in phase:
                raise ValueError('nested_notification_parts_forbidden')
            parts = []
            for index, text in enumerate(texts, 1):
                child_phase = phase + '-part-' + str(index).zfill(3)
                child = self.runtime.claim_notice(run_id, child_phase, text)
                if child['body'] != text:
                    raise ValueError('multipart_notice_body_mismatch')
                parts.append({'phase': child_phase, 'body_sha256': digest(text)})
        plan = {'delivery': 'multipart', 'parts': [
            {'phase': part['phase'], 'body_sha256': part['body_sha256']} for part in parts]}
        notice = self.runtime.start_notice(run_id, phase, receipt=plan)
        if not notice['claimed']:
            if notice['status'] == 'sent':
                return json.loads(notice['receipt'])
            raise RuntimeError('notification_unknown_requires_reconciliation')
        try:
            receipts = []
            for part in plan['parts']:
                child = self.runtime.notice(run_id, part['phase'])
                receipt = self._send_single(run_id, part['phase'], child['body'])
                receipts.append(dict(part, receipt=receipt))
            receipt = {'code': 0, 'delivery': 'multipart', 'part_count': len(receipts),
                       'uuid': notice['uuid'], 'parts': receipts}
            self.runtime.finish_notice(run_id, phase, 'sent', receipt)
            return receipt
        except RuntimeLedgerError:
            raise
        except BaseException as exc:
            states = [self.runtime.notice(run_id, part['phase'])['status'] for part in plan['parts']]
            status = 'unknown' if any(value in ('sending', 'unknown') for value in states) else 'failed'
            self.runtime.finish_notice(run_id, phase, status,
                                       dict(plan, reason='multipart_not_confirmed'))
            if isinstance(exc, (KeyboardInterrupt, SystemExit, InterruptedError)):
                raise
            raise RuntimeError('notification_not_confirmed') from None

    def _send_single(self, run_id, phase, body):
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
                        payload = dict(payload, uuid=notice['uuid'])
                        if len(feishu_transport.encode_payload(payload)) > MAX_MESSAGE_BYTES:
                            raise ValueError('notification_payload_too_large')
                        # The durable sending state exists before network I/O.
                        message_attempted = True
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
        except feishu_transport.FeishuRejected as exc:
            self.runtime.finish_notice(run_id, phase, 'failed',
                                       {'code': exc.code, 'http_status': exc.http_status})
            raise RuntimeError('notification_not_confirmed') from None
        except BaseException as exc:
            current = self.runtime.notice(run_id, phase)
            if current and current['status'] == 'sending':
                self.runtime.finish_notice(
                    run_id, phase, 'unknown' if message_attempted else 'failed',
                    {'exception_type': type(exc).__name__, 'request_sent': message_attempted})
            if isinstance(exc, (KeyboardInterrupt, SystemExit, InterruptedError)):
                raise
            raise RuntimeError('notification_not_confirmed') from None
