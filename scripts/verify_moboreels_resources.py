"""Bounded replica-only acceptance; no runtime/store constructors or publishing."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

p=argparse.ArgumentParser();p.add_argument('--candidate',required=True);p.add_argument('--mobo-candidate',required=True);p.add_argument('--snapshot',required=True);p.add_argument('--output',required=True);args=p.parse_args()
sys.path.insert(0,args.candidate)
from features.moboreels_library.settings import Settings
from features.moboreels_hit import resolve,classifications
spec=importlib.util.spec_from_file_location('acceptance_mobo_db',Path(args.mobo_candidate)/'features/moboreels_library/db.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
s=Settings.from_env();s.mysql_binary='/usr/bin/mysql.real';db=module.Database(s)
def query(sql,params):
    for value in params:sql=sql.replace('%s',module._literal(value),1)
    head,tail=sql.split(' FROM ',1)
    columns=head.removeprefix('SELECT ').removeprefix('DISTINCT ').split(',')
    expression=','.join("'%s',%s"%(column.split('.')[-1].strip(),column.strip()) for column in columns)
    return db._read('SELECT DISTINCT JSON_OBJECT('+expression+') FROM '+tail+';')
snapshot=json.loads(Path(args.snapshot).read_text())
rows=snapshot.get('list') or snapshot.get('rows') or snapshot.get('data',{}).get('list')
assert rows,'snapshot_rows_missing'
ids=[str(row['story_id']) for row in rows]
mapping=db.recommendation_resource_ids(ids);assert len(mapping)==len(set(ids))
assert mapping['164754322']=='XEY468' and mapping['147905322']=='XEY463'
targets={code:resolve(query,'kunlunads_dev',dict(id_type='resource_id',drama_id=code)) for code in sorted(set(mapping.values()))}
for story in ids:
    assert story in targets[mapping[story]]['content_ids']
    assert resolve(query,'kunlunads_dev',dict(id_type='content_id',drama_id=story))['content_ids']==[story]
all_ids=sorted({cid for target in targets.values() for cid in target['content_ids']})
metadata=classifications(query,all_ids)
codes=','.join(module._literal(cid) for cid in all_ids)
materials=db._read("SELECT JSON_OBJECT('product',product,'language',language,'count',COUNT(*)) FROM kunlunads_dev.ads_custom_source WHERE data_source_id IN ("+codes+") AND product IN ('Moboreels','Moboshort') AND type=2 AND is_delete=0 GROUP BY product,language;")
result=dict(recommendations=len(ids),unique_resources=len(targets),language_versions=len(all_ids),
            resource_ids=sorted(targets),samples={k:mapping[k] for k in ids[:3]},
            classified=len(metadata),known_audience=sum(m['audience']!='__unknown__' for m in metadata.values()),
            known_theme=sum(m['themes']!=['__unknown__'] for m in metadata.values()),materials=materials,publications=0)
Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=True))
