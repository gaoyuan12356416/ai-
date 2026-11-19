"""Mirror footage, remove mirrored glyphs and restore original subtitle pixels."""
import argparse
from pathlib import Path
import signal
import subprocess
import tempfile


def mirror_frame(frame, top, bottom):
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    first, last = max(0, int(height * top / 100)), min(height, int(height * bottom / 100))
    roi = frame[first:last]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # Light glyphs with a nearby dark outline; exclude broad bright scenery.
    light = ((gray >= 185) & ((roi.max(axis=2).astype(int) - roi.min(axis=2)) < 80)).astype('uint8') * 255
    edge = cv2.dilate((gray < 110).astype('uint8'), np.ones((7, 7), np.uint8))
    seed = light * edge
    count, labels, stats, centers = cv2.connectedComponentsWithStats(seed, 8)
    scale = width / 720
    letters = []
    for idx in range(1, count):
        x, y, w, h, area = stats[idx]
        if 7*scale <= h <= 65*scale and 1*scale <= w <= 65*scale and area >= 5*scale*scale:
            letters.append((idx, x, y, w, h, centers[idx][1]))
    groups = []
    for letter in sorted(letters, key=lambda r: r[5]):
        group = next((g for g in groups if abs(g[0][5]-letter[5]) <= 20*scale), None)
        if group is None:
            groups.append([letter])
        else:
            group.append(letter)
    mask = np.zeros(gray.shape, np.uint8)
    for group in groups:
        if len(group) < 2:
            continue
        left = max(0, min(g[1] for g in group)-round(20*scale))
        right = min(width, max(g[1]+g[3] for g in group)+round(20*scale))
        if right-left < 18*scale:
            continue
        a = max(0, min(g[2] for g in group)-round(3*scale))
        b = min(len(roi), max(g[2]+g[4] for g in group)+round(3*scale))
        for g in group:
            mask[labels == g[0]] = 255
        # Include punctuation on the same text line, but not broad background regions.
        for idx in range(1, count):
            x,y,w,h,area=stats[idx]
            if left <= x and x+w <= right and a <= y and y+h <= b and w <= 65*scale and area <= 1800*scale*scale:
                mask[labels == idx] = 255
    if not mask.any():
        return cv2.flip(frame, 1)
    radius = max(1, round(3*scale))
    mask = cv2.dilate(mask, np.ones((radius*2+1, radius*2+1), np.uint8))
    clean = frame.copy()
    clean[first:last] = cv2.inpaint(roi, mask, max(2, round(4*scale)), cv2.INPAINT_TELEA)
    result = cv2.flip(clean, 1)
    result[first:last][mask > 0] = roi[mask > 0]
    return result


def process(source, output, top, bottom, encoder, ffmpeg):
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    processes = []
    with tempfile.TemporaryFile() as errors:
        try:
            decode = subprocess.Popen([ffmpeg, '-v', 'error', '-nostdin', '-threads', '2', '-i', source,
                                       '-map', '0:v:0', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
                                      stdout=subprocess.PIPE, stderr=errors)
            processes.append(decode)
            codec = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '20', '-b:v', '0'] if encoder == 'h264_nvenc' else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18']
            encode = subprocess.Popen([ffmpeg, '-v', 'error', '-y', '-nostdin', '-threads', '2',
                                       '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', '720x1280', '-r', '30', '-i', 'pipe:0',
                                       '-i', source, '-map', '0:v:0', '-map', '1:a:0', *codec,
                                       '-pix_fmt', 'yuv420p', '-c:a', ('pcm_s16le' if Path(output).suffix == '.mov' else 'aac'), '-movflags', '+faststart', output],
                                      stdin=subprocess.PIPE, stderr=errors)
            processes.append(encode)
            size, frames = 720*1280*3, 0
            while True:
                raw = decode.stdout.read(size)
                if not raw:
                    break
                if len(raw) != size:
                    raise ValueError('Incomplete mirror frame')
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(1280,720,3)
                encode.stdin.write(mirror_frame(frame, top, bottom).tobytes())
                frames += 1
            encode.stdin.close()
            if decode.wait() or encode.wait() or not frames:
                raise ValueError('Protected mirror did not finish')
        finally:
            for child in processes:
                if child.poll() is None:
                    child.terminate()
            for child in processes:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source'); parser.add_argument('output')
    parser.add_argument('top', type=int); parser.add_argument('bottom', type=int)
    parser.add_argument('encoder', choices=['h264_nvenc','libx264'])
    parser.add_argument('ffmpeg')
    args = parser.parse_args()
    process(args.source,args.output,args.top,args.bottom,args.encoder,args.ffmpeg)
