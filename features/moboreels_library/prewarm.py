"""Refresh one recently authorized report context through the existing sidecar."""
import json
import os
from urllib.request import Request, build_opener, ProxyHandler

from .settings import read_env_file


def main():
    config = read_env_file('/etc/moboreels-library.env')
    token = os.environ.get('MOBOREELS_INTERNAL_TOKEN') or config.get('MOBOREELS_INTERNAL_TOKEN', '')
    if not token:
        raise RuntimeError('Report prewarm internal token unavailable')
    request = Request('http://127.0.0.1:8864/internal/report-prewarm', data=b'{}',
                      headers={'Authorization': 'Bearer ' + token,
                               'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
        result = json.load(response)
    print(json.dumps(result))
    return 1 if result.get('status') == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
