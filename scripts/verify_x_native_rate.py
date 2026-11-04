"""Read actual NVENC settings for X budgets; isolated GPU qualification only."""
import argparse
import gc
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    from features.random_gpu.native_video import codec_runtime, create_encoder
    cp, nvc, context = codec_runtime()
    # Values come from the unchanged X output_rate_control(512 MiB, duration)
    # and are also checked independently by test_x_random_template_native.py.
    cases = [dict(duration=30, bitrate=5000000, maxbitrate=6000000, vbvbufsize=10000000),
             dict(duration=1200, bitrate=2540000, maxbitrate=2989000, vbvbufsize=5978000),
             dict(duration=14400, bitrate=96000, maxbitrate=102000, vbvbufsize=204000)]
    results = []
    for item in cases:
        options = dict(codec="h264", profile="high", preset="P5", rc="vbr", cq=21,
                       fps=30, gop=60, idrperiod=60, global_header=1,
                       **{key:item[key] for key in ("bitrate", "maxbitrate", "vbvbufsize")})
        encoder, configured = create_encoder(nvc, 720, 1280, context,
                                              cp.cuda.get_current_stream().ptr, options)
        params = encoder.GetEncodeReconfigureParams()
        values = {}
        for name in dir(params):
            if not name.startswith("_"):
                value = getattr(params, name)
                if isinstance(value, (int, float, str, bool)):
                    values[name] = value
        results.append(dict(request=options, duration=item["duration"],
                            constructor_snapshot=values, configured=configured))
        # Write evidence before asserting so a version mismatch is reviewable.
        args.output.write_text(json.dumps(dict(version=nvc.__version__, cases=results), indent=2))
        assert values["averageBitrate"] == item["bitrate"], values
        assert values["maxBitRate"] == item["maxbitrate"], values
        assert values["vbvBufferSize"] == item["vbvbufsize"], values
        assert configured["applied_reconfigure"]["targetQuality"] == 21, configured
        assert configured["applied_reconfigure"]["averageBitrate"] == item["bitrate"], configured
        assert values["frameRateNum"] / values["frameRateDen"] == 30, values
        encoder.EndEncode()
        del encoder
        gc.collect()
    print(json.dumps(dict(version=nvc.__version__, cases=results)), flush=True)


if __name__ == "__main__":
    main()
