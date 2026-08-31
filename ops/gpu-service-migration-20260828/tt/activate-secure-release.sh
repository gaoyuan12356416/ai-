#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export LC_ALL=C.utf8

# Frozen activation for the DNS-pinned TT worker. The CPU coordinator must keep
# its write gate closed and all seven triggers paused. This script never opens
# that gate, resumes a trigger, submits a job, reconciles a ledger, or posts.

RUN_ID=gpu-service-migration-20260828T1502
TARGET_SHA=d05adad41a28383a5c9685e6b75c1c8581a2aa49
EXPECTED_CURRENT_SHA=9425b39fa45390b3dc107f353dc6ef436415365d
SAFE_FALLBACK_SHA=9425b39fa45390b3dc107f353dc6ef436415365d
EXPECTED_UUID=659e6f89-71fa-463d-842e-ccdf2c06e0fe
EXPECTED_TRUSTED_HOST=advertising-1306474899.cos.ap-hongkong.myqcloud.com
EXPECTED_TRUSTED_ADDRESS=169.254.0.47
EXPECTED_TRUSTED_MAPPING_SHA256=1f8a7208fe97db3a84f6343f30a673dc6f319e9b5c2edab2629e74a59dd51430
EXPECTED_CA_SHA256=b6e66569cc3d438dd5abe514d0df50005d570bfc96c14dca8f768d020cb96171
EXPECTED_CPU_CHECKPOINT_SHA256=8d317754fbe86a89b3f2e564a70aec5402f89fb73ec9a57a1d58cde31b36cb72
EXPECTED_FINAL_IMPORT_RECEIPT_SHA256=89b5967c76af3d1a062a24477da5966ff640794fcba3f33cd251d280c79a8a77
EXPECTED_CURRENT_STATE_FILE_COUNT=1798
EXPECTED_CURRENT_STATE_FINGERPRINT=48454e40e6fe73bf1c6805b71ddf88a6206956b6922cb392a2f89bec0727c8f8
EXPECTED_LOCK_FILE_COUNT=1130
EXPECTED_LOCK_FINGERPRINT=5000189e30b5a46f6530b135a53a519d13c75897a1de044e50e760a12f78526f
EXPECTED_TARGET_MANIFEST_SHA256=b23950accbb12afd78ee36afd0b9387f6d84363ab8c371130076e0fc1153b2de

ROOT=/data/tt-post-gpu
STATE_ROOT=/data/tt-post-publisher
TARGET_RELEASE="$ROOT/releases/$TARGET_SHA"
EXPECTED_CURRENT_RELEASE="$ROOT/releases/$EXPECTED_CURRENT_SHA"
SAFE_FALLBACK_RELEASE="$ROOT/releases/$SAFE_FALLBACK_SHA"
CPU_CHECKPOINT="/data/migrations/$RUN_ID/tt/coordinator-secure-activation.json"
FINAL_IMPORT_RECEIPT="/data/migrations/$RUN_ID/tt/final-target-receipt.json"
EVIDENCE_ROOT="/data/migrations/$RUN_ID/tt/secure-production-switch-$TARGET_SHA"
LOCK_FILE=/run/lock/tt-secure-production-switch.lock

