"""Media-only FB comparison. Requires an expiring external production permit.

No queue admission, upload, credentials, platform request or service restart.
Only child processes created by this script can be cancelled by its watchdog.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time


class ProductionNeedsResources(RuntimeError):
    pass


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temp, path)


def check_permit(path, work_root):
    try:
        permit = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ProductionNeedsResources('production_permit_unavailable') from None
    if not permit.get('allowed') or permit.get('expires_at', 0) < time.time():
        raise ProductionNeedsResources('production_permit_withdrawn')
    memory=Path('/proc/meminfo')
    if memory.exists():
        available=next(int(s.split()[1])*1024 for s in memory.read_text().splitlines()
                       if s.startswith('MemAvailable:'))
        if available<4*1024**3:
            raise ProductionNeedsResources('benchmark_ram_reserve')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            arguments = (proc/'cmdline').read_bytes().split(b'\0')
            if not any(b'features.random_gpu.native_pipeline' == a or
                       a.rsplit(b'/', 1)[-1] == b'ffmpeg' for a in arguments):
                continue
            if any(str(work_root).encode() in a for a in arguments):
                continue
            raise ProductionNeedsResources('production_media_process_started')
        except (OSError, ProcessLookupError):
            continue


def stop_owned(processes):
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    end = time.monotonic() + 3
    while time.monotonic() < end and any(p.poll() is None for p in processes):
        time.sleep(.05)
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def guarded_commands(commands, output_files, args, env):
    processes, logs = [], []
    started = time.monotonic()
    try:
        for index, command in enumerate(commands):
            check_permit(args.permit, args.work_root)
            log = output_files[index].with_suffix('.log').open('wb')
            logs.append(log)
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                env=env, cwd=args.runtime, start_new_session=True)
            processes.append(process)
        samples = []
        while any(p.poll() is None for p in processes):
            check_permit(args.permit, args.work_root)
            if time.monotonic()-started > args.case_timeout:
                raise RuntimeError('benchmark_case_timeout')
            if len(samples) == 0 or time.monotonic()-samples[-1]['monotonic'] >= 2:
                gpu = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,utilization.encoder,memory.used,memory.free',
                    '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
                samples.append(dict(monotonic=time.monotonic(), gpu=gpu.stdout.strip()))
                if gpu.returncode==0:
                    free=[float(line.split(',')[3]) for line in gpu.stdout.splitlines() if line.strip()]
                    if free and min(free)<1024:
                        raise ProductionNeedsResources('benchmark_gpu_memory_reserve')
            time.sleep(.2)
        codes = [p.returncode for p in processes]
        if any(codes):
            raise RuntimeError('benchmark_child_failed:'+','.join(map(str,codes)))
        return dict(wall_seconds=time.monotonic()-started, gpu_samples=samples)
    finally:
        stop_owned(processes)
        for log in logs:
            log.close()


def probe(ffprobe, path):
    raw = subprocess.check_output([ffprobe, '-v', 'error', '-show_streams',
                                  '-show_format', '-of', 'json', str(path)])
    data = json.loads(raw)
    video = next(s for s in data['streams'] if s['codec_type']=='video')
    audio = next((s for s in data['streams'] if s['codec_type']=='audio'), None)
    return dict(duration=float(data['format']['duration']), has_audio=audio is not None,
                video=video, audio=audio, format=data['format'])


def source_spec(job, args):
    from features.fb_gpu.random_overlay import load_asset_set, selected_asset_paths
    frozen = json.loads((job/'recipe.json').read_text(encoding='utf-8'))
    source = job/'source.mp4'
    assets = load_asset_set(Path(frozen['asset_root']), frozen['recipe']['asset_set_sha256'])
    info = probe(args.ffprobe, source)
    info['duration'] = min(args.duration, info['duration'])
    return dict(source=str(source), info=info, recipe=frozen['recipe'],
        assets={k:str(v) for k,v in selected_asset_paths(frozen['recipe'], assets).items()},
        cache_root=args.asset_cache, ffmpeg=args.ffmpeg, audio_bitrate=192000)


ENCODINGS = {
    'baseline_p5_cq21': None,
    'p4_cq23': dict(codec='h264', profile='high', preset='P4', rc='vbr', cq=23,
        bitrate=0, fps=30, gop=60, idrperiod=60),
    'p4_cq23_3m': dict(codec='h264', profile='high', preset='P4', rc='vbr', cq=23,
        bitrate=2500000, maxbitrate=3500000, vbvbufsize=5000000,
        fps=30, gop=60, idrperiod=60),
    'p4_cq25': dict(codec='h264', profile='high', preset='P4', rc='vbr', cq=25,
        bitrate=0, fps=30, gop=60, idrperiod=60),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--permit', type=Path, required=True)
    parser.add_argument('--source-job', type=Path, action='append', required=True)
    parser.add_argument('--asset-cache', required=True)
    parser.add_argument('--ffmpeg', default='/usr/bin/ffmpeg')
    parser.add_argument('--ffprobe', default='/usr/bin/ffprobe')
    parser.add_argument('--duration', type=float, default=30)
    parser.add_argument('--case-timeout', type=float, default=900)
    parser.add_argument('--levels', default='4,8,12,16,24')
    parser.add_argument('--mode', choices=['encoding','throughput'], required=True)
    parser.add_argument('--encoding', choices=list(ENCODINGS), default='baseline_p5_cq21')
    args = parser.parse_args()
    args.work_root = args.work_root.resolve()
    args.work_root.mkdir(parents=True, exist_ok=True)
    if str(args.work_root).startswith(str(args.runtime)):
        raise ValueError('benchmark_must_not_write_runtime')
    env = {k:v for k,v in os.environ.items() if k in ['PATH','LD_LIBRARY_PATH','CUDA_VISIBLE_DEVICES',
        'CUDA_MODULE_LOADING','CUPY_CACHE_DIR','HOME','LANG','PYTHONPATH','SSL_CERT_FILE']}
    env['PYTHONPATH'] = str(args.runtime)
    env['OPENBLAS_NUM_THREADS'] = '1'
    env['OMP_NUM_THREADS'] = '1'
    env['FB_EFFICIENCY_BENCH'] = '1'
    report_path = args.work_root/(args.mode+'-'+args.encoding+'.json')
    report = json.loads(report_path.read_text()) if report_path.exists() else dict(
        mode=args.mode, duration=args.duration, encoding=args.encoding, cases=[],
        publication_writes=0, production_signals=0)
    specs = [source_spec(job, args) for job in args.source_job]
    if args.mode == 'encoding':
        cases = [(f'source{i+1}-{name}', [dict(spec, encoder_options=options)])
                 for i,spec in enumerate(specs) for name,options in ENCODINGS.items()]
    else:
        levels = [int(x) for x in args.levels.split(',')]
        cases = [(f'concurrency-{n}', [dict(specs[i%len(specs)],
                    encoder_options=ENCODINGS[args.encoding]) for i in range(n)]) for n in levels]
    completed = {c['name'] for c in report['cases'] if c.get('status')=='completed'}
    for name, batch in cases:
        if name in completed:
            continue
        outputs = [args.work_root/(name+f'-{i}.mp4') for i in range(len(batch))]
        commands = [[sys.executable, '-m', 'features.random_gpu.native_pipeline', '--spec',
                     json.dumps(spec,separators=(',',':')), str(output)] for spec,output in zip(batch,outputs)]
        case = dict(name=name, concurrency=len(batch))
        print(json.dumps(dict(event='starting',case=name)),flush=True)
        try:
            result = guarded_commands(commands, outputs, args, env)
            durations = [probe(args.ffprobe,p)['duration'] for p in outputs]
            case.update(result, status='completed', video_seconds=sum(durations),
                output_bytes=sum(p.stat().st_size for p in outputs),
                media_seconds_per_wall_second=sum(durations)/result['wall_seconds'],
                outputs=[str(p) for p in outputs])
            case['native'] = [json.loads(p.with_suffix('.mp4.native.json').read_text()) for p in outputs]
        except ProductionNeedsResources as exc:
            case.update(status='yielded_to_production', reason=str(exc))
            report['cases'].append(case)
            atomic_json(report_path,report)
            print(json.dumps(dict(event='yielded',case=name,reason=str(exc))),flush=True)
            return 75
        except Exception as exc:
            case.update(status='failed', error_type=type(exc).__name__,error_code=str(exc)[:160])
        report['cases'].append(case)
        atomic_json(report_path, report)
        print(json.dumps({k:v for k,v in case.items() if k not in ['native','gpu_samples','outputs']}),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
