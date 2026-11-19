"""Independent, read-only source catalog. No existing editing/library API calls."""

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

import requests

from .runtime import Fault, check, packed

LANGUAGES = {
    2: "zh-tw",
    3: "en",
    4: "es",
    5: "pt",
    6: "fr",
    7: "ru",
    8: "it",
    9: "ja",
    10: "ar",
    11: "id",
    12: "th",
    13: "vi",
    14: "ko",
    15: "tl",
    16: "de",
    17: "hi",
    18: "bn",
    21: "ms",
    22: "tr",
}
PRODUCTS = {"dramawave": "DramaWave", "moboreels": "MoboReels"}


def literal(value):
    raw = str(value).encode().hex()
    return "CONVERT(X'" + raw + "' USING utf8mb4) COLLATE utf8mb4_unicode_ci"


def source_key(values):
    return base64.urlsafe_b64encode(packed(values).encode()).decode().rstrip("=")


def decode_key(value, product):
    check(
        product in PRODUCTS and isinstance(value, str) and len(value) <= 1500,
        "catalog_source_invalid",
        "请选择有效剧目",
    )
    try:
        values = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        assert isinstance(values, list) and len(values) == (
            4 if product == "dramawave" else 1
        )
        assert all(
            isinstance(v, str) and len(v) <= 160 and not any(ord(c) < 32 for c in v)
            for v in values
        )
        assert values[0]
        return values
    except (ValueError, AssertionError, TypeError):
        raise Fault("catalog_source_invalid", "剧目编号无效，请重新选择") from None


