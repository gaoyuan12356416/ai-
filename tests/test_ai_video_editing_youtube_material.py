import concurrent.futures
import hashlib
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.ai_video_editing import catalog_imports, smart_api, youtube_material
from features.ai_video_editing.catalog import source_key
from features.ai_video_editing.runtime import Runtime, Fault, ident, owner, packed
from features.ai_video_editing.youtube_material_io import MaterialIO, PRODUCTS


class Catalog:
    dw_table = "dw"
    mobo_scope = "institution_user_id=1 AND project_type=2 AND is_show=1"

    def query(self, statement):
        if "ads_ai.cdreader_koc_drama" in statement:
            assert "story_id=" in statement and self.mobo_scope in statement
            return [{"title": "Mobo test", "lang_id": 3}]
        assert "app_id='1479'" in statement and "content_id=" in statement
        return [{"title": "DW test", "country": "US", "language": "en"}]


class IO(MaterialIO):
    def __init__(self):
        super().__init__(SimpleNamespace(COS_BUCKET="test-123456", COS_REGION="ap-hongkong",
                                         ADMIN_MAPPING_MYSQL_DATABASE="test"))
        self.rows, self.receipts = {}, {}
        self.posts = []
        self.copied = []
        self.drop_response = False
        self.receipt_access = True
        self.db = SimpleNamespace(initiator=lambda email: 42, database="test",
                                  formal=lambda sign: self.rows.get(sign, []),
                                  receipt=lambda rid: self.receipts.get(rid, []), query=self.reverse)

    def ensure_file(self, path, item):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        self.copied.append(item["sha256"])

    def submit(self, request):
        self.posts.append(request)
        mid, rid = str(80001+len(self.posts)), str(92009+len(self.posts))
        product = next(name for pid, name in PRODUCTS.values() if pid == request["app_id"])
        row = dict(request, id=mid, type=2, is_delete=0, data_source_id=request["content_id"], product=product)
        self.rows[request["content_sign"]] = [row]
        self.receipts[rid] = [dict(id=rid, source_type=3, source_id=mid)]
        if self.drop_response:
            raise ConnectionError("reply lost after remote commit")
        return "accepted", rid, ""

    def reverse(self, sql):
        if not self.receipt_access:
            return []
        return [r for rows in self.receipts.values() for r in rows if "source_id="+r["source_id"]+" " in sql]


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rt = Runtime(self.tmp.name)
        catalog_imports.setup(self.rt)
        smart_api.setup(self.rt)
        self.actor = {"tenant_key": "t1", "user_id": "operator", "email": "operator@example.invalid"}
        self.other = dict(self.actor, tenant_key="t2")
        self.io = IO()
        self.catalog = Catalog()
        self.flow = youtube_material.Workflow(self.rt, self.catalog, self.io)
        self.rt.youtube_material = self.flow
        self.key1 = source_key(["1001", "dramawave", "US", "en"])
        self.key2 = source_key(["902"])

    def task(self, product=None, key=None, *, version=1):
        tid, sid = ident("at"), ident("aa")
        aid = ident("sa")
        source_sha = hashlib.sha256(b"source").hexdigest()
        raw = ("video bytes for " + tid).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (self.rt.root/"outputs"/(tid+".mp4")).write_bytes(raw)
        meta = {"width": 1920, "height": 1080, "duration_ms": 350000, "sha256": digest}
        unit = {"title": "render", "clips": [dict(source_id=sid,start_ms=5000,end_ms=15000)],
                "youtube_regular": dict(body_source_ids=[sid], analysis_id=aid)}
        log = [{"source_id":sid, "duration_ms":20000, "_stamp":source_sha,"candidates":[
            {"start_ms":5000,"end_ms":8000,"engine":"multimodal","reason":"woman reads letter","transcript":"Mother"},
            {"start_ms":7000,"end_ms":9000,"engine":"signal","reason":"sound spikes"},
            {"start_ms":14000,"end_ms":15500,"engine":"multimodal","reason":"unseen person"},
            {"start_ms":16000,"end_ms":19000,"engine":"multimodal","reason":"later spoiler"}]}]
        with self.rt.db(True) as db:
            db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)",
                       (sid, owner(self.actor), sid+".mp4", "", 1, source_sha,
                        packed(dict(duration_ms=20000,has_audio=True,width=1920,height=1080)), time.time()))
            db.execute("INSERT INTO tasks(id,owner,project,version,unit_id,unit_data,status,meta,created) "
                       "VALUES (?,?,?,?,?,?,'completed',?,?)",
                       (tid, owner(self.actor), ident("ap"), version, "u", packed(unit), packed(meta), time.time()))
            db.execute("INSERT INTO ai_edit_smart_analysis(id,owner,fingerprint,status,sources,created,updated) "
                       "VALUES(?,?,?,'succeeded',?,?,?)", (aid,owner(self.actor),aid,packed(log),time.time(),time.time()))
            if product:
                db.execute("INSERT INTO catalog_imports(id,owner,product,source_key,episode,status,asset_id,created,updated) "
                           "VALUES(?,?,?,?,?,'ready',?,?,?)",
                           (ident("ci"),owner(self.actor),product,key,version,sid,time.time(),time.time()))
        return tid

    def due(self, tid):
        with self.rt.db(True) as db:
            db.execute("UPDATE ai_youtube_final_material SET next_at=0 WHERE task_id=?", (tid,))

    def test_dual_products_formal_ids_versions_and_facts(self):
        drama, mobo = self.task("dramawave", self.key1), self.task("moboreels", self.key2, version=2)
        for tid, version, pid, content_id in [(drama,1,3214,"1001"),(mobo,2,3642,"902")]:
            self.assertEqual("draft", self.flow.status(self.actor,tid)["state"])
            self.assertEqual("queued", self.flow.finalize(self.actor,tid,{})["state"])
            self.flow.tick()
            item = self.flow.status(self.actor,tid)
            self.assertEqual("ready", item["state"])
            receipt = self.flow.evidence(self.actor,item["material_id"])
            self.assertEqual((version,pid,content_id,"en"),tuple(receipt[k] for k in ("version","product_id","content_id","language")))
            self.assertEqual(item["sha256"], receipt["sha256"])
            self.assertIn(item["sha256"], receipt["url"])
            self.assertEqual(receipt["size_bytes"], (self.rt.root/"outputs"/(tid+".mp4")).stat().st_size)
            self.assertEqual(["woman reads letter"], [x["description"] for x in receipt["evidence"]])
            self.assertEqual("Mother", receipt["evidence"][0]["transcript"])
            self.assertEqual(item["material_id"], self.flow.finalize(self.actor,tid,{})["material_id"])
            with self.assertRaises(Fault) as caught:
                self.flow.evidence(self.other,item["material_id"])
            self.assertEqual(404,caught.exception.status)
        self.assertEqual(2,len(self.io.posts))

    def test_local_upload_requires_explicit_verified_catalog_binding(self):
        tid = self.task()
        with self.assertRaises(Fault):
            self.flow.finalize(self.actor,tid,{})
        with self.assertRaises(Fault):
            self.flow.finalize(self.other,tid,dict(product="moboreels",key=self.key2))
        item = self.flow.finalize(self.actor,tid,dict(product="moboreels",key=self.key2,country="GB"))
        self.assertEqual("GB",item["drama"]["country"])
        with self.assertRaises(Fault):
            self.flow.finalize(self.actor,tid,dict(product="dramawave",key=self.key1))
        other = self.task("dramawave", self.key1)
        with self.assertRaises(Fault):
            self.flow.finalize(self.actor,other,dict(product="moboreels",key=self.key2))

    def test_concurrent_finalization_and_resume_after_lost_response(self):
        tid = self.task("moboreels", self.key2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            result = list(pool.map(lambda _: self.flow.finalize(self.actor,tid,{}), range(6)))
        self.assertEqual({"queued"}, {r["state"] for r in result})
        self.io.drop_response = True
        self.flow.tick()
        self.assertEqual("unknown",self.flow.retry(self.actor,tid)["state"])
        self.io.receipt_access = False
        resumed = youtube_material.Workflow(self.rt,self.catalog,self.io)
        self.due(tid)
        resumed.tick()
        self.assertEqual("", resumed.status(self.actor,tid)["material_id"])
        self.io.receipt_access = True
        self.due(tid)
        resumed.tick()
        self.assertEqual("ready",resumed.status(self.actor,tid)["state"])
        self.assertEqual(1,len(self.io.posts))

    def test_confirmed_rejection_only_blocks_its_task_and_can_be_retried(self):
        first, second = self.task("moboreels",self.key2), self.task("dramawave",self.key1)
        self.flow.finalize(self.actor,first,{})
        self.flow.finalize(self.actor,second,{})
        with patch.object(self.io,"submit",return_value=("failed","","被明确拒绝")):
            self.flow.tick()
        self.flow.tick()
        self.assertEqual("ready",self.flow.status(self.actor,second)["state"])
        self.assertEqual("failed",self.flow.status(self.actor,first)["state"])
        self.flow.retry(self.actor,first)
        self.flow.tick()
        self.assertEqual("ready",self.flow.status(self.actor,first)["state"])

    def test_wrong_formal_owner_or_receipt_never_returns_a_material_id(self):
        tid = self.task("moboreels",self.key2)
        self.flow.finalize(self.actor,tid,{})
        self.io.drop_response = True
        self.flow.tick()
        first = self.io.posts[0]
        row = self.io.rows[first["content_sign"]][0]
        row["data_source_id"] = "wrong"
        self.due(tid)
        self.flow.tick()
        item = self.flow.status(self.actor,tid)
        self.assertEqual(("unknown",""),(item["state"],item["material_id"]))
        row["data_source_id"] = first["content_id"]
        next(iter(self.io.receipts.values()))[0]["source_type"] = 1
        self.due(tid)
        self.flow.tick()
        self.assertEqual("",self.flow.status(self.actor,tid)["material_id"])
        self.assertEqual(1,len(self.io.posts))

    def test_real_transport_contract_for_cos_and_material_api(self):
        tid = self.task("dramawave",self.key1)
        self.flow.finalize(self.actor,tid,{})
        item = self.flow.saved(self.actor,tid)
        platform = self.io.platform
        platform.AD_MATERIAL_SOURCE_API_URL = "https://aa.yingliangads.com/api/material/source"
        platform.AD_MATERIAL_SOURCE_API_TOKEN = "fixture"
        cos = Mock()
        class Missing(Exception):
            status_code = 404
        uploaded = {}
        def head(**params):
            if not uploaded:
                raise Missing()
            return {"content-length":str(item["size"]), "x-cos-meta-sha256":uploaded["digest"]}
        def upload(**params):
            self.assertEqual("public-read",params["ACL"])
            self.assertTrue(params["EnableMD5"])
            with open(params["LocalFilePath"],"rb") as stream:
                value = hashlib.sha256(stream.read()).hexdigest()
            self.assertEqual(value, params["Metadata"]["x-cos-meta-sha256"])
            uploaded["digest"] = value
        cos.head_object.side_effect = head
        cos.upload_file.side_effect = upload
        cos.get_object_acl.return_value = {"AccessControlList":{"Grant":[
            {"Grantee":{"Type":"Group","URI":"http://cam.qcloud.com/groups/global/AllUsers"},"Permission":"READ"}]}}
        platform.get_cos_client = lambda **kw: cos
        real = MaterialIO(platform)
        real.source.http = Mock()
        real.source.http.head.return_value = SimpleNamespace(status_code=200,headers={"Content-Length":str(item["size"])})
        path = self.rt.root/"outputs"/(tid+".mp4")
        real.ensure_file(path,item)
        real.ensure_file(path,item)
        self.assertEqual(1,cos.upload_file.call_count)
        self.assertEqual(2,real.source.http.head.call_count)
        receipt = Mock(status_code=200)
        receipt.json.return_value = {"code":0,"data":{"id":1234}}
        real.source.http.post.return_value = receipt
        self.assertEqual(("accepted","1234",""), real.submit(item["request"]))
        kwargs = real.source.http.post.call_args.kwargs
        self.assertEqual(item["request"], kwargs["json"])
        uploaded["digest"] = "0"*64
        with self.assertRaises(ValueError):
            real.ensure_file(path,item)
        self.assertEqual(1,cos.upload_file.call_count)


if __name__ == "__main__":
    unittest.main()
