"""Read-only production-snapshot acceptance, with an independent income total."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import time

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SRC))
from features.social_material_effects import data, report, window


def verify(root, end, days):
    start = (date.fromisoformat(end)-timedelta(days=days-1)).isoformat()
    results = {}
    for channel in ('x','facebook','all'):
        began = time.monotonic()
        actual = report.query(dict(channel=[channel],start=[start],end=[end]),dict(role='admin'),root)
        seconds = time.monotonic()-began
        with data.reading(data.channels(channel),root) as (db,meta):
            expected = 0
            for source in data.channels(channel):
                value = db.execute(f'SELECT SUM(i.revenue_cents) FROM {source}.income_day i '
                    f'JOIN {source}.income_scope s USING(post_id) JOIN {source}.posts p USING(post_id) '
                    'WHERE i.day BETWEEN ? AND ? AND i.day BETWEEN s.start AND s.end '
                    "AND COALESCE(date(p.published_at),'0001-01-01')<=i.day "
                    'AND NOT EXISTS(SELECT 1 FROM json_each(s.missing) j WHERE j.value=i.day)',(start,end)).fetchone()[0]
                expected += value or 0
        assert actual['summary']['revenue_cents'] == expected, (channel,actual['summary']['revenue_cents'],expected)
        assert all(start<=r['data_date']<=end for r in actual['items'])
        sample = actual['items'][0] if actual['items'] else None
        if sample:
            detail = report.query({k:[v] for k,v in dict(view='posts',channel=sample['channel'],
                start=sample['data_date'],end=sample['data_date'],namespace=sample['namespace'],
                material_id=sample['material_id']).items()},dict(role='admin'),root)
            assert detail['total'] == sample['post_count'],(detail['total'],sample['post_count'])
            for metric in data.METRICS:
                assert detail['summary'][metric] == sample['known'][metric],(metric,detail['summary'][metric],sample['known'][metric])
        results[channel] = dict(seconds=round(seconds,3),total=actual['total'],materials=actual['material_count'],
            data_days=actual['data_days'],summary=actual['summary'],coverage=actual['coverage'],sample=sample,
            independent_income_cents=expected,versions={c:m['version'] for c,m in meta.items()})
        print(json.dumps(dict(channel=channel,days=days,seconds=round(seconds,3),total=actual['total'],
                              income=expected,matched=True)),flush=True)
    return dict(start=start,end=end,results=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(data.ROOT))
    parser.add_argument('--end',default=window.bounds(1)[1])
    parser.add_argument('--days',type=int,nargs='+',default=[1,7,30])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    results=[]
    for days in args.days:
        results.append(verify(args.root,args.end,days))
        args.output.write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
