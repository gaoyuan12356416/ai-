"""Version-consistent API projections and session-pinned drama pagination."""
import json
from pathlib import Path
from .common import LEGACY,PORTRAITS,PUBLIC,ROOT,checked,encoded,literal
from .pins import Pins,session
from .read import Gate,request


class Store:
    def __init__(self,root=PORTRAITS,reader=None,pins=None):
        self.root=Path(root);self.reader=reader;self.pins=pins or Pins()

    def for_cookie(self,cookie):
        if self.reader is None:self.reader=Gate()
        return Bound(self,cookie)


class Bound:
    def __init__(self,store,cookie):
        self.store=store;self.cookie=cookie

    def load(self,ids,*,detail=False,version=None):
        hint=checked(version) if version else checked(json.loads((self.store.root/'current.json').read_text())['version'])
        owner=session(self.cookie,self.store.pins.auth) if detail else None
        if detail and version and len(ids)==1:
            hit=self.store.pins.get(ids[0],hint,owner)
            if hit:
                raw,meta=hit
                return self.response({ids[0]:raw},meta,detail)
        rows,meta=self.fetch(hint,ids,detail,version is not None)
        if detail:
            for pid,raw in rows.items():self.store.pins.save(pid,meta['version'],meta,raw,owner)
        return self.response(rows,meta,detail)

    def fetch(self,hint,ids,detail,exact):
        try:return request(self.store.reader,hint,ids,detail=detail,exact=exact)
        except FileNotFoundError:
            if not exact:raise
            if (ROOT/'history-ready.json').exists():
                from .history import lookup
                from features.fb_page_portrait.storage import projections
                found={};meta=None
                for pid in ids:
                    raw,meta=lookup(hint,pid)
                    if raw is not None:
                        found[pid]=raw if detail else encoded(projections(json.loads(raw))[0]).encode()
                return found,meta
            exists=self.store.reader.query(["SELECT HEX(JSON_OBJECT('n',COUNT(*))) FROM information_schema.tables "
                                "WHERE table_schema='ads_ai' AND table_name="+literal(LEGACY)])
            if exists and exists[0]['n']:
                return request(self.store.reader,hint,ids,table=LEGACY,detail=detail,exact=True)
            raise

    @staticmethod
    def response(rows,meta,detail):
        from features.fb_page_portrait.storage import projections
        items={}
        for pid,raw in rows.items():
            page=json.loads(raw)
            if page['page_id']!=pid:raise ValueError('page identity mismatch')
            items[pid]=projections(page)[1] if detail else page
        return dict(items=items,meta={k:v for k,v in meta.items() if k not in ('audit','page_count')})

    def dramas(self,pid,path,version,page):
        owner=session(self.cookie,self.store.pins.auth)
        hit=self.store.pins.get(pid,checked(version),owner)
        if hit:raw,_=hit
        else:
            rows,meta=self.fetch(version,[pid],True,True);raw=rows.get(pid)
            if raw is not None:self.store.pins.save(pid,version,meta,raw,owner)
        dramas=[]
        if raw is not None:
            for a in json.loads(raw)['audiences']:
                for t in a['children']:
                    if a['key']+'/'+t['key']==path:dramas=t['dramas'];break
        return dict(items=dramas[(page-1)*50:page*50],page=page,page_size=50,total=len(dramas),version=version)

