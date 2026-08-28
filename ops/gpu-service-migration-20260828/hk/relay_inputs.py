#!/usr/bin/env python3
"""Relay existing runtime/media without local secrets or production activation."""
import argparse
import hashlib
import json
import pathlib
import shlex
import time

import paramiko

RUN_ID = "gpu-service-migration-20260828T1502"
BASE = "/data/migrations/" + RUN_ID + "/hk-inputs"
US = "43.166.178.132"
HK = "43.154.250.89"


def connect(host, key, known_hosts):
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        host, username="root", key_filename=str(key),
        allow_agent=False, look_for_keys=False, timeout=20,
        banner_timeout=20, auth_timeout=20,
    )
    return client


def execute(client, script, timeout=300):
    stdin, stdout, stderr = client.exec_command("python3 -", timeout=timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if code:
        raise RuntimeError("remote input preparation failed: " + err[-1200:])
    return json.loads(out)


def transfer(source, target, remote_path):
    """The bytes only pass through memory; no credentials/runtime land locally."""
    size = source.stat(remote_path).st_size
    digest = hashlib.sha256()
    started = time.monotonic()
    last_notice = started
    done = 0
    with source.open(remote_path, "rb") as src, target.open(remote_path + ".partial", "wb") as dst:
        src.prefetch(file_size=size, max_concurrent_requests=64)
        dst.set_pipelined(True)
        while True:
            block = src.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            dst.write(block)
            done += len(block)
            if time.monotonic() - last_notice > 20:
                print(json.dumps({"transfer": pathlib.PurePosixPath(remote_path).name,
                                  "bytes": done, "total": size}), flush=True)
                last_notice = time.monotonic()
    if done != size:
        raise RuntimeError("incomplete relay")
    target.chmod(remote_path + ".partial", 0o600)
    target.posix_rename(remote_path + ".partial", remote_path)
    return {"name": pathlib.PurePosixPath(remote_path).name,
            "bytes": done, "sha256": digest.hexdigest()}


def require_ad_stopped(client):
    for unit in ["ad-material-generation.service", "ad-material-vision.service"]:
        _, stdout, _ = client.exec_command("systemctl is-active " + shlex.quote(unit))
        state = stdout.read().decode().strip()
        if state not in {"inactive", "failed", "unknown"}:
            raise RuntimeError("ad service must be stopped: " + unit)


def prepare(us, hk):
    script = r'''
import hashlib,json,os,pathlib,subprocess
base=pathlib.Path(BASE);base.mkdir(parents=True,exist_ok=True);os.chmod(str(base),0o700)
inputs={
 'ad-runtime.tgz':['usr/bin/node','usr/lib/node_modules/@openai/codex'],
 'ad-data.tgz':['root/ad_material_generation_jobs','root/ad_material_vision_jobs',
                'root/ad_material_generation_workspace','usr/share/nginx/html/ad-material-generation'],
 'x-us-history.tgz':['data/x-post-media-repair'],
}
items=[]
for name,paths in inputs.items():
 target=base/name
 if not target.exists():
  subprocess.check_call(['tar','-czf',str(target)+'.partial','-C','/']+paths)
  os.chmod(str(target)+'.partial',0o600);os.replace(str(target)+'.partial',str(target))
 digest=hashlib.sha256()
 with target.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
 items.append({'name':name,'bytes':target.stat().st_size,'sha256':digest.hexdigest()})
print(json.dumps({'base':str(base),'files':items}))
'''.replace("BASE", repr(BASE))
    source_manifest = execute(us, script, 600)
    execute(hk, "import os,json\nos.makedirs(%r,exist_ok=True)\nos.chmod(%r,0o700)\nprint(json.dumps({'ok':True}))\n" % (BASE, BASE))
    with us.open_sftp() as src, hk.open_sftp() as dst:
        results = [transfer(src, dst, BASE + "/" + item["name"]) for item in source_manifest["files"]]
    if results != source_manifest["files"]:
        raise RuntimeError("source/relay hash mismatch")
    target_check = execute(hk, r'''
import hashlib,json,pathlib
base=pathlib.Path(BASE);items=[]
for name in ['ad-runtime.tgz','ad-data.tgz','x-us-history.tgz']:
 p=base/name;h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 items.append({'name':name,'bytes':p.stat().st_size,'sha256':h.hexdigest()})
(base/'transfer.json').write_text(json.dumps({'files':items},indent=2)+'\n')
print(json.dumps({'files':items}))
'''.replace("BASE", repr(BASE)), 180)
    if target_check["files"] != results:
        raise RuntimeError("destination hash mismatch")
    return {"ok": True, "phase": "inputs_only_no_activation", "files": results}


def prepare_x_history(us, hk):
    """Independent small-history relay while larger ad inputs continue."""
    path = BASE + "/x-us-history.tgz"
    probe = r'''
import hashlib,json,pathlib
p=pathlib.Path(PATH);h=hashlib.sha256()
with p.open('rb') as f:
 for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
print(json.dumps({'name':p.name,'bytes':p.stat().st_size,'sha256':h.hexdigest()}))
'''.replace("PATH", repr(path))
    expected = execute(us, probe)
    execute(hk, "import os,json\nos.makedirs(%r,exist_ok=True)\nos.chmod(%r,0o700)\nprint(json.dumps({'ok':True}))" % (BASE, BASE))
    with us.open_sftp() as source, hk.open_sftp() as target:
        actual = transfer(source, target, path)
    if actual != expected or execute(hk, probe) != expected:
        raise RuntimeError("independent X history transfer integrity mismatch")
    result = {"files": [expected], "phase": "x_history_verified_no_activation"}
    execute(hk, "import pathlib,json,os\np=pathlib.Path(%r);p.write_text(%r)\nos.chmod(str(p),0o600)\nprint(json.dumps({'ok':True}))" %
            (path + ".verified.json", json.dumps(result, indent=2) + "\n"))
    return result


def copy_auth(us, hk):
    """Only after both US ad units are stopped. No OAuth call is made."""
    require_ad_stopped(us)
    require_ad_stopped(hk)
    execute(hk, "import pathlib,os,json\np=pathlib.Path('/data/ad-material/auth-source');p.mkdir(parents=True,exist_ok=True);os.chmod(str(p),0o700)\nprint(json.dumps({'ok':True}))")
    with us.open_sftp() as source, hk.open_sftp() as target:
        with source.open("/root/.codex/auth.json", "rb") as src:
            payload = src.read(1024 * 1024)
        parsed = json.loads(payload)
        if parsed.get("auth_mode") != "chatgpt" or not isinstance(parsed.get("tokens"), dict):
            raise RuntimeError("unexpected authentication shape")
        destination = "/data/ad-material/auth-source/auth.json"
        try:
            existing = target.stat(destination)
        except FileNotFoundError:
            existing = None
        if existing:
            with target.open(destination, "rb") as src, target.open(BASE + "/auth-before-recopy.json", "wb") as dst:
                dst.write(src.read())
            target.chmod(BASE + "/auth-before-recopy.json", 0o600)
        with target.open(destination + ".partial", "wb") as dst:
            dst.write(payload)
        target.chmod(destination + ".partial", 0o600)
        target.posix_rename(destination + ".partial", destination)
        with target.open(destination, "rb") as reread:
            if hashlib.sha256(reread.read()).digest() != hashlib.sha256(payload).digest():
                raise RuntimeError("auth transfer integrity failure")
    result = {"ok": True, "phase": "auth_copied_no_refresh",
              "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
              "source_ad_services_stopped": True}
    execute(hk, "import pathlib,json\np=pathlib.Path(%r);p.write_text(%r)\nprint(json.dumps({'ok':True}))" %
            (BASE + "/auth-transfer.json", json.dumps(result) + "\n"))
    return result




def final_ad_data(us, hk):
    require_ad_stopped(us)
    require_ad_stopped(hk)
    source_roots = {
        "generation/jobs": "/root/ad_material_generation_jobs",
        "vision/jobs": "/root/ad_material_vision_jobs",
        "generation/workspace": "/root/ad_material_generation_workspace",
        "generation/public": "/usr/share/nginx/html/ad-material-generation",
    }
    target_roots = {key: "/data/ad-material/" + key for key in source_roots}
    scanner = r'''
import hashlib,json,os,pathlib
roots=ROOTS;files={}
for key,base in roots.items():
 for folder,dirs,names in os.walk(base):
  for name in dirs+names:
   if pathlib.Path(folder,name).is_symlink():raise RuntimeError('symlink in final ad snapshot')
  for name in names:
   path=pathlib.Path(folder,name);h=hashlib.sha256()
   with path.open('rb') as f:
    for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
   files[key+'/'+str(path.relative_to(base))]={'sha256':h.hexdigest(),'bytes':path.stat().st_size}
print(json.dumps({'files':files}))
'''
    expected = execute(us, scanner.replace("ROOTS", repr(source_roots)), 120)["files"]
    actual = execute(hk, scanner.replace("ROOTS", repr(target_roots)), 120)["files"]
    changed = [name for name, item in expected.items() if actual.get(name) != item]
    parents = sorted({str(pathlib.PurePosixPath("/data/ad-material", n).parent) for n in changed})
    execute(hk, "import pathlib,json,os\nfor p in %r:\n pathlib.Path(p).mkdir(parents=True,exist_ok=True)\n os.chmod(p,0o700)\nprint(json.dumps({'ok':True}))" % parents)
    with us.open_sftp() as source, hk.open_sftp() as target:
        for name in changed:
            prefix = next(key for key in source_roots if name.startswith(key + "/"))
            relative = name[len(prefix)+1:]
            old_path = source_roots[prefix] + "/" + relative
            new_path = "/data/ad-material/" + name
            with source.open(old_path, "rb") as src, target.open(new_path + ".partial", "wb") as dst:
                src.prefetch(file_size=expected[name]["bytes"], max_concurrent_requests=64)
                dst.set_pipelined(True)
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
            target.chmod(new_path + ".partial", 0o600)
            target.posix_rename(new_path + ".partial", new_path)
    actual = execute(hk, scanner.replace("ROOTS", repr(target_roots)), 120)["files"]
    if any(actual.get(name) != item for name, item in expected.items()):
        raise RuntimeError("final ad data did not reconcile")
    result = {"ok": True, "phase": "final_ad_data_synced", "source_files": len(expected),
              "updated_files": len(changed), "extra_hk_files_retained": len(set(actual)-set(expected)),
              "source_ad_services_stopped": True}
    execute(hk, "import pathlib,json\npathlib.Path(%r).write_text(%r)\nprint(json.dumps({'ok':True}))" %
            (BASE + "/ad-final-sync.json", json.dumps(result) + "\n"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare-inputs", "prepare-x-history", "final-ad-data-after-source-stop", "copy-auth-after-source-stop"])
    parser.add_argument("--key", type=pathlib.Path, default=pathlib.Path.home()/".ssh"/"id_ed25519_codex_remote")
    parser.add_argument("--known-hosts", type=pathlib.Path, default=pathlib.Path.home()/".ssh"/"known_hosts")
    args = parser.parse_args()
    us = connect(US, args.key, args.known_hosts)
    hk = connect(HK, args.key, args.known_hosts)
    try:
        if args.action == "prepare-inputs":
            result = prepare(us, hk)
        elif args.action == "prepare-x-history":
            result = prepare_x_history(us, hk)
        elif args.action == "final-ad-data-after-source-stop":
            result = final_ad_data(us, hk)
        else:
            result = copy_auth(us, hk)
        print(json.dumps(result), flush=True)
    finally:
        us.close()
        hk.close()


if __name__ == "__main__":
    main()
