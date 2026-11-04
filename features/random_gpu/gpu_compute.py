"""Optional CUDA computations shared by preparation workers.

No import-time CUDA initialization: workers fork before acquiring a context.
An explicitly requested GPU operation never silently returns a CPU result.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os
from pathlib import Path
import sys
import threading


_CUDA_OWNER_PID = None


def cupy_runtime():
    if _CUDA_OWNER_PID is not None and _CUDA_OWNER_PID != os.getpid():
        raise RuntimeError("gpu_context_inherited_after_fork")
    return _cupy_runtime(os.getpid())


@lru_cache(maxsize=1)
def _cupy_runtime(pid):
    global _CUDA_OWNER_PID
    # NVIDIA's runtime wheels are private to the selected interpreter. Loading
    # them here does not replace the system driver or change another service.
    for directory in map(Path, sys.path):
        runtime = directory / "nvidia/cuda_runtime"
        if not runtime.is_dir():
            continue
        os.environ.setdefault("CUDA_PATH", str(runtime))
        for relative in ("nvidia/cuda_runtime/lib/libcudart.so.12",
                         "nvidia/cuda_nvrtc/lib/libnvrtc.so.12"):
            library = directory / relative
            if library.is_file():
                ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
        break
    import cupy
    # A long device operation must not leave a host thread busy-polling. Set
    # this only before the process's primary context exists; never reset an
    # active decoder/encoder context owned by another component.
    driver = ctypes.CDLL("libcuda.so.1") if sys.platform != "win32" else None
    if driver is not None and driver.cuInit(0) == 0:
        flags, active = ctypes.c_uint(), ctypes.c_int()
        if driver.cuDevicePrimaryCtxGetState(0, ctypes.byref(flags), ctypes.byref(active)) == 0 and not active.value:
            driver.cuDevicePrimaryCtxSetFlags(0, 4)  # CU_CTX_SCHED_BLOCKING_SYNC
    if cupy.cuda.runtime.getDeviceCount() < 1:
        raise RuntimeError("gpu_compute_device_unavailable")
    _CUDA_OWNER_PID = pid
    return cupy


SHA_SOURCE = r'''
extern "C" {
__device__ __constant__ unsigned int K[64] = {
0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
__device__ unsigned int rr(unsigned int x, unsigned int n) { return (x>>n)|(x<<(32-n)); }
__global__ void sha256_chunk(const unsigned char* data, unsigned long long size,
    unsigned long long total, int final, unsigned int* h) {
    if (blockIdx.x || threadIdx.x) return;
    unsigned long long blocks = final ? (size+9+63)/64 : size/64;
    for (unsigned long long block=0; block<blocks; ++block) {
        unsigned int w[64];
        for (int j=0; j<16; ++j) {
            unsigned int value=0;
            for (int k=0; k<4; ++k) {
                unsigned long long pos=block*64+j*4+k;
                unsigned int byte=0;
                if (pos<size) byte=data[pos];
                else if (pos==size && final) byte=128;
                else if (final && pos>=blocks*64-8)
                    byte=((total*8) >> ((blocks*64-1-pos)*8)) & 255;
                value=(value<<8)|byte;
            }
            w[j]=value;
        }
        for(int j=16;j<64;++j) {
            unsigned int s0=rr(w[j-15],7)^rr(w[j-15],18)^(w[j-15]>>3);
            unsigned int s1=rr(w[j-2],17)^rr(w[j-2],19)^(w[j-2]>>10);
            w[j]=w[j-16]+s0+w[j-7]+s1;
        }
        unsigned int a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],z=h[7];
        for(int j=0;j<64;++j) {
            unsigned int t1=z+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^(~e&g))+K[j]+w[j];
            unsigned int t2=(rr(a,2)^rr(a,13)^rr(a,22))+((a&b)^(a&c)^(b&c));
            z=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
        }
        h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=z;
    }
}
}
'''


class CudaSHA256:
    """The standard sequential file digest, not a tree of chunk digests."""
    def __init__(self):
        self.cp = cupy_runtime()
        self.kernel = self.cp.RawKernel(SHA_SOURCE, "sha256_chunk")
        self.state = self.cp.asarray([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                                     0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19],
                                    dtype=self.cp.uint32)
        self.cp.cuda.get_current_stream().synchronize()
        self.total = 0
        self.pending = b""
        self.finished = False
        self.broken = False
        self.pid = os.getpid()
        self.lock = threading.Lock()

    def _check(self):
        if self.pid != os.getpid():
            raise RuntimeError("gpu_digest_inherited_after_fork")
        if self.broken:
            raise RuntimeError("gpu_digest_failed")

    def update(self, data):
        import numpy as np
        self._check()
        with self.lock:
            self._check()
            if self.finished:
                raise ValueError("digest_already_finalized")
            try:
                data = self.pending + bytes(data)
                total = self.total + len(data) - len(self.pending)
                size = len(data) // 64 * 64
                if size:
                    device = self.cp.asarray(np.frombuffer(data, dtype=np.uint8, count=size))
                    self.kernel((1,), (1,), (device, np.uint64(size), np.uint64(total),
                                            np.int32(0), self.state))
                    self.cp.cuda.get_current_stream().synchronize()
                self.total, self.pending = total, data[size:]
            except BaseException:
                self.broken = True
                raise
        return self

    def hexdigest(self):
        import numpy as np
        self._check()
        with self.lock:
            self._check()
            try:
                if not self.finished:
                    data = self.cp.asarray(np.frombuffer(self.pending or b"\0", dtype=np.uint8))
                    self.kernel((1,), (1,), (data, np.uint64(len(self.pending)),
                                            np.uint64(self.total), np.int32(1), self.state))
                result = "".join(f"{int(word):08x}" for word in self.state.get())
                self.finished = True
                return result
            except BaseException:
                self.broken = True
                raise


def sha256_file(path, chunk_bytes=1024 * 1024):
    if not 64 <= chunk_bytes <= 64 * 1024 * 1024:
        raise ValueError("invalid_digest_chunk_size")
    digest = CudaSHA256()
    size = 0
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(chunk_bytes), b""):
            size += len(data)
            digest.update(data)
    return digest.hexdigest(), size


def enabled():
    return (os.environ.get("RANDOM_GPU_COMPUTE", "0") == "1" and
            os.environ.get("PIPELINE_RENDER_BACKEND", "gpu") == "gpu")


def file_digest():
    """Select file hashing explicitly; small control/identity hashes stay host."""
    if enabled():
        return CudaSHA256()
    import hashlib
    return hashlib.sha256()
