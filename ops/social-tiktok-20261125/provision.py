"""Provision only the TikTok fact table and its reporting-tenant binding."""
import argparse
from pathlib import Path
import os
import re
import sqlite3
import subprocess
import sys

SERVICES=('social-analytics-warehouse-refresh','social-daily-prepare','youtube-publisher-daily-report','socialkit-gap-digest')
def binding(root):
    db=sqlite3.connect('file:/root/drama_material_service/data/drama_material_jobs.sqlite3?mode=ro',uri=True)
    tt=sqlite3.connect('file:/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3?mode=ro',uri=True)
    try:
        creators={r[0] for r in tt.execute("""SELECT DISTINCT t.created_by_user_id FROM tt_auto_template t
          JOIN tt_auto_template_version v ON v.template_id=t.id AND v.version=1
          WHERE t.created_by_user_id=v.created_by_user_id AND t.created_at=v.created_at""") if r[0]}
        tenants={r[0] for creator in creators for r in db.execute(
            'SELECT DISTINCT tenant_key FROM drama_admin_user WHERE user_id=? AND tenant_key<>\'\' ',(creator,))}
        installed={r[0] for r in db.execute("SELECT DISTINCT tenant_key FROM drama_admin_user WHERE tenant_key<>''")}
        if len(tenants)!=1 or tenants!=installed:raise RuntimeError('site tenant identity ambiguous')
        tenant=tenants.pop()
        if not re.fullmatch(r'[A-Za-z0-9_-]+',tenant):raise RuntimeError('invalid tenant key')
    finally:
        db.close();tt.close()
    cfg=root/'site.env'
    expected='SOCIAL_ANALYTICS_TT_SITE_TENANT='+tenant+'\n'
    if cfg.exists() and cfg.read_text()!=expected:raise ValueError('changed reporting tenant')
    if not cfg.exists():
        with cfg.open('x') as f:
            os.chmod(cfg,0o600);f.write(expected)
    return cfg
def configure(root,remove):
    cfg=root/'site.env' if remove else binding(root)
    content='[Service]\nEnvironmentFile='+str(cfg)+'\n'
    paths=[Path('/etc/systemd/system')/(s+'.service.d')/'social-tt-site.conf' for s in SERVICES]
    for p in paths:
        if p.exists() and p.read_text()!=content:raise ValueError('different unit configuration: '+str(p))
    for p in paths:
        if remove:
            if p.exists():p.unlink()
        else:
            p.parent.mkdir(parents=True,exist_ok=True)
            if not p.exists():p.write_text(content)
    subprocess.run(['systemctl','daemon-reload'],check=True)
def schema(release):
    sys.path.insert(0,str(release))
    from features.social_analytics.warehouse_refresh import Writer,environment
    writer=Writer(environment('/root/drama_material_service/.env'))
    try:
        with writer.db.cursor() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_tiktok_daily LIKE ads_ai.social_analytics_x_daily')
        writer.db.commit()
    finally:writer.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['binding','configure','unconfigure','schema']);p.add_argument('root',type=Path)
    a=p.parse_args()
    if a.action=='schema':schema(a.root)
    elif a.action=='binding':binding(a.root)
    else:configure(a.root,a.action=='unconfigure')
    print('ok',a.action)