if [[ "$#" -ne 1 || ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'usage: %s OPS_COMMIT\n' "$0" >&2
  exit 2
fi
OPS_COMMIT="$1"
PACKAGE_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
EXPECTED_PACKAGE_DIR="/data/migrations/$RUN_ID/tt/activation-code/$OPS_COMMIT"
TARGET_MANIFEST="$PACKAGE_DIR/cc23-release-manifest.json"

WORKERS=(tt-gpu-publisher.service tt-gpu-direct-outro.service)
TUNNELS=(tt-gpu-reverse-tunnel.service tt-gpu-direct-outro-reverse-tunnel.service)
UNITS=("${WORKERS[@]}" "${TUNNELS[@]}")

check_sha256() {
  local path="$1" expected="$2"
  [[ -f "$path" && ! -L "$path" ]]
  [[ "$(sha256sum "$path" | awk '{print $1}')" == "$expected" ]]
}

check_path_contract() {
  local path="$1" expected="$2"
  [[ ! -L "$path" ]]
  [[ "$(stat -c '%a:%u:%g:%F' "$path")" == "$expected" ]]
}

validate_cpu_checkpoint() {
  check_sha256 "$CPU_CHECKPOINT" "$EXPECTED_CPU_CHECKPOINT_SHA256"
  check_path_contract "$CPU_CHECKPOINT" '600:0:0:regular file'
  python3 - "$CPU_CHECKPOINT" <<'PY'
import datetime, json, sys
doc = json.load(open(sys.argv[1], "r", encoding="utf-8"))

def parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SystemExit("CPU coordinator timestamp is not UTC")
    for pattern in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(value, pattern).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    raise SystemExit("CPU coordinator timestamp is invalid")

if doc.get("schema") != 1 or doc.get("run_id") != "gpu-service-migration-20260828T1502" or doc.get("host") != "VM-0-108-centos":
    raise SystemExit("invalid CPU coordinator checkpoint identity")
at = parse_utc(doc.get("at_utc"))
age = (datetime.datetime.now(datetime.timezone.utc) - at).total_seconds()
if age < -60 or age > 300:
    raise SystemExit("CPU coordinator checkpoint is not fresh")
if doc.get("nginx_config_valid") is not True or doc.get("ingress_gate_503") is not True:
    raise SystemExit("CPU TT write gate is not proven closed")
probes = doc.get("write_probes")
expected_probes = {
    ("DELETE", "https", "ai.yingliangads.com", "/api/admin/tt-posts/999999999"),
    ("DELETE", "https", "ai.yingliangads.com", "/api/admin/tt-auto-publish/999999999"),
}
if not isinstance(probes, list) or len(probes) != 2 or {
    (p.get("method"), p.get("scheme"), p.get("authority"), p.get("path")) for p in probes
} != expected_probes or any(
    p.get("status") != 503
    or p.get("connected_address") != "127.0.0.1"
    or p.get("connected_port") != 443
    or p.get("tls_verify_result") != 0
    or p.get("body_sha256") != "fe8f640d75e755cbedec72922b85b07d52a626d606d5dbd8863da63ea19fc694"
    for p in probes
):
    raise SystemExit("CPU TT write probes are incomplete")
expected_units = {
    "tt-post-prepare.timer", "tt-post-prepare.path", "tt-post-runner.timer",
    "tt-post-runner.path", "tt-auto-post-scheduler.timer",
    "tt-auto-post-runner.timer", "tt-auto-post-runner.path",
}
triggers = doc.get("triggers")
if not isinstance(triggers, dict) or set(triggers) != expected_units:
    raise SystemExit("CPU TT trigger set is not exact")
if any(
    item.get("active_state") != "inactive"
    or item.get("unit_file_state") != "enabled"
    for item in triggers.values()
):
    raise SystemExit("CPU TT trigger is not paused")
state = doc.get("cpu_state")
if not isinstance(state, dict):
    raise SystemExit("CPU drain state is missing")
required_true = ("cutover_safe_after_ingress_gate", "drained", "runners_inactive",
                 "stable_publication_facts", "triggers_paused")
if any(state.get(key) is not True for key in required_true):
    raise SystemExit("CPU drain state is not safe")
if state.get("sample_count") != 3 or state.get("http_connections") != []:
    raise SystemExit("CPU drain samples or HTTP state are invalid")
state_at = parse_utc(state.get("at_utc"))
state_age = (datetime.datetime.now(datetime.timezone.utc) - state_at).total_seconds()
if state_age < -60 or state_age > 300:
    raise SystemExit("CPU drain snapshot is not fresh")
databases = state.get("databases")
expected_tables = {
    "auto": {"tt_auto_task"},
    "legacy": {
        "tt_post_direct_test", "tt_post_material_intake",
        "tt_post_queue", "tt_post_schedule_run",
    },
}
if not isinstance(databases, dict) or set(databases) != set(expected_tables):
    raise SystemExit("CPU database set is not exact")
for name, database in databases.items():
    tables = database.get("tables")
    if not isinstance(tables, dict) or set(tables) != expected_tables[name]:
        raise SystemExit("CPU publication table set is not exact")
    for table in tables.values():
        risk_keys = {
            "claims_effective", "claims_expired", "claims_invalid_lease",
            "claims_present", "executing_status", "unknown_outcome",
        }
        if not risk_keys.issubset(table) or any(int(table[key]) != 0 for key in risk_keys):
            raise SystemExit("CPU publication state contains an active or unknown item")
expected_state_units = {
    "tt-auto-post-runner.path", "tt-auto-post-runner.service",
    "tt-auto-post-runner.timer", "tt-auto-post-scheduler.service",
    "tt-auto-post-scheduler.timer", "tt-post-prepare.path",
    "tt-post-prepare.service", "tt-post-prepare.timer",
    "tt-post-profile-upgrade.service", "tt-post-runner.path",
    "tt-post-runner.service", "tt-post-runner.timer",
}
state_units = state.get("units")
if not isinstance(state_units, dict) or set(state_units) != expected_state_units:
    raise SystemExit("CPU drain unit set is not exact")
for name, item in state_units.items():
    if item.get("Id") != name or item.get("LoadState") != "loaded":
        raise SystemExit("CPU TT drain unit identity is invalid")
    if item.get("ActiveState") != "inactive" or item.get("SubState") != "dead":
        raise SystemExit("CPU TT drain unit is active")
    if name.endswith(".service") and str(item.get("MainPID")) != "0":
        raise SystemExit("CPU TT drain service still has a process")
PY
}

validate_import_receipt() {
  check_sha256 "$FINAL_IMPORT_RECEIPT" "$EXPECTED_FINAL_IMPORT_RECEIPT_SHA256"
  check_path_contract "$FINAL_IMPORT_RECEIPT" '600:0:0:regular file'
  python3 - "$FINAL_IMPORT_RECEIPT" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], "r", encoding="utf-8"))
expected = {
    "ok": True, "state_file_set_exact": True, "assets_exact": True,
    "cpu_database_imported": False, "services_started": False,
    "source_commit": "9425b39fa45390b3dc107f353dc6ef436415365d",
    "state_fingerprint": "d439c7fc231e7d42d9536953f146f037378c4a194f85251620b4259af13b545d",
    "cpu_backup_manifest_sha256": "442a25f66ef85f60472325b6b495be703ef06a26ffcbdd76234d8a104f391881",
}
if any(doc.get(key) != value for key, value in expected.items()):
    raise SystemExit("final import receipt does not match the frozen handoff")
PY
}

