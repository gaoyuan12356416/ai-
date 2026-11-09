"""Read exactly selected free episodes without scheduling a drama-pool run."""

import json
import math
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from features.x_accounts.language import canonical_drama_language, same_drama_language
from features.x_posts.drama_selector import (
    DramaSelectionError, _nonnegative_int, _normalize_media_url, _positive_int,
    _text, build_name_tag,
)
from features.x_posts.selector import DramawaveCandidateSelector, PoolCandidateRejection, previous_source_date
from features.x_posts.service import XPostError
from features.x_posts.source_duration import probe_source_metadata_duration

from .errors import PublishingError
from .material_identity import normalize_material_id


class _DeploymentSelector(DramawaveCandidateSelector):
    def __init__(self, deployments):
        super().__init__(None)
        self.deployments = deployments

    def _drama_deploy_rows(self, content_id, language, *, canonical_language=False):
        result = []
        for row in self.deployments.get(content_id, []):
            try:
                mapped = canonical_drama_language(row.get('language'), default='')
                if same_drama_language(mapped, language):
                    # The validator compares one canonical spelling. Treat the
                    # existing pt/pt-br alias as equivalent without changing the
                    # resource language saved in the candidate or any source row.
                    result.append({**row, 'language': language})
            except ValueError:
                continue
        return result


