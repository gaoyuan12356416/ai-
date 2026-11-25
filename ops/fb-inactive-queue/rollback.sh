#!/bin/sh
# Retain retirement history and all subsequent publication decisions.
set -eu
systemctl disable --now fb-inactive-queue.timer
systemctl stop fb-inactive-queue.service
python3 -B /opt/fb-inactive-queue/current/retire.py uninstall \
    --native /mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3 \
    --dispatch /mnt/data-disk/pipeline-dispatch/dispatch.sqlite3 \
    --runtime /opt/fb-auto-post/current
