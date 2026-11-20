"""Create revenue versions; retain every frozen version/task and enabled state."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

def migrate(path):
    changes=[]
    with closing(sqlite3.connect(path,timeout=30)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute('SELECT v.* FROM x_auto_template t JOIN x_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version').fetchall()
        for row in rows:
            config=json.loads(row['config_json'])
            if config.get('profile_model')!='growth':continue
            config['profile_model']='revenue'
            new=dict(row);new['version']=row['version']+1
            new['config_json']=json.dumps(config,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            new['config_sha256']=hashlib.sha256(new['config_json'].encode()).hexdigest()
            new['created_at']=datetime.now(timezone.utc).isoformat()
            new['created_by_name']='成长画像下线迁移';new['created_by_user_id']='system:x-growth-retirement'
            confirmation=json.loads(new['confirmation_json'])
            confirmation['growth_retirement']={'previous_version':row['version'],'at':new['created_at']}
            new['confirmation_json']=json.dumps(confirmation,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            db.execute('INSERT INTO x_auto_template_version('+','.join(new)+') VALUES('+','.join('?' for _ in new)+')',list(new.values()))
            db.execute('UPDATE x_auto_template SET current_version=?,updated_at=? WHERE id=? AND current_version=?',
                       (new['version'],new['created_at'],row['template_id'],row['version']))
            # Preserve already sampled future slots; no new random schedule is drawn.
            db.execute('INSERT INTO x_auto_random_plan SELECT template_id,?,shanghai_date,publish_times_json,plan_sha256,created_at FROM x_auto_random_plan WHERE template_id=? AND template_version=?',
                       (new['version'],row['template_id'],row['version']))
            changes.append(dict(template_id=row['template_id'],before=row['version'],after=new['version']))
        db.commit()
    return changes

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('database');a=p.parse_args();print(json.dumps(migrate(a.database)))
