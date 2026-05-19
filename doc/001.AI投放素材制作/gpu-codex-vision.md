# GPU Codex Vision Worker

Codex subprocess workloads for ad-material requirement vision analysis should run on the GPU host, not inside the CPU web API process.

## Runtime

- GPU service: `ad-material-vision.service`
- GPU local endpoint: `http://127.0.0.1:8796/api/ad-material-vision/analyze`
- CPU reverse-tunnel endpoint: `http://127.0.0.1:18796/api/ad-material-vision/analyze`
- CPU health check: `curl -sS http://127.0.0.1:18796/health`

## CPU `.env`

```bash
AD_MATERIAL_VISION_PROVIDER=codex
AD_MATERIAL_CODEX_VISION_URL=http://127.0.0.1:18796/api/ad-material-vision/analyze
```

The CPU task runner still performs product data lookup, Guangdada/DataIdea pulls, reference-image archiving, and final task persistence. The image-understanding Codex subprocess is delegated to the GPU worker.

## GPU Tunnel

`gpu-worker-reverse-tunnel.service` must include:

```bash
-R 127.0.0.1:18796:127.0.0.1:8796
```
