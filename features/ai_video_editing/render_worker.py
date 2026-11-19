"""Single-consumer portable FFmpeg worker for the independent AI video editor."""
import argparse
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


LOG = logging.getLogger('ai_video_editing.render')
ID = re.compile(r'[a-zA-Z0-9_-]{1,128}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
FPS = 30


class RenderError(Exception):
    pass


class LeaseLost(Exception):
    pass


class TransferError(Exception):
    pass


class Rejected(RenderError):
    def __init__(self, status):
        super().__init__('内部接口拒绝请求 (%d)' % status)
        self.status = status


def require(ok, msg):
    if not ok:
        raise RenderError(msg)


def frame(ms):
    return (ms * FPS + 500) // 1000


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run_tool(args, stopped=None, timeout=3600):
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as log:
        with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log) as proc:
            while True:
                try:
                    out, _ = proc.communicate(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if (stopped and stopped.is_set()) or time.monotonic() >= deadline:
                        proc.kill()
                        proc.communicate()
                        if stopped and stopped.is_set():
                            raise LeaseLost()
                        raise RenderError('媒体处理超时')
            if proc.returncode:
                # Keep driver and codec diagnostics local; never expose input paths.
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 1500))
                LOG.warning('FFmpeg/FFprobe returned %s: %s', proc.returncode,
                            log.read().decode('utf8', 'replace').strip())
                raise RenderError('媒体解码或编码失败')
            return out


