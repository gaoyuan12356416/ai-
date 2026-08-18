"""Loopback prepare-only random-overlay worker.

Only GET /health and POST /internal/fb-page-media/prepare exist.  The module has
no credential envelope, TikTok API, publish or reconcile dependency.
"""
from __future__ import annotations
import hashlib, hmac, ipaddress, json, math, os, re, shutil, subprocess, threading, time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit
import requests
from .random_overlay import derive_recipe, load_asset_set, selected_asset_paths, sha256_file

PROFILE="tt-post-random-overlay-h264-720x1280-v3"
PREPARE_PATH="/internal/fb-page-media/prepare"; HEALTH_PATH="/health"
JOB_RE=re.compile(r"fb-page-[a-f0-9]{48}"); SHA_RE=re.compile(r"[a-f0-9]{64}")

class PrepareWorkerError(RuntimeError):
    def __init__(self,code,message,status=400): self.code,self.status=code,status; super().__init__(message)

def _absolute(value,name):
    path=Path(str(value or ""))
    if not path.is_absolute(): raise PrepareWorkerError("invalid_configuration",name+" must be absolute",500)
    return path
def _origin(value):
    parsed=urlsplit(str(value or "").rstrip("/"))
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("","/") or parsed.query or parsed.fragment: raise PrepareWorkerError("invalid_configuration","public origin invalid",500)
    return "https://"+parsed.hostname.lower()

@dataclass(frozen=True)
class WorkerConfig:
    host:str; port:int; token:str; work_root:Path; asset_root:Path; asset_manifest_sha256:str; allowed_source_hosts:tuple[str,...]; cos_secret_id:str; cos_secret_key:str; cos_bucket:str; cos_region:str; cos_domain:str; cos_prefix:str; ffmpeg:str; ffprobe:str; max_source_bytes:int=2*1024*1024*1024; timeout:int=9000; failed_job_retention_seconds:int=172800; cleanup_max_jobs:int=100; cos_timeout_seconds:int=120
    @classmethod
    def from_env(cls,env=None):
        source=os.environ if env is None else env; host=str(source.get("FB_PAGE_GPU_HOST","127.0.0.1")); port=int(source.get("FB_PAGE_GPU_PORT","8836")); token=str(source.get("FB_PAGE_GPU_INTERNAL_TOKEN", ""))
        try:
            if not ipaddress.ip_address(host).is_loopback: raise ValueError
        except ValueError: raise PrepareWorkerError("invalid_configuration","GPU host must be loopback",500) from None
        if port!=8836 or not re.fullmatch(r"[A-Za-z0-9._~-]{32,512}",token): raise PrepareWorkerError("invalid_configuration","GPU port or token invalid",500)
        encoder=str(source.get("FB_PAGE_GPU_VIDEO_ENCODER","h264_nvenc")); profile=str(source.get("FB_PAGE_GPU_PROFILE",PROFILE)); prepare_only=str(source.get("FB_PAGE_GPU_PREPARE_ONLY","1"))
        if encoder!="h264_nvenc" or profile!=PROFILE or prepare_only!="1": raise PrepareWorkerError("invalid_configuration","GPU H264 prepare-only profile mismatch",500)
        hosts=tuple(dict.fromkeys(item.strip().lower() for item in str(source.get("FB_PAGE_GPU_ALLOWED_SOURCE_HOSTS","")).split(",") if item.strip()))
        if not hosts or any(not re.fullmatch(r"[a-z0-9.-]+",item) for item in hosts): raise PrepareWorkerError("invalid_configuration","allowed source hosts required",500)
        manifest=str(source.get("FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256","")).lower()
        if not SHA_RE.fullmatch(manifest): raise PrepareWorkerError("invalid_configuration","asset manifest fingerprint invalid",500)
        secret_id,secret_key,bucket,region=str(source.get("FB_PAGE_GPU_COS_SECRET_ID", "")),str(source.get("FB_PAGE_GPU_COS_SECRET_KEY", "")),str(source.get("FB_PAGE_GPU_COS_BUCKET", "")),str(source.get("FB_PAGE_GPU_COS_REGION", ""))
        domain=_origin(source.get("FB_PAGE_GPU_COS_DOMAIN")); prefix=str(source.get("FB_PAGE_GPU_COS_PREFIX","fb-page-random-overlay-h264-v3")).strip("/")
        if not secret_id or not secret_key or not re.fullmatch(r"[A-Za-z0-9._-]{3,128}",bucket) or not re.fullmatch(r"[a-z0-9-]{3,64}",region) or prefix!="fb-page-random-overlay-h264-v3": raise PrepareWorkerError("invalid_configuration","COS configuration invalid",500)
        retention=int(source.get("FB_PAGE_GPU_FAILED_JOB_RETENTION_SECONDS","172800")); cleanup_max=int(source.get("FB_PAGE_GPU_CLEANUP_MAX_JOBS","100")); cos_timeout=int(source.get("FB_PAGE_GPU_COS_TIMEOUT","120"))
        if not 86400<=retention<=604800 or not 1<=cleanup_max<=1000 or not 30<=cos_timeout<=600: raise PrepareWorkerError("invalid_configuration","cleanup or COS timeout policy invalid",500)
        return cls(host,port,token,_absolute(source.get("FB_PAGE_GPU_WORK_ROOT"),"work root"),_absolute(source.get("FB_PAGE_GPU_RANDOM_OVERLAY_ROOT"),"asset root"),manifest,hosts,secret_id,secret_key,bucket,region,domain,prefix,str(source.get("FB_PAGE_GPU_FFMPEG_BIN","/usr/bin/ffmpeg")),str(source.get("FB_PAGE_GPU_FFPROBE_BIN","/usr/bin/ffprobe")),int(source.get("FB_PAGE_GPU_MAX_SOURCE_BYTES",str(2*1024*1024*1024))),int(source.get("FB_PAGE_GPU_PREPARE_TIMEOUT","9000")),retention,cleanup_max,cos_timeout)

