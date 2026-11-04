"""Offline AAC comparison on an existing source; never touches source or tasks."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from fractions import Fraction

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def ffmpeg(*args, data=None):
    return subprocess.run(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", *map(str,args)],
                          input=data, capture_output=True, check=True, timeout=60).stdout


def metrics(source, actual):
    import numpy as np
    count=min(len(source),len(actual));a=source[:count].astype(np.float64);b=actual[:count].astype(np.float64)
    noise=np.mean((a-b)**2);power=np.mean(a*a)
    # Segment SNR includes transients and silence; report distribution separately.
    segment=[]
    for offset in range(0,count-960+1,960):
        x=a[offset:offset+960];y=b[offset:offset+960]
        if np.mean(x*x)>1e-8:
            segment.append(float(10*np.log10((np.mean(x*x)+1e-30)/(np.mean((x-y)**2)+1e-30))))
    lag_errors=[]
    for lag in range(-64,65):
        x=source[max(0,-lag):count-max(0,lag):8]
        y=actual[max(0,lag):count-max(0,-lag):8]
        lag_errors.append((float(np.mean((x-y)**2)),lag))
    # Log spectral error, limited to bins above the source's -60 dB floor.
    spectral=[];window=np.hanning(1024)[:,None]
    for offset in range(0,count-1024+1,512):
        x=np.abs(np.fft.rfft(a[offset:offset+1024]*window,axis=0))
        y=np.abs(np.fft.rfft(b[offset:offset+1024]*window,axis=0))
        keep=x>max(1e-7,float(x.max())*.001)
        if keep.any():spectral.extend((20*np.log10((y[keep]+1e-12)/(x[keep]+1e-12)))**2)
    return {"source_samples":len(source),"decoded_samples":len(actual),
            "whole_snr_db":float(10*np.log10((power+1e-30)/(noise+1e-30))),
            "segment_snr_median_db":float(np.median(segment)),
            "segment_snr_p10_db":float(np.percentile(segment,10)),
            "log_spectral_rmse_db":float(np.sqrt(np.mean(spectral))),
            "best_lag_samples":min(lag_errors)[1],"peak_abs":float(np.max(np.abs(b)))}


def decoded(path):
    import numpy as np
    return np.frombuffer(ffmpeg("-err_detect","explode","-i",path,"-f","f32le","-ac","2","pipe:1"),dtype="<f4").reshape(-1,2)


def mux_adts_exact(adts, target, samples):
    """CPU mux for timing qualification, not claimed as the production GPU mux."""
    import av
    offset=index=0
    with av.open(str(target),"w") as container:
        stream=container.add_stream("aac",rate=48000)
        stream.layout="stereo";stream.time_base=Fraction(1,48000)
        stream.codec_context.extradata=bytes([0x11,0x90])
        while offset<len(adts):
            if len(adts)-offset<7 or adts[offset:offset+2]!=b"\xff\xf1":
                raise ValueError("unqualified_adts_header")
            rate_index=(adts[offset+2]>>2)&15
            channels=((adts[offset+2]&1)<<2)|(adts[offset+3]>>6)
            if adts[offset+2]>>6!=1 or rate_index!=3 or channels!=2:
                raise ValueError("expected_aac_lc_48k_stereo")
            size=((adts[offset+3]&3)<<11)|(adts[offset+4]<<3)|(adts[offset+5]>>5)
            if size<7 or offset+size>len(adts):raise ValueError("bad_adts_frame_length")
            pts=index*1024-1024
            if pts>=samples:break
            packet=av.Packet(adts[offset+7:offset+size]);packet.stream=stream
            packet.time_base=Fraction(1,48000);packet.pts=packet.dts=pts
            packet.duration=min(1024,samples-pts)
            container.mux(packet);offset+=size;index+=1
    if index!=((samples+1023)//1024)+1:raise ValueError("candidate_missing_tail_frame")
    return {"packets":index,"priming_samples":1024,"last_packet_duration":packet.duration,
            "mux_backend":"cpu_pyav_qualification_only"}


def candidate(pcm,folder,name,mode,bitrate,enable_ms=False):
    import torch
    from torch_aac import AACEncoder
    from features.random_gpu.gpu_audio import torch_aac_perband_gain_shape
    started=time.monotonic();rows={"sf_mode":mode,"target_bitrate":bitrate,"ms_enabled":enable_ms}
    patch=torch_aac_perband_gain_shape() if mode=="perband" else nullcontext()
    with patch, AACEncoder(sample_rate=48000,channels=2,bitrate=bitrate,device="cuda",batch_size=64,sf_mode=mode) as encoder:
        encoder._enable_ms=enable_ms
        cuda=torch.as_tensor(pcm,device="cuda")
        cuda=torch.nn.functional.pad(cuda.T,(1024,1024)).T
        result=encoder.encode(cuda)
    rows["joint_gain_stereo_shape_fix"]=mode=="perband"
    duration=len(pcm)/48000
    adts=folder/(name+".aac");adts.write_bytes(result)
    target=folder/(name+".m4a")
    rows["timeline"]=mux_adts_exact(result,target,len(pcm))
    probe=json.loads(subprocess.check_output(["/usr/bin/ffprobe","-v","error","-show_entries",
        "stream=duration,start_time,time_base:format=duration","-of","json",str(target)]))
    rows["container_timing"]=probe
    rows.update(metrics(pcm,decoded(target)))
    # Count actual ADTS frames; length in ADTS includes its 7-byte header.
    offset=0;frame_count=0;payload=0
    while offset<len(result):
        length=((result[offset+3]&3)<<11)|(result[offset+4]<<3)|(result[offset+5]>>5)
        if length<7 or offset+length>len(result):raise ValueError("bad_adts_frame_length")
        payload+=length-7;offset+=length;frame_count+=1
    rows.update({"actual_payload_bitrate":payload*8/(frame_count*1024/48000),
                 "actual_adts_bitrate":len(result)*8/(frame_count*1024/48000),
                 "aac_frames":frame_count,"wall_seconds":round(time.monotonic()-started,3),
                 "sha256":hashlib.sha256(result).hexdigest(),"strict_decode_passed":True})
    return rows


def main():
    import numpy as np
    import torch
    p=argparse.ArgumentParser();p.add_argument("--source",required=True);p.add_argument("--output",required=True)
    args=p.parse_args();target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
    speech=np.frombuffer(ffmpeg("-i",args.source,"-vn","-t","4","-ac","2","-ar","48000","-f","f32le","pipe:1"),dtype="<f4").reshape(-1,2).copy()
    # A composite exercises transients, stereo differences and wideband content.
    t=np.arange(96000)/48000;rng=np.random.default_rng(304)
    noise=rng.normal(0,.025,(len(t),2));env=.3+.7*np.sin(2*np.pi*2*t)**2
    composite=noise+np.stack((.14*np.sin(2*np.pi*(300*t+1500*t*t)),.13*np.sin(2*np.pi*730*t)),axis=1)*env[:,None]
    for start in (10000,30000,55000,70000):composite[start:start+64,0]+=.3
    report={"device":torch.cuda.get_device_name(0),"torch":torch.__version__,"scope":"offline quality/timing comparison, not production qualification","fixtures":[]}
    for label,pcm in (("dialogue",speech),("composite",composite.astype(np.float32))):
        reference=target.parent/(label+"-reference.m4a")
        ffmpeg("-y","-f","f32le","-ar","48000","-ac","2","-i","pipe:0","-c:a","aac","-b:a","192k","-t",str(len(pcm)/48000),reference,data=pcm.astype("<f4").tobytes())
        item={"fixture":label,"pcm_sha256":hashlib.sha256(pcm.tobytes()).hexdigest(),"reference":metrics(pcm,decoded(reference)),"candidates":[]}
        for mode,rate,ms in (("uniform",192000,False),("perband",192000,False),("uniform",256000,False),("uniform",192000,True)):
            name=f"{label}-{mode}-{rate}-ms{int(ms)}"
            try:row=candidate(pcm,target.parent,name,mode,rate,ms)
            except Exception as exc:row={"sf_mode":mode,"target_bitrate":rate,"ms_enabled":ms,"error":type(exc).__name__,"detail":str(exc)[:300]}
            item["candidates"].append(row)
            print(json.dumps({"fixture":label,"candidate":row}),flush=True)
        report["fixtures"].append(item)
        target.write_text(json.dumps(report,indent=2)+"\n")
    return 0


if __name__=="__main__":raise SystemExit(main())
