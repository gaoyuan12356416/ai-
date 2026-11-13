import json,re,collections,hashlib,datetime,argparse,os
from pathlib import Path
import pymysql
from consumer import load_library, select
parser=argparse.ArgumentParser();parser.add_argument('--workdir',type=Path,required=True);args=parser.parse_args()
P=args.workdir.resolve();OUT=P/'site';base=json.loads((OUT/'data.json').read_text())
cat=json.loads((P/'catalog.json').read_text());library=load_library(P,os.environ.get('REPORT_MATERIAL_CATALOG_CONFIG','/root/.codex/plugins/feishu/config.json'));effects=json.loads((P/'effects.json').read_text())
catalog=collections.defaultdict(set)
for r in cat:catalog[str(r['content_id'])].add((str(r['series_code']),r['language'].lower()))
def norm(s):
 s=s.lower().strip();s=re.sub(r'^(?:en验证[_-])?ai(?:翻转|小语种|解说)-[a-z-]+-\d+-\d+:\d+-','',s);s=re.sub(r'^[a-z-]+-\d+-00:00-','',s)
 s=s.replace('en已验证_','').replace('en已验证','en').replace('纯净版','').replace('en验证','').replace('_翻转_','_')
 s=re.sub(r'(?<=[_-])(?:en|tw|zh-tw|ar|de|el|es|fr|hi|id|it|ja|ko|ms|pl|pt|ru|th|tl|tr|vi|小语种)(?=[_-])','',s)
 s=re.sub(r'(?:[._-](?:mp4|png|jpg|jpeg))+$','',s)
 return re.sub(r'[^\w\u4e00-\u9fff]','',s).replace('_','')
originals=collections.defaultdict(set)
for resource,file in base['materials']['files'].items():
 shard=json.loads((OUT/file).read_text());originals[resource].update(shard['meta'])
selection=select(catalog,originals,library)
kept=selection['kept'];potential=set(selection['potential'])
print('candidate materials',len(potential),'kept',sum(map(len,kept.values())),flush=True)
cfg=json.loads(Path('/root/.codex/plugins/feishu/config.json').read_text())['mysql'];db=pymysql.connect(host=cfg['host'],port=int(cfg['port']),user=cfg['user'],password=cfg['password'],database='kunlunads_dev',charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,read_timeout=120);ids=sorted(potential);mapping=collections.defaultdict(list)
with db.cursor() as q:
 for offset in range(0,len(ids),2000):
  batch=ids[offset:offset+2000];q.execute('SELECT id,source_id FROM ads_source WHERE source_type=3 AND status=0 AND is_invalid=0 AND source_id IN ('+','.join(['%s']*len(batch))+')',batch)
  for r in q.fetchall():mapping[str(r['source_id'])].append(str(r['id']))
  if offset%40000==0:print('IDs',offset,'/',len(ids),flush=True)
db.close();performance=collections.defaultdict(list)
for r in effects:
 mid=str(r['resource_id'])
 if mid in potential:performance[mid].append([str(r['dt']),float(r['spend'] or 0),float(r['revenue'] or 0),float(r['event2'] or 0)])
(OUT/'localizations').mkdir(exist_ok=True);manifest={};valid_count=0
for resource,mats in kept.items():
 groups=collections.defaultdict(list)
 for mid,r in mats.items():
  r['ids']=sorted(set(mapping[mid]));valid_count+=bool(r['ids'])
  if r['ids'] and r['l'].lower()!='en':groups[r['k']].append(mid)
 per={mid:performance[mid] for mids in groups.values() for mid in mids if mid in performance}
 f='localizations/'+hashlib.sha256(resource.encode()).hexdigest()[:20]+'.json';manifest[resource]=f
 (OUT/f).write_text(json.dumps({'materials':mats,'groups':groups,'performance':per,'matches':selection['matches'][resource],'matching':'loose-name-v1-manual-review'},ensure_ascii=False,separators=(',',':')))
meta={'today':base['today'],'start':base['start'],'effectsUpdated':max(str(r['updated']) for r in effects),'inventoryTime':(P/'inventory-time.txt').read_text(),'idTime':datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'files':manifest,'validCandidateMaterials':valid_count,'matching':'loose-name-v1-manual-review','scope':'Dramawave五产品、Facebook＋TikTok、全部优化师；按小语种素材库ID累计。宽松名称相似候选（3字符片段Dice≥55%且共有≥6片段，或核心名包含且长度≥12）；不是内容同版证明，全部需逐条人工核对。'}
(OUT/'low-language-meta.json').write_text(json.dumps(meta,ensure_ascii=False,separators=(',',':')))
print(json.dumps({'files':len(manifest),'valid':valid_count,'updated':meta['effectsUpdated']},ensure_ascii=False),flush=True)
