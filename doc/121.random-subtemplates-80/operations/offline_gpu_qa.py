import json,os,pwd,subprocess
from pathlib import Path
MAIN='/data/drama-synthesis-gpu/releases/9c4c3cb4eca260d46df8ab23443a32cda667c20c'
TT='/data/tt-post-gpu/releases/326e16defb8f0b1aec76e8f36d52bb6359275a98-catalog'
SHA='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
PREVIOUS='24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c'
OLD='028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f'
ROOT='/data/drama-synthesis-gpu/assets/catalog-b5df776a88bd'
QA=Path('/data/drama-synthesis-gpu/work/catalog-qa-20260920')
def environment(unit):
    pid=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value']).decode().strip()
    return dict(item.decode().split('=',1) for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in item)
env=environment('drama-synthesis-gpu-worker.service')
env.update(DRAMA_GPU_RELEASE_SHA=MAIN.rsplit('/',1)[1],DRAMA_RANDOM_OVERLAY_ROOT=ROOT,DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256=SHA,RANDOM_OVERLAY_ASSET_CATALOGS=json.dumps({OLD:'/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114',PREVIOUS:'/data/drama-synthesis-gpu/assets/catalog-24d6fad3174f',SHA:ROOT}))
owner=pwd.getpwnam('drama-synthesis-gpu')
QA.mkdir(mode=0o755,exist_ok=True);os.chown(str(QA),owner.pw_uid,owner.pw_gid)
ffmpeg=env['DRAMA_FFMPEG']
source=QA/'source.mp4'
if not source.exists():
    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc2=s=720x1280:r=30:d=5','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=5','-c:v','h264_nvenc','-bf','0','-pix_fmt','yuv420p','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709','-color_range','tv','-c:a','aac','-ac','2','-t','5',str(source)],check=True)
    source.chmod(0o444)
if not (QA/'drama-report.json').exists():
    print('Drama actual-runtime preflight',flush=True)
    subprocess.run(['runuser','-u','drama-synthesis-gpu','--','/data/drama-synthesis-gpu/runtime/current/bin/python',MAIN+'/scripts/check_drama_synthesis_gpu_runtime.py','--check-app-import'],env=env,cwd=MAIN,check=True,timeout=600)
DRAMA=r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,os.environ['QA_MAIN'])
from features.drama_synthesis.catalog import catalog_from_manifest
from features.drama_synthesis.core import freeze_random_recipe
from features.drama_synthesis.gpu import render_random_output
root=Path(os.environ['DRAMA_RANDOM_OVERLAY_ROOT']);sha=os.environ['DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256']
catalog=catalog_from_manifest(root/'manifest.json',sha)
qa=Path('/data/drama-synthesis-gpu/work/catalog-qa-20260920')
reports=[]
for i in range(12):
    request={'mode':'manual','source':'concat_video','layers':{'border':'border-%02d.png'%(9+i),'opacity_video':'opacity-video-%02d.webm'%(11+i%10),'corners':'corners-%02d.webm'%(9+i),'tint':'tint-%02d.png'%(13+i%8)}}
    recipe=freeze_random_recipe(job_id=('%032x'%(i+1)),content_id='catalog-offline-qa',request=request,catalog=catalog)
    result=render_random_output(source=qa/'source.mp4',output=qa/('drama-%d.mp4'%i),recipe=recipe,asset_root=root,manifest_sha256=sha,ffmpeg=os.environ['DRAMA_FFMPEG'],ffprobe=os.environ['DRAMA_FFPROBE'])
    reports.append({'lane':'drama','index':i,'catalog':sha,'layers':request['layers'],'result':result})
    print(json.dumps({'drama_rendered':i,'layers':request['layers']}),flush=True)
