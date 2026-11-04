"""Bounded real-source compatibility test; never modifies the input source."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    import numpy as np
    from features.random_gpu.gpu_audio import prepare_audio_track,AudioTimeline
    from scripts.verify_gpu_audio_track import cpu_reference_mux,decode,ffmpeg
    p=argparse.ArgumentParser();p.add_argument("--source",required=True);p.add_argument("--output",required=True)
    p.add_argument("--duration",default="30");p.add_argument("--audio-bitrate",type=int,default=192000)
    args=p.parse_args();report_path=Path(args.output);report_path.parent.mkdir(parents=True,exist_ok=True)
    count=[0]
    def check():count[0]+=1
    prepared=prepare_audio_track(args.source,report_path.parent,args.duration,check,audio_bitrate=args.audio_bitrate)
    candidate=report_path.with_suffix(".m4a");reference=report_path.with_name(report_path.stem+"-reference.m4a")
    cpu_reference_mux(prepared.track,candidate)
    ffmpeg("-y","-i",args.source,"-map","0:a:0","-vn","-af","aresample=48000:async=1:first_pts=0,apad",
           "-ac","2","-ar","48000","-c:a","aac","-b:a",str(args.audio_bitrate),"-t",args.duration,reference)
    a=decode(candidate);b=decode(reference);n=min(len(a),len(b));mse=np.mean((a[:n].astype(np.float64)-b[:n])**2)
    snr=float(10*np.log10((np.mean(b[:n].astype(np.float64)**2)+1e-30)/(mse+1e-30))) if mse else 300.0
    expected=AudioTimeline.create(input_rate=48000,input_samples=0,duration_seconds=args.duration).output_samples
    timing=prepared.track.samples[-1].dts+prepared.track.samples[-1].duration==expected
    rows={"source":str(args.source),"requested_duration":args.duration,"report":prepared.report,
          "spool":str(prepared.spool),"asc":prepared.track.config.hex(),"check_calls":count[0],
          "pcm_identical":bool(np.array_equal(a,b)),"decoded_samples":len(a),"reference_samples":len(b),
          "snr_vs_reference_db":snr,"precise_audio_timeline":timing,
          "first_packet_pts":prepared.track.samples[0].pts,"last_packet_duration":prepared.track.samples[-1].duration}
    rows["container"]=json.loads(subprocess.check_output(["/usr/bin/ffprobe","-v","error","-show_entries",
        "stream=duration,start_time,sample_rate,channels:format=duration","-of","json",str(candidate)]))
    rows["passed"]=rows["pcm_identical"] and timing
    report_path.write_text(json.dumps(rows,indent=2)+"\n");print(json.dumps(rows,indent=2))
    return 0 if rows["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
