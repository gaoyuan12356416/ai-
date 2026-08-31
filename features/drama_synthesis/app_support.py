"""Safe, business-DB-free projections of the media worker's runtime status."""
from __future__ import annotations

import math
import threading
import time


STAGES = {
    "connecting": "正在连接制作节点", "submitting": "正在提交制作任务", "queued": "等待制作资源",
    "downloading": "下载剧集", "normalizing": "统一视频规格",
    "concatenating": "拼接全集", "rendering": "制作随机模板", "rendering_random": "制作随机模板",
    "removing_bgm": "处理背景音乐", "waiting_cover": "等待封面",
    "uploading": "上传成片", "completed": "制作完成",
    "failed": "制作失败", "recovery_required": "执行状态待核查",
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def remote_display(snapshot):
    """Return stage percentages, never invented end-to-end progress weights."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    stage = str(snapshot.get("stage") or snapshot.get("status") or "queued")
    state = str(snapshot.get("status") or "queued")
    metrics = snapshot.get("metrics") or snapshot.get("progress") or {}
    metrics = metrics if isinstance(metrics, dict) else {}
    label = STAGES.get(stage, "制作中")
    detail = []
    percent = None

    def ratio(done, total):
        nonlocal percent
        a, b = number(done), number(total)
        if a is not None and b is not None and b > 0:
            percent = round(min(100.0, a * 100.0 / b), 1)
            return a, b
        return None

    if stage in {"downloading", "normalizing"}:
        if stage == "downloading":
            counts = ratio(metrics.get("completed_episodes"), metrics.get("total_episodes"))
        else:
            downloaded = number(metrics.get("completed_episodes"))
            episodes = number(metrics.get("total_episodes"))
            counts = (downloaded, episodes) if downloaded is not None and episodes else None
        if counts:
            detail.append("已下载 %d/%d 集" % counts)
        size = number(metrics.get("downloaded_bytes"))
        total = number(metrics.get("total_bytes"))
        if size is not None:
            detail.append("%.1f MB%s" % (size / 1e6, (" / %.1f MB" % (total / 1e6)) if total else ""))
        speed = number(metrics.get("bytes_per_second"))
        if speed is not None:
            detail.append("%.2f MB/s" % (speed / 1e6))
        normalized, segments = number(metrics.get("normalized_episodes")), number(metrics.get("total_segments"))
        if stage == "downloading" and normalized is not None and normalized > 0 and segments:
            detail.append("并行转码 %d/%d 段" % (normalized, segments))
        elif stage == "normalizing":
            counts = ratio(normalized, segments)
            if counts:
                detail.append("已处理 %d/%d 段" % counts)
    elif stage in {"rendering", "rendering_random", "concatenating", "removing_bgm"}:
        times = ratio(metrics.get("out_time_seconds"), metrics.get("duration_seconds"))
        if times:
            detail.append("已处理 %.1f/%.1f 分钟" % (times[0] / 60, times[1] / 60))
    elif stage == "uploading":
        sizes = ratio(metrics.get("uploaded_bytes"), metrics.get("total_bytes"))
        if sizes:
            detail.append("已上传 %.1f/%.1f MB" % (sizes[0] / 1e6, sizes[1] / 1e6))
    if state == "completed":
        stage, label, percent = "completed", STAGES["completed"], 100.0
    elif state in {"failed", "recovery_required"}:
        label = STAGES[state]
    if snapshot.get("connection_state") == "reconnecting":
        label = "连接恢复中"
        detail.insert(0, "远端执行状态正在核实，不会重复提交制作")
    elif snapshot.get("stalled") and state == "running":
        detail.append("超过15分钟未见新进展，需核对制作节点")
    status = "rendering"
    if stage in {"queued", "connecting"}:
        status = "queued"
    elif stage == "downloading":
        status = "downloading"
    if state in {"failed", "recovery_required"}:
        status = "failed" if state == "failed" else "rendering"
    return {
        "status": status, "stage": stage, "stage_label": label,
        "stage_percent": percent,
        "detail": "；".join([label] + detail),
    }


class ObservationStop:
    """Stop one CPU observation without cancelling accepted GPU media work."""

    def __init__(self, parent=None):
        self.parent = parent
        self.local = threading.Event()

    def is_set(self):
        return self.local.is_set() or bool(self.parent and self.parent.is_set())

    def set(self):
        self.local.set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self.is_set()
            self.local.wait(0.2 if remaining is None else min(0.2, remaining))
        return True
