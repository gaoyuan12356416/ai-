"""Browser regression for the two independent TT promotion pages. All HTTP is intercepted."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

STATIC = Path(__file__).resolve().parents[1] / 'static'
BASE = 'http://127.0.0.1:18081'
API = '/api/admin/tt-promotion'


def run(screens=None):
    channels = [
        dict(account_id='101', nickname='剧场甲', drama_language='en', eligible=True, enabled=True,
             today_published=3, today_expected=5, revenue_28d=125.50, has_profile=True, audience='女频', themes=['悬疑']),
        dict(account_id='102', nickname='剧场乙', drama_language='fr', eligible=False, enabled=True,
             reason='暂缺授权', today_published=0, today_expected=2, revenue_28d=None, has_profile=False, themes=[])]
    drama = dict(content_id='d28', name='剧集 A', language='en', iap_cents=12500, iaa_cents=50,
                 allocated_cents=12550, published_count=3)
    theme = dict(key='suspense', name='悬疑', share_bp=10000, revenue_cents=12550,
                 published_count=3, dramas=[drama])
    window = dict(start='2026-11-01', end='2026-11-24', status='ready', revenue_cents=12550,
                  iap_cents=12500, iaa_cents=50, published_count=3,
                  audiences=[dict(key='female', name='女频', share_bp=10000, revenue_cents=12550,
                                  published_count=3, children=[theme])])
    templates = []
    mutations = []
    jobs = []
    requests = []
    errors = []
    fail_first_run = True
    auth_state = "ok"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 960}, timezone_id='America/Los_Angeles')
        page.on('pageerror', lambda e: errors.append(str(e)))

        def handle(route):
            nonlocal fail_first_run
            req = route.request
            url = urlsplit(req.url)
            assert url.hostname == '127.0.0.1', url
            path = url.path
            post = req.post_data_json if req.method == 'POST' else {}
            if path == '/api/ui/topbar':
                if auth_state == "expired":
                    route.fulfill(status=401, json={"message":"expired"})
                    return
                if auth_state == "anon":
                    route.fulfill(json={"authenticated":False})
                    return
                data = dict(authenticated=True, user=dict(user_id='u1', tenant_key='test', name='测试运营', role='admin', is_admin=True, permissions={}))
            elif path == '/navigation.json':
                data = [dict(key='tiktok_platform', label='TT 平台推广',items=[
                    dict(key='ttPromotionChannels',kind='page', label='频道管理（新版）',href='/tt-promotion-channels.html'),
                    dict(key='ttPromotionAuto',kind='page', label='自动发布（新版）',href='/tt-promotion-auto.html')])]
            elif path.startswith(API):
                endpoint = path[len(API):]
                requests.append((req.method, endpoint,post))
                if auth_state == "denied":
                    route.fulfill(status=403, json={"message":"没有访问权限"})
                    return
                if endpoint == '/channels':
                    data = dict(items=channels, meta=dict(end='2026-11-24'))
                elif endpoint == '/channels/101':
                    data = dict(channel=channels[0], profile=dict(windows={'all':window, '28d':window, '7d':dict(window,revenue_cents=1750,iap_cents=1710,iaa_cents=40,audiences=[])}),
                                meta=dict(stale=True), handoff=dict(old_templates=[dict(id=4,name='在用旧模板')],new_templates=[],inflight=[]))
                elif endpoint == '/channels/102':
                    window_empty=dict(status='unavailable',start='',end='',revenue_cents=None,iap_cents=None,
                                      iaa_cents=None,published_count=None,audiences=[])
                    data=dict(channel=channels[1],profile=dict(windows={'all':window_empty,'7d':window_empty,'28d':window_empty}),
                              meta=dict(stale=True),handoff={})
                elif endpoint == '/channels/settings':
                    mutations.append((endpoint, post))
                    for item in channels:
                        if item['account_id'] in post['account_ids']:
                            item.update(post['settings'])
                    data = {'updated':len(post['account_ids'])}
                elif endpoint == '/templates' and req.method == 'GET':
                    data = {'items':templates}
                elif endpoint == '/templates' and req.method == 'POST':
                    mutations.append((endpoint,post))
                    templates.append(dict(id=7,name=post['name'],version=1,enabled=False,config=post))
                    data = templates[-1]
                elif endpoint == '/templates/7/enable':
                    assert post['expected_version'] == 1
                    mutations.append((endpoint,post))
                    templates[0]['enabled'] = True
                    data = templates[0]
                elif endpoint == '/templates/7/preview':
                    data = {'items':[dict(account_id=a,status='blocked' if a=='102' else 'ready', reason='暂缺授权' if a=='102' else '题材匹配',
                         conflicts=['旧排期占用'] if a=='102' else [],content_id='d28',material_id='51',caption=post.get('manual_caption','预览文案')) for a in post['account_ids']]}
                elif endpoint == '/templates/7/draft':
                    assert post['account_id']=='101' and post['material_id']=='51' and post['operation_id']
                    data={'id':8,'state':'waiting','account_id':'101','material_id':'51'}
                elif endpoint == '/templates/7/draft/8':
                    data={'id':8,'state':'final','caption':'AI 审阅稿 {code}','source':'ai','account_id':'101','material_id':'51'}
                elif endpoint == '/templates/7/run':
                    mutations.append((endpoint,post))
                    if fail_first_run:
                        fail_first_run = False
                        route.fulfill(status=503,json={'message':'上次提交结果待核对'})
                        return
                    jobs.append(dict(id=19,template_id=7,template_name=templates[0]['name'],status='completed',trigger_type='manual',
                         items=[dict(id='401',account_id='101',status='published',publish_id='pid-7',content_id='d28',material_id='51',
                                     caption=post['manual_caption'],copy_source='manual',publish_url='')]))
                    data=jobs[-1]
                elif endpoint == '/runs':
                    data={'items':jobs}
                elif endpoint == '/runs/19':
                    data=jobs[0]
                else:
                    raise AssertionError((req.method, endpoint))
            else:
                file = STATIC / path.lstrip('/')
                if file.is_file() and file.parent==STATIC:
                    route.fulfill(path=str(file))
                    return
                route.fulfill(status=404,body='')
                return
            route.fulfill(json=data)

        page.route('**/*', handle)
        page.goto(BASE+'/tt-promotion-channels.html')
        expect(page.locator('#channels tr')).to_have_count(2)
        expect(page.locator('#channels')).to_contain_text('待回传')
        page.get_by_role('button',name='剧场甲').click()
        expect(page.get_by_text('在用旧模板',exact=False)).to_be_visible()
        page.locator('summary').filter(has_text='女频').click()
        page.locator('summary').filter(has_text='悬疑').click()
        expect(page.get_by_text('剧集 A')).to_be_visible()
        if screens:
            page.screenshot(path=str(screens/'channel-detail.png'))
        page.get_by_role('button',name='近 7 天').click()
        expect(page.locator('.kpis')).to_contain_text('17.50')
        page.get_by_role('button',name='关闭',exact=True).click()
        page.get_by_role('button',name='剧场乙').click()
        expect(page.locator('.kpis')).to_contain_text('待回传')
        expect(page.locator('.kpis')).not_to_contain_text('0.00')
        page.get_by_role('button',name='关闭',exact=True).click()
        page.locator('[name=language]').select_option('fr')
        expect(page.locator('#channels tr')).to_have_count(1)
        page.get_by_role('checkbox',name='选择当前结果').check()
        page.get_by_role('button',name='重置').click()
        expect(page.locator('#channels tr')).to_have_count(2)
        page.get_by_role('button',name='批量设置（1）').click()
        page.locator('[name=participation]').select_option('no')
        page.get_by_role('button',name='保存频道设置').click()
        expect(page.locator('#notice')).to_contain_text('已保存 1')
        assert mutations[-1][1] == {'account_ids':['102'],'settings':{'enabled':False}}

        page.goto(BASE+'/tt-promotion-auto.html')
        page.get_by_role('button',name='创建模板').click()
        form=page.locator('#edit-template')
        form.locator('[name=name]').fill('收入选材草稿')
        form.locator('[data-add]').click()
        form.locator('[name=schedule_mode]').select_option('random')
        form.locator('[name=count]').fill('2')
        form.locator('[name=ai_copy]').select_option('yes')
        form.get_by_role('button',name='保存草稿').click()
        expect(page.locator('#templates')).to_contain_text('收入选材草稿')
        assert templates[0]['config']['schedule']=={'mode':'random','daily_count':2}
        assert not templates[0]['enabled'] and templates[0]['config']['ai_copy']
        page.get_by_role('button',name='启用',exact=True).click()
        page.get_by_role('button',name='返回',exact=True).click()
        assert not templates[0]['enabled']
        page.get_by_role('button',name='预演与执行').click()
        form=page.locator('#execution')
        form.get_by_role('button',name='只读预演').click()
        page.get_by_role('button',name='生成单条 AI 稿').click()
        page.get_by_role('button',name='采用AI 稿并锁定频道和素材').click()
        expect(form.locator('[name=mode]')).to_have_value('material')
        expect(form.locator('[name=material_id]')).to_have_value('51')
        expect(form.locator('[name=manual_caption]')).to_have_value('AI 审阅稿 {code}')
        expect(form.locator('[data-accounts] :checked')).to_have_count(1)
        expect(form.locator('#execute')).to_be_disabled()
        page.get_by_role('button',name='关闭',exact=True).click()
        page.get_by_role('button',name='预演与执行').click()
        form=page.locator('#execution')
        form.locator('[name=mode]').select_option('serial')
        form.locator('[name=content_id]').fill('d28')
        form.locator('[name=inventory]').fill('51\n52')
        form.locator('[name=when]').select_option('at')
        form.locator('[name=scheduled_at]').fill('2030-01-02T15:30')
        form.locator('[name=manual_caption]').fill('人工定稿')
        form.get_by_role('button',name='只读预演').click()
        expect(page.locator('#preview-output')).to_contain_text('旧排期占用')
        assert requests[-1][1]=='/templates/7/preview' and requests[-1][2]['episodes']==['51','52']
        assert requests[-1][2]['scheduled_at']=='2030-01-02T07:30:00.000Z'
        assert all(not p.endswith('/run') for p,_ in mutations)
        if screens:
            page.screenshot(path=str(screens/'template-preview.png'))
        form.locator('[name=manual_caption]').fill('人工定稿修订')
        expect(form.locator('#execute')).to_be_disabled()
        form.get_by_role('button',name='只读预演').click()
        form.get_by_role('button',name='确认预约').click()
        page.get_by_role('button',name='预约',exact=True).click()
        expect(page.locator('#modal-error')).to_have_text('上次提交结果待核对')
        form.get_by_role('button',name='确认预约').click()
        page.get_by_role('button',name='预约',exact=True).click()
        expect(page.locator('#dialog-title')).to_contain_text('批次 #19')
        page.locator('#dialog summary').click()
        expect(page.locator('#dialog')).to_contain_text('公开作品链接待取得')
        expect(page.locator('#dialog')).to_contain_text('人工定稿修订')
        attempts = [m for k,m in mutations if k.endswith('/run')]
        assert len(attempts)==2 and attempts[0]['operation_id']==attempts[1]['operation_id']
        page.get_by_role('button',name='关闭',exact=True).click()
        page.set_viewport_size({'width':390,'height':844})
        page.get_by_role('button',name='发布模板',exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if screens:
            page.screenshot(path=str(screens/'mobile-templates.png'))
        page.locator('#nav-toggle').click()
        expect(page.locator('#quickNav')).to_be_visible()
        page.locator('#nav-toggle').click()
        expect(page.locator('#quickNav')).not_to_be_visible()
        for state, expected_text in [('anon','请先登录'),('expired','登录已过期'),('denied','没有访问')]:
            auth_state=state
            page.goto(BASE+'/tt-promotion-auto.html')
            expect(page.locator('#notice')).to_contain_text(expected_text)
            expect(page.locator('#workspace')).not_to_be_visible()
            if state != 'denied':
                expect(page.locator('#authButton')).to_have_text('登录')
        assert not errors, errors
        browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--screens',type=Path)
    args=parser.parse_args()
    if args.screens:
        args.screens.mkdir(parents=True, exist_ok=True)
    run(args.screens)
    print('TT promotion browser regression passed.')
