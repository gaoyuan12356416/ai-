"""Offline copy/delete contracts: preserve settings, frozen work and receipts."""
import argparse
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class XTests(unittest.TestCase):
    def setUp(self):
        from scripts.test_x_auto_post_store import AutoPostStoreTests
        self.f = AutoPostStoreTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store = self.f.store

    def test_copy_keeps_config_and_starts_disabled_without_tasks(self):
        original = self.f.create_template('X original')
        enabled = self.store.set_template_enabled(original.id, True, expected_version=original.version)
        copied = self.store.copy_template(original.id, actor=self.f.actor)
        self.assertNotEqual(original.id, copied.id)
        self.assertEqual(copied.name, 'X original 副本')
        self.assertEqual(copied.config, original.config)
        self.assertEqual(copied.version, 1)
        self.assertFalse(copied.enabled)
        self.assertTrue(self.store.get_template(original.id).enabled)
        self.assertFalse(self.store.list_runs())

    def test_delete_hides_template_preserves_frozen_tasks_and_is_idempotent(self):
        from features.x_auto_posts.core import XAutoPostStoreError
        template = self.f.create_template()
        run = self.f.create_run(template, 'prior-run')
        task = self.f.create_task(run)
        before_task = self.store.get_task(task.id)
        before_run = self.store.get_run(run.id)
        result = self.store.delete_template(template.id, expected_version=template.version, actor=self.f.actor)
        self.assertTrue(result['deleted'])
        self.assertEqual(result, self.store.delete_template(template.id, expected_version=template.version))
        self.assertFalse(self.store.list_templates())
        self.assertEqual(self.store.get_task(task.id), before_task)
        self.assertEqual(self.store.get_run(run.id), before_run)
        self.assertEqual(self.store.get_template(template.id, version=template.version).config, template.config)
        for action in (lambda: self.store.get_template(template.id),
                       lambda: self.store.copy_template(template.id),
                       lambda: self.store.set_template_enabled(template.id, True, expected_version=template.version),
                       lambda: self.f.create_run(template, 'after-delete', trigger='manual')):
            with self.assertRaises(XAutoPostStoreError):
                action()

    def test_version_conflict_does_not_delete_and_other_templates_survive(self):
        from features.x_auto_posts.core import XAutoPostStoreError
        one, two = self.f.create_template('One'), self.f.create_template('Two')
        with self.assertRaises(XAutoPostStoreError):
            self.store.delete_template(one.id, expected_version=one.version + 1)
        self.assertEqual(len(self.store.list_templates()), 2)
        self.store.delete_template(one.id, expected_version=one.version)
        self.assertEqual([t.id for t in self.store.list_templates()], [two.id])

    def test_http_delete_requires_auth_and_expected_version(self):
        from features.x_auto_posts.service import XAutoPostRequestHandler, X_AUTO_ADMIN_PREFIX, AutoPostServiceError
        from scripts.test_x_auto_post_service import XAutoPostServiceTests, valid_payload
        fixture = XAutoPostServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = fixture.service()
        template = service.create_template(fixture.actor(valid_payload()))['template']
        handler = XAutoPostRequestHandler.__new__(XAutoPostRequestHandler)
        handler.server = SimpleNamespace(x_auto_service=service)
        handler.command = 'POST'
        handler.path = X_AUTO_ADMIN_PREFIX + '/templates/' + str(template['id']) + '/delete'
        handler._body = lambda: fixture.actor({'expected_version': template['version']})
        handler._authorized = lambda: False
        self.assertEqual(handler._dispatch()[0], 403)
        self.assertTrue(service.store.list_templates())
        handler._authorized = lambda: True
        handler._body = lambda: fixture.actor({'expected_version': template['version'], 'extra': True})
        with self.assertRaises(AutoPostServiceError): handler._dispatch()
        handler._body = lambda: fixture.actor({'expected_version': template['version']})
        self.assertEqual(handler._dispatch(), (200, {'ok': True, 'template_id': template['id'], 'deleted': True}))


