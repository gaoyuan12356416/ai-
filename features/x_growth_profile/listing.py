"""Present growth evidence for the accounts visible on the X account list."""
import json

from . import store


def account_profiles(accounts, db_path=None):
    visible_ids = {int(account['id']) for account in accounts}
    result = {}
    if not visible_ids:
        return result

    with store.connect(db_path) as db:
        for account_id, raw in db.execute('SELECT account_id, data_json FROM growth_profile'):
            if account_id not in visible_ids:
                continue
            profile = json.loads(raw)
            # Per-drama occurrence counts are for the selector. The list only
            # needs grouped evidence for display.
            result[str(account_id)] = {
                'computed_at': profile.get('computed_at'),
                'mature_posts': profile.get('mature_posts', 0),
                'followers': profile.get('followers', {}),
                'policy': profile.get('policy', {}),
                'themes': [
                    {key: value for key, value in node.items() if key != 'drama_counts'}
                    for node in profile.get('themes', [])
                ],
            }
    return result
