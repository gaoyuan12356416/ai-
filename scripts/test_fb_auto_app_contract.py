import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_permission_navigation_and_cookie_proxy_are_wired(self):
        app=(ROOT/"app.py").read_text(encoding="utf-8"); nav=json.loads((ROOT/"static/navigation.json").read_text(encoding="utf-8")); quick=(ROOT/"static/quick-nav.js").read_text(encoding="utf-8")
        self.assertIn('"fb_page_posts": "Facebook Page 自动发布"',app); self.assertIn('"fb_page_posts": False',app)
        self.assertIn("fb_auto_post_actor_scope(self._session())",app); self.assertIn('"fbAutoPublishTemplates"',app)
        group=next(x for x in nav if x["key"]=="facebook_platform"); self.assertEqual({x["key"] for x in group["items"]},{"fbAutoPublishTemplates","fbAutoPublishRuns"}); self.assertIn("fbAutoPublishRuns",quick)

    def test_pages_use_shared_shell_and_no_token_terms(self):
        for name in ("fb-auto-publish-templates.html","fb-auto-publish-runs.html"):
            text=(ROOT/"static"/name).read_text(encoding="utf-8")
            self.assertIn('/ui-topbar.js',text); self.assertIn('/quick-nav.js',text); self.assertIn('QuickNav.render',text); self.assertIn('UiTopbar.render',text)
            self.assertNotRegex(text,re.compile(r"page_access_token|access_token|refresh_token",re.I))

    def test_template_inline_dom_references_exist(self):
        text=(ROOT/"static"/"fb-auto-publish-templates.html").read_text(encoding="utf-8")
        declared=set(re.findall(r'\bid="([A-Za-z0-9_-]+)"',text)); referenced=set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)',text))
        self.assertFalse(referenced-declared,referenced-declared); self.assertIn("poolList",referenced); self.assertNotIn("groupList",referenced)

    def test_run_now_is_async_and_idempotently_identified(self):
        page=(ROOT/"static"/"fb-auto-publish-templates.html").read_text(encoding="utf-8")
        service=(ROOT/"features"/"fb_auto_posts"/"service.py").read_text(encoding="utf-8")
        client=(ROOT/"features"/"fb_auto_posts"/"client.py").read_text(encoding="utf-8")
        self.assertIn("operation_id:operationId",page); self.assertIn("enqueue_manual_due_slot",service)
        self.assertIn("202 if parsed.path.endswith(\"run-now\")",service)
        self.assertIn('30 if path.endswith("/run-now")',client)

    def test_inline_javascript_parses(self):
        for name in ("fb-auto-publish-templates.html","fb-auto-publish-runs.html"):
            source=(ROOT/"static"/name).read_text(encoding="utf-8")
            scripts="\n".join(match.group(1) for match in re.finditer(r"<script(?:\s[^>]*)?>(.*?)</script>",source,re.S|re.I))
            with tempfile.TemporaryDirectory() as tmp:
                target=Path(tmp)/(name+".js"); target.write_text(scripts,encoding="utf-8")
                result=subprocess.run(["node","--check",str(target)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__ == "__main__": unittest.main()