def inspect(path, ffprobe, stopped=None):
    output = run_tool([ffprobe, '-v', 'error', '-protocol_whitelist', 'file', '-f', 'mov',
                       '-show_streams', '-show_format', '-of', 'json', str(path)], stopped, timeout=90)
    try:
        probe = json.loads(output)
        streams = probe['streams']
        v = next(s for s in streams if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'))
        a = next((s for s in streams if s['codec_type'] == 'audio'), None)
        duration = float(v.get('duration') or probe['format']['duration'])
        require(math.isfinite(duration) and 0 < duration <= 86400 * 2 and min(int(v['width']), int(v['height'])) >= 1,
                '视频元数据无效')
        return v, a, round(duration * 1000)
    except (StopIteration, KeyError, ValueError, TypeError):
        raise RenderError('视频缺少有效画面') from None


def encode_args(encoder):
    if encoder == 'h264_nvenc':
        return ['-c:v', 'h264_nvenc', '-preset', 'fast', '-rc', 'vbr', '-cq', '21',
                '-b:v', '3M', '-maxrate', '6M', '-bufsize', '9M']
    require(encoder == 'libx264', '不支持的编码器')
    return ['-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20']


def protected_mirror(source, output, protection, encoder, ffmpeg, stopped):
    import sys
    from .subtitle_protection import settings
    p = settings(protection)
    executable = os.getenv('AI_VIDEO_EDITING_MIRROR_PYTHON', sys.executable if os.name == 'nt' else '/data/ai-video-editing/render-venv/bin/python')
    require(Path(executable).is_file(), '字幕保护处理器尚未配置')
    run_tool([executable, str(Path(__file__).with_name('protected_mirror.py')), str(source), str(output),
              str(p['top']), str(p['bottom']), encoder, ffmpeg], stopped)


def finish(composition, sources, output, workdir, frames, encoder, ffmpeg, stopped):
    from .composition import normalize, write_ass
    duration=round(frames*1000/FPS)
    c=normalize(composition,duration)
    base=workdir/'assembled.mp4'
    os.replace(output,base)
    if 'mirror' in c['effects'] and c['subtitle_protection']['enabled']:
        mirrored=workdir/'subtitle-protected.mp4'
        protected_mirror(base,mirrored,c['subtitle_protection'],encoder,ffmpeg,stopped)
        base=mirrored
    subtitle=workdir/'captions.ass'
    write_ass(c,subtitle,duration)
    filters=[]
    for effect in c['effects']:
        if effect == 'mirror' and c['subtitle_protection']['enabled']:
            continue
        filters.extend({'basic':['eq=contrast=1.025:saturation=1.035:brightness=0.003','unsharp=3:3:0.25'],
                        'mirror':['hflip'],'zoom':['scale=750:1334','crop=720:1280'],
                        'tilt':['rotate=0.012:fillcolor=black','scale=740:1316','crop=720:1280'],
                        'border':['drawbox=x=0:y=0:w=iw:h=ih:color=0x294a74:t=8'],
                        'vignette':['vignette=PI/7'], 'ghost':['tblend=all_mode=average'],
                        'frame_drop':['fps=24','fps=30'], 'picture_in_picture':[]}[effect])
    # All paths are generated by this service, text lives in an escaped ASS file.
    escaped=str(subtitle).replace('\\','/').replace(':',r'\:').replace("'",r"\'")
    filters.extend(["subtitles=filename='%s'"%escaped,'tpad=stop_mode=clone:stop_duration=1'])
    graph='[0:v]'+','.join(filters)+'[styled]'
    if 'picture_in_picture' in c['effects']:
        graph+=';[styled]split[main][pip];[pip]scale=144:256[small];[main][small]overlay=W-w-20:20[v]'
    else:graph+=';[styled]null[v]'
    args=[ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y','-filter_complex_threads','1','-i',str(base)]
    if c['speech_id']:
        require(c['speech_id'] in sources,'缺少配音音轨')
        args+=['-i',str(sources[c['speech_id']])]
        graph+=';[0:a]volume=%s[a0];[1:a]aresample=48000,apad[a1];[a0][a1]amix=inputs=2:duration=first:normalize=0[a]'%c['original_volume']
    else:graph+=';[0:a]volume=%s[a]'%c['original_volume']
    args+=['-filter_complex',graph,'-map','[v]','-map','[a]',*encode_args(encoder),'-r','30','-frames:v',str(frames),
           '-t','%.6f'%(frames/FPS),'-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-b:a','128k','-movflags','+faststart',str(output)]
    run_tool(args,stopped)


def render(unit, sources, output, workdir, *, encoder='h264_nvenc', ffmpeg='ffmpeg', ffprobe='ffprobe', stopped=None):
    """Render a frozen local unit; source_id maps to verified original file paths."""
    clips = unit.get('clips') if isinstance(unit, dict) else None
    require(isinstance(clips, list) and 1 <= len(clips) <= 300, '时间线片段数量无效')
    workdir = Path(workdir)
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = {}
    total_frames = 0
    manifest = []
    for idx, clip in enumerate(clips):
        if stopped and stopped.is_set():
            raise LeaseLost()
        require(isinstance(clip, dict), '片段格式错误')
        sid = clip.get('source_id')
        require(isinstance(sid, str) and sid in sources, '时间线引用未知素材')
        if sid not in meta:
            meta[sid] = inspect(sources[sid], ffprobe, stopped)
        v, audio, duration = meta[sid]
        start, end = clip.get('start_ms'), clip.get('end_ms')
        require(type(start) is int and type(end) is int and 0 <= start < end <= duration
                and frame(start) < frame(end), '片段时间码超出原片')
        fit, muted, mirror = clip.get('fit', 'contain'), clip.get('mute', False), clip.get('mirror', False)
        zoom = clip.get('zoom_percent', 100)
        require(fit in ('contain', 'cover') and type(muted) is bool and type(mirror) is bool
                and clip.get('transition', 'cut') == 'cut' and type(zoom) is int and 100 <= zoom <= 130, '片段样式无效')
        first, last = frame(start), frame(end)
        count = last - first
        # Seek to an integral second before the cut. All relative video and
        # audio trims share the same global 30 Hz frame boundaries.
        seek = max(0, first // FPS - 2)
        left, right = first - seek * FPS, last - seek * FPS
        window = ['-ss', str(seek), '-t', '%.6f' % ((right + FPS) / FPS)] if seek else ['-t', '%.6f' % ((right + FPS) / FPS)]
        args = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '2',
                '-filter_complex_threads', '1', *window, '-protocol_whitelist', 'file', '-f', 'mov', '-i', str(sources[sid])]
        # First normalize the pixel aspect ratio; resolution changes must retain
        # the source's display aspect ratio for both contain and cover.
        geometry = ('scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                    'pad=720:1280:(ow-iw)/2:(oh-ih)/2' if fit == 'contain' else
                    'scale=720:1280:force_original_aspect_ratio=increase:force_divisible_by=2,crop=720:1280')
        view = [geometry]
        from .subtitle_protection import settings as protection_settings
        protection=protection_settings(unit.get('composition',{}).get('subtitle_protection'))
        if mirror and not protection['enabled']:
            view.append('hflip')
        if zoom > 100:
            # Scale the completed canvas around its center; nearest even
            # dimensions preserve chroma alignment for the final 4:2:0 crop.
            width = 2 * ((720 * zoom + 100) // 200)
            height = 2 * ((1280 * zoom + 100) // 200)
            view.extend(['scale=%d:%d' % (width, height), 'crop=720:1280:(iw-ow)/2:(ih-oh)/2'])
        video = ('[0:%s]fps=30:start_time=0,tpad=stop_mode=clone:stop_duration=1,'
                 'trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS,'
                 'scale=trunc(iw*sar/2)*2:ih,setsar=1,%s,setsar=1,format=yuv420p[v]'
                 % (v['index'], left, right, ','.join(view)))
        sample_count = count * 1600
        if audio and not muted:
            sound = ('[0:%s]aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,'
                     'atrim=start_sample=%d:end_sample=%d,asetpts=PTS-STARTPTS,apad,atrim=end_sample=%d[a]'
                     % (audio['index'], left * 1600, right * 1600, sample_count))
        else:
            sound = 'anullsrc=r=48000:cl=stereo,atrim=end_sample=%d[a]' % sample_count
        name = 'segment_%03d.mov' % idx
        args += ['-filter_complex', video + ';' + sound, '-map', '[v]', '-map', '[a]', *encode_args(encoder),
                 '-pix_fmt', 'yuv420p', '-r', '30', '-video_track_timescale', '90000',
                 '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2', '-map_metadata', '-1', str(workdir / name)]
        run_tool(args, stopped)
        if mirror and protection['enabled']:
            protected=workdir/('protected_'+name)
            protected_mirror(workdir/name,protected,protection,encoder,ffmpeg,stopped)
            os.replace(protected,workdir/name)
        manifest.extend(["file '%s'" % name, 'duration %.9f' % (count / FPS)])
        total_frames += count
    concat = workdir / 'concat.txt'
    concat.write_text('\n'.join(manifest) + '\n', encoding='ascii')
    run_tool([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-protocol_whitelist', 'file',
              '-f', 'concat', '-safe', '1', '-i', str(concat), '-map', '0:v:0', '-map', '0:a:0',
              '-c:v', 'copy', '-video_track_timescale', '90000', '-c:a', 'aac', '-ar', '48000', '-ac', '2',
              '-b:a', '128k', '-movflags', '+faststart', '-map_metadata', '-1', str(output)], stopped)
    if unit.get('composition'):
        finish(unit['composition'],sources,output,workdir,total_frames,encoder,ffmpeg,stopped)
    v, a, duration = inspect(output, ffprobe, stopped)
    require((v['codec_name'], v['width'], v['height'], v['pix_fmt']) == ('h264', 720, 1280, 'yuv420p')
            and int(v.get('nb_frames') or 0) == total_frames and a and a['codec_name'] == 'aac'
            and a.get('channels') == 2 and int(a.get('sample_rate') or 0) == 48000
            and abs(duration - total_frames * 1000 / FPS) < 40,
            '成片的尺寸、编码、总帧数或音轨校验失败')
    return {'frames': total_frames, 'duration_ms': duration, 'sha256': fingerprint(output)}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class InternalAPI:
    def __init__(self, base, token):
        parts = urlsplit(base)
        require((parts.scheme == 'https' or parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1'))
                and parts.hostname and not parts.username and not parts.password and not parts.query and not parts.fragment,
                '内部任务地址无效')
        self.base = base.rstrip('/')
        self.token = token.strip()
        require(self.token, '缺少专用 worker 凭证')

    def open(self, path, *, lease='', data=None, headers=None, timeout=90):
        h = {'Authorization': 'Bearer ' + self.token, 'Accept-Encoding': 'identity', **(headers or {})}
        if lease:
            h['X-Edit-Lease'] = lease
        request = Request(self.base + path, data=data, headers=h, method='GET' if data is None else 'POST')
        try:
            return build_opener(NoRedirects()).open(request, timeout=timeout)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 409:
                raise LeaseLost() from None
            if 400 <= status < 500 and status != 429:
                raise Rejected(status) from None
            raise TransferError('内部接口暂不可用') from None
        except (URLError, OSError):
            raise TransferError('内部网络暂不可用') from None

    def post(self, path, lease=''):
        with self.open(path, lease=lease, data=b'{}', headers={'Content-Type': 'application/json'}, timeout=20) as resp:
            return json.loads(resp.read(1024 * 1024))

    def download(self, tid, lease, sid, path, stopped, kind='sources'):
        with self.open('/tasks/%s/%s/%s' % (tid, kind, sid), lease=lease) as resp:
            expected = resp.headers.get('X-Source-Sha256', '').lower()
            size = int(resp.headers.get('Content-Length', '0'))
            require(SHA.fullmatch(expected) and 0 < size <= 1024**3, '素材缺少可信版本或大小')
            require(shutil.disk_usage(path.parent).free > size + 128 * 1024**2, '工作盘空间不足')
            digest, actual = hashlib.sha256(), 0
            with path.open('wb') as out:
                while True:
                    if stopped.is_set():
                        raise LeaseLost()
                    block = resp.read(1024 * 1024)
                    if not block:
                        break
                    actual += len(block)
                    require(actual <= size, '素材字节数与回执不一致')
                    digest.update(block)
                    out.write(block)
            require(actual == size and digest.hexdigest() == expected, '素材内容与回执不一致')

    def complete(self, tid, lease, path, digest):
        with path.open('rb') as body:
            headers = {'Content-Type': 'video/mp4', 'Content-Length': str(path.stat().st_size),
                       'X-Output-Sha256': digest}
            with self.open('/tasks/%s/complete' % tid, data=body, headers=headers, lease=lease, timeout=300) as response:
                result = json.loads(response.read(65536))
                if not isinstance(result, dict) or not result.get('ok'):
                    raise TransferError('完成请求暂未取得回执')

    def completed(self, tid, lease, digest):
        with self.open('/tasks/%s/receipt' % tid, lease=lease,
                       headers={'X-Output-Sha256': digest}, timeout=20) as resp:
            row = json.loads(resp.read(65536))
        if not isinstance(row, dict) or type(row.get('completed')) is not bool:
            raise TransferError('暂未取得有效的查单回执')
        return row['completed']


def upload_saved(api, folder, reconcile_first=False):
    pending = json.loads((folder / 'pending.json').read_text(encoding='utf8'))
    require(all(isinstance(pending.get(k), str) and ID.fullmatch(pending[k]) for k in ('id','lease'))
            and isinstance(pending.get('sha256'), str) and SHA.fullmatch(pending['sha256']), '本地提交记录无效')
    if reconcile_first:
        if api.completed(pending['id'], pending['lease'], pending['sha256']):
            return
        if fingerprint(folder / 'output.mp4') != pending['sha256']:
            raise Rejected(422)
    api.complete(pending['id'], pending['lease'], folder / 'output.mp4', pending['sha256'])


def run_job(api, job, root, encoder, ffmpeg, ffprobe):
    tid, lease = job['id'], job['lease']
    require(all(isinstance(x, str) and ID.fullmatch(x) for x in (tid, lease)), '任务编号无效')
    folder = Path(tempfile.mkdtemp(prefix='render-', dir=str(root)))
    stopped, done = threading.Event(), threading.Event()
    def heartbeat():
        while not done.wait(30):
            try:
                api.post('/tasks/%s/heartbeat' % tid, lease)
            except LeaseLost:
                stopped.set()
                return
            except Exception:
                LOG.warning('heartbeat temporarily unavailable for %s', tid)
    thread = threading.Thread(target=heartbeat, name='ai-edit-render-heartbeat', daemon=True)
    thread.start()
    keep = False
    try:
        sources = {}
        clips = job['unit']['clips']
        require(isinstance(clips, list) and 1 <= len(clips) <= 300, '时间线无效')
        for sid in dict.fromkeys(c['source_id'] for c in clips):
            require(isinstance(sid, str) and ID.fullmatch(sid), '素材编号无效')
            path = folder / ('input_%03d.mp4' % len(sources))
            api.download(tid, lease, sid, path, stopped)
            sources[sid] = path
        aid=job['unit'].get('composition',{}).get('speech_id')
        if aid:
            require(isinstance(aid,str) and re.fullmatch(r'ag_[a-f0-9]{32}',aid),'音轨编号无效')
            path=folder/'voice.wav'
            api.download(tid,lease,aid,path,stopped,kind='audio')
            sources[aid]=path
        output = folder / 'output.mp4'
        meta = render(job['unit'], sources, output, folder, encoder=encoder, ffmpeg=ffmpeg, ffprobe=ffprobe, stopped=stopped)
        temp = folder / 'pending.tmp'
        temp.write_text(json.dumps({'id': tid, 'lease': lease, 'sha256': meta['sha256']}), encoding='utf8')
        os.replace(temp, folder / 'pending.json')
        keep = True
        for path in folder.iterdir():
            if path.name not in ('output.mp4', 'pending.json'):
                path.unlink()
        upload_saved(api, folder)
        keep = False
        LOG.info('render completed task=%s frames=%d sha256=%s', tid, meta['frames'], meta['sha256'])
    except LeaseLost:
        keep = False
    except (TransferError, OSError) as exc:
        # Let the server re-lease a pre-render transport failure. A finished
        # file gets reconciled before the worker requests another job.
        LOG.warning('render transport interrupted for %s (%s)', tid, type(exc).__name__)
    except Exception as exc:
        LOG.warning('render failed for %s (%s)', tid, type(exc).__name__)
        if not keep and not stopped.is_set():
            api.post('/tasks/%s/fail' % tid, lease)
    finally:
        done.set()
        thread.join(1)
        if not keep:
            shutil.rmtree(folder)


def reconcile(api, root):
    for folder in sorted(root.glob('render-*')):
        if folder.is_symlink() or not folder.is_dir():
            continue
        if not (folder / 'pending.json').is_file():
            # A single-consumer lock makes these interrupted local render dirs.
            shutil.rmtree(folder)
            continue
        try:
            upload_saved(api, folder, reconcile_first=True)
        except LeaseLost:
            pass
        except Rejected as exc:
            if exc.status not in (413, 422):
                raise
            # The CPU explicitly rejected this artifact before committing it.
            # A completion with a lost response never goes down this path.
            record = json.loads((folder / 'pending.json').read_text(encoding='utf8'))
            try:
                api.post('/tasks/%s/fail' % record['id'], record['lease'])
            except LeaseLost:
                pass
        shutil.rmtree(folder)


def gpu_capacity_available():
    # NVENC is a dedicated engine. Check its load and available framebuffer
    # before claiming; do not suspend unrelated CUDA training or other jobs.
    if os.environ.get('AI_VIDEO_EDITING_ENCODER', 'h264_nvenc') != 'h264_nvenc':
        return True
    try:
        sample = subprocess.run(['nvidia-smi', '-i', '0', '--query-gpu=memory.free,utilization.encoder',
                                 '--format=csv,noheader,nounits'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=4, check=True)
        memory, usage = sample.stdout.strip().split(',')[:2]
        return int(memory.strip()) >= 512 and (not usage.strip().isdigit() or int(usage.strip()) < 90)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    draining=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:draining.set())
    root = Path(os.environ.get('AI_VIDEO_EDITING_RENDER_ROOT', '/data/ai-video-editing/work')).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Single host-wide consumer. No second local process may clean an active
    # workspace or issue a concurrent NVENC encode.
    import fcntl
    with (root / 'worker.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        token_file = Path(os.environ.get('AI_VIDEO_EDITING_WORKER_TOKEN_FILE', '/etc/ai-video-editing/worker.token'))
        api = InternalAPI(os.environ.get('AI_VIDEO_EDITING_INTERNAL_URL',
                          'https://ai.yingliangads.com/api/ai-video-editing/internal'), token_file.read_text().strip())
        while not draining.is_set():
            try:
                reconcile(api, root)
                job = api.post('/claim').get('item') if gpu_capacity_available() else None
                if job:
                    run_job(api, job, root, os.environ.get('AI_VIDEO_EDITING_ENCODER', 'h264_nvenc'),
                            os.environ.get('AI_VIDEO_EDITING_FFMPEG', '/usr/bin/ffmpeg'),
                            os.environ.get('AI_VIDEO_EDITING_FFPROBE', '/usr/bin/ffprobe'))
            except Exception as exc:
                LOG.warning('worker cycle deferred (%s)', type(exc).__name__)
            if args.once:
                return
            draining.wait(5)


if __name__ == '__main__':
    main()
