"""The custom-material API and the verified COS copy used by editor finalizations."""
import hashlib
import re
from types import SimpleNamespace

from features.video_editing.material_source import (
    CATEGORY, COUNTRY_FOR_LANGUAGE, MaterialSource, ReadOnlyCatalog, _language,
)

from .catalog import LANGUAGES, decode_key, literal
from .runtime import check


PRODUCTS = {
    "dramawave": (3214, "Drama-社媒专用素材"),
    "moboreels": (3642, "社媒moboreel"),
}
COUNTRIES = {**COUNTRY_FOR_LANGUAGE, "zh": "CN", "tl": "PH", "hi": "IN", "bn": "BD", "ms": "MY"}


def resolve_drama(catalog, product, key, country=""):
    """Read the exact source version, including local uploads explicitly linked by an operator."""
    check(isinstance(product, str) and product in PRODUCTS and isinstance(key, str) and key,
          "material_drama_required", "请选择成片所属的素材产品和原剧")
    values = decode_key(key, product)
    if product == "dramawave":
        condition = " AND ".join(
            name + "=" + literal(value)
            for name, value in zip(("content_id", "app", "country", "language"), values)
        )
        rows = catalog.query(
            "SELECT DISTINCT HEX(JSON_OBJECT('title',name,'country',country,'language',language)) FROM "
            + catalog.dw_table + " WHERE app_id='1479' AND type=2 AND sub_number=1 AND sub_url<>'' AND "
            + condition + " LIMIT 2"
        )
    else:
        rows = catalog.query(
            "SELECT DISTINCT HEX(JSON_OBJECT('title',story_name,'lang_id',lang_id)) "
            "FROM ads_ai.cdreader_koc_drama WHERE " + catalog.mobo_scope
            + " AND story_id=" + literal(values[0]) + " LIMIT 2"
        )
    check(len(rows) == 1, "material_drama_unavailable", "未找到唯一的原剧版本，请重新选择", 409)
    source = rows[0]
    if product == "moboreels":
        language = LANGUAGES.get(source.get("lang_id"))
        # The supplier supplies a language, not a country. The material API also
        # needs a market. Use the same language defaults as manual ingestion.
        market = (country or COUNTRIES.get(language, "")).strip().upper()
    else:
        language = source.get("language")
        market = str(source.get("country") or "").strip().upper()
    language = _language(language)
    check(language and language != "und" and re.fullmatch(r"[a-z]{2,3}(-[a-z]{2,6})?", language),
          "material_language_missing", "原剧缺少可用的语言", 409)
    check(re.fullmatch(r"[A-Z]{2}", market), "material_country_missing", "请选择素材的国家或地区", 409)
    return dict(product=product, product_id=PRODUCTS[product][0], source_key=key,
                content_id=values[0], title=str(source["title"] or values[0]),
                country=market, language=language)


