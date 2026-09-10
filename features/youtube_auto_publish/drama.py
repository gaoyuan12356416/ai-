"""Bounded drama enrichment, independent of the replaceable material filter."""
import json
import re
from .reference import normalize_reference_url
from .templates import WorkflowError


class DramaMetadataResolver:
    def __init__(self, query_runner, schema='kunlunads_dev'):
        if not re.fullmatch(r'[A-Za-z0-9_]+', schema):
            raise ValueError('Invalid drama schema')
        self.query_runner, self.schema = query_runner, schema

    def __call__(self, materials):
        pairs = {(m['content_id'].strip(), m['language'].strip().casefold()) for m in materials
                 if 0 < len(m['content_id'].strip()) <= 256 and 0 < len(m['language'].strip()) <= 32}
        matches = {key: set() for key in pairs}
        covers = {key: set() for key in pairs}
        invalid_covers = set()
        if pairs:
            # Resource rows repeat per episode. DISTINCT collapses identical metadata,
            # while conflicting versions remain visible and must not be guessed.
            literal = lambda value: 'CONVERT(0x%s USING utf8mb4) COLLATE utf8mb4_unicode_ci' % value.encode('utf-8').hex()
            ids = ','.join(literal(value) for value in sorted({key[0] for key in pairs}))
            languages = ','.join(literal(value) for value in sorted({key[1] for key in pairs}))
            sql = ("SELECT DISTINCT HEX(JSON_OBJECT('content_id',r.content_id,"
                   "'language',LOWER(TRIM(r.language)),'name',TRIM(COALESCE(r.name,'')),"
                   "'desc',TRIM(COALESCE(r.`desc`,'')),'cover',TRIM(COALESCE(r.cover,'')))) "
                   "FROM `%s`.ads_drama_resource r FORCE INDEX (content_id) "
                   "WHERE r.content_id IN (%s) AND LOWER(TRIM(r.language)) IN (%s) LIMIT 1001"
                   % (self.schema, ids, languages))
            try:
                rows = self.query_runner(sql)
                if len(rows) > 1000:
                    raise ValueError('Ambiguous metadata result exceeds bound')
                for row in rows:
                    value = json.loads(bytes.fromhex(row[0]).decode('utf-8'))
                    key = (str(value['content_id']), str(value['language']).strip().casefold())
                    # Verify exact content IDs in Python too; SQL collation may ignore case.
                    if key in matches:
                        matches[key].add((str(value.get('name') or '').strip(),
                                          str(value.get('desc') or '').strip()))
                        # Cover differences must not invalidate localized text macros.
                        # Empty episode covers are ignored; invalid nonempty covers
                        # remain visible even when another episode has a valid URL.
                        raw_cover = str(value.get('cover') or '').strip()
                        if raw_cover:
                            cover = normalize_reference_url(raw_cover)
                            if cover:
                                covers[key].add(cover)
                            else:
                                invalid_covers.add(key)
            except Exception:
                raise WorkflowError('drama_query_failed','剧集信息查询暂不可用，请稍后重试',503) from None
        for material in materials:
            key = (material['content_id'].strip(), material['language'].strip().casefold())
            values = matches.get(key, set())
            material['macro_name'] = material['macro_desc'] = ''
            material['drama_status'] = 'matched' if len(values) == 1 else 'ambiguous' if values else 'missing'
            material['drama_message'] = ('剧集资料存在冲突，请联系管理员核对。' if len(values) > 1 else
                                         '未找到与素材剧集 ID、语言一致的剧集资料。' if not values else '')
            if len(values) == 1:
                material['macro_name'], material['macro_desc'] = next(iter(values))
            cover_values = covers.get(key, set())
            material['drama_cover_url'] = ''
            if key in invalid_covers:
                material['drama_cover_status'] = 'invalid'
                material['drama_cover_message'] = '原剧封面地址无效，请联系管理员核对或选择本地上传。'
            elif len(cover_values) > 1:
                material['drama_cover_status'] = 'ambiguous'
                material['drama_cover_message'] = '原剧封面存在多个不同版本，请联系管理员核对或选择本地上传。'
            elif cover_values:
                material['drama_cover_status'] = 'available'
                material['drama_cover_message'] = ''
                material['drama_cover_url'] = next(iter(cover_values))
            else:
                material['drama_cover_status'] = 'missing'
                material['drama_cover_message'] = '未找到与素材剧集 ID、语言一致的原剧封面，请选择本地上传。'
        return materials
