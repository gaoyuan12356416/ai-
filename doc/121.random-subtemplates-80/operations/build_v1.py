import json,os,subprocess
from pathlib import Path
SHA='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
ROOT='/data/random-overlay-gpu/assets/catalog-'+SHA[:12]
command=['nice','-n','10','/data/tt-post-gpu/runtime/bin/python','/data/tt-post-gpu/random-current/scripts/build_random_gpu_asset_cache.py','--ffmpeg','/data/drama-synthesis-gpu/runtime/bin/ffmpeg','--root','/data/random-overlay-gpu/asset-cache-v1','--assets',ROOT]
subprocess.run(command,check=True,timeout=2400)
print(json.dumps({'cache_v1_completed':True,'catalog':SHA}),flush=True)
