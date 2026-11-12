"""Retirement boundaries keep supported X publishing and stored navigation usable."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.retired_modules import filter_navigation, retired_path


class RetirementBoundaryTest(unittest.TestCase):
    def test_retired_pool_and_template_operations_are_closed(self):
        for path in (
            "/api/admin/x-auto-publish/templates",
            "/api/admin/x-auto-publish/templates/12/enable",
            "/api/admin/x-auto-publish/templates/12/run-now",
            "/api/admin/x-auto-publish/runs/12",
            "/api/admin/x-posts/material-pool",
            "/api/admin/x-posts/material-pool/schedule",
            "/api/admin/x-posts/material-pool/123",
            "/x-auto-publish-templates.html",
            "/x-auto-publish-template.html?id=12",
            "/x-auto-publish-runs.html",
            "/x-post-material-pool.html",
        ):
            with self.subTest(path=path):
                self.assertTrue(retired_path(path))

    def test_manual_drama_tag_and_history_routes_stay_open(self):
        for path in (
            "/api/admin/x-posts/material-pool/manual-publish",
            "/api/admin/x-posts/material-pool/manual-runs/123",
            "/api/admin/x-posts/material-pool/preview",
            "/api/admin/x-posts/material-pool/account-options",
            "/api/admin/x-posts/drama-pool",
            "/api/admin/x-posts/drama-pool/schedule",
            "/api/admin/x-posts/drama-pool/123/priority",
            "/api/admin/x-posts/logs",
            "/api/admin/x-posts/runs",
            "/api/admin/x-tag-posts/material-pool/schedule",
            "/api/admin/x-tag-auto-publish/templates/123/enable",
            "/internal/posts/material-pool/schedule/save",
            "/api/ad-material/playable-preview",
            "/x-post-drama-pool.html",
            "/x-video-publish.html",
            "/x-tag-material-pool.html",
        ):
            with self.subTest(path=path):
                self.assertFalse(retired_path(path))

    def test_saved_navigation_cannot_restore_retired_items(self):
        original = [{"key": "x_platform", "module": "x_accounts", "items": [
            {"key": "xAutoPublishTemplates"},
            {"key": "xAutoPublishRuns"},
            {"key": "customAlias", "view": "xPostMaterialPool"},
            {"key": "oldBookmark", "href": "/x-post-material-pool.html?q=1"},
            {"key": "xTagMaterialPool", "href": "/x-tag-material-pool.html"},
            {"key": "xVideoPublish", "href": "/x-video-publish.html"},
            {"key": "xPostDramaPool", "href": "/x-post-drama-pool.html"},
            {"key": "xPostLogs", "href": "/x-post-logs.html"},
        ]}]
        before = json.dumps(original)
        result = filter_navigation(original)
        self.assertEqual([i['key'] for i in result[0]['items']], [
            'xTagMaterialPool', 'xVideoPublish', 'xPostDramaPool', 'xPostLogs'])
        self.assertEqual(json.dumps(original), before)

    def test_prior_retirement_and_meta_cleanup_are_preserved(self):
        self.assertTrue(retired_path('/api/ad-control/rules'))
        self.assertTrue(retired_path('/api/ad-material/tasks'))
        result = filter_navigation([{'key':'ad_control','items':[
            {'key':'fbPostAdDelete','module':'ad_control_center'}]}])
        self.assertEqual(result[0]['key'],'meta_asset_delete')
        self.assertEqual(result[0]['items'][0]['href'],'/fb-post-ad-delete.html')


class RetirementUiTest(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(['node'], input=source, text=True, encoding='utf-8',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_navigation_filters_server_saved_and_default_entries(self):
        result=self.node(r'''
const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync('static/quick-nav.js','utf8');
async function read(input, fail=false) {
  const context={window:{location:{pathname:'/x-post-drama-pool.html'}},
    localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
    fetch:async()=>{if(fail)throw Error('offline');return {ok:true,json:async()=>input};}};
  vm.runInNewContext(code,context);
  return (await context.window.QuickNav.loadConfig()).flatMap(g=>g.items||[]).map(i=>i.key);
}
(async()=>{const original=JSON.parse(fs.readFileSync('static/navigation.json','utf8'));
const x=original.find(g=>g.key==='x_platform');
x.items.push({key:'xAutoPublishTemplates'},{key:'xAutoPublishRuns'},
 {key:'xPostMaterialPool'},{key:'oldBookmark',href:'/x-post-material-pool.html?q=1'},
 {key:'customAlias',view:'xPostMaterialPool'});
console.log(JSON.stringify([await read(original),await read(null,true)]));})();
''')
        for keys in result:
            for retired in ('xAutoPublishTemplates','xAutoPublishRuns','xPostMaterialPool','oldBookmark','customAlias'):
                self.assertNotIn(retired,keys)
            for keep in ('xTagAutoPublishTemplates','xTagMaterialPool','xPostDramaPool','xPostLogs','xVideoPublish'):
                self.assertIn(keep,keys)

    def test_old_bookmarks_redirect_without_reusing_cross_lane_ids(self):
        result=self.node(r'''
const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync('static/x-legacy-publish-redirect.js','utf8');
const paths=['/x-auto-publish-templates.html','/x-auto-publish-template.html',
 '/x-auto-publish-runs.html','/x-post-material-pool.html',
 '/x-tag-auto-publish-template.html','/x-tag-material-pool.html','/x-post-drama-pool.html'];
console.log(JSON.stringify(paths.map(path=>{let target=null;
 vm.runInNewContext(code,{window:{location:{pathname:path,search:'?id=17',replace:v=>target=v}}});
 return target;})));
''')
        self.assertEqual(result,[
            '/x-tag-auto-publish-templates.html','/x-tag-auto-publish-templates.html',
            '/x-post-logs.html','/x-tag-material-pool.html',None,None,None])

    def test_redirect_loads_before_old_page_scripts_and_survives_tag_cloning(self):
        redirect='/x-legacy-publish-redirect.js?v=20261112retirement1'
        for name in ('x-auto-publish-templates.html','x-auto-publish-template.html',
                     'x-auto-publish-runs.html','x-post-material-pool.html'):
            source=(ROOT/'static'/name).read_text(encoding='utf-8')
            self.assertIn(redirect,source)
            self.assertLess(source.index(redirect),source.index('</head>'))
            self.assertIn(redirect,source.replace('x-auto-publish','x-tag-auto-publish')
                          .replace('x-post-material-pool','x-tag-material-pool'))

    def test_drama_page_explains_profile_assignment_and_binding_preservation(self):
        source=(ROOT/'static/x-post-drama-pool.html').read_text(encoding='utf-8')
        for expected in ('候选发布频道','受众、题材画像','无有效画像时按原顺序分配',
                         '已开播短剧继续由原频道发布','仅从已勾选频道中按标签画像匹配新剧'):
            self.assertIn(expected,source)
        self.assertIn('account_ids: selectedIds.map',source)
        self.assertNotIn('才按账号顺序领取最新上传的未绑定短剧',source)


if __name__ == '__main__':
    unittest.main()
