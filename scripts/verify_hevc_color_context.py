"""Offline official SDK color-context experiment; no production mutation."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    parser.add_argument('--pattern',choices=('gradient','bars'),default='gradient')
    args=parser.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    os.environ['LOGGER_LEVEL']='TRACE'
    from features.random_gpu.native_video import codec_runtime,PackedFrame
    from features.random_gpu.color_contract import OutputColorContract,REFERENCE_MP4
    from features.random_gpu.gpu_mux import CudaMP4,native_video_track
    from features.tt_gpu.worker import native_encoder_options
    cp,nvc,context=codec_runtime();width,height,count=720,1280,60
    # All intensities and saturated primaries expose range/matrix changes;
    # the same GPU frame is retained throughout both encoder runs.
    y,x=cp.mgrid[:height,:width];image=cp.empty((height,width,4),dtype=cp.uint8)
    image[:,:,0]=(x*255//(width-1)).astype(cp.uint8)
    image[:,:,1]=(y*255//(height-1)).astype(cp.uint8)
    image[:,:,2]=((x//90+y//160)%2*255).astype(cp.uint8);image[:,:,3]=255
    if args.pattern=='bars':
        colors=cp.asarray(((255,0,0,255),(0,255,0,255),(0,0,255,255),(0,0,0,255),
                           (255,255,255,255),(0,255,255,255),(255,0,255,255),(255,255,0,255)),dtype=cp.uint8)
        image[:]=colors[x//90]
    frame=PackedFrame(image);cp.cuda.get_current_stream().synchronize()
    reports={};ffmpeg='/data/pipeline-dispatch/tools/tt/ffmpeg';ffprobe='/data/pipeline-dispatch/tools/tt/ffprobe'
    for label in ('silent_vui','fixed_color_context'):
        with tempfile.TemporaryDirectory(prefix=label+'-',dir=out) as scratch:
            manager=OutputColorContract(nvc,scratch) if label=='fixed_color_context' else nullcontext(None)
            with manager as color:
                options=native_encoder_options(SimpleNamespace(video_encoder='hevc_nvenc'))
                if color is not None:options.update(color.encoder_options)
                trace=out/(label+'.configuration.log');sys.stdout.flush();sys.stderr.flush();saved=os.dup(1),os.dup(2)
                try:
                    with trace.open('wb') as log:
                        os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
                        encoder=nvc.CreateEncoder(width,height,'ABGR',False,cudacontext=context,
                            cudastream=cp.cuda.get_current_stream().ptr,**options)
                finally:
                    os.dup2(saved[0],1);os.dup2(saved[1],2);os.close(saved[0]);os.close(saved[1])
                config=encoder.GetEncodeReconfigureParams()
                keys=('averageBitrate','maxBitRate','vbvBufferSize','vbvInitialDelay','targetQuality',
                      'frameRateNum','frameRateDen','rateControlMode','multiPass')
                readback={key:int(getattr(config,key)) for key in keys};readback['bframes']=encoder.GetNumBFrames()
                driver={}
                for line in trace.read_text(errors='replace').splitlines():
                    key,sep,value=line.partition(':')
                    if sep:driver[key.strip()]=value.strip()
                sequence=bytes(encoder.GetSequenceParams());packets=[];offset=len(sequence)
                elementary=out/(label+'.hevc')
                with elementary.open('wb') as stream:
                    stream.write(sequence)
                    def write(encoded):
                        nonlocal offset
                        for packet in encoded:
                            data=bytes(packet['data']);stream.write(data)
                            packets.append(dict(offset=offset,size=len(data),pts=int(packet['timestamp']),duration=1))
                            offset+=len(data)
                    for i in range(count):
                        pic=nvc.NV_ENC_PIC_PARAMS();pic.inputTimeStamp=i;pic.frameIdx=i;pic.inputDuration=1
                        write(encoder.Encode(frame,pic))
                    write(encoder.EndEncode())
                del encoder
            if sorted(p['pts'] for p in packets)!=list(range(count)):raise AssertionError('incomplete_video_timeline')
            delay=max(index-packet['pts'] for index,packet in enumerate(packets))
            for index,packet in enumerate(packets):packet['dts']=index-delay
            record=dict(codec='hevc',sequence_header_hex=sequence.hex(),packets=packets)
            track=native_video_track(elementary,record,Path(scratch)/'video.hvcc')
            target=out/(label+'.mp4');CudaMP4().mux([track],target,2)
            # No RGB conversion in the oracle: hash original decoder YUV planes.
            decoded=subprocess.check_output([ffmpeg,'-v','error','-err_detect','explode','-i',str(target),
                '-an','-pix_fmt','yuv420p','-f','rawvideo','pipe:1'])
            probe=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-of','json',str(target)]))['streams'][0]
            selected=('presetGUID','tuningInfo','frameIntervalP','useBFramesAsRef','enableAQ','aqStrength',
                      'enableTemporalAQ','enableLookahead','lookaheadDepth','gopLength','idrPeriod','repeatSPSPPS','disableSPSPPS')
            reports[label]=dict(readback=readback,driver_contract={k:driver.get(k) for k in selected},probe=probe,
                frames=len(packets),yuv_bytes=len(decoded),yuv_sha256=hashlib.sha256(decoded).hexdigest(),
                packet_pts=[p['pts'] for p in packets])
            if args.pattern=='bars':reports[label]['color_means']=color_means(decoded,width,height)
    if args.pattern=='bars':
        # Independent old FFmpeg RGB->NVENC path, one identical RGBA frame.
        from features.tt_gpu.worker import _encoder_arguments
        legacy=out/'legacy-ffmpeg.mp4'
        subprocess.run([ffmpeg,'-y','-v','error','-f','rawvideo','-pixel_format','rgba','-video_size',f'{width}x{height}',
            '-framerate','30','-i','pipe:0',*_encoder_arguments(SimpleNamespace(video_encoder='hevc_nvenc')),
            '-pix_fmt','yuv420p','-g','60','-flags','+cgop','-an',str(legacy)],input=cp.asnumpy(image).tobytes(),check=True)
        decoded=subprocess.check_output([ffmpeg,'-v','error','-i',str(legacy),'-an','-pix_fmt','yuv420p','-f','rawvideo','pipe:1'])
        reports['legacy_ffmpeg']={'color_means':color_means(decoded,width,height)}
    old=reports['silent_vui'];new=reports['fixed_color_context'];probe=new['probe']
    result=dict(pattern=args.pattern,reference_bytes=len(REFERENCE_MP4),reference_sha256=hashlib.sha256(REFERENCE_MP4).hexdigest(),
        checks=reports,pixels_identical=old['yuv_sha256']==new['yuv_sha256'],
        parameters_identical=old['readback']==new['readback'] and old['driver_contract']==new['driver_contract'],
        timeline_identical=old['packet_pts']==new['packet_pts'],
        colors_correct=probe.get('color_range')=='tv' and probe.get('color_space')=='bt470bg')
    if args.pattern=='bars':
        # The default HEVC path actually converts ABGR with BT.709. Correct
        # output must match the old FFmpeg BT.601 conversion, not preserve
        # those incorrectly converted pixels merely to keep a byte hash.
        expected=reports['legacy_ffmpeg']['color_means'];actual=new['color_means']
        result['max_color_error_vs_legacy']=max(abs(a-b) for name in expected for a,b in zip(actual[name],expected[name]))
        result['conversion_matches_legacy']=result['max_color_error_vs_legacy']<=1
        result['passed']=all(result[key] for key in ('conversion_matches_legacy','parameters_identical','timeline_identical','colors_correct'))
    else:
        result['passed']=all(result[key] for key in ('pixels_identical','parameters_identical','timeline_identical','colors_correct'))
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key!='checks'},indent=2))
    return 0 if result['passed'] else 1


def color_means(decoded,width,height):
    import numpy as np
    length=width*height;data=np.frombuffer(decoded,dtype=np.uint8)
    planes=(data[:length].reshape(height,width),data[length:length*5//4].reshape(height//2,width//2),
            data[length*5//4:length*3//2].reshape(height//2,width//2))
    return {name:[float(plane[100//factor:200//factor,(i*90+20)//factor:(i*90+70)//factor].mean())
                  for plane,factor in zip(planes,(1,2,2))]
            for i,name in enumerate(('red','green','blue','black','white','cyan','magenta','yellow'))}


if __name__=='__main__':raise SystemExit(main())
