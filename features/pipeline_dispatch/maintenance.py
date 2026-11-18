"""Temporary service-stop admission holds; existing claims keep working.

Only root-owned systemd stop/start helpers create/remove these runtime files.
They survive a failed service start and are removed after HTTP readiness, or
by the next successful start. /run clears them on host reboot.
"""
from pathlib import Path

DIRECTORY = Path('/run/pipeline-service-drain')
UNITS = ('pipeline-coordinator.service', 'drama-material-api.service',
         'x-post-automation.service', 'x-tag-post.service')


def active_holds(directory=DIRECTORY):
    return [unit for unit in UNITS if (directory / (unit + '.json')).exists()]


def matches(unit, row):
    if unit == 'pipeline-coordinator.service':
        return True  # Every distributed claim calls this shared authority.
    if row['channel'] != 'x':
        return False
    if unit == 'x-tag-post.service':
        identity = str(row['source_id'])
        return identity.isdigit() and int(identity) >= 9000000000000
    return unit in ('drama-material-api.service', 'x-post-automation.service')


def blocking_service(row, holds):
    return next((unit for unit in holds if matches(unit, row)), '')
