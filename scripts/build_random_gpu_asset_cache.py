"""Offline RGBA cache builder: no worker APIs, uploads, or social writes."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.asset_cache import VERSION, cached_path, sha256_file


def build(ffmpeg, source, root, max_bytes):
    source = source.resolve()
    key = sha256_file(source)
    try:
        existing = cached_path(source, root)
        record = json.loads((root / (key + '.json')).read_text())
        if sha256_file(existing) != record['sha256']:
            raise ValueError('Existing immutable cache checksum mismatch')
        return {'source': source.name, 'reused': True, 'size': existing.stat().st_size}
    except FileNotFoundError:
        pass
    target = root / (key + '.nut')
    partial = root / (key + '.partial')
    if target.exists() or partial.exists():
        raise RuntimeError('Unverified cache file exists; inspect rather than overwrite')
    # Never grow without a cap or leave less than 32 GiB on this 500 GiB host.
    used = sum(p.stat().st_size for p in root.glob('*.nut'))
    reserve = 8 * 1024**3
    if used + reserve > max_bytes or shutil.disk_usage(root).free < reserve + 32 * 1024**3:
        raise RuntimeError('RGBA cache capacity guard refused build')
    prefix = [ffmpeg, '-nostdin', '-v', 'error', '-filter_threads', '2', '-threads', '2']
    source_options = ['-c:v', 'libvpx-vp9'] if source.suffix == '.webm' else ['-framerate', '30']
    frames = ['-frames:v', '1'] if source.suffix == '.png' else []
    started = time.monotonic()
    command = [*prefix, *source_options, '-i', str(source), '-map', '0:v:0', '-an',
               '-vf', 'format=rgba', '-fps_mode', 'passthrough', *frames,
               '-c:v', 'rawvideo', '-threads', '2', '-fs', str(reserve), '-f', 'nut', str(partial)]
    subprocess.run(command, check=True, capture_output=True, timeout=240)
    # Compare every decoded RGBA frame and its timestamps against the source.
    def hashes(options, path):
        result = subprocess.run([*prefix, *options, '-i', str(path), '-map', '0:v:0', '-an',
                                 '-vf', 'format=rgba', '-fps_mode', 'passthrough', *frames,
                                 '-f', 'framemd5', '-'], check=True, capture_output=True, timeout=240)
        lines = result.stdout.decode().splitlines()
        rows = [line.split(',') for line in lines if line and not line.startswith('#')]
        # NUT rescales integer timestamps; compare physical times as rationals.
        from fractions import Fraction
        tb = Fraction(next(line.split(':', 1)[1].strip() for line in lines if line.startswith('#tb 0:')))
        return [(Fraction(row[2].strip()) * tb, Fraction(row[3].strip()) * tb,
                 row[4].strip(), row[5].strip()) for row in rows]
    expected, actual = hashes(source_options, source), hashes([], partial)
    if not expected or expected != actual:
        raise RuntimeError('RGBA frame/timestamp verification failed; partial retained')
    if sha256_file(source) != key:
        raise RuntimeError('Source changed during cache construction')
    os.replace(partial, target)
    os.chmod(target, 0o444)
    stat = target.stat()
    record = {'version': VERSION, 'source_sha256': key, 'sha256': sha256_file(target),
              'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'frames': len(actual),
              'rgba_frames_verified': True, 'build_seconds': time.monotonic() - started}
    receipt = root / (key + '.json')
    temporary = root / (key + '.json.tmp')
    temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
    os.replace(temporary, receipt)
    os.chmod(receipt, 0o444)
    return {'source': source.name, **record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--assets', action='append', required=True)
    parser.add_argument('--max-gib', type=int, default=96)
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_absolute() or not str(root).startswith('/data/random-overlay-gpu/'):
        parser.error('Cache must be in the verified Hong Kong data directory')
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.build.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for directory in args.assets:
            for pattern in ('border-*.png', 'tint-*.png', 'opacity-video-*.webm', 'corners-*.webm'):
                for source in sorted(Path(directory).glob(pattern)):
                    print(json.dumps(build(args.ffmpeg, source, root, args.max_gib * 1024**3)), flush=True)


if __name__ == '__main__':
    main()
