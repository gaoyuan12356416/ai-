"""Read one complete, multi-language index for an unrestricted FB template run.

The index holds material IDs. Only ranked candidates need their URL and copy.
An invalid or concurrently removed winner is excluded and the same ranking is
refilled before the planner receives any Page snapshot.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from heapq import nsmallest
from itertools import islice
from types import SimpleNamespace
import time

from features.material_products import contains as has_product, selected
from features.fb_video_publish.moboreels import contains as mobo
from features.fb_video_publish.runtime_source import BrokerReadOnlyMySQL
from features.fb_video_publish.templates import TemplateError
from features.x_tag_revenue.core import language as profile_language
from .languages import page_language
from .metrics import MetricTotals
from .repositories import CandidateSnapshot, MaterialCandidate, MaterialRepository, RepositoryError
from .repositories import _decimal, _description, _https


def supports(config, materials):
    return (isinstance(materials, MaterialRepository) and config.get('drama_launch_window_days') == 0
            and not config.get('hit_drama_publish') and not mobo(config.get('message_template'))
            and (has_product(config, 'DramawaveSource') or config.get('_profile_selection')))


def placeholders(items):
    return ','.join('%s' for _ in items)


def batches(items, size):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def rank(totals, rule):
    value = getattr(totals, rule['sort_by'])
    return (value is None, 0 if value is None else (-value if rule['sort_direction'] == 'desc' else value))


def matches(totals, rule):
    return all(MaterialRepository._in_range(getattr(totals, key), rule[key + '_min'], rule[key + '_max'])
               for key in ('spend', 'roas'))


class Catalog:
    def __init__(self, repo, config, scopes, now, pool):
        self.repo, self.config, self.scopes, self.pool = repo, config, scopes, pool
        self.mysql = BrokerReadOnlyMySQL()
        self.schema = '`' + self.mysql.schema + '`'
        self.blacklist = '`' + self.mysql.blacklist_schema + '`.ads_facebook_post_blacklist'
        self.products = selected(config)
        self.end_epoch = int(now.timestamp())
        self.languages = sorted({language for language, _ in scopes})
        self.required_description = (any(required for _, required in scopes)
                                     or config.get('_page_caption_description')
                                     or '{{desc}}' in config.get('message_template', ''))
        self.deadline = time.monotonic() + 3300

    def select(self, sql, args=()):
        for attempt in range(3):
            if time.monotonic() >= self.deadline:
                raise RepositoryError('fb_auto_catalog_scan_timeout', '素材目录分批读取超时', 409)
            try:
                return self.mysql.select(sql, args)
            except TemplateError as exc:
                if exc.code not in {'source_busy', 'source_query_timeout','source_connection_failed'} or attempt == 2:
                    raise RepositoryError(exc.code, '素材目录只读查询暂不可用', 503) from exc
                time.sleep(attempt + 1)

    def source_predicate(self):
        rule = self.config['material_rule']
        sql = ("s.data_source=%s AND s.product IN (" + placeholders(self.products) + ")"
               " AND s.type=2 AND s.is_delete=0 AND s.video_duration>0 AND s.video_duration BETWEEN %s AND %s")
        return sql, (self.config['material_data_source'], *self.products,
                     rule['duration_min_seconds'], rule['duration_max_seconds'])

    def index(self):
        bounds = self.select(f'SELECT MAX(id) hi FROM {self.schema}.ads_custom_source')[0]
        groups = defaultdict(list)
        if bounds['hi'] is None:
            return groups
        predicate, args = self.source_predicate()
        # Traverse disjoint ranges. The cursor resumes from the last returned
        # id if a range exceeds the shared broker's bounded response size.
        sql = f"""SELECT /*+ MAX_EXECUTION_TIME(8000) */ TRIM(s.data_source_id) cid,
                         LOWER(TRIM(COALESCE(s.language,''))) lang,s.id
                    FROM {self.schema}.ads_custom_source s FORCE INDEX(PRIMARY)
                   WHERE s.id BETWEEN %s AND %s AND {predicate} AND s.data_source_id=TRIM(s.data_source_id)
                     AND (LOWER(TRIM(s.language)) IN ({placeholders(self.languages)})
                          OR (LOWER(s.product)='dramawavesource' AND TRIM(COALESCE(s.language,''))=''))
                   ORDER BY s.id LIMIT 16000"""
        hi = int(bounds['hi'])
        def chunk(lo):
            end = min(hi, lo + 63999)
            part = defaultdict(list)
            while lo <= end:
                rows = self.select(sql, (lo, end, *args, *self.languages))
                for row in rows:
                    if row['cid']:
                        part[row['cid'], row['lang']].append(int(row['id']))
                if len(rows) < 16000:
                    break
                lo = int(rows[-1]['id']) + 1
            return part
        for index, part in enumerate(self.pool.map(chunk, range(0, hi + 1, 64000)), 1):
            for key, mids in part.items():
                groups[key].extend(mids)
            if index % 20 == 0:
                print(f'fb_catalog_index ranges={index}/{hi // 64000 + 1}', flush=True)
        return groups

    def drama_facts(self, ids):
        types = self.config['drama_rule'].get('resource_type_v2') or []
        type_sql = ' AND CAST(d.resource_type_v2 AS CHAR) IN (' + placeholders(types) + ')' if types else ''
        def chunk(batch):
            marks = placeholders(batch)
            rows = self.select(f"""SELECT /*+ MAX_EXECUTION_TIME(8000) */ d.id,TRIM(d.content_id) cid,
                     d.name,d.language,d.deploy_time,CAST(d.resource_type_v2 AS CHAR) resource_type
                  FROM {self.schema}.ads_drama_info d FORCE INDEX(ac)
                 WHERE d.content_id IN ({marks}) AND d.app_id=%s AND d.release_status=1
                   AND d.deploy_time>0 AND d.deploy_time<=%s{type_sql}
                   AND NOT EXISTS (SELECT 1 FROM {self.blacklist} b WHERE b.is_delete=0
                                    AND b.type=1 AND b.content_id=d.content_id)
                   AND NOT EXISTS (SELECT 1 FROM {self.schema}.ads_drama_info other FORCE INDEX(ac)
                                    JOIN {self.blacklist} b ON b.content_id=other.series_code
                                     AND b.is_delete=0 AND b.type=0
                                   WHERE other.content_id=d.content_id AND other.app_id=d.app_id)""",
                               (*batch, self.config['app_id'], self.end_epoch, *types))
            details = {}
            for row in rows:
                current = details.get(row['cid'])
                if current is None or int(current['id']) < int(row['id']):
                    details[row['cid']] = row
            descriptions = {}
            if details and self.required_description:
                rows = self.select(f"""SELECT /*+ MAX_EXECUTION_TIME(8000) */ TRIM(r.content_id) cid,
                              LOWER(TRIM(r.language)) lang,MAX(TRIM(r.`desc`)) description
                         FROM {self.schema}.ads_drama_resource r FORCE INDEX(content_id)
                        WHERE r.content_id IN ({placeholders(details)}) AND r.app_id=%s AND r.type=2
                          AND TRIM(COALESCE(r.`desc`,''))<>''
                        GROUP BY r.content_id,LOWER(TRIM(r.language))
                       HAVING COUNT(DISTINCT BINARY TRIM(r.`desc`))=1""", (*details, self.config['app_id']))
                descriptions = {(row['cid'], row['lang']): _description(row['description']) for row in rows}
            return details, descriptions
        facts, descriptions = {}, {}
        for detail, copy in self.pool.map(chunk, batches(ids, 500)):
            facts.update(detail)
            descriptions.update(copy)
        return facts, descriptions

    def hydrate(self, ids):
        predicate, args = self.source_predicate()
        def chunk(batch):
            return self.select(f"""SELECT /*+ MAX_EXECUTION_TIME(8000) */ s.id,TRIM(s.data_source_id) cid,
                          s.url,s.name,s.tag_name,s.product,LOWER(TRIM(s.language)) lang,s.video_duration
                      FROM {self.schema}.ads_custom_source s FORCE INDEX(PRIMARY)
                     WHERE s.id IN ({placeholders(batch)}) AND {predicate}""", (*batch, *args))
        result = {}
        for rows in self.pool.map(chunk, batches(ids, 500)):
            for row in rows:
                url = _https(row['url'])
                if url:
                    result[int(row['id'])] = (row, url)
        return result


def choices(groups, scores, blocked, limit, per_drama):
    """Exact top-N per portrait bucket. The per-drama bound is within each language."""
    buckets = defaultdict(list)
    for cid, bucket, drama_rank, ids in groups:
        available = (mid for mid in ids if mid not in blocked)
        for mid in islice(available, per_drama):
            buckets[bucket].append((drama_rank, scores[mid] if mid in scores else scores[None], mid, cid))
    return sorted(item for bucket in buckets.values() for item in nsmallest(limit, bucket))


def snapshots(repo, config, scopes):
    if not scopes:
        return {}
    now = repo.now_fn()
    day = now.astimezone(timezone(timedelta(hours=8))).date()
    dates = tuple((day - timedelta(days=i)).isoformat() for i in range(config['metric_window_days'], 0, -1))
    metrics = repo.metric_store.load_metric_window(product=config['metric_product'],
                                                   platform=config['metric_platform'], dates=dates)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cat = Catalog(repo, config, scopes, now, pool)
        raw = cat.index()
        zero = MetricTotals()
        drama_rule, rule = config['drama_rule'], config['material_rule']
        ids = sorted({cid for cid, _ in raw if matches(metrics.by_drama.get(cid, zero), drama_rule)})
        facts, descriptions = cat.drama_facts(ids)
        print(f'fb_catalog_facts sources={len(ids)} eligible={len(facts)}', flush=True)
        by_group = defaultdict(list)
        for (cid, raw_lang), mids in raw.items():
            if cid in facts:
                lang = raw_lang or page_language(facts[cid]['language'])
                if lang in cat.languages and ((lang, False) in scopes or descriptions.get((cid, lang))):
                    by_group[lang, cid].extend(mids)
        del raw
        classes, profile_error = {}, ''
        if config.get('_profile_selection'):
            from .profile_selection import load_classifications
            try:
                class_ids = sorted({cid for _, cid in by_group})
                reader = SimpleNamespace(schema=cat.mysql.schema, select=cat.select)
                for classified in pool.map(lambda batch: load_classifications(reader, batch), batches(class_ids, 500)):
                    classes.update(classified)
            except Exception:
                profile_error = 'candidate_metadata_unavailable'
                classes.clear()

        limit = min(repo.candidate_limit, config.get('_candidate_limit', repo.candidate_limit))
        per_drama = min(limit, 20) if config.get('drama_cooldown_hours') else limit
        scores = {None: rank(zero, rule)}
        eligible_zero = matches(zero, rule)
        # Material metrics are keyed by both drama and material. Numeric IDs of
        # the fetched index are globally unique, including when an old metric
        # generation describes a material that subsequently changed dramas.
        history = metrics.by_material
        scope_groups = defaultdict(list)
        for (lang, cid), mids in by_group.items():
            eligible = []
            for mid in mids:
                totals = history.get((cid, str(mid)))
                if totals is None:
                    if eligible_zero:
                        eligible.append(mid)
                elif matches(totals, rule):
                    scores[mid] = rank(totals, rule)
                    eligible.append(mid)
            if not eligible:
                continue
            eligible.sort(key=lambda mid: (scores.get(mid, scores[None]), mid))
            classification = classes.get((cid, profile_language(lang)), {})
            bucket = (classification.get('audience', 'unmarked'), tuple(sorted(classification.get('themes') or [])))
            group = (cid, bucket, rank(metrics.by_drama.get(cid, zero), drama_rule), eligible)
            if (lang, False) in scopes:
                scope_groups[lang, False].append(group)
            if (lang, True) in scopes and descriptions.get((cid, lang)):
                scope_groups[lang, True].append(group)
        del by_group
        print(f'fb_catalog_ranked scopes={len(scopes)} classes={len(classes)}', flush=True)
        blocked, loaded = set(), {}
        while True:
            selected_rows = {scope: choices(scope_groups[scope], scores, blocked, limit, per_drama) for scope in scopes}
            needed = {mid: (scope[0], cid) for scope, rows in selected_rows.items() for _, _, mid, cid in rows
                      if mid not in loaded}
            if not needed:
                break
            details = cat.hydrate(sorted(needed))
            for mid, (lang, cid) in needed.items():
                pair = details.get(mid)
                if pair:
                    row, url = pair
                    resolved = row['lang'] or (page_language(facts[cid]['language'])
                                                if row['product'].casefold() == 'dramawavesource' else '')
                    if row['cid'] == cid and resolved == lang:
                        drama, mat, fact = metrics.by_drama.get(cid, zero), history.get((cid, str(mid)), zero), facts[cid]
                        loaded[mid] = MaterialCandidate(str(mid), cid, url, str(row['name'] or '')[:500],
                            str(fact['name'] or '')[:500], lang, _decimal(row['video_duration']), mat.spend, mat.roas,
                            drama.spend, drama.roas, str(fact['resource_type'] or ''), descriptions.get((cid, lang), ''),
                            str(row['tag_name'] or '').strip()[:255], int(fact['deploy_time']))
                        continue
                blocked.add(mid)
            if all(mid in loaded for mid in needed):
                break
        return {scope: CandidateSnapshot(tuple(loaded[mid] for _, _, mid, _ in rows), tuple(metrics.generation_ids),
                                         dates, classes, profile_error) for scope, rows in selected_rows.items()}
