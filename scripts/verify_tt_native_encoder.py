"""Tiny offline TT NVENC contract readback and independent profile decode."""
from pathlib import Path
from types import SimpleNamespace
import argparse
import json
import os
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    p=argparse.ArgumentParser();p.add_argument("--output",required=True)
    p.add_argument("--ffmpeg",default="/data/pipeline-dispatch/tools/tt/ffmpeg")
    p.add_argument("--ffprobe",default="/data/pipeline-dispatch/tools/tt/ffprobe")
    args=p.parse_args();folder=Path(args.output);folder.mkdir(parents=True,exist_ok=True)
    os.environ["LOGGER_LEVEL"]="TRACE"
    from features.random_gpu.gpu_compute import cupy_runtime
    from features.tt_gpu.worker import native_encoder_options
    import PyNvVideoCodec as nvc
    if nvc.__version__!="2.2.3":raise RuntimeError("requires_qualified_nvc_2_2_3")
    cp=cupy_runtime();cp.cuda.Device(0).use();image=cp.zeros((1280,720,4),dtype=cp.uint8)
    image[:,:,3]=255
    class Frame:
        def __init__(self,array):
            self.owner=array;self.__cuda_array_interface__=dict(array.__cuda_array_interface__)
            self.__cuda_array_interface__["strides"]=array.strides
        def cuda(self):return self
    frame=Frame(image);reports=[]
    for encoder_name,profile in (("h264_nvenc","High"),("hevc_nvenc","Main")):
        options=native_encoder_options(SimpleNamespace(video_encoder=encoder_name));codec=options["codec"]
        trace=folder/(codec+"-configuration.log")
        sys.stdout.flush();sys.stderr.flush();saved=(os.dup(1),os.dup(2))
        try:
            with trace.open("wb") as log:
                os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
                encoder=nvc.CreateEncoder(720,1280,"ABGR",False,cudacontext=cp.cuda.driver.ctxGetCurrent(),
                    cudastream=cp.cuda.get_current_stream().ptr,**options)
        finally:
            os.dup2(saved[0],1);os.dup2(saved[1],2);os.close(saved[0]);os.close(saved[1])
        actual=encoder.GetEncodeReconfigureParams()
        mapping={"averageBitrate":"bitrate","maxBitRate":"maxbitrate","vbvBufferSize":"vbvbufsize","frameRateNum":"fps"}
        readback={name:int(getattr(actual,name)) for name in mapping}
        for name,option in mapping.items():
            if readback[name]!=options[option]:raise AssertionError((name,readback[name],options[option]))
        if int(actual.rateControlMode)!=1 or int(actual.multiPass)!=2 or encoder.GetNumBFrames()!=3:
            raise AssertionError("nvenc_rate_control_or_bframe_contract_changed")
        driver={}
        for line in trace.read_text(errors="replace").splitlines():
            key,sep,value=line.partition(":")
            if sep:driver[key.strip()]=value.strip()
        expected={"presetGUID":"P6","tuningInfo":"NV_ENC_TUNING_INFO_HIGH_QUALITY","frameIntervalP":"4",
                  "useBFramesAsRef":"2","enableAQ":"1","aqStrength":"8","enableTemporalAQ":"1",
                  "enableLookahead":"1","lookaheadDepth":"32"}
        if any(driver.get(key)!=value for key,value in expected.items()):raise AssertionError({key:driver.get(key) for key in expected})
        params=nvc.NV_ENC_PIC_PARAMS();params.inputTimeStamp=0;params.frameIdx=0;params.inputDuration=1
        encoded=list(encoder.Encode(frame,params))+list(encoder.EndEncode())
        elementary=folder/(codec+"."+codec)
        elementary.write_bytes(bytes(encoder.GetSequenceParams())+b"".join(bytes(packet["data"]) for packet in encoded))
        subprocess.run([args.ffmpeg,"-v","error","-err_detect","explode","-i",str(elementary),"-f","null","-"],
                       capture_output=True,check=True,timeout=30)
        probe=json.loads(subprocess.check_output([args.ffprobe,"-v","error","-show_entries","stream=codec_name,profile,width,height,pix_fmt",
                                                  "-of","json",str(elementary)]))["streams"][0]
        if probe.get("profile")!=profile or probe.get("codec_name")!=codec:raise AssertionError(probe)
        reports.append({"codec":codec,"options":options,"readback":readback,"driver_contract":{k:driver[k] for k in expected},
                        "probe":probe,"packets":len(encoded),"strict_decode_passed":True})
        del encoder
    result={"sdk":nvc.__version__,"scope":"one frame per codec; configuration/profile proof, not whole-pipeline quality","checks":reports,"passed":True}
    (folder/"result.json").write_text(json.dumps(result,indent=2)+"\n");print(json.dumps(result,indent=2))


if __name__=="__main__":main()
