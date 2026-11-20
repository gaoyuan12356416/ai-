"""Retiming embedded captions against the same 30-fps cuts used for picture and sound."""

import json
import re

TIMESTAMP = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")
TEXT_CODECS = {"mov_text", "subrip", "webvtt", "ass", "ssa", "text", "ttml"}


class UnsupportedCaptions(Exception):
    pass


def milliseconds(value):
    h, m, s, ms = (int(i) for i in TIMESTAMP.fullmatch(value.strip()).groups())
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def stamp(ms):
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, ms)


def parse_srt(data):
    result = []
    for block in re.split(r"\n\s*\n", data.decode("utf-8-sig").replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if not lines:
            continue
        pos = 1 if lines[0].strip().isdigit() else 0
        if len(lines) <= pos + 1 or " --> " not in lines[pos]:
            continue
        start, end = lines[pos].split(" --> ", 1)
        result.append((milliseconds(start), milliseconds(end.split()[0]), "\n".join(lines[pos + 1:])))
    return result


def write_srt(path, cues):
    path.write_text("\n\n".join("%d\n%s --> %s\n%s" % (i, stamp(a), stamp(b), text)
                               for i, (a, b, text) in enumerate(cues, 1)) + "\n", encoding="utf8")


def clip_cues(cues, start, end):
    return [(max(0, a - start), min(b, end) - start, text)
            for a, b, text in cues if a < end and b > start and min(b, end) > max(a, start)]


def selected_tracks(path, ffmpeg, ffprobe, stopped, run):
    streams = json.loads(run([ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
                                   "-select_streams", "s", "-show_streams", "-of", "json", str(path)],
                                  stopped, timeout=90))["streams"]
    tracks = []
    for number, stream in enumerate(streams):
        if stream["codec_name"] not in TEXT_CODECS:
            raise UnsupportedCaptions("源片含暂不支持的内封字幕格式，请提供文字字幕或已压制字幕的版本")
        text = run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                         "-protocol_whitelist", "file", "-f", "mov", "-i", str(path),
                         "-map", "0:" + str(stream["index"]), "-c:s", "srt", "-f", "srt", "-"],
                        stopped)
        tags = stream.get("tags", {})
        language = tags.get("language") or "und"
        caption_title = tags.get("title") or tags.get("handler_name", "")
        # Track order within a language is stable even where episode handler names differ.
        key = (language, sum(t[0][0] == language for t in tracks))
        tracks.append((key, caption_title, bool(stream.get("disposition", {}).get("default")), parse_srt(text)))
    return tracks


class Captions:
    def __init__(self, ffmpeg, ffprobe, stopped, run):
        self.ffmpeg, self.ffprobe, self.stopped = ffmpeg, ffprobe, stopped
        self.run = run
        self.source_tracks = {}
        self.output_tracks = {}

    def segment(self, sid, source, first_frame, end_frame, offset_frames, workdir, number):
        if sid not in self.source_tracks:
            self.source_tracks[sid] = selected_tracks(source, self.ffmpeg, self.ffprobe, self.stopped, self.run)
        tracks = self.source_tracks[sid]
        if not tracks:
            return None
        default_key = next((key for key, _, default, _ in tracks if default), tracks[0][0])
        start = round(first_frame * 1000 / 30)
        end = round(end_frame * 1000 / 30)
        offset = round(offset_frames * 1000 / 30)
        primary = None
        for key, title, _, cues in tracks:
            piece = clip_cues(cues, start, end)
            if not piece:
                continue
            track = self.output_tracks.setdefault(key, {"title": title, "cues": []})
            track["cues"].extend((a + offset, b + offset, text) for a, b, text in piece)
            if key == default_key:
                # YouTube receives visible source captions even when its player does not
                # expose embedded MP4 text tracks. Keep alternate tracks selectable too.
                primary = workdir / ("captions_%03d.srt" % number)
                write_srt(primary, piece)
        return primary

    def mux(self, media, workdir):
        if not self.output_tracks:
            return
        target = workdir / "captioned.mp4"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(media)]
        options = ["-map", "0:v:0", "-map", "0:a:0"]
        for i, ((language, _), track) in enumerate(self.output_tracks.items()):
            filename = workdir / ("track_%d.srt" % i)
            write_srt(filename, track["cues"])
            cmd.extend(["-f", "srt", "-i", str(filename)])
            options.extend(["-map", "%d:s:0" % (i + 1), "-metadata:s:s:%d" % i, "language=" + language,
                            "-metadata:s:s:%d" % i, "title=" + track["title"], "-disposition:s:%d" % i, "0"])
        self.run(cmd + options + ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                                  "-movflags", "+faststart", str(target)], self.stopped)
        target.replace(media)
