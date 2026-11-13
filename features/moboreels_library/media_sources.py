"""Exact public account metadata lookup; never reads credentials or internal IDs as platform IDs."""
import re
from urllib.parse import urlsplit, urlunsplit

from .db import _literal

PLATFORMS = {1: "TikTok", 2: "YouTube", 3: "Facebook", 4: "Instagram"}
HOSTS = {1: {"www.tiktok.com", "tiktok.com"}, 2: {"www.youtube.com", "youtube.com"},
         3: {"www.facebook.com", "facebook.com", "m.facebook.com"}, 4: {"www.instagram.com", "instagram.com"}}


def platform(value):
    if isinstance(value, bool) or str(value) not in {"1", "2", "3", "4"}:
        raise ValueError("请选择有效的媒体类型")
    return int(value)


def account_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("账号 ID 格式无效；请使用平台原始 ID，YouTube 使用频道 ID")
    return value


def profile_link(kind, value):
    try:
        parsed = urlsplit(str(value or "").strip())
        if (parsed.scheme != "https" or parsed.hostname not in HOSTS[kind] or parsed.username or parsed.password
                or parsed.port not in (None, 443) or len(value) > 2048 or any(ord(c) < 33 for c in value)):
            raise ValueError()
        path = parsed.path.rstrip("/")
        if kind == 1:
            good = bool(re.fullmatch(r"/@[A-Za-z0-9_.]{1,100}", path))
        elif kind == 2:
            good = bool(re.fullmatch(r"/(?:channel/[A-Za-z0-9_-]{1,100}|@[A-Za-z0-9_.-]{1,100})", path))
        elif kind == 3:
            good = bool(re.fullmatch(r"/[A-Za-z0-9_.-]{1,100}", path)) and path not in {"/watch", "/reel", "/login", "/share"}
        else:
            good = bool(re.fullmatch(r"/[A-Za-z0-9_.]{1,100}", path)) and path not in {"/p", "/reels", "/stories", "/accounts"}
        if not good or parsed.query or parsed.fragment:
            raise ValueError()
        return urlunsplit(("https", parsed.hostname, path, "" , ""))
    except (ValueError, TypeError, KeyError):
        raise ValueError("请输入对应媒体的 HTTPS 账号主页链接，不支持作品、短链或追踪参数") from None


class MediaSource:
    def __init__(self, db):
        self.db = db

    def lookup(self, kind, ids):
        kind = platform(kind)
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValueError("每批请输入 1 至 100 个平台账号 ID")
        ids = list(dict.fromkeys(account_id(x) for x in ids))
        if kind != 2 and any(not re.fullmatch(r"[1-9][0-9]{0,99}", key) for key in ids):
            raise ValueError("请粘贴平台原始数字 ID，而不是用户名或后台编号")
        literals = ",".join(_literal(x) for x in ids)
        if kind == 1:
            sql = """SELECT JSON_OBJECT('account_id',external_account_id,'alternate_id',main_account_id,
                'nickname',account_name,'platform_link',account_link) FROM ads_ai.tiktok_personal_account_snapshot
                WHERE is_active=1 AND (external_account_id IN (%s) OR main_account_id IN (%s)) LIMIT 301;""" % (literals, literals)
        elif kind == 2:
            sql = """SELECT JSON_OBJECT('account_id',channel_id,'nickname',channel_name,
                'platform_link',CONCAT('https://www.youtube.com/channel/',channel_id))
                FROM kunlunads_dev.ads_youtube_channels WHERE channel_id IN (%s) LIMIT 301;""" % literals
        elif kind == 3:
            sql = """SELECT JSON_OBJECT('account_id',page_id,'nickname',page_name,
                'platform_link',CONCAT('https://www.facebook.com/',page_id))
                FROM kunlunads_dev.ads_facebook_page_post WHERE page_id IN (%s) LIMIT 301;""" % literals
        else:
            sql = None  # No verified Instagram mapping exists in the current backend.
        found = self.db._read(sql) if sql else []
        if len(found) > 300:
            raise ValueError("账号匹配结果过多，请缩小批次")
        result = []
        for key in ids:
            candidates = set()
            for row in found:
                if key not in (str(row.get("account_id") or ""), str(row.get("alternate_id") or "")):
                    continue
                try:
                    name = str(row.get("nickname") or "").strip()
                    link = profile_link(kind, row.get("platform_link"))
                    if not name or len(name) > 255:
                        continue
                    candidates.add((name, link))
                except ValueError:
                    continue
            if len(candidates) == 1:
                name, link = candidates.pop()
                result.append({"account_id": key, "nickname": name, "platform_link": link, "match_status": "matched"})
            else:
                result.append({"account_id": key, "nickname": "", "platform_link": "",
                               "match_status": "conflict" if candidates else "missing"})
        return result
