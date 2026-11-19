"""Reusable alpha assets for CI's three image watermark layers."""
import contextlib
import subprocess
import tempfile
from pathlib import Path

from .cloud_jobs import fingerprint
from .executors import ExecutionError
from features.fb_gpu.random_overlay import selected_asset_paths, sha256_file


def run_media(ffmpeg, args):
    result = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-filter_threads", "1",
                             "-filter_complex_threads", "1", "-threads", "2", *map(str, args)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1200)
    if result.returncode:
        raise ExecutionError("cos_template_asset_conversion_failed")


def watermark(url, duration, *, width=720, height=1280, dx=0, dy=0):
    return {"Type": "Image", "Pos": "TopLeft", "LocMode": "Absolute", "Dx": str(dx), "Dy": str(dy),
            "StartTime": "0", "EndTime": str(round(duration, 3)), "Image": {"Url": url, "Mode": "Fixed",
            "Width": str(width), "Height": str(height), "Transparency": "100"}}


class Templates:
    def __init__(self, origin, root, ffmpeg):
        self.origin, self.root, self.ffmpeg = origin, Path(root), ffmpeg
        self.root.mkdir(exist_ok=True, parents=True)

    def cached(self, name, inputs, ext, create):
        import fcntl
        key = fingerprint(["ci-apng-v1", name, inputs])
        remote = self.origin.config.cos_prefix + "/templates/" + key + "." + ext
        # Distinct clients can share the same validated assets. The local lock
        # avoids spending CPU twice for a new template at higher concurrency.
        with (self.root / (key + ".lock")).open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            head = self.origin.head(remote)
            if head and head.get("x-cos-meta-sha256") and int(head["content-length"]) > 0:
                return self.origin.url(remote)
            with tempfile.TemporaryDirectory(dir=self.root) as folder:
                target = Path(folder) / ("layer." + ext)
                create(target)
                sha, size = sha256_file(target)
                return self.origin.upload(remote, target, sha, size, "image/png")

    def random(self, recipe, assets, duration, *, canvas_size=(720, 1280)):
        from PIL import Image
        width, height = canvas_size
        sd = canvas_size != (720, 1280)
        scale = f"scale={width}:{height}:flags=lanczos," if sd else ""
        paths = selected_asset_paths(recipe, assets)
        selected = recipe["assets"]
        apng = ["-map", "0:v:0", "-an", "-r", "30", "-pix_fmt", "rgba", "-c:v", "apng", "-threads", "2",
                "-compression_level", "3", "-plays", "0", "-f", "apng", "-y"]
        def border(target):
            run_media(self.ffmpeg, ["-loop", "1", "-framerate", "30", "-i", paths["border"], "-c:v", "libvpx-vp9",
                "-i", paths["opacity_video"], "-filter_complex",
                f"[0:v]{scale}format=rgba[b];[1:v]{scale}format=rgba[o];[b][o]overlay=shortest=1:format=auto,format=rgba[out]",
                "-map", "[out]", "-an", "-r", "30", "-c:v", "apng", "-threads", "2", "-compression_level", "3",
                "-plays", "0", "-f", "apng", "-y", target])
        def corners(target):
            run_media(self.ffmpeg, ["-c:v", "libvpx-vp9", "-i", paths["corners"], *(["-vf", scale.rstrip(",")] if sd else []), *apng, target])
        def tint(target):
            with Image.open(paths["tint"]) as source:
                layer = source.convert("RGBA")
                if sd:
                    layer = layer.resize(canvas_size, Image.Resampling.LANCZOS)
                layer.putalpha(layer.getchannel("A").point(lambda v: round(v * recipe["tint_opacity_bp"] / 10000)))
                layer.save(target)
        suffix = f"-{width}x{height}" if sd else ""
        urls = [self.cached("border-opacity" + suffix, [selected[x]["sha256"] for x in ("border", "opacity_video")], "apng", border),
                self.cached("corners" + suffix, selected["corners"]["sha256"], "apng", corners),
                self.cached("tint" + suffix, [selected["tint"]["sha256"], recipe["tint_opacity_bp"]], "png", tint)]
        return [watermark(url, duration, width=width, height=height) for url in urls]

    def outro_labels(self, font, content_id, duration):
        from PIL import Image, ImageDraw, ImageFont
        def create(target):
            img = Image.new("RGBA", (720, 1280))
            draw = ImageDraw.Draw(img)
            draw.rectangle((28, 40, 691, 153), fill=(0, 0, 0, 199))
            draw.rectangle((28, 40, 37, 153), fill=(255, 46, 136, 255))
            draw.text((53, 59), "DRAMA ID: " + content_id, font=ImageFont.truetype(str(font), 43), fill="white", anchor="lt")
            label = "TUTORIAL EXAMPLE  -  Follow the Drama ID shown above"
            face = ImageFont.truetype(str(font), 20)
            x = round((720 - draw.textlength(label, font=face)) / 2)
            bounds = draw.textbbox((x, 1185), label, font=face, anchor="lt")
            draw.rectangle((bounds[0]-12, bounds[1]-12, bounds[2]+12, bounds[3]+12), fill=(0,0,0,184))
            draw.text((x, 1185), label, font=face, fill="white", anchor="lt")
            img.save(target)
        url = self.cached("outro-labels", [sha256_file(font)[0], content_id], "png", create)
        return watermark(url, duration)

    def phone_layers(self, source, outro, logo, duration, has_audio, context, temp):
        """Build the 27 changing foreground frames and audio overlap; CI encodes the video."""
        transition = 0.9
        start = duration - transition
        context.check()
        args = ["-ss", "%.6f" % start, "-i", source]
        base = "[0:v]scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=rgba"
        if logo:
            args.extend(["-i", logo])
            base += "[base];[1:v]scale=132:132[logo];[base][logo]overlay=48:72:format=auto,format=rgba"
        graph = (base + ",scale=w='trunc((720-214*min(t/0.9,1))/2)*2':h='trunc((1280-378*min(t/0.9,1))/2)*2':"
                 "eval=frame,format=rgba,fade=t=out:st=0.65:d=0.25:alpha=1,"
                 "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black@0:eval=frame,format=rgba[fg]")
        foreground = temp / "phone.apng"
        run_media(self.ffmpeg, [*args,"-filter_complex",graph,"-map","[fg]","-frames:v","27","-an","-c:v","apng",
                  "-threads","2","-plays","1","-f","apng","-y",foreground])
        # Only the 0.9 s source tail enters the mix. Copy the approved outro video stream unchanged.
        mixed = temp / "outro-input.mp4"
        audio = (["-ss", "%.6f" % start, "-i", source] if has_audio else
                 ["-f","lavfi","-i","anullsrc=channel_layout=stereo:sample_rate=48000"])
        graph = ("[0:a]aresample=48000,apad,atrim=duration=0.9,asetpts=PTS-STARTPTS,afade=t=out:st=0:d=0.9[a];"
                 "[1:a]aresample=48000,asetpts=PTS-STARTPTS[b];[a][b]amix=inputs=2:duration=longest:normalize=0,"
                 "alimiter=limit=0.95[mix]")
        run_media(self.ffmpeg, [*audio,"-i",outro,"-filter_complex",graph,"-map","1:v:0","-map","[mix]",
                               "-c:v","copy","-c:a","aac","-ac","2","-ar","48000","-b:a","192k","-y",mixed])
        return self.origin.content(mixed, "video/mp4"), watermark(self.origin.url(self.origin.content(foreground, "image/png")), transition)
