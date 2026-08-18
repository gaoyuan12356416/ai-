"""Verified immutable assets and stable v3 random-overlay recipes.

This is the prepare-only extraction of the production implementation introduced
by d3202fc.  The light assets remain in the manifest for rollback compatibility
but are intentionally excluded from v3 recipes.
"""
from __future__ import annotations
import hashlib, json, re, secrets
from pathlib import Path
from typing import Any, Mapping

ASSET_CATEGORIES=("border","light","opacity_video","corners","tint")
CATEGORIES=("border","opacity_video","corners","tint")
HEX=re.compile(r"[0-9a-f]{64}"); SAFE=re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
class RandomOverlayError(RuntimeError): pass

def sha256_file(path:Path):
    digest=hashlib.sha256(); size=0
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b""): size+=len(chunk); digest.update(chunk)
    return digest.hexdigest(),size

def load_asset_set(root:Path,expected_manifest_sha256:str):
    root=Path(root); expected=str(expected_manifest_sha256 or "").lower()
    if not root.is_absolute() or not HEX.fullmatch(expected) or not root.is_dir() or root.is_symlink(): raise RandomOverlayError("overlay root or manifest invalid")
    manifest_path=root/"manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink(): raise RandomOverlayError("overlay manifest missing")
    actual,size=sha256_file(manifest_path)
    if not secrets.compare_digest(actual,expected) or not 0<size<=2*1024*1024: raise RandomOverlayError("overlay manifest fingerprint invalid")
    try: manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception: raise RandomOverlayError("overlay manifest unreadable") from None
    if not isinstance(manifest,dict) or manifest.get("version")!=1 or set(manifest.get("categories") or {})!=set(ASSET_CATEGORIES): raise RandomOverlayError("overlay manifest contract invalid")
    resolved_root=root.resolve(strict=True); verified={}
    for category in ASSET_CATEGORIES:
        rows=manifest["categories"].get(category)
        if not isinstance(rows,list) or not rows: raise RandomOverlayError("overlay category empty")
        normalized=[]; seen=set()
        for row in rows:
            if not isinstance(row,dict) or set(row)!={"media_type","name","sha256","size"}: raise RandomOverlayError("overlay asset invalid")
            name=str(row["name"]); sha=str(row["sha256"]).lower()
            try: item_size=int(row["size"])
            except Exception: raise RandomOverlayError("overlay size invalid") from None
            if not SAFE.fullmatch(name) or name in seen or not HEX.fullmatch(sha) or not 0<item_size<=2*1024*1024*1024 or row["media_type"] not in {"image/png","video/webm"}: raise RandomOverlayError("overlay asset contract invalid")
            path=(root/name)
            if not path.is_file() or path.is_symlink(): raise RandomOverlayError("overlay asset missing")
            resolved=path.resolve(strict=True); resolved.relative_to(resolved_root); got_sha,got_size=sha256_file(resolved)
            if got_size!=item_size or not secrets.compare_digest(got_sha,sha): raise RandomOverlayError("overlay asset fingerprint mismatch")
            normalized.append({**row,"path":resolved,"size":item_size,"sha256":sha}); seen.add(name)
        verified[category]=tuple(normalized)
    return {"categories":verified,"manifest_sha256":actual,"root":resolved_root,"version":1}

def _seed(identity,label): return int.from_bytes(hashlib.sha256(json.dumps({"identity":identity,"label":label},sort_keys=True,separators=(",",":")).encode()).digest()[:8],"big")
def derive_recipe(*,job_id,content_id,profile,source_url_sha256,asset_set):
    identity={"asset_set_sha256":asset_set["manifest_sha256"],"content_id":str(content_id),"job_id":str(job_id),"profile":str(profile),"source_url_sha256":str(source_url_sha256)}; selected={}
    for category in CATEGORIES:
        rows=asset_set["categories"][category]; row=rows[_seed(identity,"asset:"+category)%len(rows)]; selected[category]={key:row[key] for key in ("media_type","name","sha256","size")}
    bounded=lambda label,low,high:low+_seed(identity,label)%(high-low+1)
    recipe={"asset_set_sha256":identity["asset_set_sha256"],"assets":selected,"rotation_millidegrees":bounded("rotation",-2000,2000),"scale_bp":bounded("scale",9800,10200),"tint_opacity_bp":bounded("tint-opacity",100,1000),"version":1}; validate_recipe(recipe,asset_set); return recipe
def validate_recipe(recipe,asset_set):
    if not isinstance(recipe,dict) or set(recipe)!={"asset_set_sha256","assets","rotation_millidegrees","scale_bp","tint_opacity_bp","version"} or recipe["version"]!=1 or recipe["asset_set_sha256"]!=asset_set["manifest_sha256"]: raise RandomOverlayError("overlay recipe invalid")
    if not -2000<=int(recipe["rotation_millidegrees"])<=2000 or not 9800<=int(recipe["scale_bp"])<=10200 or not 100<=int(recipe["tint_opacity_bp"])<=1000 or set(recipe["assets"])!=set(CATEGORIES): raise RandomOverlayError("overlay recipe values invalid")
    for category in CATEGORIES:
        row=recipe["assets"][category]; matches=[item for item in asset_set["categories"][category] if item["name"]==row.get("name")]
        if len(matches)!=1 or any(row.get(key)!=matches[0].get(key) for key in ("media_type","name","sha256","size")): raise RandomOverlayError("overlay recipe asset invalid")
def selected_asset_paths(recipe,asset_set):
    validate_recipe(recipe,asset_set); return {category:next(item["path"] for item in asset_set["categories"][category] if item["name"]==recipe["assets"][category]["name"]) for category in CATEGORIES}