def cleanup_stale_failed_jobs(config,*,now_fn=time.time):
    jobs=(config.work_root/"jobs").resolve(); jobs.mkdir(mode=0o700,parents=True,exist_ok=True); removed=0; now=float(now_fn())
    for child in sorted(jobs.iterdir(),key=lambda item:item.name):
        if removed>=config.cleanup_max_jobs: break
        if child.is_symlink() or not child.is_dir() or not JOB_RE.fullmatch(child.name) or (child/"manifest.json").is_file(): continue
        try:
            resolved=child.resolve(strict=True)
            if resolved.parent!=jobs: continue
            newest=max((item.stat().st_mtime for item in child.rglob("*") if item.is_file() and not item.is_symlink()),default=child.stat().st_mtime)
            if now-newest<config.failed_job_retention_seconds: continue
            shutil.rmtree(resolved); removed+=1
        except (FileNotFoundError,OSError): continue
    return removed

def validate_request(value,config):
    if not isinstance(value,Mapping) or set(value)!={"job_id","content_id","source_url","source_trim_tail_seconds","video_template","expected_profile"}: raise PrepareWorkerError("invalid_request","prepare fields invalid")
    job=str(value.get("job_id") or ""); content=str(value.get("content_id") or ""); source=str(value.get("source_url") or "")
    parsed=urlsplit(source)
    if not JOB_RE.fullmatch(job) or not 1<=len(content)<=128 or value.get("video_template")!="random_overlay" or value.get("expected_profile")!=PROFILE or value.get("source_trim_tail_seconds") not in (0,0.0): raise PrepareWorkerError("invalid_request","prepare identity/profile/trim invalid")
    if parsed.scheme!="https" or not parsed.hostname or parsed.hostname.lower() not in config.allowed_source_hosts or parsed.username or parsed.password or parsed.fragment: raise PrepareWorkerError("invalid_request","source URL host invalid")
    return {"job_id":job,"content_id":content,"source_url":source}

