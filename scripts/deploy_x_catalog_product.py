"""Deploy the product-scoped picker using the existing guarded patch installer."""
from pathlib import Path

import deploy_x_personal_source_fix as patch


def main():
    patch.ROOT = Path(__file__).resolve().parents[1]
    patch.BASE = 'fbfc738d28'
    patch.REF = 'refs/heads/codex/x-social-material-search-20261023'
    patch.FILES = ['features/x_video_publish/sources.py',
                   'features/x_video_publish/gateway.py',
                   'static/x-video-publish.js', 'static/x-video-publish.html']
    patch.main()


if __name__ == '__main__':
    main()
