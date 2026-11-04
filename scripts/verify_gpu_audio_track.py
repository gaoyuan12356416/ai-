"""Offline integration of CUDA DSP with native AAC and raw packet spooling."""
from pathlib import Path
from fractions import Fraction
import argparse
import json
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def ffmpeg(*args,data=None):
    return subprocess.run(["/usr/bin/ffmpeg","-nostdin","-v","error",*map(str,args)],
                          input=data,capture_output=True,check=True,timeout=60).stdout


def cpu_reference_mux(track,output):
    """Qualification-only mux. Production consumes the same Track with CUDA."""
    import av
    handles={}
    try:
        with av.open(str(output),"w") as out:
            stream=out.add_stream("aac",rate=track.sample_rate);stream.layout="stereo"
            stream.time_base=Fraction(1,track.timescale);stream.codec_context.extradata=track.config
            for sample in track.samples:
                location=sample.data
                handle=handles.setdefault(location.path,None)
                if handle is None:
                    handle=handles[location.path]=location.path.open("rb")
                handle.seek(location.offset);packet=av.Packet(handle.read(location.size));packet.stream=stream
                packet.time_base=Fraction(1,track.timescale);packet.pts=sample.pts;packet.dts=sample.dts
                packet.duration=sample.duration;out.mux(packet)
    finally:
        for handle in handles.values():
            if handle is not None:handle.close()


def decode(path):
    import numpy as np
    return np.frombuffer(ffmpeg("-err_detect","explode","-i",path,"-f","f32le","-ac","2","pipe:1"),dtype="<f4").reshape(-1,2)


