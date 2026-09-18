"""Validate approved additions, alpha video frames, and strict CPU catalogs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_synthesis.catalog import catalog_from_manifest
from features.fb_gpu.random_overlay import derive_recipe
from random_subtemplate_artwork import atmosphere, corner, border, tint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "assets/random-subtemplates/20260918")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    metadata = json.loads((root / "additions.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    old = json.loads((root / "base-manifest.json").read_text(encoding="utf-8"))
    assert metadata["counts"] == {"border": 8, "corners": 8, "light": 2, "opacity_video": 10, "tint": 12}
    for category, rows in old["categories"].items():
        assert manifest["categories"][category][:len(rows)] == rows
    catalog = catalog_from_manifest((root / "manifest.json").resolve(), metadata["manifest_sha256"])
    seen = {c: set() for c in catalog["categories"]}
    for n in range(1000):
        recipe = derive_recipe(asset_set=catalog, content_id=str(n), job_id="catalog-expansion-check", profile="test", source_url_sha256="c"*64)
        for category, row in recipe["assets"].items():
            seen[category].add(row["name"])
    for category, rows in catalog["categories"].items():
        assert seen[category] == {row["name"] for row in rows}, category
    results = []
    for item in metadata["additions"]:
        path = root / "layers" / item["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        index = int(item["id"][1:]) - 1
        if path.suffix == ".png":
            actual = np.array(Image.open(path))
            expected = np.array((border if item["category"] == "border" else tint)(index).resize((720,1280), Image.Resampling.LANCZOS))
            assert np.array_equal(actual, expected), path.name
            assert actual.shape == (1280,720,4)
            assert actual[:,:,3].min() == (255 if item["category"] == "tint" else 0)
            results.append({"file": path.name, "pixels_match_approved_design": True, "alpha_min": int(actual[:,:,3].min()), "alpha_max": int(actual[:,:,3].max())})
        else:
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)]))
            assert len(probe["streams"]) == 1
            stream = probe["streams"][0]
            assert (stream["width"], stream["height"]) == (720,1280)
            assert stream["codec_name"] == "vp9" and {k.lower(): v for k, v in stream["tags"].items()}.get("alpha_mode") == "1"
            assert stream["nb_read_frames"] == "120" and abs(float(probe["format"]["duration"]) - 4) < .001
            proc = subprocess.Popen(["ffmpeg", "-v", "error", "-c:v", "libvpx-vp9", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"], stdout=subprocess.PIPE)
            hashes, alpha_errors, composite_errors = set(), [], []
            for n in range(120):
                raw = proc.stdout.read(720*1280*4)
                assert len(raw) == 720*1280*4
                hashes.add(hashlib.sha256(raw).hexdigest())
                if n in {0, 30, 60, 90, 119}:
                    actual = np.frombuffer(raw, dtype=np.uint8).reshape(1280,720,4).astype(np.float32)
                    design = (atmosphere if item["category"] == "opacity_video" else corner)(index, n/120)
                    expected = np.array(design.resize((720,1280), Image.Resampling.LANCZOS)).astype(np.float32)
                    assert actual[:,:,3].min() == 0 and actual[:,:,3].max() > 0
                    alpha_errors.append(float(np.abs(actual[:,:,3]-expected[:,:,3]).max()))
                    composite_errors.append(float(np.mean(np.abs(actual[:,:,:3]*actual[:,:,3:4]/255 - expected[:,:,:3]*expected[:,:,3:4]/255))))
            assert not proc.stdout.read(1) and proc.wait() == 0
            # RGBA -> yuva420p range conversion can round alpha by one level.
            # The subtle paper-tag sway intentionally has seven raster states;
            # its approved 360px artwork moves only a few pixels per cycle.
            assert len(hashes) >= 6 and max(alpha_errors) <= 1, (path.name, len(hashes), alpha_errors)
            assert max(composite_errors) < 1.5
            layer = atmosphere if item["category"] == "opacity_video" else corner
            assert np.array_equal(np.array(layer(index,0)), np.array(layer(index,1)))
            results.append({"file": path.name, "frames": 120, "seconds": 4, "unique_frames": len(hashes), "alpha_max_error": max(alpha_errors), "composite_mean_error_max": max(composite_errors), "loop_periodic": True})
        print(json.dumps(results[-1]), flush=True)
    report = {"ok": True, "manifest_sha256": metadata["manifest_sha256"], "original_rows_unchanged": True,
              "random_selection_reaches_all_38_active_assets": True, "results": results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
