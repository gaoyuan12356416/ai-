import json,os,subprocess
from pathlib import Path
SHA='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
pid=subprocess.check_output(['systemctl','show','drama-synthesis-gpu-worker.service','-p','MainPID','--value']).decode().strip()
env=dict(item.decode().split('=',1) for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in item)
command=['nice','-n','10','/data/drama-synthesis-gpu/runtime/current/bin/python','/data/drama-synthesis-gpu/current/scripts/build_drama_gpu_asset_cache.py','--ffmpeg','/data/drama-synthesis-gpu/runtime/bin/ffmpeg','--root','/data/drama-synthesis-gpu/assets/rgba-nut-demux-v2','--seed-root','/data/random-overlay-gpu/asset-cache-v1','--assets','/data/drama-synthesis-gpu/assets/catalog-'+SHA[:12],'--manifest-sha256',SHA]
subprocess.run(command,env=env,check=True,timeout=2400)
print(json.dumps({'cache_v2_completed':True,'catalog':SHA}),flush=True)
