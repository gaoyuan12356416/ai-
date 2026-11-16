"""Compare identical-frame encodes locally; no source URLs or platform writes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run-root',type=Path,required=True)
    p.add_argument('--ffmpeg',default='/usr/bin/ffmpeg')
    p.add_argument('--ffprobe',default='/usr/bin/ffprobe')
    a=p.parse_args()
    report=json.loads((a.run_root/'encoding-baseline_p5_cq21.json').read_text())
    cases={c['name']:c for c in report['cases'] if c.get('status')=='completed'}
    quality=a.run_root/'quality'
    quality.mkdir(exist_ok=True)
    filters=subprocess.check_output([a.ffmpeg,'-hide_banner','-filters'],stderr=subprocess.DEVNULL,text=True)
    vmaf='libvmaf' in filters
    rows=[]
    for index in range(1,4):
        baseline=cases[f'source{index}-baseline_p5_cq21']
        reference=Path(baseline['outputs'][0])
        for name in ['p4_cq23','p4_cq23_3m','p4_cq25']:
            case=cases[f'source{index}-{name}']
            candidate=Path(case['outputs'][0])
            prefix=quality/f'source{index}-{name}'
            vmaf_path=prefix.with_suffix('.vmaf.json')
            ssim_path=prefix.with_suffix('.ssim.txt')
            common=[a.ffmpeg,'-hide_banner','-nostdin','-threads','2','-i',str(candidate),
                '-threads','2','-i',str(reference),'-filter_complex_threads','2']
            if vmaf:
                graph=(f'[0:v]split[d0][d1];[1:v]split[r0][r1];'
                    f'[d0][r0]ssim=stats_file={ssim_path}[s];'
                    f'[d1][r1]libvmaf=n_threads=2:n_subsample=3:log_fmt=json:log_path={vmaf_path}[v]')
                maps=['-map','[s]','-f','null','-','-map','[v]','-f','null','-']
            else:
                graph=f'[0:v][1:v]ssim=stats_file={ssim_path}[s]'
                maps=['-map','[s]','-f','null','-']
            result=subprocess.run(common+['-filter_complex',graph]+maps,
                capture_output=True,text=True,timeout=300)
            prefix.with_suffix('.log').write_text(result.stderr)
            if result.returncode:
                raise RuntimeError('quality_comparison_failed:'+str(prefix))
            score=re.findall(r'All:([0-9.]+)',result.stderr)
            audio=[]
            for file in [reference,candidate]:
                pcm=subprocess.check_output([a.ffmpeg,'-v','error','-threads','2','-i',str(file),
                    '-map','0:a:0','-f','s16le','-'])
                audio.append(hashlib.sha256(pcm).hexdigest())
            metadata=json.loads(subprocess.check_output([a.ffprobe,'-v','error','-count_frames',
                '-show_streams','-show_format','-of','json',str(candidate)]))
            video=next(s for s in metadata['streams'] if s['codec_type']=='video')
            row=dict(source=index,encoding=name,ssim=float(score[-1]),
                vmaf=json.loads(vmaf_path.read_text())['pooled_metrics']['vmaf']['mean'] if vmaf else None,
                bytes=candidate.stat().st_size,baseline_bytes=reference.stat().st_size,
                size_reduction=1-candidate.stat().st_size/reference.stat().st_size,
                duration=float(metadata['format']['duration']),frames=int(video['nb_read_frames']),
                width=video['width'],height=video['height'],codec=video['codec_name'],
                audio_pcm_identical=audio[0]==audio[1])
            rows.append(row)
            print(json.dumps(row),flush=True)
            (quality/'results.json').write_text(json.dumps(rows,indent=2))
    try:
        from PIL import Image, ImageDraw
        width,height=300,533
        canvas=Image.new('RGB',(width*4,(height+30)*3),'white')
        draw=ImageDraw.Draw(canvas)
        names=['baseline_p5_cq21','p4_cq23','p4_cq23_3m','p4_cq25']
        for i in range(1,4):
            for col,name in enumerate(names):
                file=Path(cases[f'source{i}-{name}']['outputs'][0])
                jpg=quality/f'source{i}-{name}.jpg'
                subprocess.run([a.ffmpeg,'-v','error','-threads','1','-ss','10','-i',str(file),
                    '-frames:v','1','-vf',f'scale={width}:{height}','-y',str(jpg)],check=True)
                canvas.paste(Image.open(jpg),(col*width,(i-1)*(height+30)+30))
                draw.text((col*width+5,(i-1)*(height+30)+8),f'S{i} {name}',fill='black')
        canvas.save(quality/'comparison.jpg',quality=88)
    except ImportError:
        pass


if __name__=='__main__':
    main()
