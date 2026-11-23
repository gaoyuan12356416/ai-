"""Transport-independent shape checks for authenticated internal read actors."""


def validate_actor(actor):
    if not isinstance(actor,dict) or actor.get('role') not in ('user','admin'):
        raise ValueError('invalid_actor')
    if any(not isinstance(actor.get(k),str) or not actor[k].strip() for k in ('tenant_key','user_id')):
        raise ValueError('invalid_actor')
    if actor.get('_system_report') or actor.get('auth_type')=='api_token':
        raise ValueError('invalid_actor')
