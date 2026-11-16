"""Run explicit CI fixtures in a validation prefix and save isolated receipts."""
import argparse
import json
import os
from pathlib import Path

from features.pipeline_dispatch.cloud_jobs import CloudLedger, encoded, fingerprint
from features.pipeline_dispatch.cloud_media import CloudExecutor, FB_PROFILE, X_RANDOM, X_REPAIR, probe
from features.fb_gpu.random_overlay import sha256_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if not config["prefix"].startswith("benchmarks/") or "validation" not in config["delivery_prefix"]:
        raise ValueError("validation object prefixes required for these fixtures")
    root = Path(config["state_root"])
    os.environ["PIPELINE_MEDIA_ALLOWED_HOSTS"] = ",".join(config["source_hosts"])
    ffprobe = config["ffprobe"]
    ledger = CloudLedger(root / "actions.sqlite3")
    executor = CloudExecutor(config, ledger)
    source_url = config["canary_url"]
    source = root / "source.mp4"
    sha, size = sha256_file(source)
    duration = float(probe(ffprobe,source)["format"]["duration"])
    manifest = config["catalog_sha256"]
    cases = {"tt-source":("tt_prepare","tt-post-source-direct-v1"),
             "tt-clean":("tt_prepare","tt-post-direct-clean-h264-720x1280-v1"),
             "tt-clean-hevc":("tt_prepare","tt-post-direct-clean-hevc-720x1280-v1"),
             "tt-outro":("tt_prepare","tt-post-direct-outro-h264-720x1280-v2"),
             "tt-branded":("tt_prepare","tt-post-h264-720x1280-v2"),
             "tt-outro-hevc":("tt_prepare","tt-post-direct-outro-hevc-720x1280-v2"),
             "tt-branded-hevc":("tt_prepare","tt-post-hevc-720x1280-v2"),
             "tt-random-hevc":("tt_prepare","tt-post-random-overlay-hevc-720x1280-v3"),
             "fb-random":("fb_prepare", FB_PROFILE), "x-preflight":("x_preflight","source"),
             "x-repair":("x_repair",X_REPAIR), "x-random":("x_random",X_RANDOM),
             "tt-random":("tt_prepare","tt-post-random-overlay-h264-720x1280-v3")}
    class Fixture:
        identity = {"job_id":1, "generation":1}
        def __init__(self, name):
            self.job = {"source_key":"cos-validation:" + str(config.get("fixture_version", "1")) + ":" + name}
            self.client = self
        def check(self):
            return
        def phase(self, name):
            self.last_phase = name
        def post(self, route, payload):
            if route != "jobs/cos-intent":
                raise AssertionError(route)
            return {"accepted":True}
        def checkpoint(self, phase, receipt):
            (root / (self.job["source_key"].split(":")[-1] + "-checkpoint.json")).write_text(encoded(receipt))
    failures = []
    for name in args.cases or list(cases):
        kind,profile = cases[name]
        ctx = Fixture(name)
        receipt = root / (name + "-receipt.json")
        if receipt.exists():
            print(name, "receipt_exists", flush=True)
            continue
        request = {"source_url":source_url,"content_id":"11388","job_id":"cos-validation-" + name,
            "source_trim_tail_seconds":1.333 if name.startswith(("tt-clean","tt-outro","tt-branded")) else 0,
            "expected_profile":profile}
        if name == "tt-source" and config.get("passthrough_url"):
            request["source_url"] = config["passthrough_url"]
        if kind == "fb_prepare":
            request["video_template"] = "random_overlay"
        if kind == "x_preflight":
            request = {"source_url":source_url,"identity_mode":"source_to_fingerprint","source_sha256":sha,
                       "source_size":size,"max_duration_seconds":140}
        if kind == "x_repair":
            request = {"source_url":source_url,"job_key":fingerprint(name),"source_sha256":sha,"source_size":size,
                       "material_id":"1","pool_item_id":1,"trigger_code":"invalid_media_codec","duration_policy":"standard",
                       "profile":profile}
        if kind == "x_random":
            request = {"source_url":source_url,"job_key":fingerprint(name),"source_sha256":sha,"source_size":size,
                       "content_id":"11388","queue_id":1,"source_duration":duration,"max_duration_seconds":140,
                       "asset_manifest_sha256":manifest,"profile":profile}
        try:
            result = executor.execute({"stage":"prepare","payload":{"kind":kind,"request":request}},ctx)
            artifact=result["artifact"]
            if kind=="tt_prepare" and name=="tt-source":
                assert (artifact["sha256"], artifact["size"]) == (config.get("passthrough_sha", sha), config.get("passthrough_size", size))
            if name.startswith("tt-branded"):
                assert result["preparation"]["direct_post_eligible"] is False
            receipt.write_text(encoded(result))
            print(name, "qualified", artifact["sha256"][:16], artifact["size"],flush=True)
        except Exception as exc:
            import traceback
            frames = traceback.extract_tb(exc.__traceback__)
            print(name, "failed", type(exc).__name__, getattr(exc,"code",""),getattr(ctx,"last_phase",""),
                   [(Path(f.filename).name,f.lineno) for f in frames[-4:]], flush=True)
            failures.append(name)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
