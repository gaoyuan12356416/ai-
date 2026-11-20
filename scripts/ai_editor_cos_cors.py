"""Back up and add only the AI editor's exact-origin COS CORS rule."""
import argparse
import json
from pathlib import Path
import subprocess

RULE_ID = 'ai-editor-hongkong-upload-20261120'
RULE = {'ID': RULE_ID, 'AllowedOrigin': ['https://ai.yingliangads.com'],
        'AllowedMethod': ['PUT', 'GET', 'HEAD'],
        'AllowedHeader': ['authorization', 'content-type', 'content-md5', 'x-cos-psize-min',
                          'x-cos-psize-max', 'range', 'if-match'],
        'ExposeHeader': ['ETag', 'Content-Length', 'Content-Range'], 'MaxAgeSeconds': 600}


def client():
    from qcloud_cos import CosConfig, CosS3Client
    pid = subprocess.check_output(['systemctl', 'show', '-p', 'MainPID', '--value', 'ai-video-editing.service']).decode().strip()
    env = dict(x.split('=', 1) for x in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in x)
    if env['COS_REGION'] != 'ap-hongkong':
        raise ValueError('expected Hong Kong COS')
    return CosS3Client(CosConfig(Region=env['COS_REGION'], SecretId=env['COS_SECRET_ID'],
                      SecretKey=env['COS_SECRET_KEY'], Scheme='https', Timeout=15)), env['COS_BUCKET']


def rules(cos, bucket):
    try:
        result = cos.get_bucket_cors(Bucket=bucket).get('CORSRule', [])
        return [result] if isinstance(result, dict) else result
    except Exception as exc:
        if getattr(exc, 'get_error_code', lambda: '')() == 'NoSuchCORSConfiguration':
            return []
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'rollback'])
    parser.add_argument('backup')
    args = parser.parse_args()
    cos, bucket = client()
    path = Path(args.backup)
    before = rules(cos, bucket)
    if args.action == 'install':
        if path.exists():
            raise ValueError('backup exists: reconcile before retrying')
        if any(r.get('ID') == RULE_ID for r in before):
            raise ValueError('editor CORS rule already exists')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'bucket': bucket, 'before': before, 'rule': RULE}, indent=2))
        cos.put_bucket_cors(Bucket=bucket, CORSConfiguration={'CORSRule': before + [RULE]})
        after = rules(cos, bucket)
        if not any(r.get('ID') == RULE_ID and r.get('AllowedOrigin') == RULE['AllowedOrigin'] for r in after):
            raise ValueError('CORS readback mismatch')
        saved = json.loads(path.read_text()); saved['after'] = after
        path.write_text(json.dumps(saved, indent=2))
    else:
        saved = json.loads(path.read_text())
        if saved['bucket'] != bucket:
            raise ValueError('bucket changed')
        own = [r for r in before if r.get('ID') == RULE_ID]
        expected = [r for r in saved.get('after', []) if r.get('ID') == RULE_ID]
        if own and own != expected:
            raise ValueError('CORS rule changed: preserve concurrent configuration')
        after = [r for r in before if r.get('ID') != RULE_ID]
        if after:
            cos.put_bucket_cors(Bucket=bucket, CORSConfiguration={'CORSRule': after})
        else:
            cos.delete_bucket_cors(Bucket=bucket)
    print(json.dumps({'action': args.action, 'bucket': bucket, 'rule': RULE_ID, 'backup': str(path)}))


if __name__ == '__main__':
    main()
