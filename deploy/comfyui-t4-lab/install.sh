#!/usr/bin/env bash
# Isolated test installation. Does not start inference or alter production services.
set -euo pipefail
LAB=/data/comfyui-t4-lab
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMFY_COMMIT=30c259cac8c08ff8d015f9aff3151cb525c9b702
MODEL_REV=ee6f4a40737a995bf5818954cfce6d59443b0f04
PYTHON_SOURCE=/opt/pipeline-dispatch/runtime/python

test "$(cat /var/lib/cloud/data/instance-id)" = ins-52tbyu6m
test "$(id -u)" = 0
mkdir -p "$LAB"/{records,logs,models/diffusion_models,models/text_encoders,models/vae,input,output,temp,user/default/workflows}
date -u +%FT%TZ > "$LAB/records/install-started.txt"
systemctl list-units --type=service --state=running --no-pager > "$LAB/records/services-before.txt"
nvidia-smi > "$LAB/records/gpu-before.txt"
df -h "$LAB" > "$LAB/records/disk-before.txt"

if ! id comfyui-lab >/dev/null 2>&1; then
  useradd --system --home-dir "$LAB" --no-create-home --shell /sbin/nologin comfyui-lab
fi
if [ ! -x "$LAB/python/bin/python3.10" ]; then
  cp -a "$PYTHON_SOURCE" "$LAB/python"
fi
if [ ! -d "$LAB/ComfyUI/.git" ]; then
  git clone --depth 1 --branch v0.3.76 https://github.com/Comfy-Org/ComfyUI.git "$LAB/ComfyUI"
fi
test "$(git -C "$LAB/ComfyUI" rev-parse HEAD)" = "$COMFY_COMMIT"
if [ ! -x "$LAB/venv/bin/python" ]; then
  "$LAB/python/bin/python3.10" -m venv "$LAB/venv"
fi
"$LAB/venv/bin/python" -m pip install --disable-pip-version-check 'pip==25.3'
"$LAB/venv/bin/python" -m pip install --disable-pip-version-check \
  --index-url https://download.pytorch.org/whl/cu124 \
  'torch==2.6.0+cu124' 'torchvision==0.21.0+cu124' 'torchaudio==2.6.0+cu124'
"$LAB/venv/bin/python" -m pip install --disable-pip-version-check \
  -c "$SOURCE/constraints.txt" -r "$LAB/ComfyUI/requirements.txt"
"$LAB/venv/bin/python" -m pip check
"$LAB/venv/bin/python" -m pip freeze > "$LAB/records/requirements-resolved.txt"

download_model() {
  local relative=$1 expected=$2
  local destination="$LAB/models/$relative"
  if [ ! -f "$destination" ]; then
    curl --fail --location --retry 3 --retry-delay 3 --continue-at - \
      --limit-rate 50M --output "$destination.part" \
      "https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/$MODEL_REV/split_files/$relative"
    printf '%s  %s\n' "$expected" "$destination.part" | sha256sum --check
    mv -- "$destination.part" "$destination"
  else
    printf '%s  %s\n' "$expected" "$destination" | sha256sum --check
  fi
}
download_model diffusion_models/wan2.2_ti2v_5B_fp16.safetensors 456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e
download_model text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68
download_model vae/wan2.2_vae.safetensors e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156

cat > "$LAB/extra_model_paths.yaml" <<'YAML'
lab:
  base_path: /data/comfyui-t4-lab/models
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
YAML
install -m 0644 "$SOURCE/wan22-t4-smoke-api.json" "$LAB/user/default/workflows/wan22-t4-smoke-api.json"
chown -R comfyui-lab:comfyui-lab "$LAB/models" "$LAB/input" "$LAB/output" "$LAB/user" "$LAB/logs" "$LAB/temp"
if [ -f /etc/systemd/system/comfyui-t4-lab.service ]; then
  cp -a /etc/systemd/system/comfyui-t4-lab.service "$LAB/records/service-before-$(date -u +%Y%m%dT%H%M%SZ).service"
fi
install -m 0644 "$SOURCE/comfyui-t4-lab.service" /etc/systemd/system/comfyui-t4-lab.service
systemctl daemon-reload
# Intentionally not enabled at boot. Starting the UI does not enqueue a job.
systemctl start comfyui-t4-lab.service
date -u +%FT%TZ > "$LAB/records/install-finished.txt"
printf 'Installed; no video generation submitted. UI: 127.0.0.1:8188\n'
