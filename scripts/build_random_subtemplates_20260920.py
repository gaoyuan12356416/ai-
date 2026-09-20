"""Append 42 procedural assets while preserving the exact September 18 catalog.

At most two encoder workers run at once. Completed files are reusable only
after their receipt, source identity, SHA and actual media contract validate.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from random_subtemplate_artwork_20260920 import GROUPS, LAYERS
from verify_random_subtemplates_20260920 import BASE_SHA, SIZE, FPS, FRAMES, EXPECTED_COUNTS, artwork_digest, digest, verify_asset

REPO=Path(__file__).resolve().parents[1]


def write_json_atomic(path,value):
    temporary=path.with_suffix(path.suffix+".partial")
    temporary.write_bytes((json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode("utf-8"))
    os.replace(temporary,path)


def write_video(path,layer,index,ffmpeg):
    command=[ffmpeg,"-hide_banner","-loglevel","error","-f","rawvideo","-pix_fmt","rgba","-s","720x1280","-r",str(FPS),"-i","pipe:0",
             "-an","-c:v","libvpx-vp9","-pix_fmt","yuva420p","-lossless","1","-deadline","good","-cpu-used","4","-threads","2","-row-mt","1",
             "-auto-alt-ref","0","-fflags","+bitexact","-flags:v","+bitexact","-metadata:s:v:0","alpha_mode=1","-f","webm","-y",str(path)]
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        for n in range(FRAMES):
            frame=layer(index,n/FRAMES).resize(SIZE,Image.Resampling.LANCZOS)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close();error=proc.stderr.read()
        if proc.wait():raise RuntimeError(error.decode("utf-8","replace"))
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        if not proc.stdin.closed:proc.stdin.close()
        proc.stderr.close()


def build_one(job,output,source_sha,ffmpeg,ffprobe):
    category,index,filename=job
    path=output/"layers"/filename;receipt_path=output/"receipts"/(filename+".json")
    static=category in {"border","tint"}
    identity={"artwork_source_sha256":source_sha,"category":category,"design_index":index,"encoding_contract":"720x1280-rgba-png-or-vp9alpha-lossless-bitexact-30fps-120frames-v1"}
    reusable=False
    if path.is_file() and receipt_path.is_file():
        receipt=json.loads(receipt_path.read_text(encoding="utf-8"))
        reusable=all(receipt.get(k)==v for k,v in identity.items()) and receipt.get("sha256")==digest(path) and receipt.get("contract_verified") is True
    if not reusable:
        partial=path.with_name(path.stem+".partial"+path.suffix)
        if static:LAYERS[category](index).resize(SIZE,Image.Resampling.LANCZOS).save(partial,format="PNG")
        else:write_video(partial,LAYERS[category],index,ffmpeg)
        verified=verify_asset(partial,category,index,ffmpeg=ffmpeg,ffprobe=ffprobe,deep=False)
        os.replace(partial,path)
        receipt={**identity,"sha256":verified["sha256"],"size":path.stat().st_size,"contract_verified":True,"verification":verified}
        receipt["verification"]["file"]=filename
        write_json_atomic(receipt_path,receipt)
    else:
        # Recheck the container/alpha decode before trusting a completed artifact.
        verified=verify_asset(path,category,index,ffmpeg=ffmpeg,ffprobe=ffprobe,deep=False)
        assert verified["sha256"]==receipt["sha256"]
    row={"media_type":"image/png" if static else "video/webm","name":filename,"sha256":receipt["sha256"],"size":path.stat().st_size}
    print(json.dumps({"file":filename,"bytes":row["size"],"reused_verified":reusable}),flush=True)
    return category,index,row


def contact_sheet(path):
    """A neutral-gradient preview only; never placed in the catalog or Git."""
    cardw,cardh,cols=190,390,7
    rows=6
    sheet=Image.new("RGB",(cols*cardw,rows*cardh+62),(22,29,40));draw=ImageDraw.Draw(sheet)
    font_path=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts/msyh.ttc"
    font=ImageFont.truetype(str(font_path),14) if font_path.exists() else ImageFont.load_default()
    title=ImageFont.truetype(str(font_path),22) if font_path.exists() else font
    draw.text((18,14),"42 个新增随机样式  ·  720 × 1280  ·  透明图层预览",font=title,fill=(235,238,246))
    yy,xx=np.mgrid[0:640,0:360].astype(np.float32)
    f=(xx/360*.35+yy/640*.65)[:,:,None]
    background=np.array((38,62,79))+(np.array((138,122,137))-np.array((38,62,79)))*f
    base=Image.fromarray(np.clip(background,0,255).astype(np.uint8)).convert("RGBA")
    # A quiet neutral silhouette-like highlight reveals transparency without a face.
    g=ImageDraw.Draw(base)
    g.ellipse((105,100,255,255),fill=(155,157,165,255))
    g.rounded_rectangle((65,285,295,568),radius=90,fill=(106,129,143,255))
    g.rectangle((83,493,277,510),fill=(204,210,214,255))
    g.rectangle((104,520,256,529),fill=(181,195,204,255))
    n=0
    for category,info in GROUPS.items():
        for index,name in enumerate(info["names"]):
            overlay=LAYERS[category](index) if category in {"border","tint"} else LAYERS[category](index,.25)
            if category=="tint":overlay.putalpha(26)
            preview=Image.alpha_composite(base,overlay).convert("RGB").resize((174,310),Image.Resampling.LANCZOS)
            x=(n%cols)*cardw+8;y=(n//cols)*cardh+62
            sheet.paste(preview,(x,y))
            stem="opacity-video" if category=="opacity_video" else category
            draw.text((x,y+317),f"{stem}-{info['start']+index:02}",font=font,fill=(176,192,212))
            draw.text((x,y+339),name,font=font,fill=(239,239,235))
            n+=1
    path.parent.mkdir(parents=True,exist_ok=True);sheet.save(path)
    print(json.dumps({"contact_sheet":str(path)}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=REPO/"assets/random-subtemplates/20260920")
    parser.add_argument("--base-manifest",type=Path,default=REPO/"assets/random-subtemplates/20260918/manifest.json")
    parser.add_argument("--ffmpeg",default="ffmpeg");parser.add_argument("--ffprobe",default="ffprobe")
    parser.add_argument("--workers",type=int,choices=(1,2),default=2)
    parser.add_argument("--contact-sheet",type=Path)
    args=parser.parse_args();output=args.output
    raw=args.base_manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=BASE_SHA:raise RuntimeError("previous_manifest_sha_mismatch")
    manifest=json.loads(raw)
    for child in (output,output/"layers",output/"receipts"):child.mkdir(parents=True,exist_ok=True)
    base_copy=output/"base-manifest.json"
    if base_copy.exists() and base_copy.read_bytes()!=raw:raise RuntimeError("existing_base_manifest_changed")
    base_copy.write_bytes(raw)
    if args.contact_sheet:contact_sheet(args.contact_sheet)
    jobs=[]
    for category,info in GROUPS.items():
        stem="opacity-video" if category=="opacity_video" else category
        ext="png" if category in {"border","tint"} else "webm"
        assert len(manifest["categories"][category])==info["start"]-1
        for index in range(len(info["names"])):jobs.append((category,index,f"{stem}-{info['start']+index:02}.{ext}"))
    source_sha=artwork_digest();additions=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(lambda job:build_one(job,output,source_sha,args.ffmpeg,args.ffprobe),jobs))
    # Worker completion order cannot change manifest order or therefore its SHA.
    for category,index,row in results:
        info=GROUPS[category]
        assert not any(old["name"]==row["name"] for old in manifest["categories"][category])
        manifest["categories"][category].append(row)
        additions.append({"id":f"{info['prefix']}{info['start']+index:02}","name":info["names"][index],"description":info["descriptions"][index],
                          "category":category,"design_index":index,"file":row["name"],"sha256":row["sha256"]})
    counts={k:len(v) for k,v in manifest["categories"].items()}
    assert counts==EXPECTED_COUNTS
    write_json_atomic(output/"manifest.json",manifest)
    report={"base_manifest_sha256":BASE_SHA,"manifest_sha256":digest(output/"manifest.json"),"artwork_source_sha256":source_sha,
            "dimensions":list(SIZE),"video_fps":FPS,"video_frames":FRAMES,"counts":counts,"new_assets":42,"active_count":80,"additions":additions}
    write_json_atomic(output/"additions.json",report)
    print(json.dumps({k:v for k,v in report.items() if k!="additions"}),flush=True)


if __name__=="__main__":main()
