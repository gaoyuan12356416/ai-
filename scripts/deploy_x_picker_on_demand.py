"""Scoped picker/duration patch against the deployed product-selection release."""
from pathlib import Path

import deploy_x_personal_source_fix as patch


def main():
    patch.ROOT = Path(__file__).resolve().parents[1]
    patch.BASE = 'aa252f2e206e0c5893f7e813ab1aea84e98d099a'
    patch.REF = 'refs/heads/codex/x-material-picker-on-demand-20261023'
    patch.FILES = ['features/x_posts/source_duration.py',
                   'features/x_video_publish/sources.py',
                   'static/x-video-publish.js', 'static/x-video-publish.html']
    patch.main()


if __name__ == '__main__':
    main()
