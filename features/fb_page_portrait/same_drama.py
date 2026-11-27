"""Share classification facts only for verified HotDrama/DramaWave identities."""
from collections import defaultdict


def language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    return {'jp': 'ja', 'pt': 'pt-br'}.get(value, value)


def verified_hotdrama_links(infos):
    identities = defaultdict(set)
    for row in infos:
        app = int(row.get('app_id', 1479))
        if app in (1479, 3302):
            key = (app, str(row['content_id']), language(row.get('language')))
            identities[key].add(str(row.get('series_code') or '').strip())
    links = {}
    for (app, cid, lang), series in identities.items():
        if (app == 3302 and cid and lang and len(series) == 1 and '' not in series
                and identities.get((1479, cid, lang)) == series):
            links[(app, cid, lang)] = dict(source_app_id=1479, content_id=cid,
                                          language=lang, series_code=next(iter(series)))
    return links


def matching_resources(rows, series):
    """A contradictory source resource must not supply another drama's labels."""
    return rows if rows and {str(r.get('series_code') or '').strip() for r in rows} == {series} else []
