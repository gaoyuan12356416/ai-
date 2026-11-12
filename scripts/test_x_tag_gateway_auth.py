"""Profile routes remain usable after ordinary X retirement, with current ACLs."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.x_tag_publish import gateway


class ProfileGatewayAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.nav_path = Path(self.temp.name) / 'navigation.json'
        self.env = patch.dict(os.environ, DRAMA_NAVIGATION_CONFIG_PATH=str(self.nav_path),
                              X_AUTO_POST_INTERNAL_TOKEN='test-token')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.user = dict(user_id='owner', tenant_key='tenant', name='Owner', email='',
                         role='user', permissions={'x_accounts': True})
        self.nav = [{'key': 'x_platform', 'module': 'x_accounts', 'items': [
            {'key': gateway.PROFILE_TEMPLATE_NAV, 'module': 'x_accounts'},
            {'key': gateway.PROFILE_POOL_NAV, 'module': 'x_accounts'},
        ]}]
        self.save_nav()

    def save_nav(self):
        self.nav_path.write_text(json.dumps(self.nav), encoding='utf-8')

    def auth_rpc(self, base, path, *args, **kwargs):
        if base == 'http://127.0.0.1:8787':
            if path != '/api/auth/status':
                raise gateway.Error('feature_retired', '该功能已下线', 410)
            self.assertEqual(kwargs['cookie'], 'valid-cookie')
            return {'authenticated': True, 'user': copy.deepcopy(self.user)}
        self.assertEqual(base, 'http://127.0.0.1:18843')
        return {'items': [{'id': 123}], 'total': 1}

    def handler(self, path, method='GET', body=None):
        handler = object.__new__(gateway.Handler)
        handler.path, handler.command = path, method
        handler.headers = {'Cookie': 'valid-cookie', 'Origin': 'https://ai.yingliangads.com'}
        handler.body = lambda: body or {}
        return handler

    def test_template_and_pool_work_when_all_ordinary_probes_return_410(self):
        for path in (gateway.AUTO + '/templates?page=1', gateway.AUTO + '/runs?limit=1',
                     gateway.AUTO + '/accounts', gateway.POST + '/material-pool?page=1',
                     gateway.POST + '/material-pool/schedule',
                     gateway.POST + '/material-pool/account-options'):
            with self.subTest(path=path), patch.object(gateway, 'rpc', side_effect=self.auth_rpc) as calls, \
                 patch.object(gateway, 'post_rpc', return_value={'items': [{'id': 456}], 'total': 1}):
                status, payload = self.handler(path).dispatch()
                self.assertEqual(status, 200)
                self.assertEqual(payload['total'], 1)
                main_paths = [c.args[1] for c in calls.call_args_list if c.args[0].endswith(':8787')]
                self.assertEqual(main_paths, ['/api/auth/status'])

    def test_write_still_requires_profile_acl_before_any_internal_call(self):
        self.nav[0]['items'][1]['enabled'] = False
        self.save_nav()
        with patch.object(gateway, 'rpc', side_effect=self.auth_rpc), \
             patch.object(gateway, 'post_rpc') as post:
            with self.assertRaises(gateway.Error) as error:
                self.handler(gateway.POST + '/material-pool/schedule', 'PUT', {'enabled': True}).dispatch()
        self.assertEqual(error.exception.status, 403)
        post.assert_not_called()

    def test_permissions_and_navigation_are_rechecked_on_every_request(self):
        with patch.object(gateway, 'rpc', side_effect=self.auth_rpc):
            self.assertEqual(gateway.authorize('valid-cookie', gateway.PROFILE_POOL_NAV)['user_id'], 'owner')
            self.user['permissions']['x_accounts'] = False
            with self.assertRaises(gateway.Error) as error:
                gateway.authorize('valid-cookie', gateway.PROFILE_POOL_NAV)
            self.assertEqual(error.exception.code, 'permission_denied')
            self.user['permissions']['x_accounts'] = True
            self.nav[0]['items'][1]['enabled'] = False
            self.save_nav()
            with self.assertRaises(gateway.Error) as error:
                gateway.authorize('valid-cookie', gateway.PROFILE_POOL_NAV)
            self.assertEqual(error.exception.code, 'navigation_item_unavailable')

    def test_group_and_item_permissions_and_admin_rules_match_main(self):
        for level in ('group', 'item'):
            for field, value, code in (('enabled', False, 'navigation_item_unavailable'),
                                       ('adminOnly', True, 'admin_required'),
                                       ('module', 'other_module', 'permission_denied')):
                with self.subTest(level=level, field=field):
                    node = self.nav[0] if level == 'group' else self.nav[0]['items'][0]
                    original = dict(node)
                    node[field] = value
                    self.save_nav()
                    with self.assertRaises(gateway.Error) as error:
                        gateway.require_navigation(self.user, gateway.PROFILE_TEMPLATE_NAV)
                    self.assertEqual(error.exception.code, code)
                    node.clear()
                    node.update(original)
        self.save_nav()

    def test_admin_cannot_reenable_missing_or_disabled_page(self):
        self.user['role'] = 'admin'
        self.user['permissions'] = {}
        gateway.require_navigation(self.user, gateway.PROFILE_TEMPLATE_NAV)
        self.nav[0]['enabled'] = False
        self.save_nav()
        with self.assertRaises(gateway.Error):
            gateway.require_navigation(self.user, gateway.PROFILE_TEMPLATE_NAV)
        self.nav = []
        self.save_nav()
        with self.assertRaises(gateway.Error):
            gateway.require_navigation(self.user, gateway.PROFILE_TEMPLATE_NAV)

    def test_profile_summary_accepts_either_current_profile_menu(self):
        for keep in (gateway.PROFILE_TEMPLATE_NAV, gateway.PROFILE_POOL_NAV):
            self.nav[0]['items'] = [{'key': keep}]
            self.save_nav()
            gateway.require_navigation(self.user, (gateway.PROFILE_POOL_NAV, gateway.PROFILE_TEMPLATE_NAV))
        self.nav[0]['items'] = [{'key': 'xPostMaterialPool'}, {'key': 'xAutoPublishTemplates'}]
        self.save_nav()
        with self.assertRaises(gateway.Error):
            gateway.require_navigation(self.user, (gateway.PROFILE_POOL_NAV, gateway.PROFILE_TEMPLATE_NAV))

    def test_no_cookie_or_missing_identity_never_reads_navigation(self):
        with patch.object(gateway, 'require_navigation') as nav, patch.object(gateway, 'rpc') as rpc:
            for cookie in ('', 'a' * 16385):
                with self.assertRaises(gateway.Error) as error:
                    gateway.authorize(cookie, gateway.PROFILE_TEMPLATE_NAV)
                self.assertEqual(error.exception.status, 401)
            rpc.assert_not_called()
            for user in ({}, {'user_id': 'u'}, {'tenant_key': 't'}):
                rpc.return_value = {'authenticated': True, 'user': user}
                with self.assertRaises(gateway.Error) as error:
                    gateway.authorize('valid-cookie', gateway.PROFILE_TEMPLATE_NAV)
                self.assertEqual(error.exception.status, 401)
            nav.assert_not_called()

    def test_missing_or_invalid_navigation_fails_closed(self):
        for value in ('{', '{}', 'null'):
            self.nav_path.write_text(value, encoding='utf-8')
            with self.assertRaises(gateway.Error) as error:
                gateway.require_navigation(self.user, gateway.PROFILE_TEMPLATE_NAV)
            self.assertEqual(error.exception.status, 503)
        self.nav_path.unlink()
        with self.assertRaises(gateway.Error) as error:
            gateway.require_navigation(self.user, gateway.PROFILE_TEMPLATE_NAV)
        self.assertEqual(error.exception.status, 503)

    def test_history_retains_existing_supported_route_permission_check(self):
        with patch.object(gateway, 'rpc', side_effect=[{'authenticated': True, 'user': self.user},
                 gateway.Error('admin_required', '', 403)]) as rpc:
            with self.assertRaises(gateway.Error) as error:
                self.handler(gateway.POST + '/logs').dispatch()
        self.assertEqual(error.exception.code, 'admin_required')
        self.assertEqual(rpc.call_args.args[1], '/api/admin/x-posts/logs?page_size=1')


if __name__ == '__main__':
    unittest.main()
