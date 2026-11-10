import argparse
import json
from pathlib import Path

def render(snapshot, destination):
    data=json.loads(Path(snapshot).read_text(encoding='utf-8'))
    # Browser is a compact display projection. Full exact provenance stays in
    # the immutable database snapshot, avoiding duplicated multilingual labels.
    for page in data['pages']:
        page.pop('tags',None)
        page['delivery_languages']={}
        for drama in page['dramas']:
            key=drama['language']
            page['delivery_languages'][key]=page['delivery_languages'].get(key,0)+drama['spend']
        page['dramas']=[{k:v for k,v in d.items() if k in ('content_id','name','language','spend','af_revenue0')} |
                       {'classification':{k:d['classification'][k] for k in ('audience','theme_names')}}
                       for d in page['dramas'][:50]]
        # The language chart must retain all dramas, not the top-50 projection.
        page['geo']['countries']=page.get('geo',{}).get('countries',[])[:6]
    value=json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('&','\\u0026').replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    template=Path(__file__).with_name('report.html').read_text(encoding='utf-8')
    assert template.count('__PAYLOAD__')==1
    Path(destination).write_text(template.replace('__PAYLOAD__',value),encoding='utf-8')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('snapshot');p.add_argument('destination');a=p.parse_args();render(a.snapshot,a.destination)