def _probe(config,path):
    try: result=subprocess.run([config.ffprobe,"-v","error","-show_streams","-show_format","-of","json",str(path)],capture_output=True,text=True,timeout=120,check=True); payload=json.loads(result.stdout)
    except Exception: raise PrepareWorkerError("fb_gpu_probe_failed","media probe failed",502) from None
    streams=payload.get("streams") if isinstance(payload,dict) else []; video=next((item for item in streams if item.get("codec_type")=="video"),{}); audio=next((item for item in streams if item.get("codec_type")=="audio"),None)
    try: duration=float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
    except Exception: duration=0
    if not math.isfinite(duration) or duration<=0: raise PrepareWorkerError("fb_gpu_probe_failed","media duration invalid",502)
    return {"duration":duration,"has_audio":audio is not None,"video":video,"audio":audio}

def build_command(config,source,output,info,recipe,assets):
    rotation=int(recipe["rotation_millidegrees"])/1000; scale=int(recipe["scale_bp"])/10000; opacity=int(recipe["tint_opacity_bp"])/10000
    command=[config.ffmpeg,"-y","-nostdin","-hide_banner","-loglevel","error","-i",str(source),"-loop","1","-i",str(assets["border"]),"-stream_loop","-1","-c:v","libvpx-vp9","-i",str(assets["opacity_video"]),"-stream_loop","-1","-c:v","libvpx-vp9","-i",str(assets["corners"]),"-loop","1","-i",str(assets["tint"])]
    if not info["has_audio"]: command += ["-f","lavfi","-i","anullsrc=channel_layout=stereo:sample_rate=48000"]
    audio="0:a:0" if info["has_audio"] else "5:a:0"
    graph=("[0:v]setpts=PTS-STARTPTS,fps=30,split=2[backraw][mainraw];[backraw]scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,crop=720:1280,setsar=1,format=rgba[back];[mainraw]scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos,pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1,format=rgba,scale=w='trunc(iw*%.4f/2)*2':h='trunc(ih*%.4f/2)*2':flags=lanczos,rotate=%.6f*PI/180:ow=rotw(iw):oh=roth(ih):c=black@0[main];[back][main]overlay=(W-w)/2:(H-h)/2:shortest=1:eof_action=repeat[base];[4:v]scale=720:1280:flags=lanczos,format=rgba,colorchannelmixer=aa=%.4f,fps=30,setpts=PTS-STARTPTS[tint];[2:v]scale=720:1280:flags=lanczos,format=rgba,fps=30,setpts=PTS-STARTPTS[opacity];[1:v]scale=720:1280:flags=lanczos,format=rgba,fps=30,setpts=PTS-STARTPTS[border];[3:v]scale=720:1280:flags=lanczos,format=rgba,fps=30,setpts=PTS-STARTPTS[corners];[base][tint]overlay=0:0:shortest=1:eof_action=repeat[o1];[o1][opacity]overlay=0:0:shortest=1:eof_action=repeat[o2];[o2][border]overlay=0:0:shortest=1:eof_action=repeat[o3];[o3][corners]overlay=0:0:shortest=1:eof_action=repeat,format=yuv420p[v]")%(scale,scale,rotation,opacity)
    return command+["-filter_complex",graph,"-map","[v]","-map",audio,"-af","aresample=48000:async=1:first_pts=0,apad","-shortest","-c:v","h264_nvenc","-profile:v","high","-preset","p5","-rc","vbr","-cq","21","-b:v","0","-pix_fmt","yuv420p","-fps_mode","cfr","-g","60","-keyint_min","60","-c:a","aac","-profile:a","aac_low","-ar","48000","-ac","2","-b:a","192k","-movflags","+faststart","-t","%.6f"%info["duration"],str(output)]

