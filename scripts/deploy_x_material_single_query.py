"""Deploy only the X catalog single-statement repository change."""
from pathlib import Path

import deploy_x_personal_source_fix as patch


def main():
    patch.ROOT = Path(__file__).resolve().parents[1]
    patch.BASE = '37de345e74afd9881ca14d86f23652c246570f28'
    patch.REF = 'refs/heads/codex/x-material-single-query-20261023'
    patch.FILES = ['features/x_video_publish/sources.py']
    patch.main()


if __name__ == '__main__':
    main()
