"""Verify or publish an explicitly requested catalogue reprojection with the active FB writer."""
import argparse
import json
from pathlib import Path

from .common import MONEY, PAGE, PORTRAITS, atomic, checked, read_config


class PreparedPages:
    def __init__(self, path):
        self.path = path
        with path.open(encoding='utf-8') as src:
            header = src.readline().rstrip('\n')
        if not header.endswith(',"pages":{'):
            raise ValueError('invalid prepared FB header')
        self.meta = json.loads(header[:-len(',"pages":{')]+'}')['meta']

    def __len__(self):
        return self.meta['page_count']

    def items(self):
        with self.path.open(encoding='utf-8') as src:
            next(src)
            for line in src:
                entry = line.rstrip('\n')
                if entry == '}}':
                    if src.read(1):
                        raise ValueError('unexpected trailing FB data')
                    return
                if entry.endswith(','):
                    entry = entry[:-1]
                row = json.loads('{'+entry+'}')
                if len(row) != 1:
                    raise ValueError('invalid Page export')
                pid, page = next(iter(row.items()))
                if checked(pid, PAGE) != page['page_id']:
                    raise ValueError('Page identity mismatch')
                yield pid, page
        raise ValueError('truncated FB export')


def run(folder, action):
    pointer = json.loads((folder/'source-pointers.json').read_text())['fb']
    head = json.loads((PORTRAITS/'current.json').read_text())
    if head != pointer or read_config() not in ('live', 'live-mirror'):
        raise RuntimeError('FB source moved')
    source = json.loads((PORTRAITS/(checked(head['version'])+'.receipt.json')).read_text())
    pages = PreparedPages(folder/'fb-next.json')
    meta = pages.meta
    if checked(meta['version']) <= head['version'] or not meta.get('projection_only'):
        raise ValueError('invalid projection generation')
    seen, totals = set(), dict.fromkeys(MONEY, 0)
    for pid, page in pages.items():
        if pid in seen:
            raise ValueError('duplicate Page in preview')
        seen.add(pid)
        for key in MONEY:
            totals[key] += page[key]
    if (seen != set(source['payload_hashes']) or len(seen) != len(pages)
            or totals != meta['audit'] or any(totals[key] != source[key] for key in MONEY)):
        raise ValueError('reprojection finance or scope mismatch')
    if action == 'check':
        return
    from features.fb_page_portrait.collect import Reader, environment
    from features.fb_page_portrait.storage import publish
    from .refresh import primary, secondary
    env = environment('/root/drama_material_service/.env')
    data = dict(meta=meta, pages=pages)
    proof = primary(data, env, Reader(env))
    atomic(PORTRAITS/(meta['version']+'.receipt.json'), proof)
    publish(data, proof, PORTRAITS)
    secondary(data, env, Reader(env), proof)
    atomic(PORTRAITS/'status.json', dict(status='ok', **meta))
    atomic(folder/'fb-publish-proof.json', proof)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('action', choices=['check','publish'])
    args = parser.parse_args()
    run(args.folder, args.action)
