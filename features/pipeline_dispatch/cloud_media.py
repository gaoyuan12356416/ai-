"""Tencent CI prepare executor. All final video encoding takes place in CI."""
import hashlib
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from .cloud_jobs import CloudJobs, fingerprint
from .cloud_fb_input import cloud_input
from .cloud_objects import ObjectStore, SessionClient
from .cloud_templates import Templates, run_media, watermark
from .executors import NodeExecutors, ExecutionError, _artifact
from .prepare_routing import CLOUD_KINDS
from .tt_profiles import PROFILE_SPECS, supported_profiles
from features.fb_gpu.random_overlay import derive_recipe, load_asset_set, sha256_file


from features.random_gpu.profiles import SD_PROFILES, base_profile, canvas as profile_canvas


FB_PROFILE = "fb-random-overlay-ci-apng-h264-v1"
X_RANDOM = "x-random-overlay-ci-apng-h264-v1"
X_REPAIR = "x-h264-ci-720-duration-policy-v1"


def probe(binary, path):
    raw = subprocess.run([binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, timeout=120, check=True)
    return json.loads(raw.stdout)


def transcode(codec, duration, canvas=(720, 1280), bit_rate=None, *, tt_delivery=False):
    hevc = codec == "hevc"
    video = {"Codec": "H.265" if hevc else "H.264", "Profile": "main" if hevc else "high", "Width": str(canvas[0]),
             "Height": str(canvas[1]), "Fps": "30", "Preset": "medium", "Gop": "60", "Pixfmt": "yuv420p"}
    if tt_delivery:
        bit_rate = 900 if hevc else 1500
    video.update({"Bitrate": str(bit_rate)} if bit_rate else {"Crf": "21"})
    return {"Transcode": {"Container": {"Format": "mp4"}, "Video": video, "Audio": {
        "Codec": "aac", "Samplerate": "48000", "Bitrate": "128" if tt_delivery else "192", "Channels": "2"},
        "TimeInterval": {"Start": "0", "Duration": "%.3f" % duration}, "TransConfig": {"AdjDarMethod": "pad"}}}


class CloudExecutor(NodeExecutors):
    def __init__(self, config, ledger):
        super().__init__(dict(config, backend="cos"))
        client = SessionClient(config)
        self.origin = ObjectStore(client, config["bucket"], config["prefix"],
                                  "https://" + config["bucket"] + ".cos." + config["region"] + ".myqcloud.com")
        self.delivery = ObjectStore(client, config["delivery_bucket"], config["delivery_prefix"], config["delivery_domain"])
        self.jobs = CloudJobs(ledger, client, config["bucket"], config["region"], config["prefix"], config["queue"])
        self.templates = Templates(self.origin, self.work_root / "template-cache", config["ffmpeg"])
        self.assets = load_asset_set(Path(config["catalog"]), config["catalog_sha256"])
        self.asset_hashes = {name: sha256_file(Path(config[name])) for name in ("font", "outro", "logo")}
        if any(value[0] != config[name + "_sha256"] for name, value in self.asset_hashes.items()):
            raise ExecutionError("cos_frozen_asset_changed")
        self.formats = supported_profiles("cos") | {FB_PROFILE, X_RANDOM, X_REPAIR, SD_PROFILES[FB_PROFILE], SD_PROFILES[X_RANDOM]}

    def capabilities(self, stage=None):
        if stage == "publish":
            return []
        return sorted(CLOUD_KINDS | {"backend:cos", "prepare:x", "prepare:tt", "prepare:fb"}
                      | {"profile:" + p for p in self.formats}
                      | {"catalog:" + c + ":" + self.assets["manifest_sha256"] for c in ("fb", "x", "tt")}
                      | {"asset:tt-" + n + ":" + self.asset_hashes[n][0] for n in ("font", "outro")})

    def _preflight_store(self):
        return self.delivery

    def _x_transform(self, kind, request, context):
        return self.render(kind, request, context)

    def execute(self, job, context):
        payload = job["payload"]
        kind = payload["kind"]
        if job["stage"] != "prepare" or kind not in CLOUD_KINDS:
            raise ExecutionError("cos_prepare_only")
        if not set(payload.get("required_capabilities", [])) <= set(self.capabilities()):
            raise ExecutionError("cos_frozen_capability_mismatch")
        request = payload["request"]
        if kind == "x_preflight":
            return self.x_preflight(request, context)
        result = self.render(kind, request, context)
        return {"status": "ready", "artifact": dict(_artifact(result), backend="cos"), "preparation": result}

    def render(self, kind, request, context):
        from features.drama_synthesis.youtube import YouTubeHTTPClient
        from features.tt_gpu import worker as tt
        from features.x_posts import media_repair as repair
        from features.x_posts.random_template import validate_receipt
        channel = kind.split("_")[0]
        profile = request.get("expected_profile") or request.get("profile")
        if profile not in self.formats or base_profile(profile) not in ({FB_PROFILE} if kind == "fb_prepare" else
                 {X_RANDOM} if kind == "x_random" else {X_REPAIR} if kind == "x_repair" else supported_profiles("cos")):
            raise ExecutionError("cos_profile_mismatch")
        mode, codec = PROFILE_SPECS.get(profile, ("repair" if kind == "x_repair" else "random_overlay", "h264"))
        native_id = str(request.get("job_id") or request["job_key"])
        identity = [context.job["source_key"], kind, native_id]
        limit = int(request.get("max_media_bytes") or self.config.get("max_" + channel + "_bytes", 2 * 1024**3))
        cap = min(limit, 16 * 1024**3 if channel == "x" else 4 * 1024**3)
        with tempfile.TemporaryDirectory(dir=self.work_root) as folder:
            temp = Path(folder)
            source = temp / "source.mp4"
            remote = None
            if kind == "fb_prepare":
                context.phase("source_check")
                remote = cloud_input(self, request, identity)
            if remote:
                ci_source, duration = remote
                secs, has_audio, info = duration, True, {}
                print("cos_fb_input_remote", native_id, flush=True)
            else:
                context.phase("download")
                YouTubeHTTPClient().download(request["source_url"], source, allowed_hosts=self.config["source_hosts"],
                        max_bytes=16 * 1024**3 if channel == "x" else 2 * 1024**3, heartbeat=context.check)
                sha, size = sha256_file(source)
                if ((request.get("source_sha256") and request["source_sha256"] != sha)
                        or (request.get("source_size") and int(request["source_size"]) != size)):
                    raise ExecutionError("cos_source_fingerprint_changed")
                raw = probe(self.config["ffprobe"], source)
                streams = raw["streams"]
                has_audio = any(s["codec_type"] == "audio" for s in streams)
                secs = float(raw["format"]["duration"])
                trim = float(request.get("source_trim_tail_seconds", 0))
                duration = secs - trim
                if not math.isfinite(duration) or duration <= 0 or trim < 0 or (mode in {"source_direct","random_overlay"} and trim):
                    raise ExecutionError("cos_source_duration_invalid")
                info = {}
                if mode == "repair":
                    repair.validate_request(request, profile=profile)
                    info = repair.inspect_source(raw, request["trigger_code"], request["duration_policy"])
                    duration = info["output_duration"]
                elif channel == "tt":
                    tt.inspect_input(raw, self.config.get("max_tt_duration", 3600))
                elif channel == "fb" and (duration > 3600 or request.get("video_template", "random_overlay") != "random_overlay"):
                    raise ExecutionError("cos_fb_video_contract")
                elif channel == "x" and (abs(float(request["source_duration"]) - secs) > 0.05 or secs > request["max_duration_seconds"]):
                    raise ExecutionError("cos_x_duration_policy")

            context.phase("prepare")
            receipt = {"status": "ready", "profile": profile, "backend": "cos", "storage_backend": "cos"}
            if "job_id" in request:
                receipt.update(job_id=request["job_id"], content_id=request["content_id"])
            else:
                receipt["job_key"] = native_id
            actions = []
            def run(stage, tag, obj, params):
                context.phase("cos_" + stage)
                key, evidence = self.jobs.run(identity, stage, tag, obj, params, context)
                actions.append(evidence)
                return key

            canvas = profile_canvas(profile) if mode == "random_overlay" else info.get("canvas", (720, 1280))
            bit_rate = None
            if mode == "repair":
                rate = repair.output_rate_control(cap, duration)
                bit_rate = rate["video_bitrate_kbps"]
            params = transcode(codec, duration, canvas, bit_rate, tt_delivery=channel == "tt")
            if mode == "random_overlay":
                manifest = self.assets["manifest_sha256"]
                if request.get("asset_manifest_sha256", manifest) != manifest:
                    raise ExecutionError("cos_frozen_catalog_changed")
                recipe = derive_recipe(job_id=native_id, content_id=str(request["content_id"]), profile=profile,
                        source_url_sha256=hashlib.sha256(request["source_url"].encode()).hexdigest(), asset_set=self.assets)
                params["Watermark"] = self.templates.random(recipe, self.assets, duration, canvas_size=canvas)
                receipt.update(random_overlay_recipe=recipe, recipe=recipe, execution_profile="ci-apng-watermark-v1",
                               applied_layers=["border", "opacity_video", "corners", "tint"],
                               assets={"asset_set_sha256": manifest, "selected": recipe["assets"]})
                if not remote:
                    receipt.update(source_sha256=sha, source_size=size)
                if channel == "x":
                    receipt.update(queue_id=request["queue_id"], source_sha256=sha, source_size=size, asset_manifest_sha256=manifest)
            copy_from = None
            if mode == "source_direct":
                output = source
            else:
                if not remote:
                    ci_input = source
                    if codec == "hevc" and mode not in {"branded_preview", "direct_outro"} and duration < secs:
                        # CI's HEVC interval can truncate a fractional second. Pass
                        # the trimmed MP4 edit first, then encode its entire timeline.
                        ci_input = temp / "trimmed.mp4"
                        run_media(self.config["ffmpeg"], ["-i",source,"-map","0:v:0","-map","0:a:0?","-c","copy","-t",
                                  "%.6f" % duration,"-movflags","+faststart","-y",ci_input])
                    if codec == "hevc":
                        params["Transcode"].pop("TimeInterval", None)
                    if not has_audio:
                        silent = temp / "with-audio.mp4"
                        run_media(self.config["ffmpeg"], ["-i",ci_input,"-f","lavfi","-i","anullsrc=channel_layout=stereo:sample_rate=48000",
                                "-map","0:v:0","-map","1:a","-c:v","copy","-c:a","aac","-shortest","-y",silent])
                        ci_source = self.origin.content(silent, "video/mp4")
                    else:
                        ci_source = self.origin.content(ci_input, "video/mp4")
                if mode in {"branded_preview","direct_outro"}:
                    duration, ci_output = self.phone_outro(request, mode, codec, has_audio, source, ci_source, duration, temp, context, run)
                    receipt["assets"] = {"outro_sha256": self.asset_hashes["outro"][0], "outro_size": self.asset_hashes["outro"][1],
                                         "font_sha256": self.asset_hashes["font"][0]}
                else:
                    ci_output = run("render", "Transcode", ci_source, params)
                context.phase("output_verify")
                output = temp / "output.mp4"
                if self.config.get("delivery_cloud_copy"):
                    try:
                        info_head = self.origin.head(ci_output)
                        if info_head and info_head.get("etag"):
                            copy_from = ({"Bucket": self.jobs.bucket, "Region": self.jobs.region, "Key": ci_output},
                                         info_head["etag"])
                    except Exception as exc:
                        print("cos_delivery_head_fallback", type(exc).__name__, flush=True)
                self.jobs.client.download_file(Bucket=self.jobs.bucket, Key=ci_output, DestFilePath=str(output), PartSize=8, MAXThread=2)
                if codec == "hevc":
                    raw_out = probe(self.config["ffprobe"], output)
                    video = next(s for s in raw_out["streams"] if s["codec_type"] == "video")
                    if video.get("codec_name") == "hevc" and video.get("codec_tag_string") != "hvc1":
                        tagged = temp / "tagged.mp4"
                        run_media(self.config["ffmpeg"], ["-i",output,"-map","0","-c","copy","-tag:v","hvc1","-movflags","+faststart","-y",tagged])
                        output = tagged
                        copy_from = None
            final = probe(self.config["ffprobe"], output)
            digest, byte_count = sha256_file(output)
            if not 0 < byte_count <= cap:
                raise ExecutionError("cos_output_size_invalid")
            measured = float(final["format"]["duration"])
            if not math.isfinite(measured) or abs(measured - duration) > (0.05 if kind == "x_random" else 0.5):
                raise ExecutionError("cos_output_duration_invalid")
            video = next(s for s in final["streams"] if s["codec_type"] == "video")
            if channel == "tt":
                contract = {"codec": codec, "profile": "main" if codec == "hevc" else "high",
                            "codec_tag": "hvc1" if codec == "hevc" else "avc1"}
                checked = tt.validate_prepared_output(SimpleNamespace(media_mode=mode, profile=profile), final, output, cap, duration, video_contract=contract)
                receipt.update(media_mode=mode, transition="phone-match-0.9s" if mode in {"branded_preview","direct_outro"} else "none",
                         direct_post_eligible=mode != "branded_preview", brand_overlay_review_required=mode == "branded_preview",
                         source_trim_tail_seconds=trim, reused=False)
            elif mode == "repair":
                checked = repair.validate_output(final, output, canvas, cap, expected_duration=duration,
                           trim_applied=info["trim_applied"], max_duration_seconds=info["max_duration"])
            else:
                if (video.get("codec_name"), video.get("profile"), video.get("pix_fmt"), video.get("width"), video.get("height")) != ("h264","High","yuv420p",*canvas):
                    raise ExecutionError("cos_output_video_invalid")
                checked = {"duration": measured, "width": canvas[0], "height": canvas[1], "codec": "h264"}
            dest = self.delivery.config.cos_prefix + "/" + channel + "/" + digest[:2] + "/" + digest + ".mp4"
            receipt.update(output_url=self.delivery.url(dest), output_size=byte_count, output_sha256=digest, probe=checked,
                        storage_key=dest, ci_actions=actions)
            if kind == "x_random":
                validate_receipt(request, receipt)
            context.phase("artifact_upload")
            if copy_from:
                self.delivery.upload(dest, output, digest, byte_count, copy_from=copy_from)
            else:
                self.delivery.upload(dest, output, digest, byte_count)
            return receipt

    def phone_outro(self, request, mode, codec, has_audio, source, source_key, duration, temp, context, run):
        outro, font = Path(self.config["outro"]), Path(self.config["font"])
        logo = Path(self.config["logo"]) if mode == "branded_preview" else None
        for name,path in (("outro",outro),("font",font),("logo",logo)):
            if path and sha256_file(path) != self.asset_hashes[name]:
                raise ExecutionError("cos_frozen_asset_changed")
        outro_duration = float(probe(self.config["ffprobe"], outro)["format"]["duration"])
        if not 1 <= duration or not 1 <= outro_duration <= 120:
            raise ExecutionError("cos_phone_duration_invalid")
        main = transcode(codec, duration - 0.9, tt_delivery=True)
        if codec == "hevc":
            prefix = temp / "phone-prefix.mp4"
            run_media(self.config["ffmpeg"], ["-i",source,"-map","0:v:0","-map","0:a:0?","-c","copy","-t",
                                "%.6f" % (duration - 0.9),"-movflags","+faststart","-y",prefix])
            if not has_audio:
                silent = temp / "phone-prefix-audio.mp4"
                run_media(self.config["ffmpeg"], ["-i",prefix,"-f","lavfi","-i","anullsrc=channel_layout=stereo:sample_rate=48000",
                         "-map","0:v:0","-map","1:a","-c:v","copy","-c:a","aac","-shortest","-y",silent])
                prefix = silent
            source_key = self.origin.content(prefix, "video/mp4")
            main["Transcode"].pop("TimeInterval", None)
        if logo:
            main["Watermark"] = [watermark(self.origin.url(self.origin.content(logo, "image/png")), duration - 0.9,
                                                width=132,height=132,dx=48,dy=72)]
        main_key = run("main", "Transcode", source_key, main)
        mixed, foreground = self.templates.phone_layers(source,outro,logo,duration,has_audio,context,temp)
        tail = transcode(codec, outro_duration, tt_delivery=True)
        if codec == "hevc":
            tail["Transcode"].pop("TimeInterval", None)
        tail["Watermark"] = [self.templates.outro_labels(font,str(request["content_id"]),outro_duration), foreground]
        tail_key = run("outro", "Transcode", mixed, tail)
        combined = {"ConcatTemplate": {"DirectConcat": "true", "Container":{"Format":"mp4"}, "Index":"0",
                            "ConcatFragment":[{"Url":self.origin.url(tail_key),"FragmentIndex":"0","Mode":"End"}]}}
        return duration - 0.9 + outro_duration, run("concat","Concat",main_key,combined)
