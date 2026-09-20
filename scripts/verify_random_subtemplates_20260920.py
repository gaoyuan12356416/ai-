"""Verify the 42 additions using actual PNG pixels and decoded alpha-WebM frames."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from PIL import Image

from random_subtemplate_artwork_20260920 import GROUPS, LAYERS

BASE_SHA = "24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c"
SIZE = (720, 1280)
FPS, FRAMES = 30, 120
EXPECTED_COUNTS = {"border": 20, "corners": 20, "light": 2, "opacity_video": 20, "tint": 20}
REPO = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024*1024), b""): h.update(data)
    return h.hexdigest()


def artwork_digest():
    return digest(Path(__file__).with_name("random_subtemplate_artwork_20260920.py"))


def expected_frame(category, index, n=0):
    layer = LAYERS[category]
    source = layer(index) if category in {"border", "tint"} else layer(index, n / FRAMES)
    return np.array(source.resize(SIZE, Image.Resampling.LANCZOS))


def _check_protected_area(actual, category):
    if category == "tint":
        assert actual[:, :, 3].min() == 255, "tint_alpha_not_opaque"
    else:
        # Broad central face/subtitle column, including the lower caption area.
        assert actual[140:1200, 180:540, 3].max() == 0, "face_or_subtitle_area_not_clear"
        if category == "corners":
            assert actual[195:1085, :, 3].max() == 0, "corner_art_outside_corner_zone"


def verify_asset(path, category, index, *, ffmpeg="ffmpeg", ffprobe="ffprobe", deep=True):
    """Check a completed file; quick mode validates the full container + first frame.

    Both modes exercise the real decoder. Full mode scans all 120 frames and
    checks five frames against the deterministic source, including the last one.
    """
    path = Path(path)
    result = {"file": path.name, "sha256": digest(path), "bytes": path.stat().st_size}
    if category in {"border", "tint"}:
        with Image.open(path) as im:
            assert im.format == "PNG" and im.mode == "RGBA" and im.size == SIZE
            actual = np.array(im)
        assert np.array_equal(actual, expected_frame(category, index)), "png_does_not_match_design"
        _check_protected_area(actual, category)
        assert actual[:, :, 3].max() > 0
        result.update(pixels_match_design=True, dimensions=list(SIZE), alpha_min=int(actual[:, :, 3].min()), alpha_max=int(actual[:, :, 3].max()))
        return result

    probe = json.loads(subprocess.check_output([ffprobe, "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)]))
    assert len(probe["streams"]) == 1, "expected_one_video_stream_no_audio"
    stream = probe["streams"][0]
    assert stream["codec_type"] == "video" and stream["codec_name"] == "vp9"
    assert (stream["width"], stream["height"]) == SIZE
    assert stream["r_frame_rate"] == "30/1" and stream["nb_read_frames"] == str(FRAMES)
    assert {k.lower(): v for k, v in stream["tags"].items()}.get("alpha_mode") == "1"
    assert abs(float(probe["format"]["duration"]) - 4) < .001
    command = [ffmpeg, "-v", "error", "-c:v", "libvpx-vp9", "-threads", "2", "-i", str(path)]
    if not deep: command.extend(["-frames:v", "1"])
    command.extend(["-f", "rawvideo", "-pix_fmt", "rgba", "-"])
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    hashes, alpha_errors, composite_errors, loop_step = set(), [], [], []
    first_alpha = previous_alpha = None
    try:
        for n in range(FRAMES if deep else 1):
            raw = proc.stdout.read(SIZE[0]*SIZE[1]*4)
            assert len(raw) == SIZE[0]*SIZE[1]*4, "truncated_decoded_frame"
            hashes.add(hashlib.sha256(raw).hexdigest())
            actual_u8 = np.frombuffer(raw, np.uint8).reshape(SIZE[1], SIZE[0], 4)
            _check_protected_area(actual_u8, category)
            assert actual_u8[:, :, 3].min() == 0 and actual_u8[:, :, 3].max() > 0
            # Record the boundary jump alongside ordinary frame-to-frame changes.
            sampled_alpha = actual_u8[::4, ::4, 3].astype(np.float32)
            if first_alpha is None: first_alpha = sampled_alpha
            if previous_alpha is not None: loop_step.append(float(np.abs(sampled_alpha-previous_alpha).mean()))
            previous_alpha = sampled_alpha
            if n in {0, 30, 60, 90, 119}:
                actual = actual_u8.astype(np.float32)
                expected = expected_frame(category, index, n).astype(np.float32)
                alpha_errors.append(float(np.abs(actual[:, :, 3]-expected[:, :, 3]).max()))
                composite_errors.append(float(np.mean(np.abs(actual[:, :, :3]*actual[:, :, 3:4]/255 - expected[:, :, :3]*expected[:, :, 3:4]/255))))
        assert not proc.stdout.read(1), "extra_decoded_frames"
        error = proc.stderr.read()
        assert proc.wait() == 0, error.decode("utf-8", "replace")
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()
        proc.stdout.close(); proc.stderr.close()
    assert max(alpha_errors) <= 1, (path.name, "alpha_mismatch", alpha_errors)
    assert max(composite_errors) < 1.5, (path.name, "premultiplied_rgb_mismatch", composite_errors)
    assert np.array_equal(np.array(LAYERS[category](index, 0)), np.array(LAYERS[category](index, 1))), "source_loop_not_periodic"
    result.update(dimensions=list(SIZE), frames=FRAMES, fps=FPS, seconds=4, audio_streams=0, alpha_mode=1,
                  alpha_max_error=max(alpha_errors), composite_mean_error_max=max(composite_errors), loop_periodic=True,
                  center_clear=True, verification="all_frames" if deep else "container_and_first_frame")
    if deep:
        assert len(hashes) >= 12, (path.name, "animation_has_too_few_distinct_frames", len(hashes))
        boundary=float(np.abs(previous_alpha-first_alpha).mean())
        assert boundary <= max(loop_step)*1.6+.025, (path.name, "loop_boundary_jump", boundary, max(loop_step))
        result.update(unique_frames=len(hashes), loop_boundary_alpha_mean_delta=boundary, max_adjacent_alpha_mean_delta=max(loop_step))
    return result


def main():
    started=time.perf_counter()
    started_at=datetime.now(timezone.utc).isoformat()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=REPO/"assets/random-subtemplates/20260920")
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--ffmpeg",default="ffmpeg");parser.add_argument("--ffprobe",default="ffprobe")
    args=parser.parse_args();root=args.root
    assert digest(root/"base-manifest.json") == BASE_SHA
    metadata=json.loads((root/"additions.json").read_text(encoding="utf-8"))
    manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    base=json.loads((root/"base-manifest.json").read_text(encoding="utf-8"))
    assert metadata["base_manifest_sha256"] == BASE_SHA
    assert digest(root/"manifest.json") == metadata["manifest_sha256"]
    assert metadata["counts"] == EXPECTED_COUNTS
    assert {k:len(v) for k,v in manifest["categories"].items()} == EXPECTED_COUNTS
    assert {k:v for k,v in manifest.items() if k!="categories"} == {k:v for k,v in base.items() if k!="categories"}
    for category,rows in base["categories"].items():assert manifest["categories"][category][:len(rows)] == rows
    assert manifest["categories"]["light"] == base["categories"]["light"]
    assert len(metadata["additions"]) == 42
    expected_names={}
    for category,info in GROUPS.items():
        stem="opacity-video" if category=="opacity_video" else category
        ext="png" if category in {"border","tint"} else "webm"
        expected_names[category]={f"{stem}-{info['start']+n:02}.{ext}" for n in range(len(info["names"]))}
    for category,names in expected_names.items():
        assert {r["file"] for r in metadata["additions"] if r["category"]==category} == names
    for category, rows in manifest["categories"].items():
        assert len({row["sha256"] for row in rows}) == len(rows), (category, "duplicate_sha_in_catalog")
    sys.path.insert(0,str(REPO))
    from features.drama_synthesis.catalog import catalog_from_manifest
    from features.fb_gpu.random_overlay import derive_recipe
    catalog=catalog_from_manifest((root/"manifest.json").resolve(),metadata["manifest_sha256"])
    seen={c:set() for c in catalog["categories"]}
    for n in range(2000):
        recipe=derive_recipe(asset_set=catalog,content_id=str(n),job_id="catalog-80-expansion-check",profile="test",source_url_sha256="c"*64)
        repeated=derive_recipe(asset_set=catalog,content_id=str(n),job_id="catalog-80-expansion-check",profile="test",source_url_sha256="c"*64)
        assert repeated == recipe, "recipe_not_repeatable"
        for category,row in recipe["assets"].items():seen[category].add(row["name"])
    for category,rows in catalog["categories"].items():assert seen[category]=={row["name"] for row in rows}
    results=[]
    for item in metadata["additions"]:
        path=root/"layers"/item["file"]
        assert digest(path)==item["sha256"]
        rows={r["name"]:r for r in manifest["categories"][item["category"]]}
        assert rows[item["file"]]["sha256"]==item["sha256"] and rows[item["file"]]["size"]==path.stat().st_size
        result=verify_asset(path,item["category"],item["design_index"],ffmpeg=args.ffmpeg,ffprobe=args.ffprobe,deep=True)
        results.append(result);print(json.dumps(result),flush=True)
    assert len({r["sha256"] for r in results}) == 42, "duplicate_asset_bytes"
    report={"ok":True,"manifest_sha256":metadata["manifest_sha256"],"base_manifest_sha256":BASE_SHA,
            "counts":EXPECTED_COUNTS,"active_count":80,"new_assets":42,"original_rows_unchanged":True,
            "original_top_level_metadata_unchanged":True,"light_rows_unchanged":True,
            "strict_cpu_catalog_accepted":True,"random_selection_reaches_all_80_active_assets":True,
            "recipe_repeatability_2000_seeds":True,"no_duplicate_sha_within_any_category":True,
            "all_new_assets_distinct":True,"artwork_source_sha256":artwork_digest(),"results":results,
            "verification_started_at_utc":started_at,"verification_elapsed_seconds":round(time.perf_counter()-started,3),
            "ffmpeg_version":subprocess.check_output([args.ffmpeg,"-version"],text=True).splitlines()[0],
            "ffprobe_version":subprocess.check_output([args.ffprobe,"-version"],text=True).splitlines()[0]}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="results"}),flush=True)


if __name__=="__main__":main()
