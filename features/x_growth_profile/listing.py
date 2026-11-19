"""Present growth evidence for the accounts visible on the X account list."""
import json
from datetime import datetime, timedelta, timezone

from . import store


def account_profiles(accounts, db_path=None, now=None):
    visible_ids = {int(account['id']) for account in accounts}
    result = {}
    if not visible_ids:
        return result

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=28)).isoformat()
    age_72h = (now - timedelta(hours=72)).isoformat()
    with store.connect(db_path) as db:
        db.execute('BEGIN')
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
            cold = profile.get('cold_start', {})
            by_catalog = {}
            for catalog, group in cold.get('by_catalog', {}).items():
                # Keep aggregate evidence visible without exposing peer identities or post IDs.
                by_catalog[catalog] = {
                    key: group[key] for key in ('source', 'stage', 'language', 'languages',
                                                'posts', 'accounts', 'dramas') if key in group
                }
                by_catalog[catalog]['recommendations'] = [
                    {key: item[key] for key in ('theme', 'score', 'posts', 'accounts', 'dramas') if key in item}
                    for item in group.get('recommendations', [])
                ]
            own = {
                stage: {key: counts[key] for key in ('posts', 'dramas') if key in counts}
                for stage, counts in cold.get('own_observations', {}).items()
                if stage in ('6h', '24h', '72h')
            }
            if by_catalog or own:
                result[str(account_id)]['cold_start'] = {'by_catalog': by_catalog, 'own_observations': own}
        # The same post may have several stages, but only one row per stage.
        # A null metric must not turn a missing observation into zero exposure.
        rows = db.execute("""
            SELECT p.account_id, COUNT(DISTINCT p.post_id) AS tracked,
                   COUNT(DISTINCT CASE WHEN p.published_at <= ? THEN p.post_id END) AS old_enough,
                   COUNT(CASE WHEN s.stage = '6h'  THEN 1 END) AS n6,
                   COUNT(CASE WHEN s.stage = '24h' THEN 1 END) AS n24,
                   COUNT(CASE WHEN s.stage = '72h' THEN 1 END) AS n72,
                   COUNT(CASE WHEN s.stage = '7d'  THEN 1 END) AS n7d,
                   SUM(CASE WHEN s.stage = '24h'
                       THEN json_extract(s.metrics_json, '$.organic_impressions') END) AS impressions24,
                   COUNT(CASE WHEN s.stage = '24h'
                       THEN json_extract(s.metrics_json, '$.organic_impressions') END) AS measured24
            FROM growth_post p LEFT JOIN post_snapshot s ON s.post_id = p.post_id
            WHERE p.published_at >= ? AND p.published_at <= ?
            GROUP BY p.account_id
            """, (age_72h, since, now.isoformat()))
        for row in rows:
            if row['account_id'] not in visible_ids:
                continue
            profile = result.setdefault(str(row['account_id']), {})
            profile['observations'] = {
                'tracked_posts': row['tracked'],
                'at_least_72h_posts': row['old_enough'],
                'stages': {'6h': row['n6'], '24h': row['n24'], '72h': row['n72'], '7d': row['n7d']},
                'organic_impressions_24h': row['impressions24'],
                'measured_posts_24h': row['measured24'],
            }
    return result