def main():
    import numpy as np
    from features.random_gpu.gpu_audio import prepare_audio_track,AudioTimeline
    p=argparse.ArgumentParser();p.add_argument("--output",required=True)
    p.add_argument("--source",help="Optional existing real dialogue; read-only extraction")
    p.add_argument("--audio-bitrate",type=int,default=192000)
    args=p.parse_args();report_path=Path(args.output);report_path.parent.mkdir(parents=True,exist_ok=True)
    rows=[]
    with tempfile.TemporaryDirectory(prefix="audio-track-qualification-",dir=report_path.parent) as scratch:
        root=Path(scratch);fixtures=[]
        for rate,channels,length,label in ((48000,2,1.2,"stereo_48k_long"),(48000,1,.3,"mono_48k_short"),
                (44100,1,1.2,"mono_44100_long"),(16000,1,1.2,"mono_16k_long"),(96000,2,1.2,"stereo_96k_long")):
            t=np.arange(round(length*rate))/rate
            left=(np.sin(2*np.pi*997*t)*10000).astype("<i2")
            pcm=left[:,None] if channels==1 else np.stack((left,(np.cos(2*np.pi*1307*t)*7000).astype("<i2")),axis=1)
            source=root/(label+".wav")
            ffmpeg("-f","s16le","-ar",rate,"-ac",channels,"-i","pipe:0",source,data=pcm.tobytes())
            fixtures.append((label,source,False,1))
        for duration in ("0.01","0.10001","1.00001"):
            fixtures.append(("fractional_"+duration,fixtures[0][1],False,duration))
        if args.source:
            dialogue=root/"dialogue_44100.wav"
            ffmpeg("-i",args.source,"-vn","-t","1.2","-ar","44100","-ac","2","-c:a","pcm_s16le",dialogue)
            fixtures.append(("real_dialogue_44100",dialogue,False,1))
        delayed=root/"delayed.mkv"
        ffmpeg("-f","lavfi","-i","color=s=16x16:r=10:d=1.2","-itsoffset","0.25","-i",fixtures[0][1],
               "-map","0:v:0","-map","1:a:0","-c:v","mpeg4","-c:a","pcm_s16le",delayed)
        fixtures.append(("positive_audio_start",delayed,False,1))
        gap=root/"gap.mka"
        ffmpeg("-i",fixtures[0][1],"-af",r"asetpts=PTS+gte(T\,0.4)*0.1/TB","-c:a","pcm_s16le",gap)
        fixtures.append(("timestamp_gap_compatibility",gap,False,1))
        surround=root/"surround.wav";time=np.arange(57600)/48000
        surround_pcm=np.stack([(np.sin(2*np.pi*frequency*time)*4000).astype("<i2") for frequency in (301,503,701,103,907,1103)],axis=1)
        ffmpeg("-f","s16le","-ar","48000","-ac","6","-channel_layout","5.1","-i","pipe:0",surround,data=surround_pcm.tobytes())
        fixtures.append(("surround_51_compatibility",surround,False,1))
        noaudio=root/"noaudio.mp4"
        ffmpeg("-f","lavfi","-i","color=s=16x16:r=10:d=1.2","-an","-c:v","mpeg4",noaudio)
        fixtures.append(("missing_audio",noaudio,True,1))
        for label,source,missing,duration in fixtures:
            output=root/(label+"-candidate.m4a");reference=root/(label+"-reference.m4a")
            checks=[0]
            def check():checks[0]+=1
            prepared=prepare_audio_track(source,root,duration,check,audio_bitrate=args.audio_bitrate)
            expected_samples=AudioTimeline.create(input_rate=48000,input_samples=0,duration_seconds=duration).output_samples
            cpu_reference_mux(prepared.track,output)
            inputs=("-f","lavfi","-i","anullsrc=r=48000:cl=stereo") if missing else ("-i",source)
            ffmpeg(*inputs,"-vn","-af","aresample=48000:async=1:first_pts=0,apad","-ar","48000","-ac","2",
                   "-c:a","aac","-b:a",str(args.audio_bitrate),"-t",str(duration),reference)
            actual=decode(output);expected=decode(reference)
            reference_pcm=np.frombuffer(ffmpeg(*inputs,"-vn","-af","aresample=48000:async=1:first_pts=0,apad",
                "-ar","48000","-ac","2","-c:a","pcm_f32le","-t",str(duration),"-f","f32le","pipe:1"),dtype="<f4").reshape(-1,2)
            count=min(len(actual),len(expected));error=actual[:count].astype(np.float64)-expected[:count]
            mse=np.mean(error**2);power=np.mean(expected[:count].astype(np.float64)**2)
            snr=float(10*np.log10((power+1e-30)/(mse+1e-30))) if mse else 300.0
            probe=json.loads(subprocess.check_output(["/usr/bin/ffprobe","-v","error","-show_entries",
                "stream=duration,start_time,sample_rate,channels:format=duration","-of","json",str(output)]))
            exact=np.array_equal(actual,expected)
            def source_snr(value):
                length=min(len(reference_pcm),len(value));x=reference_pcm[:length].astype(np.float64)
                err=x-value[:length];power=np.mean(x*x);mse=np.mean(err*err)
                return float(10*np.log10((power+1e-30)/(mse+1e-30))) if mse else 300.0
            baseline_quality=source_snr(expected);candidate_quality=source_snr(actual)
            # Filter arithmetic can move a lossy encoder across quantizer
            # decisions. Compare both encoders against independent PCM too;
            # decoded-candidate vs decoded-reference alone is not an SNR score
            # for either encoder's quality.
            passed=len(actual)==len(expected) and candidate_quality>=baseline_quality-.5 and prepared.track.samples[-1].dts+prepared.track.samples[-1].duration==expected_samples
            rows.append({"fixture":label,"pcm_identical":exact,"decoded_samples":len(actual),"reference_samples":len(expected),
                         "snr_vs_reference_db":snr,"max_abs_error":float(np.max(np.abs(error))),"check_calls":checks[0],
                         "baseline_snr_vs_pcm_db":baseline_quality,"candidate_snr_vs_pcm_db":candidate_quality,
                         "probe":probe,"report":prepared.report,"passed":passed})
            prepared.spool.unlink()
        before=set(root.iterdir())
        class Cancelled(Exception):pass
        def cancel_after_spool():
            if any(p not in before for p in root.glob("audio-au-*.bin")):raise Cancelled()
        cancelled=False
        try:prepare_audio_track(fixtures[0][1],root,1,cancel_after_spool)
        except Cancelled:cancelled=True
        rows.append({"fixture":"cancel_cleans_scratch_and_spool","passed":cancelled and set(root.iterdir())==before})
        from unittest.mock import patch
        gpu_failed=False
        with patch("features.random_gpu.gpu_audio.CudaAudioDsp",side_effect=RuntimeError("forced_cuda_failure")), \
             patch("features.random_gpu.gpu_audio._decode_cpu_dsp_compatibility",side_effect=AssertionError("silent_cpu_fallback")):
            try:prepare_audio_track(fixtures[0][1],root,1)
            except RuntimeError as exc:gpu_failed=str(exc)=="forced_cuda_failure"
        rows.append({"fixture":"cuda_failure_never_falls_back_to_cpu","passed":gpu_failed and set(root.iterdir())==before})
    report={"scope":"CPU decode/encode explicitly retained; CUDA PCM DSP; raw access units for GPU mux",
            "checks":rows,"passed":all(row["passed"] for row in rows)}
    report_path.write_text(json.dumps(report,indent=2)+"\n");print(json.dumps(report,indent=2))
    return 0 if report["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
