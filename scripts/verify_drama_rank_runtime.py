"""Read-only production acceptance of deployed permissions, data and gateways."""
import argparse
import ast
from collections import Counter
import json
import logging
from pathlib import Path
import sys
from urllib.parse import urlparse
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.drama_pay_rank.routes import dispatch,ensure_navigation
from features.drama_pay_rank.core import read
from features.drama_audience.store import counts


def check_permissions():
    tree=ast.parse(Path('/root/drama_material_service/app.py').read_text())
    names={'normalize_user_permissions','has_module_permission','navigation_item_access'}
    assignments={'MODULE_PERMISSIONS','DEFAULT_USER_PERMISSIONS','ADMIN_PERMISSIONS'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names or
           isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in assignments for t in n.targets)]
    methods=[n for c in tree.body if isinstance(c,ast.ClassDef) for n in c.body
             if isinstance(n,ast.FunctionDef) and n.name in ('_require_cookie_module','_require_cookie_navigation_item')]
    config=ensure_navigation([]);responses=[]
    env=dict(json=json,logging=logging,load_navigation_config=lambda:config,
             json_response=lambda h,status,data,**kw:responses.append((status,data)))
    exec(compile(ast.Module(body=nodes+methods,type_ignores=[]),'deployed_auth','exec'),env)
    Handler=type('Handler',(),{**{n.name:env[n.name] for n in methods},
                              '_require_auth':lambda self:True,'_session':lambda self:self.session})
    def request(session,query=''):
        h=Handler();h.session=session;responses.clear()
        dispatch(h,urlparse('/api/drama-pay-rank/list'+query),env)
        return responses[-1][0]
    assert request(dict(role='user',permissions={}))==403
    allowed=dict(role='user',permissions={'drama_pay_rank':True})
    assert request(allowed)==200
    assert request(dict(allowed,auth_type='api_token'))==403
    assert request(dict(role='admin'))==200
    assert request(allowed,'?date=invalid')==400
    config[0]['items'][0]['enabled']=False
    assert request(allowed)==403
    assert 'drama_pay_rank' in env['MODULE_PERMISSIONS']
    return dict(checks=7,assignable=True,default_denied=True,cookie_only=True,navigation_enforced=True)


def verify():
    result=dict(permissions=check_permissions(),audiences=counts())
    r=read(lang='');assert r['availability']=='available'
    result['rank']=dict(date=r['date'],rows=len(r['list']),languages=len(r['languages']),
                       named=sum(bool(x['name']) for x in r['list']),
                       metadata_status=dict(Counter(x['metadata_status'] for x in r['list'])))
    assert len(read()['list'])==sum(v['language_code']=='en' for v in r['list'])
    assert read(lang='absent-test-language')['availability']=='language_empty'
    assert read(day='2000-01-01')['availability']=='not_generated'
    result['public']={}
    for path in ('/drama-pay-rank.html','/drama-pay-rank.js','/drama-pay-rank.css','/api/drama-pay-rank/list'):
        try:
            with urlopen('https://ai.yingliangads.com'+path,timeout=15) as response:
                result['public'][path]=response.status
        except HTTPError as exc:result['public'][path]=exc.code
    assert result['public']['/api/drama-pay-rank/list']==401
    assert all(code==200 for path,code in result['public'].items() if not path.startswith('/api'))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path);a=p.parse_args()
    result=verify();text=json.dumps(result,ensure_ascii=False,indent=2)
    if a.output:a.output.write_text(text,encoding='utf8')
    print(text)
