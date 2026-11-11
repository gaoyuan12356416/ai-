"""Immediate administrator config list; names are display-only, never grants."""
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
from .runtime import atomic_json,literal

CONFIG_DB='/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
USER_DB='/root/drama_material_service/data/drama_material_jobs.sqlite3'
FIELDS=('tenant_key','page_id','config_instance_id','version','language','caption_template','title',
        'created_by','created_by_name','updated_by','created_at_utc','updated_at_utc')


def current_admin(user,user_db=USER_DB):
    if not user.get('tenant_key') or not user.get('user_id'):return False
    with closing(sqlite3.connect('file:'+str(user_db)+'?mode=ro',uri=True,timeout=3)) as db:
        rows=db.execute('SELECT role,status FROM drama_admin_user WHERE tenant_key=? AND user_id=? LIMIT 2',
                        (user['tenant_key'],user['user_id'])).fetchall()
    return rows==[('admin','active')]


def configured_list(user,root,*,config_db=CONFIG_DB,user_db=USER_DB):
    if not current_admin(user,user_db):raise PermissionError('current_admin_required')
    with closing(sqlite3.connect('file:'+str(config_db)+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        rows=[dict(r) for r in db.execute('SELECT '+','.join(FIELDS)+' FROM fb_video_page WHERE tenant_key=? AND deleted=0 ORDER BY page_id',(user['tenant_key'],))]
    try:
        names=json.loads((Path(root)/'page-names.json').read_text())['names']
        if not isinstance(names,dict):names={}
    except (OSError,ValueError,KeyError):names={}
    missing={r['created_by'] for r in rows if r['created_by'] and not r['created_by_name']}
    creators={}
    if missing:
        with closing(sqlite3.connect('file:'+str(user_db)+'?mode=ro',uri=True,timeout=3)) as db:
            creators=dict(db.execute('SELECT user_id,name FROM drama_admin_user WHERE tenant_key=?',(user['tenant_key'],)))
    # No token counts/statuses come from the name snapshot. Existing source
    # checks still determine current availability and every publishing action.
    return dict(ok=True,authorization_pending=True,items=[dict(r,
        page_name=str(names.get(r['page_id']) or r['page_id'])[:255],
        created_by_name=r['created_by_name'] or creators.get(r['created_by']) or r['created_by'],
        eligible_token_count=None,blocked_reason='',authorization_pending=True) for r in rows])


def refresh_names(reader,ids,root):
    """Optional display metadata. No credentials or authorization cache."""
    names={}
    for offset in range(0,len(ids),200):
        batch=ids[offset:offset+200]
        sql="SELECT HEX(JSON_OBJECT('page_id',TRIM(page_id),'name',MAX(TRIM(page_name)))) FROM kunlunads_dev.ads_facebook_page_post WHERE TRIM(page_id) IN ("+','.join(map(literal,batch))+') GROUP BY page_id LIMIT 20001'
        rows=reader.query([sql],timeout=30)
        if len(rows)>20000:raise RuntimeError('page_name_limit')
        for row in rows:names.setdefault(row['page_id'],row['name'])
    atomic_json(Path(root)/'page-names.json',dict(generated_at=datetime.now(timezone.utc).isoformat(),names=names))
    return len(names)