class EpisodeRepository:
    def __init__(self, mysql, schema):
        self.mysql, self.schema = mysql, schema

    def _read(self, episode_ids):
        marks = ','.join('%s' for _ in episode_ids)
        # Read the exact IDs, including ineligible rows for useful row-local
        # errors. Never load or require continuity of other episode resources.
        records = self.mysql.select(f'''SELECT bundle.row_kind,bundle.row_json FROM (
            (SELECT 'episode' row_kind,JSON_OBJECT(
                'resource_id',CAST(r.id AS CHAR),'app_id',r.app_id,'resource_type',r.type,
                'content_id',r.content_id,'drama_name',r.name,'drama_description',r.desc,
                'drama_labels',r.labels,'language',r.language,'series_code',r.series_code,
                'unlocked_episodes_count',r.unlocked_episodes_count,
                'sub_number',r.sub_number,'sub_name',r.sub_name,'sub_url',r.sub_url) row_json
             FROM `{self.schema}`.ads_drama_resource r
             WHERE r.id IN ({marks}) LIMIT 201)
            UNION ALL
            (SELECT 'deploy' row_kind,JSON_OBJECT(
                'content_id',i.content_id,'app_id',i.app_id,'app',i.app,
                'language',i.language,'deploy_time',i.deploy_time) row_json
             FROM (SELECT DISTINCT r.content_id FROM `{self.schema}`.ads_drama_resource r
                   WHERE r.id IN ({marks}) AND r.app_id=1479 AND r.type=2 LIMIT 200) selected_contents
             STRAIGHT_JOIN `{self.schema}`.ads_drama_info i FORCE INDEX(ac)
                ON i.content_id=selected_contents.content_id
             WHERE i.app_id=1479 ORDER BY i.app LIMIT 4001)
            ) bundle''', [*episode_ids, *episode_ids])
        buckets = {'episode': [], 'deploy': []}
        try:
            for record in records:
                row = json.loads(record['row_json'])
                if not isinstance(row, dict):
                    raise ValueError()
                buckets[record['row_kind']].append(row)
        except (KeyError, ValueError, TypeError):
            raise PublishingError('source_result_invalid', '免费集数据格式无效，请稍后重试', 503) from None
        if len(buckets['episode']) > 200 or len(buckets['deploy']) > 4000:
            raise PublishingError('source_mapping_too_large', '免费集关联数据超出校验范围，请缩小选择后重试', 503)
        return buckets['episode'], buckets['deploy']

    @staticmethod
    def _candidate(mid, row, selector, deadline):
        try:
            if normalize_material_id(row.get('resource_id')) != mid:
                raise ValueError()
            if _positive_int(row.get('app_id'), 'app_id') != 1479 or _positive_int(row.get('resource_type'), 'type') != 2:
                raise PoolCandidateRejection('free_episode_unavailable', '该 ID 不属于 Dramawave 视频免费集')
            number = _nonnegative_int(row.get('sub_number'), 'sub_number')
            unlocked = _nonnegative_int(row.get('unlocked_episodes_count'), 'unlocked_episodes_count')
            if number == 0 or number > unlocked:
                raise PoolCandidateRejection('episode_not_free', '该 ID 不是可发布的免费集，请填写免费集视频 ID')
            content_id = _text(row.get('content_id'), 'content_id', limit=128)
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', content_id):
                raise ValueError()
            series_code = _text(row.get('series_code'), 'series_code', limit=128)
            name = _text(row.get('drama_name'), 'drama name', limit=255)
            description = _text(row.get('drama_description'), 'drama description', limit=4096, normalize_whitespace=True)
            labels = _text(row.get('drama_labels'), 'drama labels', required=False, limit=512)
            language = canonical_drama_language(row.get('language'), default='')
            sub_name = _text(row.get('sub_name'), 'episode name', required=False, limit=255)
            url = _normalize_media_url(row.get('sub_url'))
        except (DramaSelectionError, ValueError, TypeError):
            raise PoolCandidateRejection('free_episode_metadata_invalid', '免费集的剧目、语言或视频地址不完整，请核对源库信息') from None
        deployed_at = selector._validate_drama_deploy_time(content_id, language, canonical_language=True)
        # The current resource catalog has no video-duration column. The shared
        # probe reads bounded MP4 metadata and caches verified URL measurements;
        # it never downloads the full video or writes a guessed duration to SQL.
        try:
            duration = float(row.get('video_duration') or 0)
        except (TypeError, ValueError, OverflowError):
            raise PoolCandidateRejection('material_duration_invalid', '免费集视频时长数据无效') from None
        if not math.isfinite(duration):
            raise PoolCandidateRejection('material_duration_invalid', '免费集视频时长数据无效')
        if duration <= 0:
            try:
                duration = float(probe_source_metadata_duration(url, deadline=min(deadline, time.monotonic() + 8)))
            except (XPostError, ValueError, TypeError, OSError):
                raise PoolCandidateRejection('material_duration_missing', '免费集已找到，但暂未读到视频时长，请稍后重试') from None
        if not math.isfinite(duration) or duration <= 0:
            raise PoolCandidateRejection('material_duration_missing', '免费集已找到，但暂未读到视频时长，请稍后重试')
        if duration > 14400:
            raise PoolCandidateRejection('material_duration_exceeds_limit', '免费集视频时长超过 4 小时上限')
        tag = next((value.strip() for value in labels.split(',') if value.strip()), name)
        return {
            'material_id': mid, 'id': mid, 'material_key': mid,
            'source_kind': 'free_episode', 'resource_id': mid, 'episode_number': number,
            'source_type': 'material', 'source_date': previous_source_date(),
            'pool_item_id': None, 'pool_created_at': '', 'media_kind': 'video',
            'content_id': content_id, 'series_code': series_code,
            'material_name': sub_name or f'{name} 第 {number} 集',
            'drama_name': name, 'description': description, 'drama_description': description,
            'material_language': language, 'language': language, 'drama_language': language,
            'material_url': url, 'preview_url': url, 'thumbnail_url': '',
            'source_duration': duration, 'duration_seconds': duration,
            'drama_deploy_time': deployed_at, 'tag': tag, 'name_tag': build_name_tag(name, labels),
            'spend': 0.0, 'facebook_violation_count': 0, 'tiktok_violation_count': 0,
            'twitter_violation_count': 0, 'resource_audit_count': 0, 'dangerous_tag_count': 0,
        }

    def resolve(self, episode_ids, *, deadline=None):
        deadline = time.monotonic() + 65 if deadline is None else deadline
        rows, deployment_rows = self._read(episode_ids)
        by_id, deployments = defaultdict(list), defaultdict(list)
        for row in rows:
            try:
                mid = normalize_material_id(row.get('resource_id'))
            except ValueError:
                continue
            by_id[mid].append(row)
        for row in deployment_rows:
            deployments[str(row.get('content_id') or '')].append(row)
        selector = _DeploymentSelector(deployments)
        result, errors = {}, {}
        def resolve_one(mid):
            try:
                found = by_id[mid]
                if not found:
                    raise PoolCandidateRejection('free_episode_not_found', '未找到该免费集 ID')
                if len(found) != 1:
                    raise PoolCandidateRejection('free_episode_ambiguous', '该免费集 ID 存在重复源数据，请核对源库信息')
                return mid, self._candidate(mid, found[0], selector, deadline), None
            except PoolCandidateRejection as exc:
                return mid, None, {'code': exc.error_code, 'message': exc.error_message}
            except DramaSelectionError:
                return mid, None, {'code': 'free_episode_metadata_invalid', 'message': '免费集元数据无效，请核对源库信息'}
        # Match the shared probe's two slots. Independent episode metadata reads
        # progress together, while each read and the entire request stay bounded.
        # Cache hits remain usable even after the network-read budget is spent.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='x-episode-metadata') as executor:
            for mid, item, error in executor.map(resolve_one, episode_ids):
                if error is None:
                    result[mid] = item
                else:
                    errors[mid] = error
        return {'items': result, 'errors': errors}
