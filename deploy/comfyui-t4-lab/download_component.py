"""Download pinned public model components with bounded HTTP ranges.

Operational fallback for a slow single-stream CDN route. No inference or secrets.
"""
import argparse
import concurrent.futures
import hashlib
import os
from pathlib import Path
import ssl
import time
import urllib.request

import certifi


REVISION = "ee6f4a40737a995bf5818954cfce6d59443b0f04"
COMPONENTS = {
    "vae": (
        "vae/wan2.2_vae.safetensors", 1409400960,
        "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
    ),
    "text_encoder": (
        "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", 6735906897,
        "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
    ),
}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=COMPONENTS)
    parser.add_argument("--workers", type=int, choices=range(1, 33), default=16)
    args = parser.parse_args()
    relative, size, expected = COMPONENTS[args.component]
    root = Path("/data/comfyui-t4-lab")
    destination = root / "models" / relative
    if destination.is_file():
        if destination.stat().st_size != size or digest(destination) != expected:
            raise RuntimeError("Existing model failed validation; leaving it untouched")
        print("Already verified:", relative, flush=True)
        return
    chunks = root / "download-parts" / destination.name
    chunks.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/{REVISION}/split_files/{relative}"
    chunk_size = 32 * 1024 * 1024
    count = (size + chunk_size - 1) // chunk_size
    # The copied standalone Python's compiled-in CA path belongs to its build host.
    # Use the installed, maintained CA bundle; keep certificate verification on.
    tls = ssl.create_default_context(cafile=certifi.where())

    def fetch(index):
        start = index * chunk_size
        end = min(start + chunk_size, size) - 1
        expected_size = end - start + 1
        part = chunks / f"{index:04d}.bin"
        if part.is_file() and part.stat().st_size == expected_size:
            return index
        temporary = part.with_suffix(".partial")
        for attempt in range(3):
            try:
                request = urllib.request.Request(
                    url,
                    headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"},
                )
                with urllib.request.urlopen(request, timeout=90, context=tls) as response:
                    wanted_range = f"bytes {start}-{end}/{size}"
                    if response.status != 206 or response.headers.get("Content-Range") != wanted_range:
                        raise RuntimeError("Server did not honor requested byte range")
                    written = 0
                    with temporary.open("wb") as output:
                        while written < expected_size:
                            block = response.read(min(1024 * 1024, expected_size - written))
                            if not block:
                                break
                            output.write(block)
                            written += len(block)
                if written != expected_size:
                    raise RuntimeError("Incomplete byte range")
                os.replace(temporary, part)
                return index
            except Exception as error:
                # Do not print signed redirect URLs from transport exceptions.
                if attempt == 2:
                    raise RuntimeError(f"Chunk {index} failed: {type(error).__name__}") from None
                time.sleep(2 * (attempt + 1))
        raise AssertionError("Unreachable")

    print(f"Downloading {args.component}: {size} bytes, {count} chunks, {args.workers} workers", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch, index) for index in range(count)]
        for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
            future.result()
            print(f"Completed {completed}/{count} chunks", flush=True)
    staged = destination.with_suffix(".safetensors.verified-download")
    result = hashlib.sha256()
    with staged.open("wb") as output:
        for index in range(count):
            with (chunks / f"{index:04d}.bin").open("rb") as source:
                for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                    output.write(block)
                    result.update(block)
    if staged.stat().st_size != size or result.hexdigest() != expected:
        raise RuntimeError("Model checksum mismatch; model was not activated")
    os.replace(staged, destination)
    print(f"SHA256 verified: {relative} {expected}", flush=True)


if __name__ == "__main__":
    main()