old_sha='028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f'
old_root=Path('/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114')
old_catalog=catalog_from_manifest(old_root/'manifest.json',old_sha)
recipe=freeze_random_recipe(job_id='f'*32,content_id='catalog-legacy-qa',request={'mode':'auto','source':'concat_video'},catalog=old_catalog)
result=render_random_output(source=qa/'source.mp4',output=qa/'drama-legacy.mp4',recipe=recipe,asset_root=root,manifest_sha256=sha,ffmpeg=os.environ['DRAMA_FFMPEG'],ffprobe=os.environ['DRAMA_FFPROBE'])
reports.append({'lane':'drama-legacy','catalog':old_sha,'result':result})
previous_sha='24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c'
previous_root=Path('/data/drama-synthesis-gpu/assets/catalog-24d6fad3174f')
previous_catalog=catalog_from_manifest(previous_root/'manifest.json',previous_sha)
recipe=freeze_random_recipe(job_id='e'*32,content_id='catalog-previous-qa',request={'mode':'manual','source':'concat_video','layers':{'border':'border-08.png','opacity_video':'opacity-video-10.webm','corners':'corners-08.webm','tint':'tint-12.png'}},catalog=previous_catalog)
result=render_random_output(source=qa/'source.mp4',output=qa/'drama-previous.mp4',recipe=recipe,asset_root=root,manifest_sha256=sha,ffmpeg=os.environ['DRAMA_FFMPEG'],ffprobe=os.environ['DRAMA_FFPROBE'])
reports.append({'lane':'drama-previous','catalog':previous_sha,'result':result})
(qa/'drama-report.json').write_text(json.dumps(reports,indent=2))
print(json.dumps({'drama_new_combinations':12,'both_old_catalogs_rendered_with_new_default':True}),flush=True)
'''
env['QA_MAIN']=MAIN
if not (QA/'drama-report.json').exists():
    subprocess.run(['runuser','-u','drama-synthesis-gpu','--','/data/drama-synthesis-gpu/runtime/current/bin/python','-c',DRAMA],env=env,cwd=MAIN,check=True,timeout=600)
LANE=r'''
import json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0,os.environ['QA_CODE'])
if os.environ['QA_LANE']=='tt':
    from features.tt_gpu.random_overlay import load_asset_set,selected_asset_paths
else:
    from features.fb_gpu.random_overlay import load_asset_set,selected_asset_paths
qa=Path('/data/drama-synthesis-gpu/work/catalog-qa-20260920')
assets=load_asset_set(Path('/data/random-overlay-gpu/assets/catalog-b5df776a88bd'),'b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c')
names={'border':'border-20.png','opacity_video':'opacity-video-20.webm','corners':'corners-20.webm','tint':'tint-20.png'}
recipe={'version':1,'asset_set_sha256':assets['manifest_sha256'],'assets':{c:{key:next(item for item in assets['categories'][c] if item['name']==name)[key] for key in ['name','sha256','size','media_type']} for c,name in names.items()},'rotation_millidegrees':700,'scale_bp':10020,'tint_opacity_bp':1000}
paths=selected_asset_paths(recipe,assets)
lane=os.environ['QA_LANE'];output=qa/(lane+'.mp4')
if lane=='tt':
    from features.tt_gpu.worker import WorkerConfig,build_random_overlay_command,probe_media,inspect_input
    config=WorkerConfig.from_env();info=inspect_input(probe_media(config,qa/'source.mp4'),3600)
    command=build_random_overlay_command(config,qa/'source.mp4',output,info,5,recipe,paths)
    ffprobe=config.ffprobe_bin
else:
    from features.fb_gpu.prepare_worker import WorkerConfig,build_command,_probe
    config=WorkerConfig.from_env();info=_probe(config,qa/'source.mp4')
    command=build_command(config,qa/'source.mp4',output,info,recipe,paths)
    ffprobe=config.ffprobe
subprocess.run(command,check=True,timeout=300,stdout=subprocess.DEVNULL)
result=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(output)]))
video=next(s for s in result['streams'] if s['codec_type']=='video')
assert (video['width'],video['height'])==(720,1280)
assert video['codec_name']==('hevc' if lane=='tt' else 'h264')
assert abs(float(video['duration'])-5)<0.05
report={'lane':lane,'codec':video['codec_name'],'width':720,'height':1280,'seconds':float(video['duration']),'catalog':assets['manifest_sha256'],'no_upload_or_publish':True}
(qa/(lane+'-report.json')).write_text(json.dumps(report,indent=2))
print(json.dumps(report),flush=True)
'''
for lane,unit,code,python in [('tt','tt-gpu-publisher.service',TT,'/data/tt-post-gpu/runtime/bin/python'),('fb','fb-page-random-overlay-gpu.service','/opt/fb-page-random-overlay/current','/opt/fb-page-random-overlay/venv/bin/python')]:
    if (QA/(lane+'-report.json')).exists():
        continue
    lane_env=environment(unit);lane_env.update(QA_CODE=code,QA_LANE=lane)
    if lane=='tt':
        lane_env.update(TT_POST_GPU_RANDOM_OVERLAY_ROOT='/data/random-overlay-gpu/assets/catalog-b5df776a88bd',TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256=SHA,RANDOM_OVERLAY_ASSET_CATALOGS=json.dumps({OLD:'/data/tt-post-publisher/random-overlay-assets/v1',PREVIOUS:'/data/random-overlay-gpu/assets/catalog-24d6fad3174f',SHA:'/data/random-overlay-gpu/assets/catalog-b5df776a88bd'}))
    else:
        lane_env.update(FB_PAGE_GPU_RANDOM_OVERLAY_ROOT='/data/random-overlay-gpu/assets/catalog-b5df776a88bd',FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256=SHA)
    subprocess.run([python,'-c',LANE],env=lane_env,cwd=code,check=True,timeout=600)
print(json.dumps({'offline_gpu_qa':True,'path':str(QA)}),flush=True)
