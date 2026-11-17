"""Material product identity for Post catalogs; publishing accounts stay scoped separately."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

_APP = ContextVar('post_material_app_id', default=1479)


def source_app_sql(alias, schema):
    if not all(re.fullmatch(r'[A-Za-z0-9_]+', value) for value in (alias, schema)):
        raise ValueError('invalid catalog identifier')
    # Social-only/blank product labels retain their existing DramaWave mapping.
    # Named products resolve through the owning catalog, with no product allowlist.
    # A duplicate app name fails the scalar query instead of selecting an arbitrary ID.
    return ("COALESCE((SELECT a.id FROM `%s`.ads_apps_setting a "
            "WHERE LOWER(TRIM(a.name))=LOWER(TRIM(%s.product)) "
            "COLLATE utf8mb4_unicode_ci),1479)" % (schema, alias))


def current_app_id():
    return _APP.get()


@contextmanager
def source_app_scope(app_id):
    value = int(app_id or 1479)
    if value <= 0:
        raise ValueError('invalid material app identity')
    token = _APP.set(value)
    try:
        yield
    finally:
        _APP.reset(token)