class CosObjectStore:
    def __init__(self,config,client=None):
        self.config=config
        if client is None:
            try:
                from qcloud_cos import CosConfig,CosS3Client
                client=CosS3Client(CosConfig(Region=config.cos_region,SecretId=config.cos_secret_id,SecretKey=config.cos_secret_key,Scheme="https",Timeout=config.cos_timeout_seconds,KeepAlive=False),retry=0)
            except Exception: raise PrepareWorkerError("invalid_configuration","COS SDK initialization failed",500) from None
        self.client=client
    @staticmethod
    def _status(exc):
        for name in ("get_status_code","status_code","status"):
            value=getattr(exc,name,None)
            try: return int(value() if callable(value) else value)
            except (TypeError,ValueError): pass
        return 0
    @staticmethod
    def _head_matches(head,size,sha):
        lowered={str(key).lower():value for key,value in (head or {}).items()}
        try: remote_size=int(lowered.get("content-length",lowered.get("content_length",-1)))
        except (TypeError,ValueError): return False
        remote_sha=str(lowered.get("x-cos-meta-sha256",lowered.get("sha256",""))).lower()
        return remote_size==int(size) and hmac.compare_digest(remote_sha,sha)
    def _head(self,key):
        try:
            return self.client.head_object(Bucket=self.config.cos_bucket,Key=key)
        except Exception as exc:
            if self._status(exc)==404: return None
            raise PrepareWorkerError("fb_gpu_cos_head_failed","COS object verification failed",502) from None
    def upload(self,path,job_id,sha256,size):
        del job_id
        key=f"{self.config.cos_prefix}/{sha256[:2]}/{sha256}.mp4"
        head=self._head(key)
        if head is not None:
            if not self._head_matches(head,size,sha256): raise PrepareWorkerError("fb_gpu_cos_identity_conflict","COS object identity conflict",409)
            reused=True
        else:
            try:
                with Path(path).open("rb") as body:
                    self.client.put_object(Bucket=self.config.cos_bucket,Key=key,Body=body,ACL="public-read",ContentType="video/mp4",Metadata={"x-cos-meta-sha256":sha256,"x-cos-meta-profile":PROFILE})
            except PrepareWorkerError: raise
            except Exception: raise PrepareWorkerError("fb_gpu_cos_upload_failed","COS upload failed",502) from None
            head=self._head(key)
            if head is None or not self._head_matches(head,size,sha256): raise PrepareWorkerError("fb_gpu_cos_upload_invalid","COS upload verification mismatch",502)
            reused=False
        url=self.config.cos_domain+"/"+"/".join(quote(part,safe="-._~") for part in key.split("/"))
        parsed=urlsplit(url)
        if parsed.scheme!="https" or parsed.hostname!=urlsplit(self.config.cos_domain).hostname: raise PrepareWorkerError("fb_gpu_cos_url_invalid","COS public URL invalid",500)
        return {"url":url,"key":key,"reused":reused}

class PrepareProcessor:
    def __init__(self,config,*,session_factory=requests.Session,runner=subprocess.run,object_store=None):
        self.config=config; self.assets=load_asset_set(config.asset_root,config.asset_manifest_sha256); self.session_factory=session_factory; self.runner=runner; self.object_store=object_store or CosObjectStore(config); self.lock=threading.Lock(); (config.work_root/"jobs").mkdir(parents=True,exist_ok=True); cleanup_stale_failed_jobs(config); self.last_cleanup_at=time.monotonic()
    def _download(self,url,path):
        session=self.session_factory(); session.trust_env=False
        try:
            response=session.get(url,stream=True,timeout=120,allow_redirects=False)
            if response.status_code!=200: raise PrepareWorkerError("fb_gpu_source_download_failed","source download failed",502)
            size=0; digest=hashlib.sha256()
            with path.open("xb") as handle:
                for chunk in response.iter_content(1024*1024):
                    size+=len(chunk)
                    if size>self.config.max_source_bytes: raise PrepareWorkerError("fb_gpu_source_too_large","source too large",413)
                    digest.update(chunk); handle.write(chunk)
            return digest.hexdigest()
        finally: session.close()
    def prepare(self,raw):
        request=validate_request(raw,self.config); job=request["job_id"]; root=self.config.work_root/"jobs"/job; manifest=root/"manifest.json"
        with self.lock:
            if time.monotonic()-getattr(self,"last_cleanup_at",0)>=3600:
                cleanup_stale_failed_jobs(self.config); self.last_cleanup_at=time.monotonic()
            if manifest.is_file():
                stored=json.loads(manifest.read_text(encoding="utf-8"))
                if stored.get("request")!=request: raise PrepareWorkerError("fb_gpu_job_conflict","job identity conflict",409)
                return {**stored["result"],"reused":True}
            root.mkdir(mode=0o700,parents=True,exist_ok=True); source=root/"source.mp4"; download_tmp=root/"source.download.tmp"
            if source.is_file(): source_sha,_=sha256_file(source)
            else:
                try: source_sha=self._download(request["source_url"],download_tmp); os.replace(download_tmp,source)
                except Exception:
                    download_tmp.unlink(missing_ok=True); raise
            source_info=_probe(self.config,source)
            recipe=derive_recipe(job_id=job,content_id=request["content_id"],profile=PROFILE,source_url_sha256=hashlib.sha256(request["source_url"].encode()).hexdigest(),asset_set=self.assets); output=root/"output.tmp.mp4"; output.unlink(missing_ok=True)
            try:
                try: self.runner(build_command(self.config,source,output,source_info,recipe,selected_asset_paths(recipe,self.assets)),capture_output=True,text=True,timeout=self.config.timeout,check=True)
                except Exception: raise PrepareWorkerError("fb_gpu_transcode_failed","random-overlay transcode failed",502) from None
                output_info=_probe(self.config,output); video=output_info["video"]
                if video.get("codec_name")!="h264" or str(video.get("profile") or "").lower()!="high" or int(video.get("width") or 0)!=720 or int(video.get("height") or 0)!=1280: raise PrepareWorkerError("fb_gpu_output_contract_invalid","H264 output contract invalid",502)
                output_sha,size=sha256_file(output); stored=self.object_store.upload(output,job,output_sha,size)
                result={"job_id":job,"content_id":request["content_id"],"profile":PROFILE,"output_url":stored["url"],"output_sha256":output_sha,"output_size":size,"probe":{"duration":output_info["duration"]},"random_overlay_recipe":recipe,"storage_key":stored["key"],"status":"ready"}
                tmp=manifest.with_suffix(".tmp"); tmp.write_text(json.dumps({"request":request,"source_sha256":source_sha,"result":result},sort_keys=True,separators=(",",":")),encoding="utf-8"); os.replace(tmp,manifest); source.unlink(missing_ok=True); return {**result,"reused":False}
            finally: output.unlink(missing_ok=True)

