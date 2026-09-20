import json,subprocess
from pathlib import Path
qa=Path('/data/drama-synthesis-gpu/work/catalog-qa-20260920')
ffprobe='/data/drama-synthesis-gpu/runtime/bin/ffprobe'
paths=[qa/('drama-%d.mp4'%i) for i in range(12)]+[qa/'drama-legacy.mp4',qa/'drama-previous.mp4',qa/'tt.mp4',qa/'fb.mp4']
rows=[]
for p in paths:
 data=json.loads(subprocess.check_output([ffprobe,'-v','error','-count_frames','-show_streams','-show_format','-of','json',str(p)]))
 video=next(s for s in data['streams'] if s['codec_type']=='video')
 assert (video['width'],video['height'])==(720,1280)
 assert video['codec_name']==('hevc' if p.name=='tt.mp4' else 'h264')
 assert video['nb_read_frames']=='150' and video['r_frame_rate']=='30/1'
 assert abs(float(video['duration'])-5)<.05 and abs(float(data['format']['duration'])-5)<.15
 assert len([s for s in data['streams'] if s['codec_type']=='audio'])==1
 rows.append({'file':p.name,'codec':video['codec_name'],'video_frames':150,'fps':30,'video_seconds':float(video['duration']),'container_seconds':float(data['format']['duration']),'dimensions':[720,1280],'audio_streams':1,'bytes':p.stat().st_size})
report={'ok':True,'clips':rows,'private_local_only':True,'no_upload_or_publish':True}
(qa/'media-verification.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'ok':True,'clips':len(rows),'total_output_frames':sum(r['video_frames'] for r in rows),'seconds_each':5,'no_upload_or_publish':True}))
