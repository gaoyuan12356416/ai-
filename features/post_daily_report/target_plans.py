"""Read frozen per-account plans without importing a publisher or creating state."""
import json
from datetime import date

from .common import parse_time, BEIJING


POLICY = 'target_midnight_v1'


def load_target_plans(db, prefix='tt_auto', day=None):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    effective = '9999-12-31'
    if 'social_random_policy' in names:
        row = db.execute('SELECT effective_date FROM social_random_policy WHERE policy=?', (POLICY,)).fetchone()
        if row:
            effective = date.fromisoformat(row[0]).isoformat()
    plans = {}
    if 'social_random_day' not in names:
        return effective, plans
    sql = 'SELECT source_key,local_date,config_version,config_json,targets_json FROM social_random_day WHERE source_key LIKE ?'
    args = [prefix + ':%']
    if day is not None:
        sql += ' AND local_date=?'
        args.append(day)
    for source, local_date, version, raw_config, raw_targets in db.execute(sql, args):
        template_id = int(source[len(prefix) + 1:])
        plan = {'version': 0, 'config': {}, 'slots': [], 'error': ''}
        plans[(template_id, local_date)] = plan
        try:
            plan['version'] = int(version)
            config, targets = json.loads(raw_config), json.loads(raw_targets)
            if not isinstance(config, dict) or not isinstance(targets, list):
                raise ValueError('invalid frozen plan')
            plan['config'] = config
            required_accounts = {str(x) for x in config.get('account_ids', [])}
            if required_accounts != {str(t['target_id']) for t in targets}:
                raise ValueError('incomplete frozen target scope')
            daily_count = int(config.get('schedule', {}).get('daily_count') or 0)
            seen = set()
            for target in targets:
                account = str(target['target_id'])
                if not account or not isinstance(target['slots'], list):
                    raise ValueError('invalid target')
                if daily_count and len(target['slots']) != daily_count:
                    plan['error'] = '账号随机日计划证据不完整，已知计划单列'
                for slot in target['slots']:
                    due = parse_time(slot['scheduled_at'])
                    if due is None or due.astimezone(BEIJING).date().isoformat() != local_date:
                        raise ValueError('invalid slot date')
                    key = (account, due)
                    if key not in seen:
                        seen.add(key)
                        plan['slots'].append({'account_id': account, 'due': due, 'ordinal': int(slot['ordinal'])})
        except (KeyError, TypeError, ValueError):
            plan['error'] = '账号随机日计划证据不完整，已知计划单列'
    return effective, plans
