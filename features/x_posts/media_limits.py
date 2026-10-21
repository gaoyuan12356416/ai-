"""Post-video upload bounds from X's media API (checked 2026-10-21).

https://docs.x.com/x-api/media/quickstart/best-practices
The former 512 MiB application ceiling applied a DM limit to Post videos.
Keep image/GIF limits and the separately frozen duration policy unchanged.
"""

STANDARD_POST_VIDEO_BYTES = 8 * 1024**3
MAX_POST_VIDEO_BYTES = 16 * 1024**3
MAX_UPLOAD_SEGMENTS = 10000  # v2 append accepts segment_index 0..9999.


def video_byte_limit(premium):
    return MAX_POST_VIDEO_BYTES if premium else STANDARD_POST_VIDEO_BYTES
