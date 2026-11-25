"""Read one completed, trigger-fresh catalog generation for the whole plan."""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from threading import Lock

from features.material_products import selected
from .repositories import RepositoryError, _https

ROOT = Path('/mnt/data-disk/fb-planner-catalog')


def open_for_plan(config, now, root=ROOT):
    cutoff = config.get('_catalog_trigger')
    if (not cutoff or str(config.get('app_id')) != '1479' or config.get('material_data_source') != 6
            or not set(selected(config)) <= {'Dramawave','DramawaveSource'} or not (root/'enabled').is_file()):
        return None
    trigger = datetime.fromisoformat(cutoff).timestamp()
    conn = None
    try:
        path = root/'current.sqlite3'
        conn = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,check_same_thread=False)
        started, finished = conn.execute('SELECT started,finished FROM generation').fetchone()
        if started >= trigger and now.timestamp()-finished <= 7200:
            return PlanningCatalog(conn)
    except (OSError,sqlite3.Error,TypeError,ValueError):
        pass
    if conn is not None:
        conn.close()
    # The timer detects new native due slots independently of metric readiness.
    # An unavailable builder must not indefinitely hold the original live path.
    if now.timestamp()-trigger < 2400:
        raise RepositoryError('fb_auto_catalog_refresh_pending','正在更新本次触发的共享素材目录',409)
    return None


class PlanningCatalog:
    def __init__(self, conn):
        self.db=conn
        self.lock=Lock()

    def index(self, cat):
        rule=cat.config['material_rule']
        languages=','.join('?' for _ in cat.languages)
        products=','.join('?' for _ in cat.products)
        sql=f"""SELECT cid,lang,id FROM material WHERE product COLLATE NOCASE IN ({products})
            AND duration BETWEEN ? AND ? AND (lang IN ({languages})
            OR (product='DramawaveSource' COLLATE NOCASE AND lang='')) ORDER BY id"""
        result=defaultdict(list)
        args=[*cat.products,rule['duration_min_seconds'],rule['duration_max_seconds'],*cat.languages]
        for cid,lang,mid in self.db.execute(sql,args):
            result[cid,lang].append(mid)
        return result

    def resources(self, ids):
        result=[]
        values=list(ids)
        with self.lock:
            for start in range(0,len(values),800):
                batch=values[start:start+800]
                for (data,) in self.db.execute('SELECT items FROM resource WHERE cid IN ('+','.join('?' for _ in batch)+')',batch):
                    result.extend(json.loads(data))
        return result

    def hydrate(self, ids):
        result={}
        for start in range(0,len(ids),800):
            batch=ids[start:start+800]
            sql='SELECT id,cid,lang,product,duration,url,name,tag FROM material WHERE id IN ('+','.join('?' for _ in batch)+')'
            for mid,cid,lang,product,duration,raw_url,name,tag in self.db.execute(sql,batch):
                url=_https(raw_url)
                if url:
                    result[mid]=(dict(id=mid,cid=cid,lang=lang,product=product,video_duration=duration,
                                      name=name,tag_name=tag),url)
        return result

