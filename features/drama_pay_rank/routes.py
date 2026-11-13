from urllib.parse import parse_qs
from .core import read


def dispatch(handler, parsed, app):
    if not handler._require_cookie_module('drama_pay_rank'):
        return
    if not handler._require_cookie_navigation_item('dramaPayRank'):
        return
    try:
        query=parse_qs(parsed.query,keep_blank_values=True)
        data=read(query.get('date',[''])[-1],query.get('language_code',['en'])[-1])
        app['json_response'](handler,200,{'data':data},no_store=True)
    except ValueError:
        app['json_response'](handler,400,{'error':'invalid_date'},no_store=True)


def ensure_navigation(config):
    from copy import deepcopy
    groups=deepcopy(config)
    if any(i.get('key')=='dramaPayRank' for g in groups for i in g.get('items',[])):
        return groups
    item=dict(key='dramaPayRank',label='短剧付费 TOP100',kind='page',href='/drama-pay-rank.html',
              module='drama_pay_rank',enabled=True,order=5)
    for group in groups:
        if group.get('label')=='数据报表':
            group.setdefault('items',[]).append(item)
            return groups
    groups.append(dict(key='drama_reports',label='数据报表',enabled=True,order=70,items=[item]))
    return groups
