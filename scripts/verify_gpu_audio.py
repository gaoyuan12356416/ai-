"""Offline CUDA audio qualification. No source fetch, COS, or publishing calls."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import time
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def ffmpeg_pcm(ffmpeg, pcm, rate, *, duration, channels=2):
    command = [ffmpeg, "-nostdin", "-v", "error", "-f", "f32le", "-ar", str(rate),
               "-ac", str(pcm.shape[1]), "-i", "pipe:0", "-af",
               "aresample=48000:async=1:first_pts=0,apad", "-ac", str(channels),
               "-t", str(duration), "-f", "f32le", "pipe:1"]
    result = subprocess.run(command, input=pcm.astype("<f4").tobytes(),
                            capture_output=True, check=True, timeout=45)
    import numpy as np
    return np.frombuffer(result.stdout, dtype="<f4").reshape(-1, channels)


def dsp_checks(ffmpeg):
    import numpy as np
    from features.random_gpu.gpu_audio import AudioTimeline, CudaAudioDsp
    dsp = CudaAudioDsp(block_samples=8192)
    cp = dsp.cp
    results = []
    for rate, channels in ((48000, 2), (48000, 1), (44100, 2), (16000, 1), (96000, 2)):
        t = np.arange(rate, dtype=np.float64) / rate
        a = 0.20*np.sin(2*np.pi*997*t) + 0.07*np.sin(2*np.pi*3001*t)
        b = 0.15*np.sin(2*np.pi*1307*t) + 0.05*np.cos(2*np.pi*4999*t)
        pcm = (a[:, None] if channels == 1 else np.stack((a, b), axis=1)).astype(np.float32)
        timeline = AudioTimeline.create(input_rate=rate, input_samples=len(pcm), duration_seconds=1)
        start = time.monotonic()
        out = np.concatenate([cp.asnumpy(block) for block in dsp.blocks(pcm, timeline)])
        wall = time.monotonic()-start
        ref = ffmpeg_pcm(ffmpeg, pcm, rate, duration=1)
        same_length = len(out) == len(ref) == 48000
        left, right = out[480:-480], ref[480:-480]
        snr = float(10*np.log10((np.mean(right.astype(np.float64)**2)+1e-30) /
                               (np.mean((left-right).astype(np.float64)**2)+1e-30)))
        whole_snr = float(10*np.log10((np.mean(ref.astype(np.float64)**2)+1e-30) /
                                     (np.mean((out-ref).astype(np.float64)**2)+1e-30)))
        peak_error = float(np.max(np.abs(out-ref)))
        # Exact block-boundary stability is more useful than a mirrored kernel test.
        other = CudaAudioDsp(block_samples=10001)
        out_other = np.concatenate([cp.asnumpy(block) for block in other.blocks(pcm, timeline)])
        block_identical = bool(np.array_equal(out, out_other))
        results.append({"input_rate":rate,"input_channels":channels,"output_samples":len(out),
                        "reference_samples":len(ref),"interior_snr_db":snr,
                        "whole_signal_snr_db":whole_snr,"max_abs_error":peak_error,
                        "different_block_sizes_identical":block_identical,"wall_seconds":round(wall,4),
                        "passed":same_length and block_identical and snr >= 50 and whole_snr >= 60 and peak_error <= 0.01})
    for origin in (0.25, -0.25):
        pcm = np.full((48000,2),0.25,dtype=np.float32)
        timeline = AudioTimeline.create(input_rate=48000,input_samples=len(pcm),duration_seconds=1.5,start_seconds=origin)
        out = np.concatenate([cp.asnumpy(block) for block in dsp.blocks(pcm,timeline)])
        expected = np.zeros((72000,2),dtype=np.float32)
        first=max(0,round(origin*48000));last=min(72000,round(origin*48000)+48000)
        expected[first:last]=0.25
        results.append({"origin_seconds":origin,"output_samples":len(out),
                        "passed":bool(np.array_equal(out,expected))})
    empty=np.empty((0,2),dtype=np.float32)
    timeline=AudioTimeline.create(input_rate=48000,input_samples=0,duration_seconds=0.5)
    out=np.concatenate([cp.asnumpy(block) for block in dsp.blocks(empty,timeline)])
    results.append({"missing_audio":True,"output_samples":len(out),
                    "passed":len(out)==24000 and bool((out==0).all())})
    pcm=np.zeros((60000,2),dtype=np.float32)
    for duration in ("1.00001","1.00002","1.00003","0.100001"):
        timeline=AudioTimeline.create(input_rate=48000,input_samples=len(pcm),duration_seconds=duration)
        count=sum(len(block) for block in dsp.blocks(pcm,timeline))
        reference=ffmpeg_pcm(ffmpeg,pcm,48000,duration=duration)
        results.append({"fractional_duration":duration,"output_samples":count,
                        "reference_samples":len(reference),"passed":count==len(reference)})
    return {"backend":"cuda","device":cp.cuda.runtime.getDeviceProperties(0)["name"].decode(),
            "checks":results,"passed":all(v["passed"] for v in results),
            "scope":"PCM resample/rematrix/pad only; continuous PCM timeline; not compressed audio decode"}


def aac_checks(ffmpeg, directory):
    import numpy as np
    import torch
    import torch_aac
    from torch_aac import AACEncoder
    from torch_aac.cpu._bitwriter_native import is_available
    from features.random_gpu.gpu_audio import cuda_aac_bitpacker
    # Never let device='auto' silently turn this into a CPU test.
    if not torch.cuda.is_available():
        raise RuntimeError("aac_cuda_unavailable")
    t=np.arange(96000,dtype=np.float64)/48000
    envelope=np.minimum(1,np.maximum(0,(t-0.1)*20))*np.minimum(1,np.maximum(0,(1.9-t)*20))
    pcm=np.stack((0.2*np.sin(2*np.pi*(300*t+900*t*t))*envelope,
                  0.15*np.sin(2*np.pi*(800*t+600*t*t))*envelope),axis=1).astype(np.float32)
    # An isolated pulse exercises channel independence and timing near the center.
    pcm[48000:48032,0]+=0.25
    started=time.monotonic()
    with AACEncoder(sample_rate=48000,channels=2,bitrate=192000,device="cuda",batch_size=64) as encoder:
        encoded=encoder.encode(torch.as_tensor(pcm,device="cuda"))
    path=directory/"torch-aac-stereo-192k.aac";path.write_bytes(encoded)
    elapsed=time.monotonic()-started
    result=subprocess.run([ffmpeg,"-v","error","-err_detect","explode","-i",str(path),
                           "-f","f32le","-acodec","pcm_f32le","pipe:1"],
                          capture_output=True,check=True,timeout=45)
    decoded=np.frombuffer(result.stdout,dtype="<f4").reshape(-1,2)
    # Compare plausible codec delays explicitly, rather than hiding sample shifts.
    candidates=[]
    for lag in range(-2048,2049):
        source_start=max(0,-lag);decoded_start=max(0,lag)
        count=min(len(pcm)-source_start,len(decoded)-decoded_start)
        if count<1024:continue
        # Sparse correlation makes the exhaustive delay search inexpensive.
        a=pcm[source_start:source_start+count:16,0]
        b=decoded[decoded_start:decoded_start+count:16,0]
        candidates.append((float(np.mean((a-b).astype(np.float64)**2)),lag))
    _,lag=min(candidates)
    source_start=max(0,-lag);decoded_start=max(0,lag)
    count=min(len(pcm)-source_start,len(decoded)-decoded_start)
    a=pcm[source_start:source_start+count];b=decoded[decoded_start:decoded_start+count]
    snr=float(10*np.log10((np.mean(a.astype(np.float64)**2)+1e-30)/(np.mean((a-b).astype(np.float64)**2)+1e-30)))
    report={"torch":torch.__version__,"torch_aac":getattr(torch_aac,"__version__","unknown"),
            "gpu":torch.cuda.get_device_name(0),"c_bitwriter_available":is_available(),
            "cpu_residual":["block-switch state machine","AAC ICS/section/scalefactor serialization",
                            "short-window or M/S spectral Huffman when selected","C bit packing","ADTS header assembly"],
            "input_samples":len(pcm),"decoded_samples":len(decoded),"best_alignment_lag_samples":lag,
            "aligned_snr_db":snr,"effective_bitrate":len(encoded)*8/2,
            "wall_seconds":round(elapsed,4),"strict_decode_passed":True,
            "sha256":hashlib.sha256(encoded).hexdigest(),
            "production_qualified":False,
            "reason":"codec quality not qualified; see real-dialogue matrix; exact timing is reported separately"}
    # The candidate's frame_audio omits the initial overlap and the last window.
    # Explicit overlap padding can be represented by MP4 edit-list/skip metadata;
    # it must never be hidden by an alignment-only quality score.
    device_pcm=torch.as_tensor(pcm,device="cuda")
    padded=torch.nn.functional.pad(device_pcm.T,(1024,1024)).T
    with AACEncoder(sample_rate=48000,channels=2,bitrate=192000,device="cuda",batch_size=64) as encoder:
        padded_bytes=encoder.encode(padded)
    padded_path=directory/"torch-aac-padded.aac";padded_path.write_bytes(padded_bytes)
    m4a=directory/"torch-aac-padded.m4a"
    subprocess.run([ffmpeg,"-y","-v","error","-itsoffset",str(-1024/48000),
                    "-i",str(padded_path),"-map","0:a:0","-c:a","copy","-t","2",
                    "-avoid_negative_ts","disabled","-use_editlist","1",str(m4a)],
                   capture_output=True,check=True,timeout=45)
    result=subprocess.run([ffmpeg,"-v","error","-err_detect","explode","-i",str(m4a),
                           "-f","f32le","-acodec","pcm_f32le","pipe:1"],
                          capture_output=True,check=True,timeout=45)
    final=np.frombuffer(result.stdout,dtype="<f4").reshape(-1,2)
    count=min(len(final),len(pcm));error=pcm[:count]-final[:count]
    padded_snr=float(10*np.log10((np.mean(pcm[:count].astype(np.float64)**2)+1e-30)/
                               (np.mean(error.astype(np.float64)**2)+1e-30)))
    reference=directory/"ffmpeg-aac-reference.m4a"
    subprocess.run([ffmpeg,"-y","-v","error","-f","f32le","-ar","48000","-ac","2",
                    "-i","pipe:0","-c:a","aac","-profile:a","aac_low","-b:a","192k",
                    "-t","2",str(reference)],input=pcm.astype("<f4").tobytes(),
                   capture_output=True,check=True,timeout=45)
    reference_decoded=subprocess.run([ffmpeg,"-v","error","-i",str(reference),"-f","f32le","pipe:1"],
                                     capture_output=True,check=True,timeout=45)
    ref=np.frombuffer(reference_decoded.stdout,dtype="<f4").reshape(-1,2)
    count=min(len(ref),len(pcm));error=pcm[:count]-ref[:count]
    ref_snr=float(10*np.log10((np.mean(pcm[:count].astype(np.float64)**2)+1e-30)/
                            (np.mean(error.astype(np.float64)**2)+1e-30)))
    report["padded_candidate"]={"decoded_samples":len(final),"expected_samples":len(pcm),
        "unshifted_snr_db":padded_snr,"reference_ffmpeg_snr_db":ref_snr,
        "reference_decoded_samples":len(ref),"priming_samples":1024,
        "cpu_mux":"FFmpeg stream-copy with edit list, qualification only",
        "strict_decode_passed":True,"production_qualified":False}
    from scripts.qualify_gpu_aac import mux_adts_exact
    exact_path=directory/"torch-aac-exact-timeline.m4a"
    exact_timing=mux_adts_exact(padded_bytes,exact_path,len(pcm))
    exact_result=subprocess.run([ffmpeg,"-v","error","-err_detect","explode","-i",str(exact_path),
                                 "-f","f32le","-acodec","pcm_f32le","pipe:1"],
                                capture_output=True,check=True,timeout=45)
    exact_timing["pcm_identical_to_padding_reference"]=exact_result.stdout==result.stdout
    exact_timing["container"]=json.loads(subprocess.check_output([str(Path(ffmpeg).with_name("ffprobe")),
        "-v","error","-show_entries","stream=duration,start_time,time_base:format=duration","-of","json",str(exact_path)]))
    report["exact_packet_timeline"]=exact_timing
    with cuda_aac_bitpacker() as packer:
        with AACEncoder(sample_rate=48000,channels=2,bitrate=192000,device="cuda",batch_size=64) as encoder:
            gpu_packed=encoder.encode(padded)
        pack_calls=packer.calls
    report["gpu_bitpacker"]={"calls":pack_calls,"byte_identical_to_native_bitpacker":gpu_packed==padded_bytes,
        "remaining_cpu":["block-switch state machine","AAC syntax/ICS/section/scalefactor preparation",
                         "short-window or M/S spectral Huffman when selected","ADTS header assembly"],
        "full_aac_encoder_on_gpu":False}
    from features.random_gpu.gpu_audio import CudaAdtsAssembler
    payloads=[];offset=0
    while offset<len(padded_bytes):
        size=((padded_bytes[offset+3]&3)<<11)|(padded_bytes[offset+4]<<3)|(padded_bytes[offset+5]>>5)
        if size<7:raise ValueError("invalid_candidate_adts")
        payloads.append(padded_bytes[offset+7:offset+size]);offset+=size
    reassembled=CudaAdtsAssembler().assemble(payloads)
    report["gpu_adts"]={"aac_frames":len(payloads),"byte_identical_to_candidate":reassembled==padded_bytes,
                        "scope":"ADTS header/packet length/position generation only"}
    return report


def bitpacker_checks():
    import numpy as np
    from features.random_gpu.gpu_audio import CudaBitPacker
    rng=np.random.default_rng(20261104);pack=CudaBitPacker();checks=[]
    for count in (0,1,2,137,500):
        lengths=rng.integers(0,33,size=count,dtype=np.uint8)
        codes=rng.integers(0,2**32,size=count,dtype=np.uint32)
        bits="".join(format(int(code)&((1<<int(n))-1),"0%db"%int(n)) for code,n in zip(codes,lengths) if n)
        padded=bits+"0"*((-len(bits))%8)
        expected=bytes(int(padded[i:i+8],2) for i in range(0,len(padded),8))
        output=np.zeros(len(expected)+3,dtype=np.uint8)
        got=pack(codes,lengths,output)
        checks.append({"symbols":count,"bits":got,"passed":got==len(bits) and output[:len(expected)].tobytes()==expected})
    return {"backend":"cuda","checks":checks,"passed":all(x["passed"] for x in checks)}


def adts_checks():
    from features.random_gpu.gpu_audio import CudaAdtsAssembler
    assembler=CudaAdtsAssembler();results=[]
    payloads=[bytes([i%256])*count for i,count in enumerate((1,37,8184))]
    for channels in (1,2):
        for rate_index in (0,3,12):
            expected=[]
            for payload in payloads:
                size=len(payload)+7
                fields=((0xfff,12),(0,1),(0,2),(1,1),(1,2),(rate_index,4),(0,1),
                        (channels,3),(0,4),(size,13),(0x7ff,11),(0,2))
                bits="".join(format(value,"0%db"%length) for value,length in fields)
                expected.append(int(bits,2).to_bytes(7,"big")+payload)
            actual=assembler.assemble(payloads,sample_rate_index=rate_index,channels=channels)
            results.append({"channels":channels,"sample_rate_index":rate_index,
                            "passed":actual==b"".join(expected)})
    return {"backend":"cuda","checks":results,"passed":all(x["passed"] for x in results)}


def decoder_checks(ffmpeg):
    import numpy as np
    from features.random_gpu.gpu_audio import decode_contiguous_audio,AudioQualificationError
    checks=[]
    with tempfile.TemporaryDirectory(prefix="audio-decode-qualification-") as directory:
        root=Path(directory);wave=root/"source.wav"
        pcm=(np.sin(np.arange(44100)*2*np.pi*997/44100)*12000).astype("<i2")
        def run(*args,data=None):
            return subprocess.run([ffmpeg,"-nostdin","-v","error",*map(str,args)],input=data,
                                  capture_output=True,check=True,timeout=30)
        run("-f","s16le","-ar","44100","-ac","1","-i","pipe:0",wave,data=pcm.tobytes())
        decoded=decode_contiguous_audio(wave,root/"source.pcm")
        checks.append({"fixture":"native_pcm_44100_mono","passed":decoded.rate==44100 and decoded.channels==1
                       and decoded.samples==44100 and np.array_equal(decoded.array()[:,0],pcm)})
        delayed=root/"delayed.mka"
        run("-copyts","-itsoffset","0.25","-i",wave,"-c:a","copy","-avoid_negative_ts","disabled",delayed)
        decoded=decode_contiguous_audio(delayed,root/"delayed.pcm")
        checks.append({"fixture":"coarse_container_clock_positive_origin","start_seconds":str(decoded.start_seconds),
                       "passed":decoded.samples==44100 and decoded.start_seconds==.25})
        gap=root/"gap.mka"
        run("-i",wave,"-af",r"asetpts=PTS+gte(T\,0.4)*0.1/TB","-c:a","pcm_s16le",gap)
        try:
            decode_contiguous_audio(gap,root/"gap.pcm");rejected=False
        except AudioQualificationError as exc:
            rejected=str(exc)=="discontinuous_audio_timestamps"
        checks.append({"fixture":"timestamp_gap_100ms_rejected","passed":rejected})
        noaudio=root/"noaudio.mp4"
        run("-f","lavfi","-i","color=s=16x16:r=10:d=0.1","-an","-c:v","mpeg4",noaudio)
        decoded=decode_contiguous_audio(noaudio,root/"noaudio.pcm")
        checks.append({"fixture":"no_audio_stream","passed":decoded.samples==0 and decoded.channels==2})
    return {"backend":"cpu_pyav_compressed_decode","checks":checks,"passed":all(x["passed"] for x in checks)}


def main():
    p=argparse.ArgumentParser();p.add_argument("--output",required=True)
    p.add_argument("--ffmpeg",default="/usr/bin/ffmpeg");p.add_argument("--aac",action="store_true")
    args=p.parse_args();out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    report={"dsp":dsp_checks(args.ffmpeg),"bitpacker":bitpacker_checks(),"adts":adts_checks(),
            "decoder":decoder_checks(args.ffmpeg)}
    if args.aac:
        try:report["aac"]=aac_checks(args.ffmpeg,out.parent)
        except Exception as exc:report["aac"]={"production_qualified":False,"error":type(exc).__name__,"detail":str(exc)[:300]}
    out.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))
    return 0 if all(report[key]["passed"] for key in ("dsp","bitpacker","adts","decoder")) else 1


if __name__=="__main__":
    raise SystemExit(main())
