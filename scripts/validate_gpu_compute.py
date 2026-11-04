"""Isolated CUDA SHA validation; writes no platform or worker state."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.gpu_compute import CudaSHA256


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    rng = random.Random(581793)
    cases = [b"", b"abc", b"a" * 1000000]
    cases += [rng.randbytes(n) for n in (1, 55, 56, 63, 64, 65, 119, 120, 127, 128, 129, 1024*1024+17)]
    results = []
    for data in cases:
        for step in (63, 1024*1024) if len(data)<200 else (1024*1024,):
            started = time.monotonic(); cpu = time.process_time()
            digest = CudaSHA256()
            for pos in range(0,len(data),step):
                digest.update(data[pos:pos+step])
            actual = digest.hexdigest()
            assert digest.hexdigest() == actual
            expected = hashlib.sha256(data).hexdigest()
            if actual != expected: raise AssertionError((len(data), step, actual, expected))
            results.append(dict(size=len(data),step=step,sha256=actual,passed=True,
                                seconds=time.monotonic()-started,cpu_seconds=time.process_time()-cpu))
    report = dict(status="passed",implementation="cuda_standard_sequential_sha256",tests=results)
    if args.output:Path(args.output).write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report))


if __name__ == "__main__": main()
