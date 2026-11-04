"""Short real-codec FFmpeg duration reference and optional CUDA remux check."""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_gpu_mux_boundaries import boxes, find_box


def metadata(path):
    data = Path(path).read_bytes()
    moov = find_box(data, [b'moov'])
    mvhd = find_box(moov, [b'mvhd'])
    pos = 20 if mvhd[0] else 12
    scale = struct.unpack_from('>I', mvhd, pos)[0]
    duration = struct.unpack_from('>Q' if mvhd[0] else '>I', mvhd, pos+4)[0]
    result = {'movie_duration': str(Fraction(duration, scale)), 'movie_scale': scale, 'tracks': []}
    for kind, trak in boxes(moov):
        if kind != b'trak':
            continue
        hdlr = find_box(trak, [b'mdia', b'hdlr'])[8:12].decode()
        mdhd = find_box(trak, [b'mdia', b'mdhd'])
        pos = 20 if mdhd[0] else 12
        scale = struct.unpack_from('>I', mdhd, pos)[0]
        media_duration = struct.unpack_from('>Q' if mdhd[0] else '>I', mdhd, pos+4)[0]
        tkhd = find_box(trak, [b'tkhd'])
        pos = 28 if tkhd[0] else 20
        track_duration = struct.unpack_from('>Q' if tkhd[0] else '>I', tkhd, pos)[0]
        movie_scale = result['movie_scale']
        result['tracks'].append({'kind': hdlr, 'media_scale': scale,
            'media_duration': str(Fraction(media_duration, scale)),
            'track_duration': str(Fraction(track_duration, movie_scale))})
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    p.add_argument('--ffmpeg', default='/usr/bin/ffmpeg')
    p.add_argument('--gpu', action='store_true')
    args = p.parse_args()
    result = []
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='qa-mux-duration-', dir=destination.parent) as scratch:
        for duration in ('0.01', '0.10001', '1.00001'):
            source = Path(scratch)/(duration+'.mp4')
            subprocess.run([args.ffmpeg, '-nostdin', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc2=s=16x16:r=30:d=1.1',
                '-f', 'lavfi', '-i', 'sine=r=48000:d=1.1',
                '-t', duration, '-c:v', 'libx264', '-threads', '1',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2', '-b:a', '192k',
                '-movflags', '+faststart', str(source)], check=True, capture_output=True, timeout=30)
            entry = {'requested_duration': duration, 'reference': metadata(source)}
            from scripts.validate_gpu_mux import tracks_from_mp4
            tracks = tracks_from_mp4(source)
            entry['packets'] = [{'kind': t.kind, 'samples': len(t.samples),
                'first_pts': t.samples[0].pts, 'first_dts': t.samples[0].dts,
                'presentation_end': str(Fraction(max(s.pts+s.duration for s in t.samples), t.timescale))}
                for t in tracks]
            if args.gpu:
                from features.random_gpu.gpu_mux import CudaMP4
                target = Path(scratch)/(duration+'-gpu.mp4')
                CudaMP4().mux(tracks, target, duration)
                entry['candidate'] = metadata(target)
                # Different movie timescales can round by <1 tick. Compare
                # each track against its actual encoded presentation end.
                expected = [Fraction(row['presentation_end']) for row in entry['packets']]
                actual = [Fraction(row['track_duration']) for row in entry['candidate']['tracks']]
                tick = Fraction(1, entry['candidate']['movie_scale'])
                entry['track_duration_passed'] = all(0 <= b-a < tick for a,b in zip(expected,actual))
                movie = Fraction(entry['candidate']['movie_duration'])
                entry['movie_duration_passed'] = 0 <= movie-max(expected) < tick
            result.append(entry)
    destination.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
    return 0 if not args.gpu or all(x['track_duration_passed'] and x['movie_duration_passed'] for x in result) else 1


if __name__ == '__main__':
    raise SystemExit(main())
