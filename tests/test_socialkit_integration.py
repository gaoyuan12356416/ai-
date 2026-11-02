import ast
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from features.socialkit_analytics.navigation import ensure_navigation

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('socialkit_integrate', ROOT/'ops/socialkit-analytics/integrate.py')
integrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integrate)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        source = ast.parse((ROOT/'app.py').read_text(encoding='utf-8'))
        actor = next(node for node in ast.walk(source) if isinstance(node, ast.FunctionDef) and node.name == '_socialkit_analytics_actor')
        access = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'navigation_item_access')
        self.session = dict(tenant_key='t', user_id='u', role='user', permissions={'socialkit_analytics': True})
        self.responses = []
        self.nav = ensure_navigation([dict(key='social_reports', module='social_analytics', items=[dict(key='oldReport')])])
        namespace = dict(SESSION_COOKIE_NAME='session', load_session=lambda cookie:self.session,
                         has_module_permission=lambda session,key:session.get('role')=='admin' or session.get('permissions',{}).get(key,False),
                         load_navigation_config=lambda:self.nav,
                         json_response=lambda handler,status,payload,**kw:self.responses.append((status,payload)))
        module = ast.Module(body=[access,actor],type_ignores=[])
        exec(compile(module,'<isolated-live-auth-functions>','exec'),namespace)
        self.actor=namespace['_socialkit_analytics_actor']
        self.access=namespace['navigation_item_access']
        self.handler=type('Handler',(),{'_cookies':lambda _: {'session':'opaque'}})()

    def test_independent_module_does_not_require_old_module(self):
        self.assertEqual(self.actor(self.handler)['user_id'],'u')
        self.assertFalse(self.access(self.session,'oldReport',self.nav)['allowed'])

    def test_anonymous_token_and_ungranted(self):
        for session,status in [(None,401),(dict(self.session,auth_type='api_token'),401),(dict(self.session,permissions={}),403)]:
            self.session=session
            self.assertIsNone(self.actor(self.handler));self.assertEqual(self.responses[-1][0],status)

    def test_disabled_navigation_remains_disabled(self):
        self.nav[0]['enabled']=False
        self.assertIsNone(self.actor(self.handler));self.assertEqual(self.responses[-1][0],403)

    def test_saved_item_acl_is_preserved(self):
        self.nav[0]['items'][-1]['adminOnly']=True
        again=ensure_navigation(self.nav)
        self.assertEqual(again,self.nav)
        self.assertFalse(self.access(self.session,'socialkitAnalytics',again)['allowed'])

    def test_old_item_keeps_both_group_and_explicit_module_requirements(self):
        self.session['permissions']['youtube_auto_publish'] = True
        original = [dict(key='social_reports', module='social_analytics',
                         items=[dict(key='customOldReport', module='youtube_auto_publish')])]
        self.assertFalse(self.access(self.session, 'customOldReport', original)['allowed'])
        normalized = ensure_navigation(original)
        self.assertFalse(self.access(self.session, 'customOldReport', normalized)['allowed'])

    def test_integration_is_idempotent(self):
        app=(ROOT/'app.py').read_text(encoding='utf-8')
        nav=(ROOT/'static/quick-nav.js').read_text(encoding='utf-8')
        self.assertEqual(integrate.patch_app(app),app)
        self.assertEqual(integrate.patch_nav(nav),nav)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the browser navigation parity check')
    def test_browser_fallback_preserves_backend_authorization(self):
        source = (ROOT/'static/quick-nav.js').read_text(encoding='utf-8')
        # Execute the actual SocialKit migration block, before unrelated legacy migrations.
        block = source.split('    // SocialKit has its own module;', 1)[1]
        block = '    // SocialKit has its own module;' + block.split('    // Migrate both saved server navigation', 1)[0]
        cases = [
            [dict(key='social_reports', module='social_analytics', items=[dict(key='oldReport')])],
            [dict(key='social_reports', module='social_analytics', items=[dict(key='customOldReport', module='youtube_auto_publish')])],
            [dict(key='social_reports', module='youtube_auto_publish', items=[dict(key='oldReport')])],
            [dict(key='custom', adminOnly=True, items=[dict(key='socialkitAnalytics', module='socialkit_analytics')]),
             dict(key='social_reports', module='social_analytics', items=[dict(key='oldReport')])],
        ]
        script = 'const cases=JSON.parse(process.argv[1]); const result=cases.map(config=>{const normalized=JSON.parse(JSON.stringify(config));\n'+block+'\nreturn normalized;});process.stdout.write(JSON.stringify(result));'
        outputs = json.loads(subprocess.check_output(['node','-e',script,json.dumps(cases)],text=True,encoding='utf-8'))
        for original, frontend in zip(cases, outputs):
            backend = ensure_navigation(original)
            self.assertEqual(frontend, backend)
            for permissions in ({}, {'socialkit_analytics':True}, {'youtube_auto_publish':True}, {'social_analytics':True}, {'social_analytics':True,'youtube_auto_publish':True}):
                session = dict(self.session,permissions=permissions)
                for group in original:
                    for item in group['items']:
                        self.assertEqual(self.access(session,item['key'],original)['allowed'],self.access(session,item['key'],frontend)['allowed'])


if __name__=='__main__':unittest.main()
