# T4 / Wan2.2 isolated test environment

Target: `43.154.250.89`, instance `ins-52tbyu6m`. User requested a test on the
existing T4 and explicitly prioritizes existing rendering: submit inference only
when the GPU is idle. Installation and HTTP checks do not enqueue any inference.

- Root: `/data/comfyui-t4-lab`; service: `comfyui-t4-lab.service`.
- ComfyUI v0.3.76, commit `30c259cac8c08ff8d015f9aff3151cb525c9b702`.
- Own copy of Python 3.10 runtime, isolated venv, PyTorch 2.6 / CUDA 12.4.
- Wan2.2 TI2V 5B fp16 plus scaled fp8 UMT5 encoder and Wan2.2 VAE.
- Model repository revision and SHA256 checks are pinned in `install.sh`.
- No changes to NVIDIA drivers, system Python, production workers or firewall.
- UI binds only to loopback. No paid API nodes or automatic inference.
- The service is not enabled at boot. Resolved dependencies are saved in `records`.

Install only after this deployment directory has been committed and fetched from
GitHub on the target. Run `bash deploy/comfyui-t4-lab/install.sh` as root.

Access from the authorized Windows client:

```powershell
ssh -N -L 127.0.0.1:8188:127.0.0.1:8188 -i "$env:USERPROFILE\.ssh\id_ed25519_codex_remote" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes root@43.154.250.89
```

Open `http://127.0.0.1:8188`. API workflow is provided as
`wan22-t4-smoke-api.json`; import it in ComfyUI or submit to `/prompt` only when
the GPU is idle. The first profile is 640x352, 33 frames at 24fps, 12 steps:
it checks execution only and is not a production quality benchmark. Increase
resolution, frames and steps after verifying resources and actual results.
Model inference generates silent video.

Before inference, check current production GPU activity and ComfyUI `/queue`.
Do not stop production workers or submit duplicates if a request outcome is
uncertain; reconcile `/history` and `/queue` using the original prompt ID.

Rollback: when the test queue has finished, run
`systemctl stop comfyui-t4-lab.service`. This releases test compute and keeps
models, outputs, logs and the deployment record. To undo service registration,
remove only `/etc/systemd/system/comfyui-t4-lab.service` and run
`systemctl daemon-reload`; keep `/data/comfyui-t4-lab` for results and reuse.
