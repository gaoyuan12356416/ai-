"""Offline TT same-source/recipe comparison, with the actual TT media binary."""
from pathlib import Path
from types import SimpleNamespace
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    from features.tt_gpu import worker
    from features.fb_gpu.random_overlay import load_asset_set,selected_asset_paths
    p=argparse.ArgumentParser();p.add_argument("--fixture",required=True);p.add_argument("--output",required=True)
    p.add_argument("--duration",type=float,default=2);p.add_argument("--codec",choices=("h264","hevc"),required=True)
    args=p.parse_args();fixture=Path(args.fixture);out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    record=json.loads((fixture/"recipe.json").read_text());recipe=record["recipe"]
    assets=load_asset_set(Path(record["asset_root"]),recipe["asset_set_sha256"])
    paths=selected_asset_paths(recipe,assets);source=fixture/"source.mp4"
    ffmpeg="/data/pipeline-dispatch/tools/tt/ffmpeg";ffprobe="/data/pipeline-dispatch/tools/tt/ffprobe"
    def probe(path):
        return json.loads(subprocess.check_output([ffprobe,"-v","error","-show_streams","-show_format","-of","json",str(path)]))
    info=worker.inspect_input(probe(source),3600)
    config=SimpleNamespace(ffmpeg_bin=ffmpeg,video_encoder=args.codec+"_nvenc",
        compositor_backend=worker.BACKEND,media_mode=worker.RANDOM_OVERLAY_MEDIA_MODE)
    os.environ["RANDOM_GPU_PIPELINE_V2"]="1"
    os.environ["RANDOM_GPU_ASSET_CACHE_ROOT"]="/data/random-overlay-gpu/asset-cache-v1"
    os.environ["RANDOM_GPU_PYTHON"]=sys.executable
    baseline=out/"baseline.mp4";candidate=out/"candidate.mp4"
    os.environ["RANDOM_GPU_NATIVE"]="0"
    baseline_command=worker.build_random_overlay_command(config,source,baseline,info,args.duration,recipe,paths)
    os.environ["RANDOM_GPU_NATIVE"]="1"
    native_command=worker.build_random_overlay_command(config,source,candidate,info,args.duration,recipe,paths)
    (out/"commands.json").write_text(json.dumps({"baseline":baseline_command,"candidate":native_command},indent=2))
    timings={}
    for label,command in (("baseline",baseline_command),("candidate",native_command)):
        start=time.monotonic()
        worker.media_runner(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=180,check=True)
        timings[label]=time.monotonic()-start
    reports={}
    for label,path in (("baseline",baseline),("candidate",candidate)):
        subprocess.run([ffmpeg,"-v","error","-err_detect","explode","-i",str(path),"-f","null","-"],capture_output=True,check=True,timeout=60)
        audio=subprocess.check_output([ffmpeg,"-v","error","-i",str(path),"-vn","-f","f32le","-ac","2","pipe:1"])
        metadata=probe(path)
        frame_data=json.loads(subprocess.check_output([ffprobe,"-v","error","-select_streams","v:0","-show_frames",
            "-show_entries","frame=best_effort_timestamp_time,pkt_duration_time,pict_type","-of","json",str(path)]))
        reports[label]={"probe":metadata,"audio_pcm_samples":len(audio)//8,"audio_pcm_sha256":hashlib.sha256(audio).hexdigest(),
                        "video_frames":frame_data["frames"]}
    quality=subprocess.run([ffmpeg,"-v","info","-i",str(candidate),"-i",str(baseline),"-filter_complex",
        "[0:v]split=2[c0][c1];[1:v]split=2[b0][b1];[c0][b0]psnr[p];[c1][b1]ssim[s]",
        "-map","[p]","-map","[s]","-an","-f","null","-"],capture_output=True,text=True,check=True,timeout=60)
    (out/"quality.log").write_text(quality.stderr)
    psnr=re.findall(r"PSNR .*average:([\d.inf]+)",quality.stderr)
    ssim=re.findall(r"SSIM .*All:([\d.]+)",quality.stderr)
    times=lambda label:[frame.get("best_effort_timestamp_time") for frame in reports[label]["video_frames"]]
    result={"codec":args.codec,"requested_duration":args.duration,"recipe_sha256":hashlib.sha256(json.dumps(recipe,sort_keys=True).encode()).hexdigest(),
        "timings":timings,"reports":reports,"video_timestamps_identical":times("baseline")==times("candidate"),
        "audio_pcm_identical":reports["baseline"]["audio_pcm_sha256"]==reports["candidate"]["audio_pcm_sha256"],
        "psnr_average_db":psnr[-1] if psnr else None,"ssim_all":ssim[-1] if ssim else None,
        "strict_decode_passed":True,"native_report":json.loads(candidate.with_suffix(".mp4.native.json").read_text())}
    (out/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({key:value for key,value in result.items() if key not in {"reports","native_report"}},indent=2))


if __name__=="__main__":main()
