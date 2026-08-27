#!/usr/bin/env python3
"""Operator-only synthetic media acceptance; no CPU DB or social publication.

Run as the HK media service user with its isolated EnvironmentFile. Creates two
fixed canary job identities, bounded fixtures and COS outputs under the dedicated
canary prefix. Keeps outputs/evidence for inspection; never deletes user jobs.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.drama_synthesis.core import freeze_random_recipe
from features.drama_synthesis.gpu_cache import public_result

BASE = Path('/data/drama-synthesis-gpu')
WORK = BASE / 'work/acceptance'
REPORT = WORK / 'http-media-20260827-v3.json'
API = 'http://127.0.0.1:8787'
PREFIX = 'drama-synthesis-canary/20260827'


def command(args, timeout=180):
    proc = subprocess.run(args, capture_output=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError('media_subprocess_failed:' + Path(args[0]).name)
    return proc.stdout


def job_identity(mode):
    return hashlib.sha256(('hk-synthetic-20260827-v3-' + mode).encode()).hexdigest()[:32]


def manifest_snapshot(job_id):
    path = Path(os.environ['GPU_VIDEO_RESULT_ROOT']) / (job_id + '.json')
    assert path.is_file() and not path.is_symlink()
    raw = path.read_bytes()
    assert len(raw) <= 1024 * 1024
    value = json.loads(raw)
    assert value['job_id'] == job_id and value['gpu_result_manifest_version'] == 2
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'mtime_ns': path.stat().st_mtime_ns}


def verify_replay(api, row):
    before = manifest_snapshot(row['job_id'])
    assert not (BASE / 'work/jobs' / row['job_id']).exists()
    status, repeated = api('/api/gpu-video/render', row['payload'])
    assert status == 200 and public_result(repeated) == public_result(row['output'])
    assert manifest_snapshot(row['job_id']) == before
    assert not (BASE / 'work/jobs' / row['job_id']).exists()
    return before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--replay-only', action='store_true', help='Recheck completed v3 jobs without a fixture server or rendering')
    args = parser.parse_args()
    if not args.apply:
        parser.error('--apply is required for synthetic media and COS writes')
    import pwd
    import requests
    assert pwd.getpwuid(os.getuid()).pw_name == 'drama-synthesis-gpu'
    assert os.environ['COS_PREFIX'] == PREFIX
    assert os.environ['YOUTUBE_LIVE_ENABLED'] == '0'
    assert os.environ['DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED'] == '0'
    assert os.environ['DRAMA_WORK_ROOT'] == str(BASE / 'work/jobs')
    token = os.environ['GPU_VIDEO_WORKER_TOKEN']
    ffmpeg, ffprobe = os.environ['DRAMA_FFMPEG'], os.environ['DRAMA_FFPROBE']
    headers = {'Authorization': 'Bearer ' + token}
    WORK.mkdir(mode=0o750, parents=True, exist_ok=True)

    def api(path, payload=None, authenticated=True):
        response = requests.request('GET' if payload is None else 'POST', API + path,
                                    json=payload, headers=headers if authenticated else {},
                                    timeout=(10, 1200), allow_redirects=False)
        return response.status_code, response.json()

    assert api('/healthz', authenticated=False)[0] == 200
    assert api('/api/gpu-video/render', {'job_id': 'invalid'}, authenticated=False)[0] == 401
    assert api('/api/gpu-video/render', {'job_id': '../invalid'})[0] == 400
    status, result = api('/api/gpu-video/random-overlay/catalog')
    assert status == 200
    catalog = result['item']
    assert {k: len(v) for k, v in catalog['categories'].items()} == {
        'border': 3, 'opacity_video': 5, 'corners': 3, 'tint': 7}
    assert 'light' not in catalog['categories']

    if args.replay_only:
        evidence = json.loads(REPORT.read_text(encoding='utf-8'))
        assert evidence['ok'] and evidence['release_sha'] == ROOT.name
        assert len(evidence['results']) == 2
        for row in evidence['results']:
            assert row['job_id'] == job_identity(row['mode'])
            assert verify_replay(api, row) == row['manifest_after_render']
        evidence['restart_replay_verified'] = True
        REPORT.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps({'ok': True, 'replay_only': True, 'jobs': 2, 'report': str(REPORT), 'publications': 0}), flush=True)
        return

    # Fresh-work acceptance and persisted replay are distinct phases. Refuse to
    # overwrite earlier fixtures/jobs when this whole suite is invoked again.
    for mode in ('auto', 'manual'):
        job_id = job_identity(mode)
        assert not (Path(os.environ['GPU_VIDEO_RESULT_ROOT']) / (job_id + '.json')).exists()
        assert not (BASE / 'work/jobs' / job_id).exists()

    fixtures = {'/episode1.mp4': WORK / 'episode1.mp4',
                '/episode2.mp4': WORK / 'episode2.mp4',
                '/cover.jpg': WORK / 'cover.jpg'}
    for index in (1, 2):
        command([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                 '-f', 'lavfi', '-i', 'testsrc2=size=360x640:rate=25:duration=2',
                 '-f', 'lavfi', '-i', f'sine=frequency={440 * index}:sample_rate=48000:duration=2',
                 '-vf', f'hue=h={0 if index == 1 else 180}',
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                 '-c:a', 'aac', '-ac', '2', '-shortest', str(fixtures[f'/episode{index}.mp4'])])
    command([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
             '-f', 'lavfi', '-i', 'color=c=0x213247:size=1280x720:duration=1',
             '-frames:v', '1', str(fixtures['/cover.jpg'])])

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = fixtures.get(self.path)
            if path is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(path.stat().st_size))
            self.end_headers()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(65536), b''):
                    self.wfile.write(chunk)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture_base = f'http://127.0.0.1:{server.server_port}'
    evidence = {'suite': 'hk-synthetic-http-media-20260827-v3', 'results': [], 'release_sha': ROOT.name,
                'manifest_sha256': catalog['manifest_sha256'], 'publications': 0}
    started = time.monotonic()
    try:
        for mode in ('auto', 'manual'):
            job_id = job_identity(mode)
            selection = {'mode': mode, 'source': 'concat_video' if mode == 'auto' else 'no_bgm_video'}
            if mode == 'manual':
                selection['layers'] = {k: rows[0]['name'] for k, rows in catalog['categories'].items()}
            recipe = freeze_random_recipe(job_id=job_id, content_id='hk-runtime-test',
                                          request=selection, catalog=catalog)
            payload = {'job_id': job_id, 'content_id': 'hk-runtime-test',
                       'episode_start': 1, 'episode_end': 2,
                       'episodes': [{'episode_number': n, 'episode_url': fixture_base + f'/episode{n}.mp4'}
                                    for n in (1, 2)],
                       'outputs': {'concat_video': mode == 'auto', 'no_bgm_video': mode == 'auto',
                                   'random_template_video': True},
                       'random_template_recipe': recipe}
            if mode == 'auto':
                payload['cover_16x9_url'] = fixture_base + '/cover.jpg'
            else:
                payload.update(await_cover_16x9=True, cover_wait_timeout=60)
            print(json.dumps({'stage': 'render_started', 'mode': mode, 'job_id': job_id}), flush=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(api, '/api/gpu-video/render', payload)
                if mode == 'manual':
                    deadline = time.monotonic() + 25
                    while not (BASE / 'work/jobs' / job_id).exists() and not future.done():
                        if time.monotonic() > deadline:
                            raise RuntimeError('render_did_not_start')
                        threading.Event().wait(0.1)
                    if not future.done():
                        assert api('/api/gpu-video/render', {'job_id': 'hk-busy-probe'})[0] == 503
                        assert api('/healthz', authenticated=False)[0] == 200
                        assert api('/api/gpu-video/random-overlay/catalog')[0] == 200
                        assert api('/api/gpu-video/cover', {'job_id': job_id,
                                   'cover_16x9_url': fixture_base + '/cover.jpg'})[0] == 200
                        evidence['cover_callback_during_busy_render'] = True
                status, output = future.result(timeout=1200)
            if status != 200:
                raise RuntimeError('render_failed:' + mode + ':' + str(status) + ':' + str(output.get('code')))
            assert output['random_template_recipe_sha256'] == recipe['recipe_sha256']
            row = {'job_id': job_id, 'mode': mode, 'recipe': recipe, 'payload': payload, 'output': output, 'media': {}}
            for key in ('output_video_url', 'output_video_no_bgm_url', 'output_random_template_url'):
                url = output.get(key, '')
                if not url:
                    assert mode == 'manual' and key != 'output_random_template_url'
                    continue
                expected_prefix = 'https://' + os.environ['COS_DOMAIN'].strip().strip('/') + '/' + PREFIX + '/' + job_id + '/'
                assert url.startswith(expected_prefix)
                path = WORK / (job_id + '-' + key + '.mp4')
                digest, length = hashlib.sha256(), 0
                with requests.get(url, stream=True, timeout=(10, 120), allow_redirects=False) as response:
                    assert response.status_code == 200
                    with path.open('wb') as destination:
                        for chunk in response.iter_content(1024 * 1024):
                            length += len(chunk)
                            if length > 100 * 1024 * 1024:
                                raise RuntimeError('canary_output_too_large')
                            digest.update(chunk)
                            destination.write(chunk)
                probe = json.loads(command([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))
                video = next(s for s in probe['streams'] if s.get('codec_type') == 'video')
                audio = next(s for s in probe['streams'] if s.get('codec_type') == 'audio')
                duration = float(probe['format']['duration'])
                assert 4.75 <= duration <= 5.5 and video['codec_name'] == 'h264' and audio['codec_name'] == 'aac'
                if key == 'output_random_template_url':
                    assert (video['width'], video['height'], video['profile']) == (720, 1280, 'High')
                    assert 4.85 <= float(video['duration']) <= 5.15
                    assert 146 <= int(video['nb_frames']) <= 155
                    assert digest.hexdigest() == output['random_template_output_sha256']
                command([ffmpeg, '-hide_banner', '-loglevel', 'error', '-xerror', '-nostdin', '-i', str(path), '-f', 'null', '-'])
                row['media'][key] = {'sha256': digest.hexdigest(), 'bytes': length, 'duration': duration,
                                     'width': video['width'], 'height': video['height'], 'decoded': True}
                if key == 'output_random_template_url':
                    row['media'][key].update(video_duration=video['duration'], video_frames=video['nb_frames'])
                    frame_paths = []
                    for position in (0.5, 1.1, 3.1, 4.8):
                        frame = WORK / (job_id + '-frame-' + str(position) + '.png')
                        command([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                                 '-ss', str(position), '-i', str(path), '-frames:v', '1', str(frame)])
                        frame_paths.append(str(frame))
                    row['media'][key]['inspection_frames'] = frame_paths
            row['manifest_after_render'] = verify_replay(api, row)
            row['idempotent_readback'] = True
            evidence['results'].append(row)
            REPORT.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            print(json.dumps({'stage': 'render_verified', 'mode': mode, 'outputs': len(row['media'])}), flush=True)
        assert evidence.get('cover_callback_during_busy_render')
        evidence.update(ok=True, elapsed_seconds=round(time.monotonic() - started, 2))
        REPORT.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps({'ok': True, 'report': str(REPORT), 'variants': 2, 'verified_outputs': 4,
                          'elapsed_seconds': evidence['elapsed_seconds'], 'publications': 0}), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    main()
