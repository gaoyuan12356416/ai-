"""Product contracts for screenshot covers only; no video-product expansion."""

PRODUCTS = (
    {"app_id": "1479", "app": "dramawave", "country": "us", "language": "en",
     "app_package": "com.dramawave.app", "cover_only": False},
    {"app_id": "979", "app": "freereels", "country": "us", "language": "en",
     "app_package": "com.freereels.app", "cover_only": False},
    {"app_id": "3360", "app": "moboreels", "country": "", "language": "",
     "app_package": "com.changdu.mobovideo", "cover_only": True},
    {"app_id": "2373", "app": "moboshort", "country": "", "language": "",
     "app_package": "com.changdu.moboshort", "cover_only": True},
)

# Keep these separate: the Mobo change must not change DramaWave's thresholds.
AUTO_PROFILES = (
    {"name": "dramawave", "product": "dramawave", "app_id": "1479",
     "min_spend": 1000.0, "spend_operator": ">=", "min_roas": 45.0},
    {"name": "moboreels", "product": "moboreels", "app_id": "3360",
     "min_spend": 1000.0, "spend_operator": ">", "min_roas": None},
    {"name": "moboshort", "product": "MoboShort", "app_id": "2373",
     "min_spend": 1000.0, "spend_operator": ">", "min_roas": None},
)


def get_product(app_id):
    app_id = str(app_id or "").strip()
    return next((dict(p) for p in PRODUCTS if p["app_id"] == app_id), None)


def list_products():
    return [{**{key: p[key] for key in ("app_id", "app", "country", "language")},
             "label": "%s | %s" % (p["app_id"], p["app"])} for p in PRODUCTS]


def app_package(app_id):
    product = get_product(app_id)
    return product["app_package"] if product else str(app_id or "").strip()


def sql_literal(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def sql_identifier(value):
    return "`" + str(value).replace("`", "``") + "`"


def resolve_cover_resource(app_id, content_id, query, database, table, normalize_url):
    """Mobo covers are metadata rows; they need no playable episode resources."""
    product = get_product(app_id)
    if not product or not product["cover_only"]:
        raise ValueError("product does not use metadata-only screenshot covers")
    sql = """SELECT CAST(app_id AS CHAR), name, cover, app, country, language
        FROM {db}.{table}
        WHERE app_id={app_id} AND content_id={content_id} AND type=2
          AND TRIM(COALESCE(cover, '')) <> ''
        ORDER BY updated_at DESC, id DESC LIMIT 1""".format(
            db=sql_identifier(database), table=sql_identifier(table),
            app_id=sql_literal(product["app_id"]), content_id=sql_literal(content_id))
    rows = query(" ".join(sql.split()))
    if not rows:
        raise ValueError("app_id=%s content_id=%s 未找到可用原封面" % (app_id, content_id))
    row = rows[0]
    if str(row[0]) != product["app_id"]:
        raise ValueError("cover resource product mismatch")
    url = normalize_url(row[2])
    if not url:
        raise ValueError("未找到可用封面素材")
    return {"app_id": product["app_id"], "total_episodes": 0, "app": product["app"],
            "country": row[4] or "", "language": row[5] or "",
            "drama_name": row[1] or "", "cover_source_url": url}
