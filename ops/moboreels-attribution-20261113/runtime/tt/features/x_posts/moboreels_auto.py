"""Moboreels auto-selection adapter retaining the existing material boundary."""
import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal

from .moboreels import LANGUAGES, source_enabled
from features.moboreels_library.macro_client import MoboreelsLinkError


def validate_rules(rules):
    # Empty and zero lower bounds are the existing UI defaults. A positive
    # bound/explicit cap would misrepresent unrelated Dramawave advertising data.
    for rule in (rules.drama, rules.material):
        for metric in (rule.spend, rule.d0_roas):
            if (metric.minimum is not None and metric.minimum > 0) or metric.maximum is not None:
                raise MoboreelsLinkError('Moboreels 模板不能使用 Dramawave 消耗或 ROAS 筛选，请清空这些筛选')
    if rules.drama.resource_types or rules.drama.launch_window_days:
        raise MoboreelsLinkError('Moboreels 上游分类及无时区上架日期不能按 Dramawave 筛选解释，请清空资源分类和上线天数')


class Source:
    def __init__(self, original, module):
        self.original, self.module = original, module

    def blacklist_snapshot(self):
        # Legacy blacklist keys belong to the internal series namespace.
        return self.module.BlacklistSnapshot(drama_series_codes=frozenset(), material_data_source_ids=frozenset(),
            sha256=hashlib.sha256(b'moboreels:independent-story-namespace').hexdigest(),
            loaded_at_utc=datetime.now(timezone.utc).isoformat(), source_row_count=0)

    def list_drama_rows(self, *, language, now_epoch, deploy_since_epoch=None, resource_types=()):
        ids = [key for key, value in LANGUAGES.items() if value.lower() == str(language).lower()]
        if not ids:
            return []
        repo = self.original
        from features.moboreels_library.settings import read_env_file
        config = read_env_file(os.environ.get('MOBOREELS_MACRO_ENV_FILE', '/etc/moboreels-macro.env'))
        institution = os.environ.get('MOBOREELS_INSTITUTION_USER_ID', config.get('MOBOREELS_INSTITUTION_USER_ID', ''))
        if not institution:
            raise MoboreelsLinkError('Moboreels 自动选剧机构范围尚未配置')
        # Keep the original product/data-source/video/deletion/language scope.
        # No unconstrained source scan or old drama_info join is used.
        sql = f'''SELECT d.story_id,d.story_name,d.lang_id FROM `ads_ai`.cdreader_koc_drama d
            WHERE d.institution_user_id=%s AND d.project_type=2 AND d.is_show=1 AND d.lang_id=%s
            AND EXISTS (SELECT 1 FROM `{repo.schema}`.ads_custom_source s
                WHERE s.data_source_id=d.story_id AND s.data_source=%s AND s.product=%s
                  AND s.type=2 AND s.is_delete=0 AND LOWER(TRIM(s.language))=LOWER(%s))
            ORDER BY d.story_id LIMIT 10001'''
        rows = list(repo._stream_select(sql, (institution,ids[0],repo.material_data_source,repo.product,language)))
        if len(rows) > 10000:
            raise MoboreelsLinkError('Moboreels 候选范围过大，请缩小素材池')
        result = []
        for row in rows:
            sid = str(row['story_id'])
            result.append(self.module.DramaSourceRow(source_row_id=sid,content_id=sid,
                series_code=sid,language=language,resource_type_v2='moboreels',
                deploy_time=1,app_id=repo.app_id,release_status=1,
                name=str(row['story_name']),app='Moboreels'))
        return result

    def list_material_rows(self, **kwargs):
        return self.original.list_material_rows(**kwargs)


class Metrics:
    def __init__(self, module):
        self.module = module

    def load(self, **kwargs):
        module = self.module
        class Empty:
            def drama(self, content_id):
                return module.MetricTotals()
            def material(self, content_id, material_id):
                return module.MetricTotals()
        return Empty()


def adapt(selector, rules, module):
    """Construct a request-local selector; no shared selector state is mutated."""
    validate_rules(rules)
    return type(selector)(Source(selector.source, module), Metrics(module),
        getattr(selector, "history", None) or selector.legacy_reader, selector.store, material_validator=selector.material_validator,
        product=selector.product, app_id=selector.app_id,
        material_data_source=selector.material_data_source)