class Server(ThreadingHTTPServer):
    def __init__(self,address,processor,token): super().__init__(address,Handler); self.processor,self.token=processor,token
class Handler(BaseHTTPRequestHandler):
    server_version="FBPrepareGPU/1"
    def log_message(self,*_args): pass
    def _json(self,status,payload):
        raw=json.dumps(payload,separators=(",",":")).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        try: loopback=ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError: loopback=False
        if not loopback: self._json(404,{"ok":False,"code":"not_found"}); return
        if self.path==HEALTH_PATH: self._json(200,{"ok":True,"service":"fb-page-prepare-gpu","profile":PROFILE,"prepare_only":True})
        else: self._json(404,{"ok":False,"code":"not_found"})
    def do_POST(self):
        try: loopback=ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError: loopback=False
        if not loopback: self._json(404,{"ok":False,"code":"not_found"}); return
        if self.path!=PREPARE_PATH: self._json(404,{"ok":False,"code":"not_found"}); return
        if not hmac.compare_digest(str(self.headers.get("Authorization") or ""),"Bearer "+self.server.token): self._json(403,{"ok":False,"code":"forbidden"}); return
        if self.headers.get("Transfer-Encoding") or str(self.headers.get("Content-Type") or "").split(";",1)[0].strip().lower()!="application/json": self._json(400,{"ok":False,"code":"invalid_request"}); return
        try:
            length=int(self.headers.get("Content-Length",""));
            if not 0<length<=65536: raise PrepareWorkerError("invalid_request","body length invalid")
            raw=json.loads(self.rfile.read(length).decode()); result=self.server.processor.prepare(raw); self._json(200,{"ok":True,"item":result})
        except PrepareWorkerError as exc: self._json(exc.status,{"ok":False,"code":exc.code,"message":str(exc)})
        except Exception: self._json(500,{"ok":False,"code":"fb_gpu_internal_error","message":"prepare worker internal error"})
def serve(env=None):
    config=WorkerConfig.from_env(env); processor=PrepareProcessor(config); Server((config.host,config.port),processor,config.token).serve_forever()

__all__=["PROFILE","Handler","PrepareProcessor","PrepareWorkerError","Server","WorkerConfig","build_command","cleanup_stale_failed_jobs","serve","validate_request"]
