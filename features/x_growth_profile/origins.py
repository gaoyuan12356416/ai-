"""Read frozen classifications as the Auto ledger owner (SQLite WAL safe)."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from .model import instant

def read_origins(start):
    from features.x_tag_publish.transport import Error
    from features.x_tag_publish.policy import lane_paths
    try:
        parsed=instant(start)
        now=datetime.now(timezone.utc)
        if not now-timedelta(days=30)<=parsed<=now:
            raise ValueError()
        result=subprocess.run(['/usr/sbin/runuser','-u','x-post-daily','--','/usr/bin/python3','-B','-m',
                               'features.x_growth_profile.origins',str(lane_paths()['X_AUTO_POST_DB_PATH']),parsed.isoformat().replace('+00:00','Z')],
                              cwd='/opt/x-tag-publisher/current',capture_output=True,text=True,timeout=20,check=True,
                              env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        if len(result.stdout)>8*1024*1024:
            raise ValueError()
        return json.loads(result.stdout)
    except (OSError,TypeError,ValueError,subprocess.SubprocessError):
        raise Error('growth_origins_unavailable','成长画像来源暂不可用',503) from None

def query(path,start):
    import sqlite3
    values={}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        for qid,raw in db.execute("SELECT execution_queue_id,selection_json FROM x_auto_task WHERE published_at_utc>=? AND execution_queue_id<>''",(start,)):
            value=json.loads(raw or '{}')
            values[str(qid)]={'drama':{'catalog':(value.get('drama') or {}).get('catalog','dramawave')},
                              'tag_profile':{'classification':(value.get('tag_profile') or {}).get('classification') or {}}}
    return values

if __name__=='__main__':
    print(json.dumps(query(sys.argv[1],sys.argv[2]),ensure_ascii=False,separators=(',',':')))
