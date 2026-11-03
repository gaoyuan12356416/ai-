import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('private_reports', Path(__file__).parents[1] / 'features/private_reports.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Handler:
    def _cookies(self): return {'sid': 'opaque'}
    def send_response(self, status): self.status = status
    def send_header(self, key, value): pass
    def end_headers(self): pass
    def _session(self): raise AssertionError('API token path must not be used')


class Connection:
    def execute(self, *args): return self
    def fetchone(self): return ('active', 'tenant')
    def close(self): pass


class OwnerTests(unittest.TestCase):
    def run_case(self, session, enabled=True, config=True):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'owners.json'
            if config: p.write_text(json.dumps({'yuesiying': {'user_id': 'owner', 'tenant_key': 'tenant'}}))
            app = {'feishu_auth_enabled': lambda: enabled, 'load_session': lambda token: session,
                   'SESSION_COOKIE_NAME': 'sid', 'JOB_DB_LOCK': threading.Lock(),
                   'get_job_db_connection': Connection}
            h = Handler()
            with patch.dict(module.os.environ, {'PRIVATE_REPORT_OWNERS_FILE': str(p)}):
                module.authorize(h, app)
            return h.status

    def test_owner(self):
        self.assertEqual(204, self.run_case({'user_id': 'owner', 'tenant_key': 'tenant', 'source': 'feishu_web', 'expires_at': time.time()+60}))
    def test_other_user(self):
        self.assertEqual(403, self.run_case({'user_id': 'other', 'tenant_key': 'tenant', 'source': 'feishu_web', 'expires_at': time.time()+60}))
    def test_feishu_internal(self):
        self.assertEqual(204, self.run_case({'user_id': 'owner', 'tenant_key': 'tenant', 'source': 'feishu_internal', 'expires_at': time.time()+60}))
    def test_wrong_tenant(self):
        self.assertEqual(403, self.run_case({'user_id': 'owner', 'tenant_key': 'other', 'source': 'feishu_web', 'expires_at': time.time()+60}))
    def test_service_token_denied(self):
        self.assertEqual(403, self.run_case({'user_id': 'owner', 'tenant_key': 'tenant', 'source': 'api_token', 'expires_at': time.time()+60}))
    def test_logged_out(self): self.assertEqual(401, self.run_case(None))
    def test_expired(self): self.assertEqual(401, self.run_case({'expires_at': 0}))
    def test_disabled_auth(self): self.assertEqual(503, self.run_case(None, enabled=False))
    def test_missing_config(self): self.assertEqual(503, self.run_case(None, config=False))


if __name__ == '__main__': unittest.main()
