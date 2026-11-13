"""Add the narrow metric-reader startup fix to a prepared FB catalog overlay."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_hit_drama import atomic,sha,save
from scripts.deploy_pipeline_runtime import verify_source


def prepare(folder):
    source=Path(__file__).resolve().parents[1];commit=verify_source(source,'HEAD')
    manifest=json.loads((folder/'manifest.json').read_text())
    assert manifest['platform']=='fb' and manifest['status']=='prepared'
    candidate=Path(manifest['candidate']);previous=Path(manifest['previous'])
    assert Path('/opt/fb-auto-post/current').resolve()==previous,'fb_release_changed'
    name='features/fb_auto_posts/service.py';target=candidate/name
    old='    metric_store = FBAutoPostStore(metric_path)'
    new='    from features.fb_auto_posts.metric_reader import open_existing_metrics\n    metric_store = open_existing_metrics(FBAutoPostStore, metric_path)'
    text=target.read_text();assert text.count(old)==1
    assert sha(target)==sha(previous/name),'service_baseline_changed'
    content=text.replace(old,new,1).encode();compile(content,name,'exec');atomic(target,content)
    manifest['files'][name]=dict(before=sha(previous/name),after=sha(target))
    name='features/fb_auto_posts/metric_reader.py';atomic(candidate/name,(source/name).read_bytes())
    manifest['files'][name]=dict(before=sha(previous/name),after=sha(candidate/name))
    manifest['controller_commit']=commit;save(folder/'manifest.json',manifest)
    print(json.dumps(dict(status='prepared',commit=commit,scope='FB metric reader startup')))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--folder',required=True,type=Path);a=p.parse_args();prepare(a.folder)
