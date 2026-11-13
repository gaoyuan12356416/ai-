"""Preserve the concurrent X upload-continuity transport in a scoped rollback."""
from pathlib import Path
import record_fb_followup as followup

followup.GROUP = 'x'
followup.CURRENT = Path('/mnt/data-disk/x-upload-continuity-20261113/deployment/candidates/x-v2')
followup.FOLLOWUP = 'd6c66c58c91122d15ea19d22fb345e48a9332961'
followup.CHANGED = {
    'features/x_video_publish/transport.py': '627b2cf28250e2db7e157fa93374a461306e3c4a43e9a7e983cf3f35d986ed7a',
}

if __name__ == '__main__':
    followup.main()
