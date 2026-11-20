"""Resolve Post drama catalogs; DramaWave material variants share catalog 1479."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

_APP = ContextVar('post_material_app_id', default=1479)

# Material-library product labels are not separate drama libraries. Keep this
# family narrow: MoboShort/Moboreels resolve through the independent AI library;
# HotDrama and other products retain their own internal catalog lookup.
DRAMAWAVE_MATERIAL_PRODUCTS = (
    'drama-b', 'drama-c', 'drama-comics', 'drama-novel',
    'drama-ai素材', 'drama-社媒专用素材', '[w2a]drama-double',
)
# Readers without a product label (the existing YouTube SELECT contract) use
# the current product-catalog identities. Keep attribution IDs unchanged.
MOBO_MATERIAL_APP_IDS = frozenset({'2373', '2430', '2432', '2471', '3360', '3442', '3642'})
MOBO_MATERIAL_PRODUCTS = ('社媒moboreel',)


def is_mobo_product(product, app_id=None):
    """Recognize Mobo materials independently of the chosen caption macros."""
    value = str(product or '').strip().casefold()
    return (value.startswith(('mobo', '[w2a]mobo')) or value in MOBO_MATERIAL_PRODUCTS
            or str(app_id or '').strip() in MOBO_MATERIAL_APP_IDS)


def mobo_product_sql(alias):
    if not re.fullmatch(r'[A-Za-z0-9_]+', alias):
        raise ValueError('invalid catalog identifier')
    value = 'LOWER(TRIM(%s.product))' % alias
    variants = ','.join("'%s'" % name for name in MOBO_MATERIAL_PRODUCTS)
    return "LOCATE('mobo',%s)=1 OR LOCATE('[w2a]mobo',%s)=1 OR %s IN (%s)" % (value, value, value, variants)


def dramawave_product_sql(alias):
    if not re.fullmatch(r'[A-Za-z0-9_]+', alias):
        raise ValueError('invalid catalog identifier')
    product = "LOWER(TRIM(%s.product))" % alias
    variants = ','.join("'%s'" % name for name in DRAMAWAVE_MATERIAL_PRODUCTS)
    return ("LOCATE('dramawave',{product})=1 OR "
            "LOCATE('[w2a]dramawave',{product})=1 OR "
            "{product} IN ({variants})").format(product=product, variants=variants)


def source_app_sql(alias, schema):
    if not all(re.fullmatch(r'[A-Za-z0-9_]+', value) for value in (alias, schema)):
        raise ValueError('invalid catalog identifier')
    family = dramawave_product_sql(alias)
    # Resolve family variants before the app-name lookup, so a material product
    # ID such as Drama-comics never replaces the authoritative drama app ID.
    # Other named products keep the exact prior scalar lookup and blank fallback.
    return ("CASE WHEN %s THEN 1479 ELSE "
            "COALESCE((SELECT a.id FROM `%s`.ads_apps_setting a "
            "WHERE LOWER(TRIM(a.name))=LOWER(TRIM(%s.product)) "
            "COLLATE utf8mb4_unicode_ci),1479) END" % (family, schema, alias))


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
