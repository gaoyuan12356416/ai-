"""Synthetic, private-owner production acceptance. Never use real user media.

prepare/status emit short-lived signed requests for a capturing test harness;
do not print those responses to user logs or save them in public artifacts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.ai_video_editing.cloud_uploads import CloudUploads
from features.ai_video_editing.runtime import Runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'complete', 'status', 'render'])
    parser.add_argument('receipt')
    args = parser.parse_args()
    pid = subprocess.check_output(['systemctl', 'show', '-p', 'MainPID', '--value', 'ai-video-editing.service']).decode().strip()
    env = dict(x.split('=', 1) for x in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in x)
    from qcloud_cos import CosConfig, CosS3Client
    client = CosS3Client(CosConfig(Region=env['COS_REGION'], SecretId=env['COS_SECRET_ID'],
                        SecretKey=env['COS_SECRET_KEY'], Scheme='https', Timeout=20))
    runtime = Runtime(env['AI_VIDEO_EDITING_ROOT'])
    cloud = runtime.cloud = CloudUploads(runtime, client, env['COS_BUCKET'], env['COS_REGION'])
    file = Path(args.receipt)
    actor = {'tenant_key': 'ai-editor-acceptance', 'user_id': 'hk-cos-20261120'}
    if args.action == 'prepare':
        if file.exists():
            raise ValueError('receipt already exists; inspect before retrying')
        specification = json.load(sys.stdin)
        session = cloud.create(actor, {'operation_id': specification['operation_id'], 'name': '香港上传链路验收.mp4',
            'series': 'private-synthetic-acceptance', 'episode': 0, 'size': specification['size']})
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({'upload_id': session['id'], 'sha256': specification['sha256']}))
        requests = [cloud.sign(session['id'], actor, n, {'md5': digest})
                    for n, digest in enumerate(specification['md5'], 1)]
        print(json.dumps({'upload_id': session['id'], 'requests': requests}))
        return
    saved = json.loads(file.read_text())
    if args.action == 'complete':
        row = cloud.complete(saved['upload_id'], actor)
    else:
        row = cloud.row(saved['upload_id'], actor)
    answer = cloud.dto(row)
    if row['status'] == 'ready':
        asset = runtime.asset(row['asset_id'], actor)
        if asset['sha256'] != saved['sha256']:
            raise ValueError('checksum differs')
        answer['location'] = cloud.location(asset)
        answer['cpu_file_exists'] = Path(asset['path']).exists()
        answer['sha256_match'] = True
        if args.action == 'render':
            project = runtime.save(actor, {'operation_id': 'hk-cos-acceptance-create', 'name': '香港上传取源验收',
                'units': [{'id': 'unit', 'title': 'synthetic acceptance', 'clips': [
                    {'source_id': asset['id'], 'start_ms': 0, 'end_ms': 1000}]}]})['item']
            result = runtime.render(project['id'], actor, {'operation_id': 'hk-cos-acceptance-render',
                'expected_version': project['version']})
            saved['render'] = result
            file.write_text(json.dumps(saved))
            answer['render'] = result
        if saved.get('render'):
            answer['tasks'] = runtime.list_records('tasks', actor)
    print(json.dumps(answer, ensure_ascii=False))


if __name__ == '__main__':
    main()
