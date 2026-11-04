"""Reuse an isolated frozen TT spec while injecting official output metadata."""
from pathlib import Path
from unittest.mock import patch
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--previous',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();previous=Path(args.previous);out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    from features.random_gpu import native_pipeline,native_video
    from features.random_gpu.color_contract import OutputColorContract
    command=json.loads((previous/'commands.json').read_text())['candidate'];spec=json.loads(command[command.index('--spec')+1])
    if spec['encoder_options']['codec']!='hevc':raise ValueError('fixture_must_be_tt_hevc')
    _,nvc,_=native_video.codec_runtime();original=native_video.create_encoder
    with tempfile.TemporaryDirectory(prefix='color-scene-',dir=out) as scratch:
        with OutputColorContract(nvc,scratch) as color:
            def create_encoder(nvc,width,height,context,stream,options):
                return original(nvc,width,height,context,stream,dict(options,**color.encoder_options))
            with patch.object(native_video,'create_encoder',create_encoder):
                rendered=native_pipeline.render(spec,out/'candidate.mp4',work_dir=scratch)
    ffmpeg='/data/pipeline-dispatch/tools/tt/ffmpeg';ffprobe='/data/pipeline-dispatch/tools/tt/ffprobe'
    candidate=out/'candidate.mp4';baseline=previous/'baseline.mp4'
    subprocess.run([ffmpeg,'-v','error','-err_detect','explode','-i',str(candidate),'-f','null','-'],check=True,capture_output=True)
    metadata=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(candidate)]))
    quality=subprocess.run([ffmpeg,'-v','info','-i',str(candidate),'-i',str(baseline),'-filter_complex',
        '[0:v]split=2[c0][c1];[1:v]split=2[b0][b1];[c0][b0]psnr[p];[c1][b1]ssim[s]',
        '-map','[p]','-map','[s]','-an','-f','null','-'],capture_output=True,text=True,check=True)
    (out/'quality.log').write_text(quality.stderr)
    old_result=json.loads((previous/'result.json').read_text())
    frames=json.loads(subprocess.check_output([ffprobe,'-v','error','-select_streams','v:0','-show_frames','-show_entries',
        'frame=best_effort_timestamp_time','-of','json',str(candidate)]))['frames']
    timestamps=[frame['best_effort_timestamp_time'] for frame in frames]
    audio=subprocess.check_output([ffmpeg,'-v','error','-i',str(candidate),'-vn','-f','f32le','-ac','2','pipe:1'])
    result=dict(probe=metadata,native_report=rendered,strict_decode_passed=True,
        psnr_average_db=re.findall(r'PSNR .*average:([\d.inf]+)',quality.stderr)[-1],
        ssim_all=re.findall(r'SSIM .*All:([\d.]+)',quality.stderr)[-1],
        prior_psnr_average_db=old_result['psnr_average_db'],prior_ssim_all=old_result['ssim_all'],
        video_timestamps_identical=timestamps==[f['best_effort_timestamp_time'] for f in old_result['reports']['baseline']['video_frames']],
        audio_unchanged_from_prior_candidate=hashlib.sha256(audio).hexdigest()==old_result['reports']['candidate']['audio_pcm_sha256'])
    video=metadata['streams'][0];result['colors_correct']=video.get('color_range')=='tv' and video.get('color_space')=='bt470bg'
    result['passed']=all(result[k] for k in ('strict_decode_passed','video_timestamps_identical','audio_unchanged_from_prior_candidate','colors_correct'))
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('probe','native_report')},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
