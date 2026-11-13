"""Idempotent, scoped integration into the current (mixed-release) backend."""
import re


def once(text, old, new):
    if new in text:return text
    if text.count(old)!=1:raise ValueError('integration_anchor_changed:'+old[:70])
    return text.replace(old,new,1)


def patch_app(text):
    text=once(text,'MODULE_PERMISSIONS = {','MODULE_PERMISSIONS = {\n    "drama_pay_rank": "短剧付费 TOP100",')
    text=once(text,'DEFAULT_USER_PERMISSIONS = {','DEFAULT_USER_PERMISSIONS = {\n    "drama_pay_rank": False,')
    text=once(text,'    def do_GET(self):','    def do_GET(self):\n        if urlparse(self.path).path == "/api/drama-pay-rank/list":\n            from features.drama_pay_rank.routes import dispatch\n            return dispatch(self, urlparse(self.path), globals())')
    text=once(text,'def load_navigation_config():','def load_navigation_config():\n    from features.drama_pay_rank.routes import ensure_navigation as ensure_drama_pay_navigation')
    start=text.index('def load_navigation_config():');end=text.index('\ndef ',start+5)
    block=text[start:end]
    if 'return ensure_drama_pay_navigation(' not in block:
        block,count=re.subn(r'(?m)^(\s*)return (.+)$',r'\1return ensure_drama_pay_navigation(\2)',block)
        if count!=1:raise ValueError('navigation_return_changed')
        text=text[:start]+block+text[end:]
    text=once(text,'            "/moboreels-library.html",','            "/drama-pay-rank.html",\n            "/drama-pay-rank.js",\n            "/drama-pay-rank.css",\n            "/moboreels-library.html",')
    return text


def patch_nav(text):
    anchor='    const normalized = cloneNav(config);'
    addition='''
    // DRAMA_PAY_RANK_NAV_V1
    if (!normalized.some(g => (g.items || []).some(i => i.key === "dramaPayRank"))) {
      const rankItem = {key:"dramaPayRank", label:"短剧付费 TOP100", kind:"page",
        href:"/drama-pay-rank.html", module:"drama_pay_rank", enabled:true, order:5};
      const reports = normalized.find(g => g.label === "数据报表");
      if (reports) (reports.items || (reports.items=[])).push(rankItem);
      else normalized.push({key:"drama_reports",label:"数据报表",enabled:true,order:70,items:[rankItem]});
    }'''
    if 'DRAMA_PAY_RANK_NAV_V1' in text:return text
    return once(text,anchor,anchor+addition)