class FBTests(unittest.TestCase):
    def setUp(self):
        from scripts.test_fb_auto_store import StoreTests
        self.f = StoreTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.store = self.f.store

    def test_copy_preserves_source_config_owner_scope_and_leaves_original(self):
        source = self.f.enable()
        copied = self.store.copy_template(source['id'], self.f.actor, source['version'])
        self.assertNotEqual(source['id'], copied['id'])
        self.assertEqual(copied['name'], source['name'] + ' 副本')
        self.assertEqual({**copied['config'], 'name': source['config']['name']}, source['config'])
        self.assertEqual(copied['status'], 'disabled')
        self.assertEqual(copied['version'], 1)
        self.assertEqual(copied['owner_user_id'], self.f.actor.owner_user_id)
        self.assertEqual(self.store.get_template(source['id'], self.f.actor)['status'], 'enabled')
        self.assertEqual(self.store.list_runs(self.f.actor)['total'], 0)

    def test_delete_retains_runs_and_tasks_but_prevents_future_activation(self):
        from scripts.test_fb_auto_store import Pages, Materials
        from features.fb_auto_posts.core import StoreError
        original = self.f.template
        run = self.store.create_run(original['id'], 'manual:prior', 'manual', self.f.actor, Pages(), Materials())
        before = self.store.get_run(run['run_id'], self.f.actor)
        result = self.store.delete_template(original['id'], self.f.actor, original['version'])
        self.assertTrue(result['deleted'])
        self.assertEqual(result, self.store.delete_template(original['id'], self.f.actor, original['version']))
        self.assertEqual(self.store.list_templates(self.f.actor)['total'], 0)
        self.assertEqual(self.store.get_run(run['run_id'], self.f.actor), before)
        for action in (lambda: self.store.get_template(original['id'], self.f.actor),
                       lambda: self.store.copy_template(original['id'], self.f.actor, original['version']),
                       lambda: self.store.set_template_status(original['id'], True, self.f.actor, original['version'])):
            with self.assertRaises(StoreError):
                action()

    def test_scope_and_version_are_checked_before_mutation(self):
        from features.fb_auto_posts.core import ActorScope, StoreError
        original = self.f.template
        stranger = ActorScope('someone', 'Someone', False, 'other-owner')
        for actor, version in ((stranger, original['version']), (self.f.actor, original['version'] + 1)):
            for method in (self.store.copy_template, self.store.delete_template):
                with self.assertRaises(StoreError):
                    method(original['id'], actor, version)
        self.assertEqual(self.store.list_templates(self.f.actor)['total'], 1)

    def test_http_copy_delete_require_auth_and_allowlisted_fields(self):
        from features.fb_auto_posts.service import Handler, Runtime, ServiceError, FB_AUTO_ADMIN_PREFIX
        from features.fb_auto_posts.client import route_allowed
        handler = Handler.__new__(Handler)
        handler.runtime = SimpleNamespace(store=self.store, internal_token='offline-test', actor=lambda body: self.f.actor)
        handler.command = 'POST'
        response = []
        handler.send_json = lambda status, result: response.append((status, result))
        handler.read_json = lambda: {'expected_version': self.f.template['version']}
        handler.path = FB_AUTO_ADMIN_PREFIX + '/templates/' + str(self.f.template['id']) + '/copy'
        self.assertTrue(route_allowed('POST', handler.path))
        self.assertFalse(route_allowed('GET', handler.path))
        handler.headers = {}
        with self.assertRaises(ServiceError): handler.dispatch()
        handler.headers = {'Authorization': 'Bearer offline-test'}
        handler.read_json = lambda: {'expected_version': 1, 'extra': True}
        with self.assertRaises(ServiceError): handler.dispatch()
        handler.read_json = lambda: {'expected_version': self.f.template['version']}
        handler.dispatch()
        self.assertEqual(response[-1][0], 200)
        copied = response[-1][1]['template']
        self.assertEqual(copied['status'], 'disabled')
        handler.path = FB_AUTO_ADMIN_PREFIX + '/templates/' + str(copied['id']) + '/delete'
        self.assertTrue(route_allowed('POST', handler.path))
        handler.read_json = lambda: {'expected_version': copied['version']}
        handler.dispatch()
        self.assertTrue(response[-1][1]['deleted'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=['x', 'fb'], required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(XTests if args.platform == 'x' else FBTests)
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