def safe_url(value):
    try:
        parsed = urlsplit(value)
        assert (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
        assert (
            parsed.port in (None, 443)
            and len(value) <= 8192
            and not any(ord(c) < 33 for c in value)
        )
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        assert addresses and all(
            ipaddress.ip_address(a[4][0]).is_global for a in addresses
        )
        return value
    except (ValueError, AssertionError, OSError, TypeError):
        raise Fault("catalog_url_invalid", "源视频地址无效或不可访问", 502) from None


def environment_file(path):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return values


class Catalog:
    def __init__(self, platform):
        self.platform = platform
        self.settings = environment_file("/etc/moboreels-library.env")
        self.settings.update(os.environ)
        self.cache = {}
        self.lock = threading.Lock()
        self.provider_lock = threading.Lock()
        self.last_request = 0

    def query(self, sql):
        command = list(self.platform.MYSQL_BASE_CMD)
        check(
            command and not any("63353" in c for c in command),
            "catalog_config",
            "剧库只读端配置无效",
            503,
        )
        env = os.environ.copy()
        env.update(
            MYSQL_PWD=self.platform.MYSQL_PASSWORD,
            MYSQL_MAX_EXECUTION_TIME_MS="5000",
            SQL_GATE_JOB_NAME="ai-video-editing-catalog",
            SQL_GATE_WAIT_TIMEOUT_SECONDS="15",
        )
        env.pop("SQL_GATE_BYPASS", None)
        env.pop("SQL_GATE_TEST_MODE", None)
        command = [
            command[0],
            "--connect-timeout=5",
            *command[1:],
            "SET SESSION MAX_EXECUTION_TIME=5000; SELECT @@read_only; " + sql,
        ]
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
                start_new_session=os.name != "nt",
            )
            out, _ = process.communicate(timeout=30)
            check(
                process.returncode == 0,
                "catalog_query_failed",
                "剧库读取暂未完成，请重试或缩短搜索条件",
                503,
            )
            rows = out.splitlines()
            check(
                rows and rows[0] == "1", "catalog_readonly", "剧库只读端验证失败", 503
            )
            return [json.loads(bytes.fromhex(row).decode()) for row in rows[1:] if row]
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise Fault(
                "catalog_timeout", "剧库读取超时，请使用剧目编号或更完整的剧名", 503
            ) from None
        except (OSError, ValueError):
            raise Fault(
                "catalog_unavailable", "剧库暂不可用，请稍后重试", 503
            ) from None

    @property
    def dw_table(self):
        db, table = self.platform.DB_NAME, self.platform.SOURCE_TABLE
        check(
            all(re.fullmatch(r"[A-Za-z0-9_]+", v) for v in (db, table)),
            "catalog_config",
            "剧库配置无效",
            503,
        )
        return f"`{db}`.`{table}`"

    @property
    def mobo_scope(self):
        institution = self.settings.get("MOBOREELS_INSTITUTION_USER_ID", "")
        check(institution, "catalog_config", "MoboReels 剧库尚未配置", 503)
        return (
            "institution_user_id="
            + literal(institution)
            + " AND project_type=2 AND is_show=1"
        )

    def search(self, product, query="", page=1):
        check(
            product in PRODUCTS and len(query) <= 100 and 1 <= page <= 250,
            "catalog_query_invalid",
            "剧库查询条件无效",
        )
        key = (product, query, page)
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        if product == "dramawave":
            where = "app_id='1479' AND type=2 AND sub_number=1 AND sub_url<>''"
            fields = "HEX(JSON_OBJECT('id',CAST(content_id AS CHAR),'name',name,'cover',cover,'app',app,'country',country,'language',language,'episodes',all_episodes_count))"
            suffix = " LIMIT 41 OFFSET " + str((page - 1) * 40)
            if query:
                rows = self.query(
                    "SELECT "
                    + fields
                    + " FROM "
                    + self.dw_table
                    + " FORCE INDEX (content_id) WHERE "
                    + where
                    + " AND content_id="
                    + literal(query)
                    + suffix
                )
                if not rows:
                    prefix = (
                        query.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                        + "%"
                    )
                    rows = self.query(
                        "SELECT "
                        + fields
                        + " FROM "
                        + self.dw_table
                        + " FORCE INDEX (name) WHERE "
                        + where
                        + " AND name LIKE "
                        + literal(prefix)
                        + suffix
                    )
            else:
                rows = self.query(
                    "SELECT "
                    + fields
                    + " FROM "
                    + self.dw_table
                    + " FORCE INDEX (aacn) WHERE "
                    + where
                    + suffix
                )
            for row in rows:
                row["key"] = source_key(
                    [
                        str(row["id"]),
                        row["app"] or "",
                        row["country"] or "",
                        row["language"] or "",
                    ]
                )
        else:
            where = self.mobo_scope
            if query:
                where += (
                    " AND (story_id="
                    + literal(query)
                    + " OR LOCATE("
                    + literal(query)
                    + ",story_name)>0 OR LOCATE("
                    + literal(query)
                    + ",c_name)>0)"
                )
            rows = self.query(
                "SELECT HEX(JSON_OBJECT('id',story_id,'name',story_name,'subtitle',c_name,'cover',img_src,'lang_id',lang_id)) "
                "FROM ads_ai.cdreader_koc_drama WHERE "
                + where
                + " ORDER BY source_publish_time DESC,story_id DESC LIMIT 41 OFFSET "
                + str((page - 1) * 40)
            )
            for row in rows:
                row["key"] = source_key([str(row["id"])])
                row["language"] = LANGUAGES.get(row.pop("lang_id", 0), "und")
        result = {
            "items": rows[:40],
            "has_more": len(rows) > 40,
            "page": page,
            "product": product,
        }
        with self.lock:
            if len(self.cache) >= 100:
                self.cache.clear()
            self.cache[key] = (time.monotonic() + 60, result)
        return result

    def episodes(self, product, key, private=False):
        values = decode_key(key, product)
        if product == "dramawave":
            condition = " AND ".join(
                k + "=" + literal(v)
                for k, v in zip(("content_id", "app", "country", "language"), values)
            )
            rows = self.query(
                "SELECT HEX(JSON_OBJECT('number',sub_number,'url',sub_url,'name',name,'language',language)) FROM "
                + self.dw_table
                + " WHERE app_id='1479' AND type=2 AND sub_number>0 AND sub_url<>'' AND "
                + condition
                + " ORDER BY sub_number LIMIT 2001"
            )
        else:
            drama = self.query(
                "SELECT HEX(JSON_OBJECT('name',story_name,'lang_id',lang_id)) FROM ads_ai.cdreader_koc_drama WHERE "
                + self.mobo_scope
                + " AND story_id="
                + literal(values[0])
                + " LIMIT 1"
            )
            check(drama, "catalog_missing", "该剧已下架或不在当前剧库", 404)
            d = drama[0]
            check(
                type(d["lang_id"]) is int and d["lang_id"] > 0,
                "catalog_language",
                "该剧缺少有效语言信息",
                409,
            )
            raw = self.provider_episodes(values[0], d["lang_id"])
            rows = [
                dict(
                    number=r["epis_no"],
                    url=r["epis_url"],
                    name=d["name"],
                    language=LANGUAGES.get(d["lang_id"], "und"),
                )
                for r in raw
            ]
        check(
            rows and len(rows) <= 2000,
            "catalog_episodes_missing",
            "该剧暂无可导入分集",
            404,
        )
        selected = {}
        for r in rows:
            n = int(r["number"])
            check(
                0 < n <= 100000 and r["url"].startswith("https://"),
                "catalog_episode_invalid",
                "剧集数据不完整",
                502,
            )
            check(
                n not in selected or selected[n]["url"] == r["url"],
                "catalog_episode_conflict",
                "同集存在不同资源，请联系维护人员",
                409,
            )
            selected[n] = dict(r, number=n)
        result = sorted(selected.values(), key=lambda r: r["number"])
        return {
            "items": (
                result
                if private
                else [{k: v for k, v in r.items() if k != "url"} for r in result]
            ),
            "product": product,
            "key": key,
        }

    def provider_episodes(self, story, language):
        institution = self.settings.get("MOBOREELS_INSTITUTION_USER_ID", "")
        token = self.settings.get("MOBOREELS_TOKEN", "")
        check(
            institution and token, "catalog_config", "MoboReels 分集读取凭证未配置", 503
        )
        with self.provider_lock:
            delay = 0.4 - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            rid, stamp = uuid.uuid4().hex, str(int(time.time() * 1000))
            signature = hashlib.sha256(
                f"{rid}_{stamp}_{institution}_{token}".lower().encode()
            ).hexdigest()
            try:
                response = requests.post(
                    "https://kocapi-cn.cdreader.com/api/story/getepislist",
                    headers={
                        "reqid": rid,
                        "timestamp": stamp,
                        "instuserid": institution,
                        "sign": signature,
                    },
                    json={"story_id": story, "lang_id": language, "project_type": 2},
                    timeout=(10, 45),
                    allow_redirects=False,
                )
                data = response.json()
                if response.status_code == 200 and data.get("code") != 200 and "video cannot be empty" in str(data.get("msg", data.get("message", ""))).lower():
                    raise Fault("catalog_no_episodes", "供应方暂无该剧分集视频，请选择其他剧目", 404)
                check(
                    response.status_code == 200
                    and data.get("code") == 200
                    and isinstance(data.get("data"), list),
                    "catalog_provider_failed",
                    "MoboReels 分集读取暂不可用，请重试",
                    502,
                )
                return data["data"]
            except (requests.RequestException, ValueError):
                raise Fault(
                    "catalog_provider_failed", "MoboReels 分集读取暂不可用，请重试", 502
                ) from None
