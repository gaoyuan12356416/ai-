"""Read-only validation of split Creative references for an existing task."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    live=Path('/root/drama_material_service')
    pid=subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value'],text=True).strip()
    for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in item:
            k,v=item.split(b'=',1);os.environ[k.decode()]=v.decode()
    os.chdir(live);sys.path.insert(0,str(live))
    import app
    package='features.fb_ad_asset_delete'
    root=Path(__file__).resolve().parents[1]/'features/fb_ad_asset_delete'
    assert package not in sys.modules
    spec=importlib.util.spec_from_file_location(package,root/'__init__.py',submodule_search_locations=[str(root)])
    module=importlib.util.module_from_spec(spec);sys.modules[package]=module;spec.loader.exec_module(module)
    from features.fb_ad_asset_delete.source import SqlSource
    from features.fb_ad_asset_delete.execution import SOURCE_READ_SLOTS
    def query(sql,timeout):
        with SOURCE_READ_SLOTS:
            return app.ad_control_run_mysql(sql,timeout_seconds=timeout,via_stdin=True)
    source=SqlSource(query,schema=app.AD_CONTROL_DB_NAME)
    with sqlite3.connect('file:/mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3?mode=ro',uri=True) as conn:
        objects=[]
        allowed_ads={r[0] for r in conn.execute("SELECT object_id FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='ad' AND status<>'blocked'",(args.job_id,))}
        for key,oid,payload,result in conn.execute("SELECT object_key,object_id,payload,result FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='creative' AND status='blocked'",(args.job_id,)):
            if json.loads(result).get('code')=='source_unavailable':
                obj=json.loads(payload);obj.update(key=key,object_id=oid,kind='creative');objects.append(obj)
    started=time.monotonic()
    refs=source.shared_references(objects)
    report=dict(job_id=args.job_id,objects=len(objects),accounts=len({a for o in objects for a in o['account_ids']}),
                references=len(refs),outside_objects=len({r['key'] for r in refs if r['ad_id'] not in allowed_ads}),
                seconds=round(time.monotonic()-started,3),delete_requests=0)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()
