"""Add matching scalars to the existing verified cache without re-reading full details."""
from contextlib import closing
import json
import os
from pathlib import Path
import sys


def main(writer):
    os.chdir(writer)
    sys.path.insert(0,str(writer))
    from features.fb_page_current import selection
    from features.fb_page_current.common import PORTRAITS, PUBLIC, checked, literal
    from features.fb_page_current.read import Gate
    with closing(selection.connect(selection.FILE)) as old:
        meta=json.loads(old.execute('SELECT payload FROM head').fetchone()[0])
        version=checked(meta['version'])
        if json.loads((PORTRAITS/'current.json').read_text())['version']!=version:
            raise RuntimeError('refresh in progress')
        proof=json.loads((PORTRAITS/(version+'.receipt.json')).read_text())
        rows=Gate().query(["SELECT HEX(JSON_OBJECT('p',page_id,'h',payload_sha256,'r',paid_cents+social_cents,"
             "'v',JSON_UNQUOTE(JSON_EXTRACT(summary_json,'$.hierarchy_version')))) FROM "+PUBLIC+
             ' WHERE profile_version='+literal(version)])
        facts={r['p']:r for r in rows}
        if {p:r['h'] for p,r in facts.items()}!=proof['payload_hashes']:
            raise ValueError('published receipt differs from replica')
        builder=selection.Builder(meta,proof)
        try:
            for pid,raw in old.execute('SELECT page_id,projection FROM pages'):
                page=json.loads(raw)
                r=facts[pid]
                if r['v']!=meta['hierarchy_version']:
                    raise ValueError('headline and detail hierarchy differ')
                page.update(revenue_cents=r['r'],hierarchy_version=r['v'])
                builder.add(pid,page,r['h'])
            builder.finish()
        finally:
            builder.close()
    print(json.dumps(dict(version=version,pages=len(rows),same_inode=os.path.samefile(
        selection.FILE,PORTRAITS/'matching/current.sqlite3'))))


if __name__=='__main__':
    main(Path(sys.argv[1]).resolve())
