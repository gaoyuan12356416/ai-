"""Read an existing paid FB action and qualify its output without submitting another CI job."""
import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_gpu.random_overlay import derive_recipe, sha256_file
from features.pipeline_dispatch.cloud_jobs import CloudJobs, fingerprint, records
from features.pipeline_dispatch.cloud_media import CloudExecutor, FB_PROFILE, probe, transcode
import hashlib


def require(test, message):
    if not test:
        raise ValueError(message)


def qualify(ex, item, conn, temp):
    request = item["request"]
    native = str(request["job_id"])
    identity = [item["source_key"], "fb_prepare", native]
    key = fingerprint([identity, "render"])
    row = conn.execute("SELECT * FROM action WHERE identity=?", (key,)).fetchone()
    require(row and row["provider_id"] == item["provider_id"], "provider identity changed")
    ci = ex.jobs
    data = records(ci.client.ci_get_media_jobs(Bucket=ci.bucket, JobIDs=row["provider_id"])["JobsDetail"])[0]
    require(ci._matches(data, row) and data["JobId"] == row["provider_id"] and data["State"] == "Success", "CI output not proven")
    require(request["expected_profile"] == FB_PROFILE and request.get("video_template") == "random_overlay"
            and not request.get("source_trim_tail_seconds"), "unexpected FB recipe")
    duration = float(data["Operation"]["Transcode"]["TimeInterval"]["Duration"])
    require(math.isfinite(duration) and 0 < duration <= 3600, "invalid duration")
    recipe = derive_recipe(job_id=native, content_id=str(request["content_id"]), profile=FB_PROFILE,
        source_url_sha256=hashlib.sha256(request["source_url"].encode()).hexdigest(), asset_set=ex.assets)
    operation = transcode("h264", duration)
    operation.update(Watermark=ex.templates.random(recipe, ex.assets, duration), UserData=key,
                     Output={"Bucket": ci.bucket, "Region": ci.region, "Object": row["target"]})
    body = {"Tag": "Transcode", "QueueId": ci.queue, "Input": {"Object": data["Input"]["Object"]},
            "Operation": operation, "CallBack": "no"}
    require(fingerprint(body) == row["digest"], "frozen paid request digest mismatch")
    output = temp / (key + ".mp4")
    head = ex.origin.head(row["target"])
    ci.client.download_file(Bucket=ci.bucket, Key=row["target"], DestFilePath=str(output), PartSize=8, MAXThread=2)
    sha, size = sha256_file(output)
    meta = probe(ex.config["ffprobe"], output)
    video = next(v for v in meta["streams"] if v["codec_type"] == "video")
    measured = float(meta["format"]["duration"])
    require(0 < size == int(head["content-length"]) and size <= 2 * 1024**3 and abs(measured-duration) <= .5,
            "output size or duration mismatch")
    require((video["codec_name"],video["profile"],video["pix_fmt"],video["width"],video["height"])
            == ("h264","High","yuv420p",720,1280), "output format mismatch")
    dest = ex.delivery.config.cos_prefix + "/fb/" + sha[:2] + "/" + sha + ".mp4"
    url = ex.delivery.upload(dest, output, sha, size, copy_from=({"Bucket":ci.bucket,"Region":ci.region,"Key":row["target"]}, head["etag"]))
    prepared = {"status":"ready","backend":"cos","storage_backend":"cos","profile":FB_PROFILE,
        "job_id":native,"content_id":request["content_id"],"random_overlay_recipe":recipe,"recipe":recipe,
        "execution_profile":"ci-apng-watermark-v1","applied_layers":["border","opacity_video","corners","tint"],
        "assets":{"asset_set_sha256":ex.assets["manifest_sha256"],"selected":recipe["assets"]},
        "output_url":url,"output_size":size,"output_sha256":sha,"storage_key":dest,
        "probe":{"width":720,"height":1280,"duration":measured},
        "ci_actions":[{"action":key,"job_id":row["provider_id"],"evidence":"provider_success"}]}
    return dict(item, action=key, binding=row["digest"], preparation=prepared)


def main():
    a=argparse.ArgumentParser();a.add_argument("scope",type=Path);a.add_argument("output",type=Path);args=a.parse_args()
    cfg=json.load(open("/etc/pipeline-cos-cloud/worker.json"))
    source=Path(cfg["state_root"])/"actions.sqlite3"
    conn=sqlite3.connect(source.as_uri()+"?mode=ro",uri=True);conn.row_factory=sqlite3.Row
    items=json.loads(args.scope.read_text())
    with tempfile.TemporaryDirectory(dir=cfg["state_root"]) as folder:
        cfg["state_root"]=folder
        executor=CloudExecutor(cfg,None)
        result=[qualify(executor,item,conn,Path(folder)) for item in items]
    args.output.write_text(json.dumps(result));print("verified original paid outputs",len(result))


if __name__=="__main__":
    os.umask(0o077)
    main()