class MaterialIO:
    def __init__(self, platform):
        self.platform = platform
        self.db = ReadOnlyCatalog(platform)
        self.source = MaterialSource(SimpleNamespace(app=platform, store=None))
        self.bucket = platform.COS_BUCKET
        self.region = platform.COS_REGION

    def request(self, task, drama, actor):
        check(re.fullmatch(r"[a-z0-9-]+-\d+", self.bucket or "") and self.region == "ap-hongkong",
              "material_cos_unavailable", "素材库 COS 配置不完整", 503)
        tid, sha, version = task["task_id"], task["sha256"], task["version"]
        sign = hashlib.sha256(f"ai-editor:youtube-material:v1/{tid}/{version}/{sha}".encode()).hexdigest()
        key = "ai-video-editing/youtube-final/v1/" + tid + "-" + sha + ".mp4"
        url = f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{key}"
        return {
            "app_id": drama["product_id"], "content_id": drama["content_id"],
            "language": drama["language"], "country": drama["country"], "content_sign": sign,
            "name": f"{drama['title'][:140]}_v{version}_{tid}.mp4", "url": url,
            "category": CATEGORY, "user_id": 248, "initiator": self.db.initiator(actor.get("email")),
        }

    def ensure_file(self, path, item):
        # The editor outputs a private local file. Only a selected, immutable
        # render gets a delivery copy in the same bucket used by the material API.
        check(getattr(self.platform, "AD_MATERIAL_SOURCE_API_URL", "") ==
              "https://aa.yingliangads.com/api/material/source" and
              getattr(self.platform, "AD_MATERIAL_SOURCE_API_TOKEN", ""),
              "material_api_unavailable", "素材入库接口尚未配置", 503)
        sha, size, url = item["sha256"], item["size"], item["request"]["url"]
        prefix = f"https://{self.bucket}.cos.{self.region}.myqcloud.com/"
        check(url.startswith(prefix), "material_storage_changed", "素材库存储配置已变更", 409)
        key = url[len(prefix):]
        client = self.platform.get_cos_client(timeout=90, retry=0)
        copy = {"data": {"target_bucket": self.bucket, "target_region": self.region,
                         "target_key": key, "artifact": {"sha256": sha, "size_bytes": size},
                         "request": item["request"]}}
        before = self.source._target_head(copy, client)
        if before is None:
            check(path.is_file() and path.stat().st_size == size,
                  "material_file_changed", "成片文件与渲染记录不一致", 409)
            digest = hashlib.sha256()
            with path.open("rb") as media:
                for block in iter(lambda: media.read(2 ** 20), b""):
                    digest.update(block)
            check(digest.hexdigest() == sha, "material_file_changed", "成片摘要与渲染记录不一致", 409)
            stamp = path.stat()
            client.upload_file(Bucket=self.bucket, Key=key, LocalFilePath=str(path),
                               PartSize=16, MAXThread=3, EnableMD5=True, ACL="public-read",
                               ContentType="video/mp4", Metadata={"x-cos-meta-sha256": sha})
            current = path.stat()
            check((stamp.st_ino, stamp.st_mtime_ns, stamp.st_size) ==
                  (current.st_ino, current.st_mtime_ns, current.st_size),
                  "material_file_changed", "传输期间成片发生变化", 409)
        check(self.source._target_head(copy, client) is not None,
              "material_copy_missing", "素材库视频还未传输完成", 503)
        self.source._public_acl(copy, client)
        self.source._public_head(copy)

    def submit(self, request):
        return self.source.post(request)

    def formal(self, item):
        """A lost POST response can be reconciled only through one real, linked receipt."""
        request = item["request"]
        rows = self.db.formal(request["content_sign"])
        if not rows:
            return "", "", ""
        criteria = {
            "product": PRODUCTS[item["drama"]["product"]][1], "type": 2, "is_delete": 0,
            "data_source_id": request["content_id"], "url": request["url"],
            "name": request["name"], "category": request["category"],
            "user_id": request["user_id"], "initiator": request["initiator"],
            "content_sign": request["content_sign"],
        }
        if len(rows) != 1 or any(str(rows[0].get(k)) != str(v) for k, v in criteria.items()):
            return "", "", "正式素材归属或签名尚未核对成功"
        row = rows[0]
        if (_language(row.get("language")) != request["language"]
                or str(row.get("country") or "").strip().upper() != request["country"]):
            return "", "", "正式素材的语言或国家与定稿不符"
        mid = str(row.get("id") or "")
        check(re.fullmatch(r"[1-9][0-9]{0,18}", mid), "material_invalid_id", "正式素材编号无效", 503)
        receipt_id = item.get("receipt_id") or ""
        if receipt_id:
            receipts = self.db.receipt(receipt_id)
        else:
            # The immutable signature, all frozen fields, and the unique reverse
            # receipt must agree before an interrupted POST can become ready.
            receipts = self.db.query(
                "SELECT JSON_OBJECT('id',CAST(id AS CHAR),'source_id',CAST(source_id AS CHAR),"
                "'source_type',source_type) FROM `" + self.db.database
                + "`.ads_source WHERE source_id=" + mid + " AND source_type=3 LIMIT 2"
            )
        if len(receipts) != 1:
            return "", "", "正在核对与正式素材对应的入库回执"
        linked = receipts[0]
        rid = str(linked.get("id") or "")
        if (str(linked.get("source_id")) != mid or str(linked.get("source_type")) != "3"
                or not re.fullmatch(r"[1-9][0-9]{0,18}", rid) or (receipt_id and rid != receipt_id)):
            return "", "", "入库回执与正式素材尚未匹配"
        return mid, rid, ""
