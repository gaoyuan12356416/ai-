import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts import deploy_video_editing_cpu as deploy


class Reply(io.BytesIO):
    status = 200


class CPUDeploymentHealthTests(unittest.TestCase):
    def test_legacy_health_alone_cannot_accept_missing_editor_dispatch(self):
        def request(url, **kwargs):
            if url.endswith('/api/auth/status'):
                return Reply(b'{"authenticated":false}')
            raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b'{"error":"not_found"}'))
        with patch.object(deploy.urllib.request, 'urlopen', side_effect=request), patch.object(deploy.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'health verification failed'):
                deploy.healthy()

    def test_new_route_requires_authenticated_gate_response(self):
        def request(url, **kwargs):
            if url.endswith('/api/auth/status'):
                return Reply(b'{"authenticated":false}')
            raise HTTPError(url, 401, 'Unauthorized', {}, io.BytesIO(b'{"error":"login_required"}'))
        with patch.object(deploy.urllib.request, 'urlopen', side_effect=request):
            deploy.healthy()

    def test_rollback_checks_old_health_without_requiring_new_route(self):
        with patch.object(deploy.urllib.request, 'urlopen', return_value=Reply(b'{}')) as request:
            deploy.healthy(require_editor=False)
            self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
