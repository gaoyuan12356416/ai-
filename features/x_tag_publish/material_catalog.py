"""Request-local product catalogs for tag auto publishing.

Mobo story IDs remain in their own catalog; no DramaWave identity, release
date, advertising metrics or label mapping is borrowed for those stories.
"""
import copy
import hashlib
import os
from datetime import datetime, timezone

from features.x_auto_posts.repositories import BlacklistSnapshot, DramaSourceRow, MetricWindowSnapshot

MOBO_PRODUCTS = frozenset({'moboreels', 'moboshort'})


class CatalogSource:
    def __init__(self, original, products):
        self.original = original
        self.products = tuple(products)
        self.mobo_products = tuple(p for p in products if p.casefold() in MOBO_PRODUCTS)
        self.has_dramawave = any(p.casefold() not in MOBO_PRODUCTS for p in products)
        self.mobo_ids = set()

    def __getattr__(self, name):
        return getattr(self.original, name)

    def blacklist_snapshot(self):
        if self.has_dramawave:
            return self.original.blacklist_snapshot()
        return BlacklistSnapshot(frozenset(), frozenset(), datetime.now(timezone.utc).isoformat(),
                                 0, hashlib.sha256(b'moboreels:independent-story-namespace').hexdigest())

    def list_drama_rows(self, **kwargs):
        rows = list(self.original.list_drama_rows(**kwargs)) if self.has_dramawave else []
        # The Mobo catalog supplies no timezone-qualified launch date or the
        # DramaWave resource classifications. Those explicit filters cannot match.
        if kwargs.get('resource_types') or kwargs.get('deploy_since_epoch'):
            return rows
        from features.x_posts.moboreels import LANGUAGES
        from features.moboreels_library.settings import read_env_file
        from features.moboreels_library.macro_client import MoboreelsLinkError
        language = kwargs['language']
        from features.hit_drama_target import target_language
        lang_ids = [key for key, value in LANGUAGES.items() if target_language(value) == target_language(language)]
        if not lang_ids:
            return rows
        config = read_env_file(os.environ.get('MOBOREELS_MACRO_ENV_FILE', '/etc/moboreels-macro.env'))
        institution = os.environ.get('MOBOREELS_INSTITUTION_USER_ID', config.get('MOBOREELS_INSTITUTION_USER_ID', ''))
        if not institution:
            raise MoboreelsLinkError('Moboreels 自动选剧机构范围尚未配置')
        placeholders = ','.join(['%s'] * len(self.mobo_products))
        sql = f'''SELECT d.story_id,d.story_name FROM ads_ai.cdreader_koc_drama d
          WHERE d.institution_user_id=%s AND d.project_type=2 AND d.is_show=1 AND d.lang_id=%s
            AND EXISTS (SELECT 1 FROM `{self.schema}`.ads_custom_source s
              WHERE s.data_source_id=d.story_id AND s.data_source=%s
                AND s.product IN ({placeholders}) AND s.type=2 AND s.is_delete=0
                AND LOWER(TRIM(s.language))=LOWER(%s))
          ORDER BY d.story_id LIMIT 10001'''
        candidates = list(self.original._stream_select(sql,
            (institution, lang_ids[0], self.material_data_source, *self.mobo_products, language)))
        if len(candidates) > 10000:
            raise MoboreelsLinkError('Moboreels 候选范围过大，请缩小素材池')
        for row in candidates:
            sid = str(row['story_id'])
            self.mobo_ids.add(sid)
            rows.append(DramaSourceRow(sid, sid, sid, language, '', 0, 0, 1,
                                       str(row['story_name']), 'Moboreels', catalog='moboreels'))
        return rows


class CatalogMetrics:
    def __init__(self, original, source):
        self.original, self.source = original, source

    def load(self, **kwargs):
        ids = tuple(i for i in kwargs['content_ids'] if i not in self.source.mobo_ids)
        if self.source.has_dramawave:
            return self.original.load(**dict(kwargs, content_ids=ids))
        return MetricWindowSnapshot(kwargs['platform'], (), {}, {})


def for_request(selector, request):
    products = request.rules.material_products
    if isinstance(selector.source, CatalogSource) or not any(p.casefold() in MOBO_PRODUCTS for p in products):
        return selector
    # A shallow clone preserves the claimed store and the same per-thread audit
    # context, without mutating a selector shared by concurrent requests.
    result = copy.copy(selector)
    result.source = CatalogSource(selector.source, products)
    result.metrics = CatalogMetrics(selector.metrics, result.source)
    return result
