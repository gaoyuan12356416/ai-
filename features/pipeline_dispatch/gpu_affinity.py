"""Bind each preparation process before importing a CUDA consumer."""
import os
import subprocess


def discover_devices(environ=None):
    env = os.environ if environ is None else environ
    # Respect an operator's existing visibility restriction, including disabled CUDA.
    if "CUDA_VISIBLE_DEVICES" in env:
        devices = [part.strip() for part in env["CUDA_VISIBLE_DEVICES"].split(",") if part.strip()]
        if not devices or devices == ["-1"]:
            raise RuntimeError("GPU preparation has no visible CUDA devices")
        return devices
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=10, check=True)
    devices = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not devices or any(not value.startswith("GPU-") for value in devices):
        raise RuntimeError("GPU device discovery failed")
    return devices


def bind_prepare_process(config, stage, index, environ=None):
    if config.get("backend") != "gpu" or stage != "prepare":
        return None
    devices = config.get("gpu_devices")
    if not devices:
        raise RuntimeError("GPU preparation requires discovered devices")
    device = devices[index % len(devices)]
    env = os.environ if environ is None else environ
    env["CUDA_VISIBLE_DEVICES"] = device
    return device
