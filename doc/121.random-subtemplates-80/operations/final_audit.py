import hashlib,json,os,subprocess,urllib.request
from pathlib import Path
SHA='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
PREVIOUS='24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c'
OLD='028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f'
def run(args):return subprocess.check_output(args,timeout=30).decode().strip()
def env_for(unit):
    pid=run(['systemctl','show',unit,'-p','MainPID','--value'])
    assert int(pid)>1
    return pid,dict(item.decode().split('=',1) for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in item)
hk=Path('/data/drama-synthesis-gpu/current').exists()
out={'time':run(['date','-Is']),'host':run(['hostname']),'services':{},'catalogs':{}}
if hk:
    specs=[('drama-synthesis-gpu-worker.service','DRAMA_RANDOM_OVERLAY_ROOT','DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256'),('tt-gpu-publisher.service','TT_POST_GPU_RANDOM_OVERLAY_ROOT','TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256'),('fb-page-random-overlay-gpu.service','FB_PAGE_GPU_RANDOM_OVERLAY_ROOT','FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256')]
    for unit,key,shakey in specs:
        pid,env=env_for(unit);root=Path(env[key]);raw=(root/'manifest.json').read_bytes()
        assert env[shakey]==SHA and hashlib.sha256(raw).hexdigest()==SHA
        manifest=json.loads(raw)
        counts={k:len(v) for k,v in manifest['categories'].items()}
        assert counts=={'border':20,'opacity_video':20,'corners':20,'tint':20,'light':2}
        out['services'][unit]=run(['systemctl','show',unit,'-p','ActiveState,MainPID,NRestarts'])
        out['catalogs'][unit]={'root':str(root),'sha256':SHA,'counts':counts}
        if unit!='fb-page-random-overlay-gpu.service':
            approved=json.loads(env['RANDOM_OVERLAY_ASSET_CATALOGS'])
            assert set(approved)=={OLD,PREVIOUS,SHA}
            assert hashlib.sha256((Path(approved[OLD])/'manifest.json').read_bytes()).hexdigest()==OLD
            assert hashlib.sha256((Path(approved[PREVIOUS])/'manifest.json').read_bytes()).hexdigest()==PREVIOUS
            out['catalogs'][unit]['legacy_manifest_unchanged']=True
    roots=['/data/random-overlay-gpu/asset-cache-v1','/data/drama-synthesis-gpu/assets/rgba-nut-demux-v2']
    out['cache_coverage']={}
    for directory in roots:
        total=0;covered=0
        for category in ['border','opacity_video','corners','tint']:
            for row in manifest['categories'][category]:
                p=Path(directory)/(row['sha256']+'.nut');rec=json.loads(p.with_suffix('.json').read_text())
                assert rec['source_sha256']==row['sha256'] and rec['rgba_frames_verified'] and rec['size']==p.stat().st_size
                if 'demux-v2' in directory:assert rec['timing_basis']=='demux'
                covered+=1;total+=rec['size']
        assert covered==80
        out['cache_coverage'][directory]={'entries':covered,'bytes':total}
    pid,env=env_for('drama-synthesis-gpu-worker.service')
    req=urllib.request.Request('http://127.0.0.1:8787/api/gpu-video/random-overlay/catalog',headers={'Authorization':'Bearer '+env['GPU_VIDEO_WORKER_TOKEN']})
    with urllib.request.urlopen(req,timeout=15) as response:catalog=json.load(response)['item']
    out['drama_live_http_catalog']={k:len(v) for k,v in catalog['categories'].items()}
    assert catalog['manifest_sha256']==SHA
    out['git']={p:run(['git','-C',p,'rev-parse','HEAD']) for p in ['/data/drama-synthesis-gpu/current','/data/tt-post-gpu/random-current']}
    out['fb_release']=str(Path('/opt/fb-page-random-overlay/current').resolve())
    backup=Path('/data/random-overlay-gpu/backups/20260920-subtemplates80')
else:
    for unit in ['drama-material-api.service','drama-material-job-worker.service']:
        pid,env=env_for(unit)
        assert env['DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256']==SHA
        manifest=json.loads(Path(env['DRAMA_RANDOM_OVERLAY_MANIFEST_FILE']).read_text())
        out['catalogs'][unit]={k:len(v) for k,v in manifest['categories'].items() if k!='light'}
        out['services'][unit]=run(['systemctl','show',unit,'-p','ActiveState,MainPID,NRestarts'])
    with urllib.request.urlopen('https://ai.yingliangads.com/api/ui/topbar',timeout=15) as response:out['public_topbar_status']=response.status
    control=Path('/mnt/data-disk/migrations/gpu-service-migration-20260828T1502/control')
    out['maintenance_records']={}
    for p in control.glob('*.json'):
        try:
            value=json.loads(p.read_text())
            if isinstance(value,dict) and value.get('group')=='tt' and 'restored' in value:
                assert value['restored']
                out['maintenance_records'][p.name]={'restored':value['restored'],'original':value['original'],'current':value.get('current')}
        except (ValueError,UnicodeError):pass
    backup=Path('/mnt/data-disk/random-overlay-gpu/backups/20260920-subtemplates80')
    pause=json.loads((backup/'fb-claim-resume.json').read_text());assert pause['resumed'] in (True,'process_already_exited','no_client_was_running')
    pid=pause['pid']
    if Path('/proc/%d/stat'%pid).exists():assert Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[0] not in ['T','t']
    out['fb_prepare_timer']=run(['systemctl','show','fb-auto-post-prepare.timer','-p','ActiveState','--value'])
    assert out['fb_prepare_timer']==json.loads((backup/'fb-prepare-timer.json').read_text())['active_state']
health={}
for port,path in ([(8787,'/healthz'),(8830,'/health'),(8836,'/health')] if hk else [(18788,'/healthz'),(18830,'/health'),(18836,'/health')]):
    with urllib.request.urlopen('http://127.0.0.1:%d%s'%(port,path),timeout=15) as response:health[port]=json.load(response)
assert health[8830 if hk else 18830]['random_overlay_asset_set_sha256']==SHA
out['health']=health
if hk:
    for unit in ['drama-synthesis-gpu-tunnel.service','tt-gpu-reverse-tunnel.service','fb-page-random-overlay-tunnel.service']:
        assert run(['systemctl','is-active',unit])=='active'
        out['services'][unit]=run(['systemctl','show',unit,'-p','ActiveState,MainPID,NRestarts'])
else:
    assert 'tt' not in json.loads((control/'gates.json').read_text()).get('groups',[])
    for counts in out['catalogs'].values():assert counts=={'border':20,'opacity_video':20,'corners':20,'tint':20}
out['ok']=True
(backup/'final-verification.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps(out,ensure_ascii=False,indent=2))
