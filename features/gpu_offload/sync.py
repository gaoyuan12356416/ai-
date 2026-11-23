"""Copy each already-published immutable version once; never query source SQL."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import struct
import time


def synchronize(root, state_path, port, token, rate=2*1024*1024):
    manifest = json.loads((Path(root) / 'current.json').read_text())
    source = (Path(root) / manifest['database']).resolve()
    if source.parent != (Path(root) / 'snapshots').resolve() or source.suffix != '.sqlite3':
        raise ValueError('immutable_snapshot_required')
    if source.stat().st_size > 511*1024*1024: raise ValueError('snapshot_too_large')
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=180)
    try:
        connection.request('GET', '/v1/status', headers={'Authorization': 'Bearer '+token})
        response = connection.getresponse(); status = json.loads(response.read(65536))
    finally: connection.close()
    if status.get('version') != manifest['version']:
        digest = hashlib.sha256()
        with source.open('rb') as stream:
            for data in iter(lambda: stream.read(1024*1024), b''): digest.update(data)
        raw = json.dumps(manifest, separators=(',', ':')).encode()
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=180)
        try:
            connection.putrequest('POST', '/v1/snapshot')
            connection.putheader('Authorization', 'Bearer '+token)
            connection.putheader('Content-Length', str(source.stat().st_size + 4 + len(raw)))
            connection.putheader('X-Snapshot-SHA256', digest.hexdigest())
            connection.endheaders(); connection.send(struct.pack('!I', len(raw)) + raw)
            started, sent = time.monotonic(), 0
            with source.open('rb') as stream:
                for data in iter(lambda: stream.read(256*1024), b''):
                    connection.send(data); sent += len(data)
                    pause = sent/rate-(time.monotonic()-started)
                    if pause > 0: time.sleep(min(0.25, pause))
            response = connection.getresponse(); result = json.loads(response.read(65536))
            if response.status != 200 or result.get('version') != manifest['version']:
                raise RuntimeError('snapshot_upload_failed')
        finally: connection.close()
    state = Path(state_path)
    state.parent.mkdir(parents=True, exist_ok=True)
    temporary = state.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': manifest['version'], 'data_time': manifest['generated_at'],
                                     'synced_at': time.time()}))
    temporary.replace(state)
    return {'ok': True, 'version': manifest['version']}


def main():
    try:
        result = synchronize(os.environ['OFFLOAD_SOURCE_ROOT'], os.environ['OFFLOAD_SYNC_STATE'],
                             int(os.environ.get('OFFLOAD_FORWARD_PORT', '19961')), os.environ['OFFLOAD_INTERNAL_TOKEN'])
        print(json.dumps(result), flush=True)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__, 'previous_snapshot_preserved': True}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__': main()
