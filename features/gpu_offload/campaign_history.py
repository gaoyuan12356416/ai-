"""Merge existing historical facts from the local index without expanding them."""
from contextlib import closing
import json
import sqlite3

from .campaign import service as original


def write_merged_seed(wire, database, previous_meta, dates, next_history, output):
    """Preserve merge_history's row order, membership and date semantics.

    The public index contains the same fact rows as the private refresh seed;
    private Campaign mapping metadata stays in the newly encoded Campaigns.
    Only one previous fact is materialized at a time.
    """
    refreshed=set(dates)
    ci,pi=wire['cf'].index('campaign_id'),wire['cf'].index('platform')
    valid_campaigns={(int(row[pi] or 0),row[ci]) for row in wire['c']}
    ei=wire['xf'].index('entity_key')
    valid_entities={row[ei] for row in wire['x']}
    meta=dict(wire['m']);minimum=None;maximum=None;first=True

    def field(key):
        nonlocal first
        if not first:output.write(',')
        first=False;output.write(json.dumps(key)+':')

    def record_date(value):
        nonlocal minimum,maximum
        minimum=value if minimum is None else min(minimum,value)
        maximum=value if maximum is None else max(maximum,value)

    def dump(value):json.dump(value,output,ensure_ascii=False,separators=(',',':'),default=original.json_default)

    output.write('{')
    for key,value in wire.items():
        if key in ('d','xd','m'):continue
        field(key);dump(value)
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-16384')
        for table,fields_key in (('d','df'),('xd','xdf')):
            fields=wire[fields_key];day=fields.index('dt');count=0
            old_fields=json.loads(db.execute('SELECT v FROM meta WHERE k=?',(fields_key,)).fetchone()[0])
            positions={name:i for i,name in enumerate(old_fields)}
            field(table);output.write('[')
            for row in wire[table]:
                if count:output.write(',')
                dump(row);record_date(row[day]);count+=1
            for (encoded,) in db.execute('SELECT payload FROM '+table+' ORDER BY seq'):
                row=json.loads(encoded)
                if row[positions['dt']] in refreshed:continue
                valid=(int(row[positions['platform']] or 0),row[positions['campaign_id']]) in valid_campaigns if table=='d' else row[positions['entity_key']] in valid_entities
                if not valid:continue
                if count:output.write(',')
                dump([row[positions[name]] if name in positions else None for name in fields])
                record_date(row[positions['dt']]);count+=1
            output.write(']')
    meta.update(stat_start=minimum or '',stat_end=maximum or '',
                refreshed_through=max([*dates,previous_meta.get('refreshed_through') or previous_meta.get('stat_end','')],default=''),
                history_next_date=next_history,refreshed_dates=sorted(dates),refresh_lookback_days=original.REFRESH_LOOKBACK_DAYS)
    field('m');dump(meta);output.write('}')
    return meta
