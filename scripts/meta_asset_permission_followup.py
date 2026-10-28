#!/usr/bin/env python3
"""Read-only reconciliation using BM share grants and same-edge task evidence."""
import argparse
import collections
import hashlib
import json
import subprocess
import time
from pathlib import Path

import meta_asset_permission_sync as m


def tasks_equal(left, right):
    if left is None or right is None:
        return left is right
    return (isinstance(left.get('tasks'), list) and isinstance(right.get('tasks'), list)
            and set(left['tasks']) == set(right['tasks']))


def delegation(kind, live, share, owned, bm):
    owner = (live.get('business') or {}).get('id')
    if owner == bm or (owner is None and owned):
        return True, 'bm_owns_asset'
    if share is None or not isinstance(share.get('permitted_tasks'), list):
        return None, 'bm_share_grant_unavailable'
    full = m.full_access(kind, share['permitted_tasks'])
    return full, 'bm_shared_full_control' if full else 'owner_shared_without_full_control'


def classify(row, source, source_admin, target, target_admin, source_visible, target_visible, grant, owned):
    kind = row['kind']
    row.update(source_tasks=(source or {}).get('tasks'), before_tasks=(target or {}).get('tasks') or [],
               target_assigned=target is not None, target_visible=target_visible,
               bm_permitted_tasks=(grant or {}).get('permitted_tasks'))
    row['desired_tasks'] = sorted(set(row['before_tasks']) | m.expected_tasks(kind, row['before_tasks']))
    if kind == 'page' and not row['before_tasks'] and any(x.startswith('PROFILE_PLUS_') for x in (row['bm_permitted_tasks'] or [])) and m.full_access('page', row['bm_permitted_tasks']):
        row['desired_tasks'] = ['PROFILE_PLUS_FULL_CONTROL']
    state, reason = m.eligibility(kind, row['source_live'], row['backend'])
    if state != 'normal':
        row.update(category=state, reason=reason)
        return row
    can, proof = delegation(kind, row['source_live'], grant, owned, row['business_id'])
    row.update(delegable=can, delegation_proof=proof)
    if not source_visible or not source or not isinstance(source.get('tasks'), list):
        row.update(category='unknown', reason='source_access_or_assignment_unconfirmed')
        return row
    if (target is not None) != target_visible or (target is not None and not isinstance(target.get('tasks'), list)):
        row.update(category='unknown', reason='target_access_or_assignment_unconfirmed')
        return row
    difference = 'already_sufficient' if target and m.full_access(kind, target['tasks']) else 'insufficient' if target else 'missing'
    row['difference'] = difference
    if can is False:
        row.update(category='unknown' if difference == 'already_sufficient' else 'not_delegable',
                   reason='full_target_tasks_conflict_with_bm_share' if difference == 'already_sufficient' else proof)
    elif can is None:
        row.update(category='unknown', reason=proof)
    elif not tasks_equal(source, source_admin) or not tasks_equal(target, target_admin):
        row.update(category='unknown', reason='same_edge_admin_task_disagreement')
    else:
        row.update(category=difference, reason='verified_full_access' if difference == 'already_sufficient' else 'needs_full_access')
    return row


def prior_restriction(path):
    raw = (path / 'manifest.json').read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != (path / 'manifest.sha256').read_text().strip():
        raise ValueError('prior_manifest_digest_mismatch')
    events = [json.loads(s) for s in (path / 'journal.jsonl').read_text().splitlines()]
    pauses = [e for e in events if e['event'] == 'bm_paused']
    if not pauses:
        raise ValueError('prior_run_has_no_restriction')
    last = pauses[-1]
    intent = next(e for e in reversed(events) if e['event'] == 'intent' and e['key'] == last['key'])
    return {'prior_run_dir': str(path), 'prior_manifest_sha256': digest,
            'restricted_at': last['at'], 'key': last['key'],
            'actor_credential_id': intent['actor']['credential_id'],
            'actor_fingerprint': intent['actor']['fingerprint'], 'target_id': intent['target_id']}