validate_state_idle() {
  python3 - "$STATE_ROOT" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
groups = ("manifests", "publishes", "direct-outro-work/manifests",
          "direct-outro-work/publishes")
files, locks, risk = {}, {}, []
for relative in ("jobs", "direct-outro-work/jobs"):
    folder = root / relative
    if not folder.is_dir() or folder.is_symlink():
        raise SystemExit("missing or redirected job directory")
    if any(folder.iterdir()):
        raise SystemExit("TT active job directory is not empty")
for relative in ("locks", "direct-outro-work/locks"):
    folder = root / relative
    if not folder.is_dir() or folder.is_symlink():
        raise SystemExit("missing or redirected lock directory")
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.is_symlink() or path.suffix != ".lock":
            raise SystemExit("unexpected TT lock directory member")
        raw = path.read_bytes()
        if raw:
            raise SystemExit("TT historical lock file is not empty")
        locks[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
for relative in groups:
    folder = root / relative
    if not folder.is_dir() or folder.is_symlink():
        raise SystemExit("missing or redirected state directory")
    is_publish = relative.endswith("publishes")
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.is_symlink() or path.suffix != ".json":
            raise SystemExit("unexpected TT state directory member")
        raw = path.read_bytes()
        value = json.loads(raw)
        state = str(value.get("state", value.get("status", "")))
        if not state:
            raise SystemExit("TT state file lacks status")
        if is_publish:
            if value.get("job_id") != path.stem:
                raise SystemExit("TT publish ledger identity mismatch")
            if state not in {"published", "failed", "init_rejected"}:
                risk.append((relative, path.stem, state))
        files[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
if risk:
    raise SystemExit("TT publish ledger contains a nonterminal or unknown state")
fingerprint = hashlib.sha256(json.dumps(
    files, sort_keys=True, separators=(",", ":")
).encode()).hexdigest()
lock_fingerprint = hashlib.sha256(json.dumps(
    locks, sort_keys=True, separators=(",", ":")
).encode()).hexdigest()
if len(files) != 1798 or fingerprint != "48454e40e6fe73bf1c6805b71ddf88a6206956b6922cb392a2f89bec0727c8f8":
    raise SystemExit("TT state no longer matches the frozen drained set")
if len(locks) != 1130 or lock_fingerprint != "5000189e30b5a46f6530b135a53a519d13c75897a1de044e50e760a12f78526f":
    raise SystemExit("TT historical lock set changed")
result = {"schema": 1, "file_count": len(files), "fingerprint": fingerprint,
          "lock_file_count": len(locks), "lock_fingerprint": lock_fingerprint,
          "risk_count": 0, "active_job_file_count": 0}
print(json.dumps(result, sort_keys=True))
PY
}

validate_target_release_manifest() {
  check_sha256 "$TARGET_MANIFEST" "$EXPECTED_TARGET_MANIFEST_SHA256"
  python3 - "$TARGET_MANIFEST" "$TARGET_RELEASE" "$TARGET_SHA" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path

manifest = json.load(open(sys.argv[1], "r", encoding="utf-8"))
release = Path(sys.argv[2])
commit = sys.argv[3]
if manifest.get("schema") != 1 or manifest.get("source_commit") != commit:
    raise SystemExit("target release manifest identity mismatch")
entries = manifest.get("entries")
if not isinstance(entries, dict) or len(entries) != manifest.get("entry_count"):
    raise SystemExit("target release manifest is incomplete")
fingerprint = hashlib.sha256(json.dumps(
    entries, sort_keys=True, separators=(",", ":")
).encode()).hexdigest()
if fingerprint != manifest.get("entries_fingerprint"):
    raise SystemExit("target release manifest fingerprint mismatch")
actual = {}
if release.stat().st_uid != 0 or release.stat().st_gid != 0 or stat.S_IMODE(release.stat().st_mode) != 0o755:
    raise SystemExit("target release root permissions mismatch")
for path in sorted(release.rglob("*")):
    relative = path.relative_to(release).as_posix()
    if path.is_symlink() or not path.is_file():
        if path.is_dir() and not path.is_symlink():
            if path.stat().st_uid != 0 or path.stat().st_gid != 0 or stat.S_IMODE(path.stat().st_mode) != 0o755:
                raise SystemExit("target release directory permissions mismatch")
            continue
        raise SystemExit("target release contains a redirected or special member")
    raw = path.read_bytes()
    if path.stat().st_uid != 0 or path.stat().st_gid != 0:
        raise SystemExit("target release file ownership mismatch")
    exact_mode = stat.S_IMODE(path.stat().st_mode)
    if exact_mode not in {0o644, 0o755}:
        raise SystemExit("target release file permissions mismatch")
    mode = "100755" if exact_mode == 0o755 else "100644"
    actual[relative] = {
        "mode": mode,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
if actual != entries:
    raise SystemExit("target release differs from the frozen GitHub manifest")
PY
}

validate_loaded_units() {
  local expected_unit_file_state="$1" unit property
  [[ "$expected_unit_file_state" == "enabled" || "$expected_unit_file_state" == "disabled" ]]
  for unit in "${UNITS[@]}"; do
    [[ "$(systemctl show -p FragmentPath --value "$unit")" == "/etc/systemd/system/$unit" ]]
    [[ "$(systemctl show -p UnitFileState --value "$unit")" == "$expected_unit_file_state" ]]
    [[ "$(systemctl show -p NeedDaemonReload --value "$unit")" == "no" ]]
  done
  [[ -z "$(systemctl show -p DropInPaths --value tt-gpu-reverse-tunnel.service)" ]]
  [[ -z "$(systemctl show -p DropInPaths --value tt-gpu-direct-outro-reverse-tunnel.service)" ]]
  for unit in "${WORKERS[@]}"; do
    [[ "$(systemctl show -p WorkingDirectory --value "$unit")" == "$ROOT/current" ]]
    [[ "$(systemctl show -p DropInPaths --value "$unit")" == "/etc/systemd/system/$unit.d/40-tt-private-trust.conf" ]]
    property="$(systemctl show -p ExecStart --value "$unit")"
    [[ "$property" == *"/data/tt-post-gpu/runtime/bin/python /data/tt-post-gpu/current/scripts/tt_gpu_worker.py"* ]]
  done
  property="$(systemctl show -p ExecStart --value tt-gpu-reverse-tunnel.service)"
  [[ "$property" == *"-R 127.0.0.1:18830:127.0.0.1:8830 root@43.166.187.96"* ]]
  [[ "$property" == *"-i /etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel"* ]]
  [[ "$property" == *"UserKnownHostsFile=/etc/x-post-media-repair-tunnel/known_hosts"* ]]
  [[ "$property" == *"StrictHostKeyChecking=yes"* && "$property" == *"ExitOnForwardFailure=yes"* ]]
  property="$(systemctl show -p ExecStart --value tt-gpu-direct-outro-reverse-tunnel.service)"
  [[ "$property" == *"-R 127.0.0.1:18834:127.0.0.1:8832 root@43.166.187.96"* ]]
  [[ "$property" == *"-i /etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel"* ]]
  [[ "$property" == *"UserKnownHostsFile=/etc/x-post-media-repair-tunnel/known_hosts"* ]]
  [[ "$property" == *"StrictHostKeyChecking=yes"* && "$property" == *"ExitOnForwardFailure=yes"* ]]
}

validate_no_managed_processes() {
  python3 - "$ROOT" <<'PY'
import os, sys
from pathlib import Path
root = sys.argv[1].rstrip("/") + "/"
for process in Path("/proc").iterdir():
    if not process.name.isdigit():
        continue
    try:
        cmdline = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        cwd = os.readlink(process / "cwd")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    worker = "tt_gpu_worker.py" in cmdline and (root in cmdline or cwd.startswith(root))
    tunnel = (
        "root@43.166.187.96" in cmdline
        and (
            "-R 127.0.0.1:18830:127.0.0.1:8830" in cmdline
            or "-R 127.0.0.1:18834:127.0.0.1:8832" in cmdline
        )
    )
    if worker or tunnel:
        raise SystemExit("stray TT managed process exists")
PY
}

read_only_preflight() {
  local unit
  [[ "$EUID" -eq 0 && "$(hostname)" == "VM-0-125-centos" ]]
  for command in findmnt sha256sum systemctl python3 curl ss flock ssh-keygen locale; do command -v "$command" >/dev/null; done
  [[ "$(locale charmap)" == "UTF-8" ]]
  [[ "$(python3 -c 'import sys; print(sys.getfilesystemencoding())')" == "utf-8" ]]
  [[ "$PACKAGE_DIR" == "$EXPECTED_PACKAGE_DIR" ]]
  [[ "$(readlink -f "$0")" == "$PACKAGE_DIR/activate-secure-release.sh" && ! -L "$0" ]]
  [[ -f "$PACKAGE_DIR/.source-commit" && ! -L "$PACKAGE_DIR/.source-commit" ]]
  [[ "$(tr -d '\r\n' <"$PACKAGE_DIR/.source-commit")" == "$OPS_COMMIT" ]]
  [[ "$(findmnt -n -o UUID -T /data)" == "$EXPECTED_UUID" ]]
  [[ "$(df --output=avail -k /data | tail -n 1 | tr -d ' ')" -ge 31457280 ]]
  check_path_contract /data '755:0:0:directory'
  check_path_contract "$ROOT" '700:0:0:directory'
  check_path_contract "$STATE_ROOT" '700:0:0:directory'
  check_path_contract "$ROOT/config" '700:0:0:directory'
  check_path_contract "$ROOT/ffmpeg" '700:0:0:directory'
  check_path_contract "$ROOT/ops" '700:0:0:directory'
  check_path_contract "$ROOT/trust" '700:0:0:directory'
  check_path_contract "$ROOT/assets" '700:0:0:directory'
  check_path_contract "$ROOT/runtime" '755:0:0:directory'
  check_path_contract "$ROOT/runtime/bin" '755:0:0:directory'
  check_path_contract "$STATE_ROOT/assets" '700:0:0:directory'
  check_path_contract "$STATE_ROOT/random-overlay-assets" '700:0:0:directory'
  check_path_contract "$STATE_ROOT/random-overlay-assets/v1" '700:0:0:directory'
  [[ -d "$ROOT" && ! -L "$ROOT" && "$(readlink -f "$ROOT")" == "$ROOT" ]]
  [[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && "$(readlink -f "$STATE_ROOT")" == "$STATE_ROOT" ]]
  [[ -d "$TARGET_RELEASE" && ! -L "$TARGET_RELEASE" ]]
  [[ -d "$EXPECTED_CURRENT_RELEASE" && ! -L "$EXPECTED_CURRENT_RELEASE" ]]
  [[ -d "$SAFE_FALLBACK_RELEASE" && ! -L "$SAFE_FALLBACK_RELEASE" ]]
  [[ "$(readlink -f "$ROOT/current")" == "$EXPECTED_CURRENT_RELEASE" ]]
  [[ "$(tr -d '\r\n' <"$TARGET_RELEASE/.source-commit")" == "$TARGET_SHA" ]]
  [[ "$(tr -d '\r\n' <"$SAFE_FALLBACK_RELEASE/.source-commit")" == "$SAFE_FALLBACK_SHA" ]]
  validate_target_release_manifest
  check_sha256 "$TARGET_RELEASE/features/tt_gpu/worker.py" de1e61f1286f50c5f84e559e5f62b28b4c725e78ac7cd2dbeedc433491723967
  check_sha256 "$TARGET_RELEASE/scripts/tt_gpu_worker.py" b14783bbbff98aa9886c081da501d892243bdf85d617811d9f9203b652ad3198
  check_sha256 "$SAFE_FALLBACK_RELEASE/features/tt_gpu/worker.py" fad7e217d2ac975a5b68be828bba7b6d28d7cf5ed81f59e7e3c56281d77f0b05
  check_sha256 "$SAFE_FALLBACK_RELEASE/scripts/tt_gpu_worker.py" cc4a8c3e6ece6dfb5210b8d50c35f9d457cd7f92602d8ed401db98e850645fea
  check_sha256 "$ROOT/config/base.env" b209d31abc4d89348b9507d2e1e84ab02ef86b68f73fad7cb2e83333c39d11f9
  check_sha256 "$ROOT/config/direct-outro.env" e644704ea9313b25c507343bcff54fe5df5bccfb838b92bc27b6f9ae9d641dc6
  check_sha256 "$ROOT/config/secrets.env" 1c55981f1aa6527fe70494f70693e33de6beec916beebf555f32ee69605481e8
  check_sha256 "$ROOT/trust/ca-bundle.pem" "$EXPECTED_CA_SHA256"
  check_path_contract "$ROOT/config/base.env" '600:0:0:regular file'
  check_path_contract "$ROOT/config/direct-outro.env" '600:0:0:regular file'
  check_path_contract "$ROOT/config/secrets.env" '600:0:0:regular file'
  check_path_contract "$ROOT/trust/ca-bundle.pem" '644:0:0:regular file'
  check_sha256 /etc/systemd/system/tt-gpu-publisher.service aa59c710840c387adf43127ad29edb385c9aabf6c8a21b748813e1d12359501c
  check_sha256 /etc/systemd/system/tt-gpu-direct-outro.service 1eaf0d1a21d9ecc8e4b44168cf3083a7ac73263d80eba289d4168d740befcbfa
  check_sha256 /etc/systemd/system/tt-gpu-reverse-tunnel.service 75b568a643ae940c35df7fbade40cdfffaf110516b268b7b786edaee6017c9ab
  check_sha256 /etc/systemd/system/tt-gpu-direct-outro-reverse-tunnel.service fbd63b7a45857c2502e57fc8cca5a39e59676f2946e3ba7448d4ea07912c59a5
  check_sha256 /etc/systemd/system/tt-gpu-publisher.service.d/40-tt-private-trust.conf a73002bc8bd207c5d807e29f3ce1d35aa533322d62f2bcc79fbb1d1d7fb93ad2
  check_sha256 /etc/systemd/system/tt-gpu-direct-outro.service.d/40-tt-private-trust.conf a73002bc8bd207c5d807e29f3ce1d35aa533322d62f2bcc79fbb1d1d7fb93ad2
  check_sha256 "$ROOT/ops/tt_migration.py" 408c79c1d92af2bd275473ec25ee99073c62a48ddae218c70addaac9b4c4e5d9
  check_sha256 "$ROOT/ops/verify_trust.py" 97fc7914ef16a85985daf0dfa3829f1bbdaa9b6a13f9f542b0583840412cc967
  check_sha256 "$ROOT/ffmpeg/ffmpeg" 9127e8a64b65c48f769c228475e4498406db6d016d892e285ef51efb21c957ce
  check_sha256 "$ROOT/ffmpeg/ffmpeg.bin" c34815e5271aecd549e2334a659eebee62de5c86f763d1f15026b11582f1184d
  check_sha256 "$ROOT/ffmpeg/ffprobe" bf7b813bb81f01695a38841e697d6fd858c194baf13017e78c2855af502e644a
  check_path_contract "$ROOT/ops/tt_migration.py" '644:0:0:regular file'
  check_path_contract "$ROOT/ops/verify_trust.py" '644:0:0:regular file'
  check_path_contract "$ROOT/ffmpeg/ffmpeg" '755:0:0:regular file'
  check_path_contract "$ROOT/ffmpeg/ffmpeg.bin" '755:0:0:regular file'
  check_path_contract "$ROOT/ffmpeg/ffprobe" '755:0:0:regular file'
  [[ -x "$ROOT/ffmpeg/ffmpeg" && -x "$ROOT/ffmpeg/ffmpeg.bin" && -x "$ROOT/ffmpeg/ffprobe" ]]
  check_sha256 "$ROOT/assets/DejaVuSans-Bold.ttf" b6589ec47b9332395fcb47413a2bd14eb9eb8ed06ab14dbf3dab1678e77a939e
  check_path_contract "$ROOT/assets/DejaVuSans-Bold.ttf" '644:0:0:regular file'
  check_sha256 "$STATE_ROOT/assets/TT-new-outro.mp4" b6efd06c9304380aa118c4c3963057cc82e10ab569caa97d0cd9aeef588fe1fc
  check_sha256 "$STATE_ROOT/assets/dramawave-logo-rounded.png" 3a159c7ec57d5ce526cb2bb406ddf364937495dd3e2f97dba0697c4339d6ad75
  check_path_contract "$STATE_ROOT/assets/TT-new-outro.mp4" '600:0:0:regular file'
  check_path_contract "$STATE_ROOT/assets/dramawave-logo-rounded.png" '600:0:0:regular file'
  check_sha256 "$ROOT/requirements.lock" 770bddd673dc32e811cb0872dca8252233be0ee51ade6570dd9912e467316211
  check_path_contract "$ROOT/requirements.lock" '600:0:0:regular file'
  check_sha256 "$ROOT/runtime/bin/python" 614e97717a91e5d74c5e65a74bf25e01fc18fab375139e215f4ddbe8d133cb19
  check_path_contract "$ROOT/runtime/bin/python" '755:0:0:regular file'
  "$ROOT/runtime/bin/python" - <<'PY'
import importlib.metadata as metadata
import sys
expected = {
    "pycryptodome": "3.23.0",
    "cos-python-sdk-v5": "1.9.42",
    "requests": "2.33.1",
    "certifi": "2026.4.22",
    "urllib3": "2.6.3",
}
if sys.version.split()[0] != "3.10.20":
    raise SystemExit("TT runtime Python version changed")
if sys.executable != "/data/tt-post-gpu/runtime/bin/python":
    raise SystemExit("TT runtime executable changed")
if sys.prefix != "/data/tt-post-gpu/runtime":
    raise SystemExit("TT runtime prefix changed")
if any(metadata.version(name) != version for name, version in expected.items()):
    raise SystemExit("TT runtime dependency version changed")
PY
  PYTHONDONTWRITEBYTECODE=1 "$ROOT/runtime/bin/python" - "$TARGET_RELEASE" <<'PY'
import sys
import urllib.request

sys.path.insert(0, sys.argv[1])
from features.tt_gpu import worker

api = worker.TikTokContentPostingAPI()
if api.opener is not worker._TIKTOK_NO_REDIRECT_OPENER:
    raise SystemExit("TT default posting client opener mismatch")
if any(isinstance(handler, urllib.request.ProxyHandler) for handler in api.opener.handlers):
    raise SystemExit("TT default posting client retained a proxy handler")
redirects = [
    handler for handler in api.opener.handlers
    if isinstance(handler, worker._TikTokNoRedirect)
]
if len(redirects) != 1 or redirects[0].redirect_request(
    None, None, 302, "Found", {}, "https://redirect.invalid/"
) is not None:
    raise SystemExit("TT default posting client redirect policy mismatch")
PY
  [[ -f /etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel && ! -L /etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel ]]
  [[ -f /etc/x-post-media-repair-tunnel/known_hosts && ! -L /etc/x-post-media-repair-tunnel/known_hosts ]]
  [[ "$(stat -c '%a:%U:%G' /etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel)" == "400:root:root" ]]
  [[ "$(stat -c '%a:%U:%G' /etc/x-post-media-repair-tunnel/known_hosts)" == "644:root:root" ]]
  check_sha256 /etc/x-post-media-repair-tunnel/known_hosts 1e99a8797cf69938c63f1cbd152cf40e63186c586f6ebb1cb5526a6acc51d98b
  [[ "$(ssh-keygen -y -f /etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel | ssh-keygen -lf - | awk '{print $2}')" == "SHA256:+nkbj63n9YgoP++S+2f+B9O6GLrAlzoHLIWeBKRJnhg" ]]
  validate_loaded_units disabled
  for unit in "${UNITS[@]}"; do
    [[ "$(systemctl show -p ActiveState --value "$unit")" == "inactive" ]]
    [[ "$(systemctl show -p MainPID --value "$unit")" == "0" ]]
  done
  validate_no_managed_processes
  [[ -z "$(ss -H -ltnp 'sport = :8830')" ]]
  [[ -z "$(ss -H -ltnp 'sport = :8832')" ]]
  validate_cpu_checkpoint
  validate_import_receipt
  validate_state_idle >/dev/null
  python3 - "$ROOT/config/base.env" "$EXPECTED_TRUSTED_HOST" "$EXPECTED_TRUSTED_ADDRESS" "$EXPECTED_TRUSTED_MAPPING_SHA256" <<'PY'
import hashlib, sys
path, expected_host, expected_address, expected_sha = sys.argv[1:]
values = {}
for raw in open(path, "r", encoding="utf-8"):
    line = raw.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1); values[key] = value
mapping = f"{expected_host}={expected_address}"
if values.get("TT_POST_GPU_TRUSTED_SOURCE_RESOLUTIONS") != mapping or hashlib.sha256(mapping.encode()).hexdigest() != expected_sha:
    raise SystemExit("trusted source mapping mismatch")
expected = {"TT_POST_GPU_MEDIA_MODE": "random_overlay", "TT_POST_GPU_PORT": "8830",
 "TT_POST_GPU_WORK_ROOT": "/data/tt-post-publisher", "TT_POST_GPU_VIDEO_ENCODER": "hevc_nvenc",
 "TT_POST_GPU_STORAGE_BACKEND": "cos", "TT_POST_LIVE_ENABLED": "1",
 "TT_POST_MANUAL_CANARY_ENABLED": "0", "TT_POST_DIRECT_AUDIT_APPROVED": "1",
 "TT_POST_URL_PROPERTY_VERIFIED": "1",
 "TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256": "028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f"}
if any(values.get(key) != value for key, value in expected.items()):
    raise SystemExit("frozen TT base configuration mismatch")
PY
}

atomic_current_link() {
  local release="$1" temporary="$ROOT/.current-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  [[ ! -e "$temporary" && ! -L "$temporary" ]]
  ln -s -- "$release" "$temporary"
  if mv -Tf -- "$temporary" "$ROOT/current"; then
    return 0
  fi
  rm -f -- "$temporary"
  return 1
}

MUTATION_STARTED=0 SUCCESS=0 EVIDENCE_DIR= BACKUP_DIR=
rollback_after_failure() {
  local original_rc="$1" stop_command_ok=true disable_command_ok=true units_stopped=true fallback_linked=true unit attempt
  set +e
  trap '' HUP INT TERM
  systemctl stop "${TUNNELS[@]}" "${WORKERS[@]}" || stop_command_ok=false
  systemctl disable "${TUNNELS[@]}" "${WORKERS[@]}" || disable_command_ok=false
  for attempt in $(seq 1 30); do
    units_stopped=true
    for unit in "${UNITS[@]}"; do
      if [[ "$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)" != "inactive" \
        || "$(systemctl show -p SubState --value "$unit" 2>/dev/null)" != "dead" \
        || "$(systemctl show -p MainPID --value "$unit" 2>/dev/null)" != "0" \
        || "$(systemctl show -p ControlPID --value "$unit" 2>/dev/null)" != "0" \
        || -n "$(systemctl show -p ControlGroup --value "$unit" 2>/dev/null)" \
        || "$(systemctl show -p UnitFileState --value "$unit" 2>/dev/null)" != "disabled" ]]; then units_stopped=false; fi
    done
    [[ "$units_stopped" == true ]] && break
    sleep 0.5
  done
  validate_no_managed_processes || units_stopped=false
  if [[ "$units_stopped" == true ]]; then
    atomic_current_link "$SAFE_FALLBACK_RELEASE" || fallback_linked=false
    [[ "$(readlink -f "$ROOT/current" 2>/dev/null)" == "$SAFE_FALLBACK_RELEASE" ]] || fallback_linked=false
  else
    fallback_linked=false
  fi
  if [[ -n "$EVIDENCE_DIR" && -d "$EVIDENCE_DIR" ]]; then
    rm -f "$EVIDENCE_DIR/deployment-result.json" "$EVIDENCE_DIR/evidence-sha256.txt"
    python3 - "$EVIDENCE_DIR/rollback-result.json" "$original_rc" "$stop_command_ok" "$disable_command_ok" "$units_stopped" "$fallback_linked" <<'PY'
import json, os, sys
from pathlib import Path
output = Path(sys.argv[1]); values = [value == "true" for value in sys.argv[3:]]
doc = {"schema": 1, "result": "rollback_complete" if all(values) else "rollback_incomplete",
       "original_exit_code": int(sys.argv[2]), "stop_command_ok": values[0],
       "disable_command_ok": values[1], "units_stopped": values[2],
       "fallback_linked": values[3], "units_disabled": values[1] and values[2],
       "cpu_gate_action": "none"}
output.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n"); os.chmod(output, 0o600)
PY
    (cd "$EVIDENCE_DIR" && find . -maxdepth 1 -type f ! -name evidence-sha256.txt -print0 | sort -z | xargs -0 sha256sum >evidence-sha256.txt) || true
  fi
  [[ "$stop_command_ok" == true && "$disable_command_ok" == true && "$units_stopped" == true && "$fallback_linked" == true ]] || return 1
  return "$original_rc"
}
on_exit() {
  local rc="$?"
  trap - EXIT ERR INT TERM HUP
  if [[ "$SUCCESS" -eq 1 ]]; then exit 0; fi
  if [[ "$MUTATION_STARTED" -eq 1 ]]; then rollback_after_failure "$rc"; rc="$?"; fi
  exit "$rc"
}
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
trap on_exit EXIT

read_only_preflight
exec 9>"$LOCK_FILE"
flock -n 9
read_only_preflight

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="$EVIDENCE_ROOT/$STAMP"
BACKUP_DIR="$ROOT/backups/$STAMP-pre-$TARGET_SHA"
mkdir -p "$EVIDENCE_ROOT"; chmod 0700 "$EVIDENCE_ROOT"
mkdir "$EVIDENCE_DIR"; mkdir "$BACKUP_DIR"; chmod 0700 "$EVIDENCE_DIR" "$BACKUP_DIR"
printf '%s\n' "$(readlink -f "$ROOT/current")" >"$BACKUP_DIR/current-before.txt"
cp -a "$ROOT/config/base.env" "$ROOT/config/direct-outro.env" "$BACKUP_DIR/"
for unit in "${UNITS[@]}"; do cp -a "/etc/systemd/system/$unit" "$BACKUP_DIR/"; done
cp -a /etc/systemd/system/tt-gpu-publisher.service.d "$BACKUP_DIR/"
cp -a /etc/systemd/system/tt-gpu-direct-outro.service.d "$BACKUP_DIR/"
chmod -R go-rwx "$BACKUP_DIR"
sha256sum "$ROOT/config/base.env" "$ROOT/config/direct-outro.env" "$ROOT/config/secrets.env" "$ROOT/trust/ca-bundle.pem" \
  "/etc/systemd/system/tt-gpu-publisher.service" "/etc/systemd/system/tt-gpu-direct-outro.service" \
  "/etc/systemd/system/tt-gpu-reverse-tunnel.service" "/etc/systemd/system/tt-gpu-direct-outro-reverse-tunnel.service" \
  /etc/systemd/system/tt-gpu-publisher.service.d/* /etc/systemd/system/tt-gpu-direct-outro.service.d/* >"$BACKUP_DIR/configuration-sha256.txt"
systemctl show -p Id,ActiveState,SubState,MainPID,NRestarts,FragmentPath,DropInPaths,NeedDaemonReload,UnitFileState "${UNITS[@]}" >"$EVIDENCE_DIR/systemd-before.txt"
PRE_STATE_JSON="$(validate_state_idle)"; printf '%s\n' "$PRE_STATE_JSON" >"$EVIDENCE_DIR/state-before.json"
cp -a "$CPU_CHECKPOINT" "$EVIDENCE_DIR/coordinator-secure-activation.json"

MUTATION_STARTED=1
systemctl enable "${UNITS[@]}"
validate_loaded_units enabled
atomic_current_link "$TARGET_RELEASE"
systemctl start "${WORKERS[@]}"

wait_health() {
  local port="$1" expected_profile="$2" expected_mode="$3" expected_asset_sha="$4" expected_transition="$5" output="$6" attempt
  for attempt in $(seq 1 40); do
    if curl --noproxy '*' --silent --show-error --fail --max-time 2 "http://127.0.0.1:${port}/health" >"$output.tmp" 2>/dev/null; then
      if python3 - "$output.tmp" "$expected_profile" "$expected_mode" "$expected_asset_sha" "$expected_transition" <<'PY'
import json, sys
body = json.load(open(sys.argv[1], "r", encoding="utf-8"))
expected_gates = {"TT_POST_LIVE_ENABLED": True, "TT_POST_DIRECT_AUDIT_APPROVED": True,
                  "TT_POST_URL_PROPERTY_VERIFIED": True, "ready": True}
expected_manual = {"active": False, "enabled": False,
                   "privacy_level": "SELF_ONLY", "test_bypass": False}
if body.get("status") != "ok" or body.get("profile") != sys.argv[2] or body.get("media_mode") != sys.argv[3]: raise SystemExit(1)
if body.get("storage_backend") != "cos" or body.get("gates") != expected_gates: raise SystemExit(1)
if body.get("random_overlay_asset_set_sha256") != sys.argv[4] or body.get("direct_post_eligible") is not True: raise SystemExit(1)
if body.get("asset_identity_ready") is not True or body.get("storage", {}).get("backend") != "cos": raise SystemExit(1)
if body.get("brand_overlay_review_required") is not False or body.get("local_origin_enabled") is not False: raise SystemExit(1)
if body.get("manual_canary") != expected_manual or body.get("transition") != sys.argv[5]: raise SystemExit(1)
if body.get("storage", {}).get("local_origin_enabled") is not False: raise SystemExit(1)
PY
      then mv -f "$output.tmp" "$output"; return 0; fi
    fi
    rm -f "$output.tmp"; sleep 0.5
  done
  return 1
}
wait_health 8830 tt-post-random-overlay-hevc-720x1280-v3 random_overlay 028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f none "$EVIDENCE_DIR/random-overlay-health.json"
wait_health 8832 tt-post-direct-outro-hevc-720x1280-v2 direct_outro "" phone-match-0.9s "$EVIDENCE_DIR/direct-outro-health.json"

python3 - "$TARGET_RELEASE" "$EXPECTED_TRUSTED_HOST" "$EXPECTED_TRUSTED_ADDRESS" "$EXPECTED_CA_SHA256" "${WORKERS[@]}" <<'PY'
import hashlib, os, subprocess, sys
from pathlib import Path
target, expected_mapping, expected_ca_sha, units = sys.argv[1], f"{sys.argv[2]}={sys.argv[3]}", sys.argv[4], sys.argv[5:]
expected = {"tt-gpu-publisher.service": {"TT_POST_GPU_PORT": "8830", "TT_POST_GPU_WORK_ROOT": "/data/tt-post-publisher", "TT_POST_GPU_MEDIA_MODE": "random_overlay"},
            "tt-gpu-direct-outro.service": {"TT_POST_GPU_PORT": "8832", "TT_POST_GPU_WORK_ROOT": "/data/tt-post-publisher/direct-outro-work", "TT_POST_GPU_MEDIA_MODE": "direct_outro"}}
for unit in units:
    pid = int(subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", unit], universal_newlines=True).strip())
    if pid <= 0 or os.readlink(f"/proc/{pid}/cwd") != target: raise SystemExit(f"{unit}: process cwd mismatch")
    env = {}
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1); env[key.decode()] = value.decode()
    required = dict(expected[unit]); required.update({"TT_POST_GPU_TRUSTED_SOURCE_RESOLUTIONS": expected_mapping,
      "TT_POST_GPU_VIDEO_ENCODER": "hevc_nvenc", "TT_POST_GPU_STORAGE_BACKEND": "cos",
      "SSL_CERT_FILE": "/data/tt-post-gpu/trust/ca-bundle.pem", "SSL_CERT_DIR": "/data/tt-post-gpu/trust/certs"})
    if any(env.get(key) != value for key, value in required.items()): raise SystemExit(f"{unit}: process environment mismatch")
if hashlib.sha256(Path("/data/tt-post-gpu/trust/ca-bundle.pem").read_bytes()).hexdigest() != expected_ca_sha: raise SystemExit("CA digest changed")
PY

for specification in "tt-gpu-publisher.service:8830" "tt-gpu-direct-outro.service:8832"; do
  unit="${specification%%:*}"; port="${specification##*:}"; pid="$(systemctl show -p MainPID --value "$unit")"; listener="$(ss -H -ltnp "sport = :$port")"
  [[ "$listener" == *"127.0.0.1:$port"* && "$listener" == *"pid=$pid,"* ]]
done

systemctl start "${TUNNELS[@]}"
for attempt in $(seq 1 15); do
  for unit in "${UNITS[@]}"; do systemctl is-active --quiet "$unit"; [[ "$(systemctl show -p MainPID --value "$unit")" -gt 0 ]]; [[ "$(systemctl show -p NRestarts --value "$unit")" == "0" ]]; done
  sleep 1
done
for unit in "${UNITS[@]}"; do systemctl is-active --quiet "$unit"; [[ "$(systemctl show -p MainPID --value "$unit")" -gt 0 ]]; [[ "$(systemctl show -p NRestarts --value "$unit")" == "0" ]]; done

POST_STATE_JSON="$(validate_state_idle)"; printf '%s\n' "$POST_STATE_JSON" >"$EVIDENCE_DIR/state-after.json"
cmp -s "$EVIDENCE_DIR/state-before.json" "$EVIDENCE_DIR/state-after.json"
systemctl show -p Id,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,FragmentPath,DropInPaths,NeedDaemonReload,UnitFileState "${UNITS[@]}" >"$EVIDENCE_DIR/systemd-after.txt"
cp -a -- "$0" "$EVIDENCE_DIR/activate-secure-release.sh"
(cd "$EVIDENCE_DIR"; find . -maxdepth 1 -type f ! -name evidence-sha256.txt ! -name deployment-result.json -print0 | sort -z | xargs -0 sha256sum >evidence-sha256.txt)
python3 - "$EVIDENCE_DIR/deployment-result.json" "$BACKUP_DIR" "$TARGET_SHA" "$EXPECTED_TRUSTED_ADDRESS" "$OPS_COMMIT" <<'PY'
import hashlib, json, os, sys
from pathlib import Path
output = Path(sys.argv[1]); result = {"schema": 1, "result": "hk_local_activation_passed", "target_sha": sys.argv[3], "ops_commit": sys.argv[5],
 "trusted_address_sha256": hashlib.sha256(sys.argv[4].encode()).hexdigest(), "backup_dir": sys.argv[2],
 "state_unchanged": True, "risk_count": 0, "active_job_file_count": 0, "cpu_gate_action": "none",
 "end_to_end_verified": False, "script_issued_post_request": False}
temporary = output.with_suffix(".json.tmp")
if output.exists() or temporary.exists(): raise SystemExit("activation result already exists")
temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
os.chmod(temporary, 0o600); os.replace(temporary, output)
PY
SUCCESS=1
trap - EXIT ERR INT TERM HUP
