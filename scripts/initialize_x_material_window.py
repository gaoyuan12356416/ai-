"""Append default-window template versions without moving existing tasks or slots."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import sqlite3


def initialize(db, now=None):
    now=now or datetime.now(timezone.utc)
    stamp=now.isoformat();day=now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    db.row_factory=sqlite3.Row
    changes=[]
    db.execute('BEGIN IMMEDIATE')
    try:
        rows=db.execute('''SELECT t.id,t.current_version,v.* FROM x_auto_template t
            JOIN x_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version
            WHERE t.deleted_at_utc='' ORDER BY t.id''').fetchall()
        for row in rows:
            config=json.loads(row['config_json'])
            if 'material_window_days' in config:continue
            config['material_window_days']=30
            encoded=json.dumps(config,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            digest=hashlib.sha256(encoded.encode()).hexdigest();version=row['version']+1
            confirmation=json.loads(row['confirmation_json'])
            confirmation.update(material_window_days=30,source='user_requested_default_20261123')
            db.execute('''INSERT INTO x_auto_template_version(
                template_id,version,config_json,config_sha256,confirmed,confirmation_json,
                confirmed_by_user_id,confirmed_by_name,confirmed_at_utc,
                created_by_user_id,created_by_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (row['id'],version,encoded,digest,row['confirmed'],json.dumps(confirmation,ensure_ascii=False),
                 '', 'Codex' if row['confirmed'] else '', stamp if row['confirmed'] else '', '', 'Codex', stamp))
            # Version/config changes normally change the random seed. Preserve
            # the already planned times so this default never adds today's slots.
            db.execute('''INSERT INTO x_auto_random_plan
                SELECT template_id,?,shanghai_date,publish_times_json,plan_sha256,?
                FROM x_auto_random_plan WHERE template_id=? AND template_version=? AND shanghai_date>=?''',
                (version,stamp,row['id'],row['version'],day))
            schedule=config.get('schedule',{})
            if schedule.get('mode')=='random' and not db.execute('SELECT 1 FROM x_auto_random_plan WHERE template_id=? AND template_version=? AND shanghai_date=?',(row['id'],version,day)).fetchone():
                seed=hashlib.sha256((str(row['id'])+'|'+str(row['version'])+'|'+row['config_sha256']+'|'+day).encode()).digest()
                rng=random.Random(int.from_bytes(seed[:16],'big'))
                times=['%02d:%02d'%divmod(v,60) for v in sorted(rng.sample(range(1440),int(schedule['daily_count'])))]
                data=json.dumps(times,separators=(',',':'))
                db.execute('INSERT INTO x_auto_random_plan VALUES(?,?,?,?,?,?)',(row['id'],version,day,data,hashlib.sha256(data.encode()).hexdigest(),stamp))
            db.execute("UPDATE x_auto_template SET current_version=?,updated_by_user_id='',updated_by_name='Codex',updated_at=? WHERE id=? AND current_version=?",
                (version,stamp,row['id'],row['version']))
            change=dict(template_id=row['id'],from_version=row['version'],version=version,material_window_days=30)
            db.execute('INSERT INTO x_auto_event(event_type,message,details_json,created_at) VALUES(?,?,?,?)',
                ('template_material_window_default','按用户要求为后续任务设置 30 天素材窗口期',json.dumps(change),stamp))
            changes.append(change)
        db.commit()
    except Exception:
        db.rollback();raise
    return changes


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('database');args=p.parse_args()
    with sqlite3.connect(args.database,timeout=30) as db:print(json.dumps(initialize(db)))
