"""Deploy the bounded cross-language title lookup only."""
from pathlib import Path

import deploy_x_personal_source_fix as patch


def main():
    patch.ROOT = Path(__file__).resolve().parents[1]
    patch.BASE = '4317b66d13f0e39dce3d46faf9146e89586a248a'
    patch.REF = 'refs/heads/codex/x-title-language-search-20261023'
    patch.FILES = ['features/x_video_publish/sources.py']
    patch.main()


if __name__ == '__main__':
    main()