def audit(args):
    directory = Path(args.run_dir).resolve()
    if (directory / 'manifest.json').exists():
        raise ValueError('manifest_already_exists')
    if not str(directory).startswith('/mnt/data-disk/'):
        raise ValueError('run_directory_must_be_on_data_disk')
    uuid = subprocess.check_output(['findmnt', '-no', 'UUID', '/mnt/data-disk'], text=True).strip()
    if uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise ValueError('data_disk_uuid_mismatch')
    directory.mkdir(parents=True, exist_ok=True)
    restriction = prior_restriction(Path(args.prior_run).resolve())
    prior_manifest = json.loads((Path(args.prior_run)/'manifest.json').read_text())
    prior_page_ids = sorted(a['asset_id'] for a in prior_manifest['assets'] if a['kind']=='page')
    db = m.Database(args.credential_config)
    try:
        credentials, graph = db.credentials(), m.Graph(args.graph_version)
        admin = next(c for c in credentials if c['id'] == restriction['actor_credential_id'])
        if m.public_credential(admin)['fingerprint'] != restriction['actor_fingerprint']:
            raise ValueError('restricted_credential_changed')
        users = graph.rows(args.business + '/system_users', admin, 'id,name,role')
        if not m.verify_admin(graph, admin, args.business, users):
            raise ValueError('restricted_actor_no_longer_admin')
        source, target = m.single_credential(credentials, 803), m.single_credential(credentials, 916)
        identities = {side: m.verify_identity(graph, c, users) for side, c in [('source', source), ('target', target)]}
        if identities['target']['meta_id'] != restriction['target_id']:
            raise ValueError('target_identity_changed')
        m.write_json(directory / 'identities.json', identities)
        times = {}

        def snapshot(name, fn):
            path = directory / (name + '.json')
            if path.exists() and time.time() - path.stat().st_mtime < 3600:
                value = json.loads(path.read_text())
                m.emit('complete_snapshot_reused', name=name)
            else:
                value = fn()
                m.write_json(path, value)
            times[name] = {'completed_at_epoch': path.stat().st_mtime}
            m.write_json(directory / 'snapshot-times.json', times)
            return value

        shares = {}
        for kind, edge in [('account', 'client_ad_accounts'), ('page', 'client_pages')]:
            shares[kind] = snapshot('bm_' + kind + '_grants', lambda: m.mapping(graph.rows(args.business + '/' + edge, admin, 'id,permitted_tasks')))
        owned = snapshot('owned_pages', lambda: graph.rows(args.business + '/owned_pages', admin, 'id'))
        backend = {kind: db.assets(kind) for kind in ('account', 'page')}
        parts = {}
        for kind, edge, direct in [('account', 'assigned_ad_accounts', 'adaccounts'), ('page', 'assigned_pages', 'accounts')]:
            part = {}
            if kind == 'account':
                for side, c in [('source', source), ('target', target)]:
                    part[side + '_visible'] = snapshot(kind + '_' + side + '_visible', lambda: m.mapping(graph.rows('me/' + direct, c, 'id')))
                ids = sorted(part['source_visible'])
            else:
                # Follow-up is bounded to the original frozen Page universe.
                # Current own-token assignments plus individual Page business
                # reads establish access without a slow global me/accounts scan.
                ids = prior_page_ids
            part['scope_ids'] = ids
            for side, c in [('source', source), ('target', target)]:
                uid = identities[side]['meta_id']
                for view, reader in [('self', c), ('admin', admin)]:
                    part[side + '_' + view] = snapshot(kind + '_' + side + '_' + view, lambda: (
                        m.filtered_pages(graph, uid, reader, ids, args.business) if kind == 'page' else
                        m.mapping(graph.rows(uid + '/' + edge, reader, 'id,name,account_status,tasks,business'))))
            if kind == 'page':
                active_ids = [key for key in ids if backend[kind].get(key) and backend[kind][key].get('status') == 0 and backend[kind][key].get('forbidden_status') == 0]
                part['live'] = snapshot('page_live_status', lambda: graph.concurrent_get(active_ids, 'id,business,is_published,is_permanently_closed', source, directory/'page_live_status.partial.json'))
                target_ids = [key for key in active_ids if key in part['target_self']]
                target_access = snapshot('page_target_access', lambda: graph.concurrent_get(target_ids, 'id,business,is_published,is_permanently_closed', target, directory/'page_target_access.partial.json'))
                part['source_visible'] = {key: value for key,value in part['live'].items() if 'read_error' not in value}
                part['target_visible'] = {key: value for key,value in target_access.items() if 'read_error' not in value}
            else:
                part['live'] = part['source_self']
            parts[kind] = part
        assets = []
        for kind, part in parts.items():
            for key in sorted(set(part['scope_ids']) | set(part['source_self'])):
                sr = part['source_self'].get(key)
                row = {'kind': kind, 'asset_id': key, 'business_id': args.business,
                       'name': (sr or part['source_visible'].get(key) or {}).get('name', ''),
                       'backend': backend[kind].get(key), 'source_live': part['live'].get(key) or {},
                       'source_list_tasks': (part['source_visible'].get(key) or {}).get('tasks'),
                       'source_admin_tasks': (part['source_admin'].get(key) or {}).get('tasks'),
                       'target_admin_tasks': (part['target_admin'].get(key) or {}).get('tasks'),
                       'checked_at': m.now()}
                assets.append(classify(row, sr, part['source_admin'].get(key), part['target_self'].get(key), part['target_admin'].get(key),
                                       key in part['source_visible'], key in part['target_visible'], shares[kind].get(key), key in owned))
        admins = m.discover_admins(graph, credentials, args.business, users, directory)
        pending = [a for a in assets if a['reason'] == 'same_edge_admin_task_disagreement']
        for c in admins:
            if not pending or c['id'] == admin['id']:
                continue
            page_ids = [a['asset_id'] for a in pending if a['kind'] == 'page']
            if not page_ids:
                break
            try:
                ps = m.filtered_pages(graph, identities['source']['meta_id'], c, page_ids, args.business)
                pt = m.filtered_pages(graph, identities['target']['meta_id'], c, page_ids, args.business)
            except m.GraphError as exc:
                if m.error_kind(exc.response) in ('restricted', 'rate_limited'):
                    raise
                m.emit('secondary_admin_read_failed', credential_id=c['id'], response=m.safe_response(exc.response))
                continue
            m.write_json(directory / ('secondary_admin_' + str(c['id']) + '.json'), {'source': ps, 'target': pt})
            for a in pending:
                key = a['asset_id']
                if a['kind'] == 'page' and tasks_equal(parts['page']['source_self'].get(key), ps.get(key)) and tasks_equal(parts['page']['target_self'].get(key), pt.get(key)):
                    a.update(category=a['difference'], reason='verified_full_access' if a['difference']=='already_sufficient' else 'needs_full_access',
                             corroborating_admin_id=c['id'], source_admin_tasks=(ps.get(key) or {}).get('tasks'), target_admin_tasks=(pt.get(key) or {}).get('tasks'))
            pending = [a for a in pending if a['category'] == 'unknown']
        manifest = {'format_version': 2, 'created_at': m.now(), 'code_version': args.code_version,
                    'graph_version': graph.version, 'business_id': args.business, 'identities': identities,
                    'read_admin': m.public_credential(admin), 'complete': True, 'assets': assets,
                    'recovery': restriction, 'write_interval': 30, 'token_interval': 300,
                    'permission_evidence': 'same_assigned_edge_admin_and_self_plus_individual_page_business_reads',
                    'page_scope': 'prior_frozen_page_ids_revalidated_individually',
                    'delegation_evidence': 'current_owned_business_or_bm_client_asset_permitted_tasks',
                    'counts': {kind: dict(collections.Counter(a['category'] for a in assets if a['kind']==kind)) for kind in ('account','page')}}
        m.write_json(directory/'manifest.json', manifest)
        digest = hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest()
        (directory/'manifest.sha256').write_text(digest+'\n')
        m.export_csv(directory/'comparison.csv', assets)
        m.emit('followup_audit_complete', counts=manifest['counts'], manifest_sha256=digest)
    finally:
        db.conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--prior-run', required=True)
    parser.add_argument('--code-version', required=True)
    parser.add_argument('--business', default=m.DEFAULT_BM)
    parser.add_argument('--graph-version', default='v26.0')
    parser.add_argument('--credential-config', default='/root/codex_test/authorize_fb_accounts_to_user913_20260819.py')
    args = parser.parse_args()
    import fcntl
    directory=Path(args.run_dir).resolve()
    if not str(directory).startswith('/mnt/data-disk/'):
        raise ValueError('run_directory_must_be_on_data_disk')
    if subprocess.check_output(['findmnt','-no','UUID','/mnt/data-disk'],text=True).strip() != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise ValueError('data_disk_uuid_mismatch')
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'.run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        audit(args)


if __name__ == '__main__':
    main()
