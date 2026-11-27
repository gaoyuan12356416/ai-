"""Import real historical samples only; never infer old counters from today's values."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.social_material_effects import sampling


def read(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=20)


def import_file(target,path,source):
    samples,posts=[],[]
    # Close the source read transaction before writing: seeding may read the
    # destination cache itself, and SQLite's rollback journal needs that lock.
    with closing(read(path)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'counter_day' in tables:
            for pid,metric,day,value,stamp in db.execute('SELECT * FROM counter_day'):
                samples.append((pid,{metric:value},stamp))
        if source=='facebook' and 'observation' in tables:
            for pid,native,raw,stamp,retry,error in db.execute('SELECT * FROM observation'):
                values=json.loads(raw)
                times=values.get('_times') or {m:stamp for m in sampling.METRICS}
                if values.get('_views_at'):times['views']=values['_views_at']
                posts.append(dict(values,post_id=pid,metrics_at=stamp,_metric_times=times))
        if source=='x' and 'observations' in tables:
            for pid,raw,stamp in db.execute('SELECT post_id,metrics_json,metrics_at FROM observations'):
                samples.append((pid,json.loads(raw),stamp))
        if source=='x' and 'posts' in tables and any(r[1]=='payload' for r in db.execute('PRAGMA table_info(posts)')):
            for raw, in db.execute('SELECT payload FROM posts'):
                posts.append(json.loads(raw))
    return sum(sampling.save(target,*sample) for sample in samples)+sum(sampling.save_post(target,p) for p in posts)


def main(folder):
    import fcntl
    backup=folder/'backup'
    files={
        'facebook':list(backup.glob('facebook*.sqlite3'))+[
            Path('/mnt/data-disk/social-material-channels-20261126/history-2d9398da/observations-at-start.sqlite3'),
            Path('/mnt/data-disk/social-effect-window-20261127/qa/facebook-observations.sqlite3'),
            Path('/mnt/data-disk/social-effect-window-20261127/qa/facebook.sqlite3')],
        'x':list((backup/'x-snapshots').glob('*.sqlite3'))+list(backup.glob('x*.sqlite3'))}
    targets={'facebook':Path('/mnt/data-disk/social-material-effects/facebook-observations.sqlite3'),
             'x':Path('/mnt/data-disk/x-material-effects/observations.sqlite3')}
    locks={'facebook':targets['facebook'].with_suffix('.lock'),'x':targets['x'].parent/'refresh.lock'}
    result={}
    for source,path in targets.items():
        with locks[source].open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with closing(sqlite3.connect(path,timeout=20)) as db:
                sampling.prepare(db)
                # Seed the latest cache before the new collector can replace it.
                own=import_file(db,path,source)
                imported=[]
                for old in sorted(set(files[source])):
                    if old.is_file():
                        imported.append(dict(file=str(old),samples=import_file(db,old,source)))
                        db.commit()
                sampling.prune(db)
                db.commit()
                counts=list(db.execute("SELECT date(observed_at,'+8 hours'),COUNT(DISTINCT post_id) "
                    "FROM counter_history WHERE observed_at>='2026-11-22' GROUP BY 1"))
                result[source]=dict(seed=own,files=imported,sample_days=counts)
    result['recovered_at']=datetime.now(timezone.utc).isoformat()
    (folder/'recovery.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v['sample_days'] for k,v in result.items() if isinstance(v,dict)}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path)
    main(parser.parse_args().folder)
