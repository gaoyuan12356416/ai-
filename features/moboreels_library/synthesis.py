"""Adapt the existing MoboReels catalog and expiring episodes to composition jobs."""

PRODUCT = {"app_id": "3360", "app": "moboreels", "country": "", "language": "",
           "label": "3360 | moboreels"}
LANGUAGES = {2: "zh-tw", 3: "en", 4: "es", 5: "pt", 6: "fr", 7: "ru",
             8: "it", 9: "ja", 10: "ar", 11: "id", 12: "th", 13: "vi",
             14: "ko", 15: "tl", 16: "de", 17: "hi", 18: "bn", 21: "ms", 22: "tr"}


def include_product(items):
    if any(str(item.get("app_id")) == PRODUCT["app_id"] for item in items):
        return items
    return [*items, dict(PRODUCT)]


def fetch_episodes(content_id):
    # Reuse the library's scoped replica reads, signing and URL validation.
    # Build a fresh reader so submission and actual execution never share old URLs.
    from .catalog import Catalog
    from .content_tools import ContentTools
    from .db import Database, DatabaseError
    from .promotions import PromotionError
    from .provider import ProviderClient
    from .settings import Settings

    story_id = str(content_id or "").strip()
    if not story_id or len(story_id) > 64 or any(ord(c) < 32 for c in story_id):
        raise ValueError("请输入有效的 Moboreels 剧 ID")
    settings = Settings.from_env()
    catalog = Catalog(settings, Database(settings))
    try:
        drama = catalog.detail(story_id)
        if not drama or str(drama.get("story_id")) != story_id:
            raise ValueError("Moboreels 短剧库中未找到剧 ID：%s" % story_id)
        data = ContentTools(settings, catalog, ProviderClient(settings)).episodes(story_id, refresh=True)
    except (DatabaseError, PromotionError) as exc:
        raise ValueError(str(exc)) from None
    episodes = data["list"]
    if not episodes:
        raise ValueError("Moboreels 剧目暂无可用分集视频：%s" % story_id)
    return [{
        "app_id": PRODUCT["app_id"], "app": PRODUCT["app"], "country": "",
        "language": LANGUAGES.get(drama.get("lang_id"), "und"),
        "drama_name": drama.get("story_name") or "",
        "cover_url": drama.get("img_src") or "",
        "episode_number": item["epis_no"], "episode_url": item["epis_url"],
        "all_episodes_count": len(episodes),
    } for item in episodes]
