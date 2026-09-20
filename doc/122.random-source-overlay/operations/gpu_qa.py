"""Private real-GPU acceptance; does not download, upload, or publish business jobs."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

releases=json.loads(Path(sys.argv[1]).read_text())
qa=Path('/data/drama-synthesis-gpu/work/source-overlay-qa-20260920')
codes={'drama':'/data/drama-synthesis-gpu/releases/'+releases['drama'],
       'tt':'/data/tt-post-gpu/releases/'+releases['tt'],
       'fb':'/data/random-overlay-gpu/releases/'+releases['fb']}

def environment(unit):
    pid=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value']).decode().strip()
    return dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)

owner=pwd.getpwnam('drama-synthesis-gpu')
qa.mkdir(mode=0o755,exist_ok=True);os.chown(str(qa),owner.pw_uid,owner.pw_gid)
if (qa/'report.json').exists():os.replace(str(qa/'report.json'),str(qa/('report-previous-%d.json'%time.time())))
env=environment('drama-synthesis-gpu-worker.service')
env.update(QA_ROOT=str(qa),QA_CODE=codes['drama'],DRAMA_GPU_RELEASE_SHA=releases['drama'],
           DRAMA_GPU_COMPOSITOR_CACHE_ROOT=str(qa/'cache'))
ffmpeg=env['DRAMA_FFMPEG']
if not (qa/'source-short.mp4').exists():
    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc2=s=720x1280:r=30:d=4',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4',
        '-c:v','h264_nvenc','-bf','0','-pix_fmt','yuv420p','-colorspace','bt709',
        '-color_primaries','bt709','-color_trc','bt709','-color_range','tv',
        '-c:a','aac','-ac','2','-t','4',str(qa/'source-short.mp4')],check=True,timeout=60)
    (qa/'source-short.mp4').chmod(0o444)
for pipeline in ['cuda','opencl']:
    e=dict(env);e['DRAMA_GPU_FRAME_PIPELINE']=pipeline
    log=qa/('preflight-'+pipeline+'.log')
    with log.open('w') as f:
        subprocess.run(['runuser','-u','drama-synthesis-gpu','--',
            '/data/drama-synthesis-gpu/runtime/current/bin/python',
            codes['drama']+'/scripts/check_drama_synthesis_gpu_runtime.py','--check-app-import'],
            env=e,cwd=codes['drama'],stdout=f,stderr=subprocess.STDOUT,check=True,timeout=600)
    print(json.dumps({'preflight':pipeline,'ok':True}),flush=True)

DRAMA=r'''
import hashlib,json,os,sys,time
from pathlib import Path
sys.path.insert(0,os.environ['QA_CODE'])
from features.drama_synthesis.catalog import catalog_from_manifest
from features.drama_synthesis.core import freeze_random_recipe
from features.drama_synthesis.gpu_compositor import canonical_json
from features.drama_synthesis.gpu import render_random_output
qa=Path(os.environ['QA_ROOT']);root=Path(os.environ['DRAMA_RANDOM_OVERLAY_ROOT']);sha=os.environ['DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256']
catalog=catalog_from_manifest(root/'manifest.json',sha)
base=freeze_random_recipe(job_id='0'*31+'1',content_id='source-overlay-offline-qa',
    request={'mode':'manual','source':'concat_video','layers':{'border':'border-10.png','opacity_video':'opacity-video-16.webm','corners':'corners-20.webm','tint':'tint-13.png'}},catalog=catalog)
rows=[]
for name,source,overlay,pipeline in [
    ('drama-minute',Path('/data/drama-synthesis-gpu/experiments/frame-pipeline-20260915/source-60.mp4'),{'version':1,'opacity_bp':350,'scale_bp':13000},'cuda'),
    ('drama-old',qa/'source-short.mp4',None,'cuda'),
    ('drama-min',qa/'source-short.mp4',{'version':1,'opacity_bp':200,'scale_bp':11000},'cuda'),
    ('drama-max-opencl',qa/'source-short.mp4',{'version':1,'opacity_bp':500,'scale_bp':15000},'opencl')]:
    recipe=dict(base);recipe.pop('recipe_sha256');recipe.pop('source_overlay',None)
    if overlay is not None:recipe['source_overlay']=overlay
    recipe['recipe_sha256']=hashlib.sha256(canonical_json(recipe).encode()).hexdigest()
    os.environ['DRAMA_GPU_FRAME_PIPELINE']=pipeline
    started=time.monotonic()
    result=render_random_output(source=source,output=qa/(name+'.mp4'),recipe=recipe,
        asset_root=root,manifest_sha256=sha,ffmpeg=os.environ['DRAMA_FFMPEG'],ffprobe=os.environ['DRAMA_FFPROBE'])
    rows.append({'name':name,'source_overlay':overlay,'pipeline':pipeline,'elapsed_seconds':round(time.monotonic()-started,3),'recipe':recipe,'result':result})
    print(json.dumps({'rendered':name,'elapsed_seconds':rows[-1]['elapsed_seconds']}),flush=True)
(qa/'drama-results.json').write_text(json.dumps(rows,indent=2))
'''
subprocess.run(['runuser','-u','drama-synthesis-gpu','--',
    '/data/drama-synthesis-gpu/runtime/current/bin/python','-c',DRAMA],env=env,cwd=codes['drama'],check=True,timeout=900)

LANE=r'''
import json,os,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,os.environ['QA_CODE'])
lane=os.environ['QA_LANE'];qa=Path(os.environ['QA_ROOT'])
if lane=='tt':
    from features.tt_gpu.random_overlay import load_asset_set,selected_asset_paths
    from features.tt_gpu.worker import WorkerConfig,build_random_overlay_command,probe_media,inspect_input
    config=WorkerConfig.from_env();info=inspect_input(probe_media(config,qa/'source-short.mp4'),3600)
else:
    from features.fb_gpu.random_overlay import load_asset_set,selected_asset_paths
    from features.fb_gpu.prepare_worker import WorkerConfig,build_command,_probe
    config=WorkerConfig.from_env();info=_probe(config,qa/'source-short.mp4')
assets=load_asset_set(Path('/data/random-overlay-gpu/assets/catalog-b5df776a88bd'),'b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c')
names={'border':'border-10.png','opacity_video':'opacity-video-16.webm','corners':'corners-20.webm','tint':'tint-13.png'}
base={'version':1,'asset_set_sha256':assets['manifest_sha256'],'assets':{c:{key:next(item for item in assets['categories'][c] if item['name']==name)[key] for key in ['name','sha256','size','media_type']} for c,name in names.items()},'rotation_millidegrees':700,'scale_bp':10020,'tint_opacity_bp':1000}
rows=[]
cases=[('old',None,qa/'source-short.mp4',4),('new',{'version':1,'opacity_bp':350,'scale_bp':13000},qa/'source-short.mp4',4)]
if lane=='fb':cases.append(('previous-stalled-source',{'version':1,'opacity_bp':350,'scale_bp':13000},Path('/var/lib/fb-page-random-overlay/jobs/fb-page-03e739bb938593abf39fbf5d4013467621e89d777fbce903/source.mp4'),108.3))
for label,overlay,source,duration in cases:
    recipe=dict(base)
    if overlay is not None:recipe['source_overlay']=overlay
    paths=selected_asset_paths(recipe,assets);output=qa/(lane+'-'+label+'.mp4')
    if lane=='fb':info=_probe(config,source)
    if lane=='tt':command=build_random_overlay_command(config,source,output,info,duration,recipe,paths)
    else:command=build_command(config,source,output,info,recipe,paths)
    started=time.monotonic()
    subprocess.run(command,check=True,timeout=180,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    rows.append({'name':output.stem,'source_overlay':overlay,'recipe':recipe,'elapsed_seconds':round(time.monotonic()-started,3)})
    print(json.dumps({'rendered':output.stem,'elapsed_seconds':rows[-1]['elapsed_seconds']}),flush=True)
(qa/(lane+'-results.json')).write_text(json.dumps(rows,indent=2))
'''
for lane,unit,python in [('tt','tt-gpu-publisher.service','/data/tt-post-gpu/runtime/bin/python'),('fb','fb-page-random-overlay-gpu.service','/opt/fb-page-random-overlay/venv/bin/python')]:
    e=environment(unit);e.update(QA_CODE=codes[lane],QA_ROOT=str(qa),QA_LANE=lane)
    subprocess.run([python,'-c',LANE],env=e,cwd=codes[lane],check=True,timeout=600)

outputs=[]
for p in sorted(qa.glob('*.mp4')):
    if p.name=='source-short.mp4':continue
    data=json.loads(subprocess.check_output([env['DRAMA_FFPROBE'],'-v','error','-count_frames','-show_streams','-show_format','-of','json',str(p)],timeout=90))
    videos=[s for s in data['streams'] if s['codec_type']=='video'];audio=[s for s in data['streams'] if s['codec_type']=='audio']
    assert len(videos)==len(audio)==1,p.name
    v=videos[0];duration=60 if p.stem=='drama-minute' else 108.3 if p.stem=='fb-previous-stalled-source' else 4
    assert (v['width'],v['height'],v['avg_frame_rate'])==(720,1280,'30/1'),p.name
    assert v['codec_name']==('hevc' if p.stem.startswith('tt-') else 'h264'),p.name
    assert int(v['nb_read_frames'])==round(duration*30),p.name
    assert abs(float(v['duration'])-duration)<.08,p.name
    assert audio[0]['channels']==2 and audio[0]['sample_rate']=='48000',p.name
    subprocess.run([ffmpeg,'-v','error','-xerror','-i',str(p),'-f','null','-'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=90)
    outputs.append({'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'frames':int(v['nb_read_frames']),'seconds':float(v['duration']),'audio_streams':len(audio),'codec':v['codec_name']})
assert len(outputs)==9
e=environment('tt-gpu-publisher.service')
subprocess.run(['/data/tt-post-gpu/runtime/bin/python',codes['tt']+'/scripts/check_random_gpu_timeline.py','--output',str(qa/'tt-timeline'),'--worker','tt','--ffmpeg',e['TT_POST_GPU_FFMPEG_BIN'],'--ffprobe',e['TT_POST_GPU_FFPROBE_BIN'],'--source-overlay'],env=e,cwd=codes['tt'],check=True,timeout=600)
report={'ok':True,'releases':releases,'outputs':outputs,'preflight':['cuda','opencl'],'no_upload_or_publication':True}
(qa/'report.new.json').write_text(json.dumps(report,indent=2));os.replace(str(qa/'report.new.json'),str(qa/'report.json'));print(json.dumps(report,indent=2),flush=True)
