"""Pixel-exact comparison against the deployed renderer, with paired timings.

Run only on an idle/test GPU. The external idle runner owns production priority.
Results checkpoint per case so interrupted idle windows do not erase evidence.
"""
import argparse
import importlib
import json
from pathlib import Path
import statistics
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def baseline_renderer(root):
    package = types.ModuleType('gpu_math_reference')
    package.__path__ = [str(Path(root) / 'features')]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + '.random_gpu.native_resize').CachedWeightRenderer


def save(path, rows):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(rows, indent=2), encoding='utf8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-root', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=4)
    parser.add_argument('--timing-frames', type=int, default=8)
    args = parser.parse_args()
    import numpy as np
    from features.random_gpu.native_video import codec_runtime
    from features.random_gpu.native_resize import CachedWeightRenderer
    reference = baseline_renderer(args.baseline_root)
    cp, _, _ = codec_runtime()
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / 'kernel-results.json'
    rows = json.loads(path.read_text()) if path.exists() else []
    done = {x['case'] for x in rows}

    class Frame:
        def __init__(self, planes):
            self.planes = planes
        def cuda(self):
            return self.planes

    shapes = [(720,1280),(1080,1920),(1920,1080),(640,360),(3840,2160),(718,1282)]
    formats = [(8,0,'left'),(8,1,'center'),(10,0,'topleft')]
    for shape_index, (iw, ih) in enumerate(shapes):
        for fmt_index, (bits, legacy, chroma) in enumerate(formats):
            case = f'{iw}x{ih}-{bits}-{legacy}-{chroma}'
            if case in done:
                continue
            rng = np.random.default_rng(20261118 + shape_index*100 + fmt_index)
            recipe = dict(rotation_millidegrees=(-992 if legacy else 992), scale_bp=9924,
                tint_opacity_bp=959, source_overlay=dict(version=1, opacity_bp=397, scale_bp=14120))
            video = dict(codec_name='h264' if bits==8 else 'hevc',
                pix_fmt='yuv420p' if bits==8 else 'yuv420p10le', width=iw, height=ih,
                time_base='1/30', chroma_location=chroma, color_space='bt709', color_range='tv')
            old, new = reference(recipe,video), CachedWeightRenderer(recipe,video)
            old.legacy_fit = new.legacy_fit = legacy
            max_error = different = 0
            for frame_index in range(args.frames):
                dtype = np.uint8 if bits==8 else np.uint16
                planes = [cp.asarray(rng.integers(0,1<<bits,shape,dtype=dtype))
                          for shape in ((ih,iw),(ih//2,iw))]
                if bits>8:
                    planes = [p << (16-bits) for p in planes]
                frame = Frame(planes)
                layers = {name:cp.asarray(rng.integers(0,256,(32,32,4),dtype=np.uint8))
                          for name in ('border','opacity','corners','tint')}
                before, after = old.render(frame,layers).get(), new.render(frame,layers).get()
                pairs = [(before,after), (old.back.get(),new.back.get())]
                if iw*1280 != ih*720:
                    pairs.append((old.main.get(),new.main.get()))
                for before, after in pairs:
                    delta = np.abs(before.astype(np.int16)-after.astype(np.int16))
                    different += int(np.count_nonzero(delta))
                    max_error = max(max_error,int(delta.max()))
            times = {'baseline':[], 'candidate':[]}
            for order in [('baseline','candidate'),('candidate','baseline')]*3:
                for name in order:
                    renderer = old if name=='baseline' else new
                    start,end = cp.cuda.Event(),cp.cuda.Event()
                    start.record()
                    for _ in range(args.timing_frames):
                        renderer.render(frame,layers)
                    end.record();end.synchronize()
                    times[name].append(cp.cuda.get_elapsed_time(start,end)/args.timing_frames)
            row = dict(case=case,frames=args.frames,different_channels=different,max_error=max_error,
                baseline_ms=statistics.median(times['baseline']),candidate_ms=statistics.median(times['candidate']),
                timings_ms=times)
            row['speedup'] = row['baseline_ms']/row['candidate_ms']
            rows.append(row);save(path,rows)
            print(json.dumps(row),flush=True)
            if different:
                raise AssertionError('pixel_mismatch: '+case)
            del old,new,frame,planes,layers
            cp.get_default_memory_pool().free_all_blocks()
    print(json.dumps({'complete':True,'cases':len(rows),'pixel_exact':all(x['different_channels']==0 for x in rows)}),flush=True)


if __name__ == '__main__':
    main()
