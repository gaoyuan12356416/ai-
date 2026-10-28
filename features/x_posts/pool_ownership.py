"""Reporting ownership follows the exact pool row frozen on a system queue."""
from .attribution import AttributionError

POLICY = 'x-pool-creator-v1'
UNASSIGNED = '__unassigned_x_pool__'


def identity(connection, queue, users, *, unresolved_tenant=''):
    if queue.get('manual_run_id'):
        return None
    kind = queue.get('source_type')
    specs = {'material': ('pool_item_id', 'material_id'),
             'drama': ('drama_pool_item_id', 'content_id')}
    if kind not in specs:
        return None
    key, content = specs[kind]
    pool_id = queue.get(key)
    if not pool_id:
        if queue.get('schedule_run_id') and not unresolved_tenant:
            raise AttributionError('x_attribution_pool_binding_missing')
        return None  # Unbound legacy canaries are outside the pool correction.
    row = connection.execute('SELECT * FROM x_post_' + kind + '_pool WHERE id=?', (pool_id,)).fetchone()
    if not row or str(row[content]) != str(queue[content]):
        raise AttributionError('x_attribution_pool_identity_conflict')
    pool = dict(row)
    owner_id = str(pool.get('created_by_user_id') or '')
    candidates = [u for u in users if u['user_id'] == owner_id]
    if len({(u['tenant_key'], u['user_id']) for u in candidates}) > 1:
        raise AttributionError('x_attribution_pool_tenant_ambiguous')
    actor = candidates[0] if candidates and candidates[0]['tenant_key'] else None
    if actor:
        owner, tenant, name = actor['user_id'], actor['tenant_key'], actor['name']
        reason = ''
    elif unresolved_tenant:
        # Explicitly unassigned, never turn a recovery service actor into an admin.
        owner, tenant, name = UNASSIGNED, unresolved_tenant, '待归属（系统或缺失入池人）'
        reason = 'pool_creator_not_found'
    else:
        raise AttributionError('x_attribution_pool_creator_unavailable')
    return dict(owner=owner, tenant=tenant, owner_name=name,
                source_owner=owner_id, source_tenant=actor['tenant_key'] if actor else '',
                fallback_reason=reason, owner_basis=POLICY, pool_source_type=kind,
                pool_item_id=int(pool_id), pool_created_at=pool['created_at'])
