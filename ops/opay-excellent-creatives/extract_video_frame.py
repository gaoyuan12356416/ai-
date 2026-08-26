#!/usr/bin/env python3
"""Extract one representative video frame with the server OpenCV runtime."""

import argparse
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_url")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--timeout-ms", type=int, default=12000)
    args = parser.parse_args(argv)

    timeout_us = max(1000, args.timeout_ms) * 1000
    os.environ.setdefault(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS",
        "timeout;%d|rw_timeout;%d" % (timeout_us, timeout_us),
    )
    import cv2

    capture = cv2.VideoCapture(args.source_url)
    try:
        if not capture.isOpened():
            raise RuntimeError("opencv_video_open_failed")
        capture.set(cv2.CAP_PROP_POS_MSEC, 1000)
        ok, frame = capture.read()
        if not ok or frame is None:
            capture.set(cv2.CAP_PROP_POS_MSEC, 0)
            ok, frame = capture.read()
        if not ok or frame is None:
            raise RuntimeError("opencv_video_frame_failed")
        height, width = frame.shape[:2]
        if width > 640:
            target_height = max(2, int(round(height * 640 / width)))
            frame = cv2.resize(frame, (640, target_height), interpolation=cv2.INTER_AREA)
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(args.destination), frame):
            raise RuntimeError("opencv_video_write_failed")
    finally:
        capture.release()


if __name__ == "__main__":
    main()
