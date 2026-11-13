"""Read production configuration; render only into an in-memory task fixture."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=Path('/opt/fb-auto-post/current'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(args.runtime.resolve()))
    from features.fb_auto_posts import page_caption
    from features.fb_auto_posts.core import FBAutoPostStore
    dbpath=Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
    creator,tenant='892fd2e8','149bb4a14b57975f'
    start=time.monotonic()
    with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True,timeout=10)) as live:
        live.row_factory=sqlite3.Row
        rows=[dict(row) for row in live.execute('SELECT page_id,language,caption_template FROM fb_video_page WHERE tenant_key=? AND created_by=? AND deleted=0 ORDER BY page_id',(tenant,creator))]
        templates=[dict(row) for row in live.execute("SELECT t.id,t.created_by,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled' AND t.created_by=?",(creator,))]
        snapshot=page_caption.load(live,creator,[r['page_id'] for r in rows])
    read_seconds=round(time.monotonic()-start,4)
    assert len(rows)==236 and templates,'review_scope_changed'
    bank=json.loads((args.runtime/'ops/fb-page-caption/approved-20261113.json').read_text())
    approved=0
    for row in rows:
        hook,label,tag=bank[row['language'] or 'en']
        expected=f'💕{hook} 👉 {{url}}\n\n📺{label}\n{{desc}}\n\n#shortdrama #shortdrama{tag} #dramaseries #dramawave'
        assert row['caption_template']==expected,'approved_copy_changed:'+row['page_id']
        approved+=1
    store=SimpleNamespace(_message=FBAutoPostStore._message,_link_values=FBAutoPostStore._link_values)
    count=0
    with closing(sqlite3.connect(':memory:')) as isolated:
        isolated.execute('CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,message_text TEXT,short_url TEXT,long_url TEXT)')
        for template in templates:
            config=json.loads(template['config_json'])
            for row in rows:
                count+=1
                page=SimpleNamespace(page_id=row['page_id'],page_name='QA fixture',language=row['language'] or 'en')
                material=SimpleNamespace(material_id='999999999',content_id='999999999',material_name='QA fixture',
                    drama_name='QA fixture',drama_description='QA 简介 · ملخص · Preview only',language=page.language,material_tag='QA')
                isolated.execute('INSERT INTO fb_auto_task(id) VALUES(?)',(count,))
                audit=page_caption.freeze(store,isolated,config,page,material,count,1794513600,snapshot)
                message,short,long=isolated.execute('SELECT message_text,short_url,long_url FROM fb_auto_task WHERE id=?',(count,)).fetchone()
                assert audit['source']=='page_management' and message.count(short)==1
                assert '{url}' not in message and '{desc}' not in message
                assert material.drama_description in message
                assert parse_qs(urlsplit(long).query)['af_adset_id']==[page.page_id]
    result=dict(ok=True,approved_pages=approved,templates_tested=len(templates),rendered_pairs=count,
        languages=len({row['language'] or 'en' for row in rows}),blank_language_pages=sum(not r['language'] for r in rows),
        english_captions=sum((r['language'] or 'en')=='en' for r in rows),configuration_read_seconds=read_seconds,
        production_writes=0,platform_requests=0,source='production configuration with synthetic material and in-memory tasks')
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
