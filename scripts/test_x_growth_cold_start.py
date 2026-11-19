"""Regression cases for cold-start evidence, source isolation and coverage denominators."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_growth_profile.collector import profile_coverage
from features.x_growth_profile import collector, store
from features.x_growth_profile.model import build_profiles

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def sample(aid, drama, reach=40, *, stage='72h', lang='en', catalog='dramawave', theme='revenge', age=None):
    hours = int(stage[:-1]) if age is None else age
    return dict(account_id=aid, post_id=f'{aid}-{drama}', content_id=drama, stage=stage,
                catalog=catalog, language=lang, duration=100,
                observed_at=NOW.isoformat(), published_at=(NOW-timedelta(hours=hours)).isoformat(),
                metrics={'organic_impressions': reach}, metadata={'themes': [theme]})


def build(rows, languages, tenants=None, followers=None):
    return build_profiles(rows, followers or {}, now=NOW, account_languages=languages, account_tenants=tenants)


def groups(profiles, aid):
    return profiles[aid]['cold_start']['by_catalog']


class ColdStartTest(unittest.TestCase):
    def test_alias_peers_and_cross_language_keep_real_counts_and_zero_native_history(self):
        rows = [sample(1, 'a', lang='pt'), sample(2, 'b', lang='pt-br')]
        p = build(rows, {1: 'pt', 2: 'pt-br', 3: 'pt-br', 4: 'ja'})
        self.assertEqual(groups(p, 1), {})  # Only one other channel survives leave-one-out.
        near = groups(p, 3)['dramawave']
        global_group = groups(p, 4)['dramawave']
        self.assertEqual((near['source'], near['stage'], near['languages']), ('language_peers', '72h', ['pt-br']))
        self.assertEqual((global_group['source'], global_group['language'], global_group['languages']),
                         ('catalog_peers', 'ja', ['pt-br']))
        for group in (near, global_group):
            self.assertEqual((group['posts'], group['accounts'], group['dramas']), (2, 2, 2))
            self.assertEqual(group['recommendations'], [
                dict(theme='revenge', score=50, weight=1., posts=2, accounts=2, dramas=2)])
        self.assertEqual(p[4]['mature_posts'], 0)
        self.assertEqual(p[4]['themes'], [])

    def test_never_pools_products_tenants_or_stages_to_claim_support(self):
        inconsistent = [
            [sample(1, 'a'), sample(2, 'b', catalog='moboreels')],
            [sample(1, 'a'), sample(2, 'a')],
            [sample(1, 'a'), sample(1, 'b')],
            [sample(1, 'a'), sample(2, 'b', stage='24h')],
            [sample(1, 'a'), sample(2, 'b', reach=0)],
            [sample(1, 'a'), sample(2, 'b', age=90)],
            [sample(1, 'a'), sample(2, 'b', theme='__unknown__')],
        ]
        channels = {1:'en', 2:'en', 3:'en'}
        for rows in inconsistent:
            with self.subTest(rows=rows):
                self.assertEqual(groups(build(rows, channels), 3), {})
        p = build([sample(1, 'a'), sample(2, 'b')], channels, {1:'A', 2:'B', 3:'A'})
        self.assertEqual(groups(p, 3), {})
        conflict = sample(2, 'b')
        conflict['metadata']['catalog'] = 'moboreels'
        self.assertEqual(groups(build([sample(1, 'a'), conflict], channels), 3), {})

    def test_visible_population_controls_donors_and_recipients(self):
        p = build([sample(1, 'a'), sample(2, 'b'), sample(3, 'c')], {1:'en', 2:'en', 4:'en'})
        self.assertEqual(set(p), {1, 2, 4})
        self.assertEqual(groups(p, 4)['dramawave']['posts'], 2)

    def test_preview_signal_low_reach_and_stage_precedence(self):
        rows = [sample(1, 'a', stage='6h'), sample(1, 'b', stage='6h'),
                sample(1, 'a', stage='24h'), sample(1, 'b', stage='24h'),
                sample(2, 'c'), sample(3, 'd')]
        p = build(rows, {1:'en', 2:'en', 3:'en'})
        ref = groups(p, 1)['dramawave']
        self.assertEqual((ref['source'], ref['stage'], ref['posts']), ('own', '24h', 2))
        self.assertEqual(p[1]['cold_start']['own_observations'], {
            '24h': dict(posts=2, dramas=2), '6h': dict(posts=2, dramas=2)})
        self.assertEqual(p[1]['mature_posts'], 0)
        self.assertEqual(p[1]['policy']['weights'], {})

    def test_only_local_catalog_maturity_suppresses_prior(self):
        rows = [sample(1, str(i), reach=1100) for i in range(8)]
        rows.extend([sample(2, 'a', catalog='moboreels'), sample(3, 'b', catalog='moboreels')])
        p = build(rows, {1:'en', 2:'en', 3:'en'})
        self.assertTrue(p[1]['themes'][0]['mature'])
        self.assertEqual(set(groups(p, 1)), {'moboreels'})

    def test_rank_uses_relative_performance_not_theme_count_or_duplicate_tag(self):
        rows = []
        for aid in [1, 2]:
            for drama, reach, theme in [('a', 90, 'revenge'), ('b', 70, 'revenge'),
                                        ('c', 10, 'family'), ('d', 5, 'family')]:
                r = sample(aid, drama, reach, theme=theme)
                r['metadata']['themes'].extend(['growth', theme])
                rows.append(r)
        p = build(rows, {1:'en', 2:'en', 3:'en'})
        group = groups(p, 3)['dramawave']
        topics = group['recommendations']
        self.assertEqual([item['theme'] for item in topics], ['revenge', 'growth'])
        self.assertGreater(topics[0]['score'], 50)
        self.assertEqual(topics[1]['score'], 50)
        self.assertEqual((topics[0]['posts'],group['posts']), (4,8))

    def test_missing_and_promoted_impressions_cannot_seed_direction(self):
        r = sample(1, 'a')
        for promoted in [None, 2]:
            r['metrics'] = dict(impressions=400, promoted_impressions=promoted)
            p = build([r, sample(2, 'b')], {1:'en',2:'en',3:'en'})
            self.assertEqual(groups(p, 3), {})

    def test_coverage_denominator_uses_account_cache_and_excludes_maturity_and_unknown(self):
        rows = [sample(1, str(i), reach=200) for i in range(8)]
        rows.extend([sample(2, 'x'), sample(3, 'y')])
        accounts = {i: {'followers_count': f} for i, f in enumerate([None, 88, 0, 1200, None, 200, 100], start=0) if i}
        followers = {5: [dict(observed_at=NOW.isoformat(), followers=0)],
                     6: [dict(observed_at=NOW.isoformat(), followers=1000)]}
        p = build(rows, {aid:'en' for aid in accounts}, followers=followers)
        coverage = profile_coverage(p, accounts)
        self.assertEqual(coverage['under_1000_without_mature_themes'], {'channels': 2, 'with_directions': 2})
        self.assertEqual(coverage['followers_unknown']['channels'], 1)
        self.assertEqual(coverage['followers_under_100']['channels'], 3)
        self.assertEqual(coverage['visible'], 6)

    def test_late_product_receipt_repairs_recorded_catalog_and_preserves_measured_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            account_db = Path(tmp) / 'accounts.sqlite3'
            account_db.touch()
            post_db = Path(tmp) / 'posts.sqlite3'
            with sqlite3.connect(post_db) as db:
                db.execute('CREATE TABLE x_post_queue(id,account_id,content_id,material_language,preflight_duration,tag)')
                db.execute("INSERT INTO x_post_queue VALUES(1,1,'story','en',600,'')")
                db.execute('CREATE TABLE x_post_publish_log(x_post_id,published_at,x_media_id,queue_id,status,unknown_outcome)')
                db.execute("INSERT INTO x_post_publish_log VALUES('p',?,'m',1,'published',0)", (NOW.isoformat(),))
            db.close()
            growth = Path(tmp) / 'growth.db'
            store.initialize(growth)
            receipt = {'1': {'drama': {'catalog': 'moboreels'}, 'tag_profile': {
                'classification': {'themes':['family'], 'catalog':'moboreels'}}}}
            responses = [{'items': {}}, {'items': receipt}, {'items': {}}]
            with patch.object(collector, 'ACCOUNT_DB', str(account_db)), patch.dict(
                    os.environ, {'X_INTERNAL_TOKEN':'test', 'X_POST_DB_PATH':str(post_db)}), patch.object(
                    collector, 'request', side_effect=responses), store.connect(growth, write=True) as db:
                accounts = {1:{'x_user_id':'123'}}
                collector.discover(db, accounts, NOW)
                self.assertEqual(db.execute('SELECT catalog FROM growth_post').fetchone()[0], 'dramawave')
                db.execute("INSERT INTO post_snapshot VALUES('p','6h',?,'{}')", (NOW.isoformat(),))
                collector.discover(db, accounts, NOW)
                collector.discover(db, accounts, NOW)
                product, labels = db.execute('SELECT catalog,metadata_json FROM growth_post').fetchone()
                self.assertEqual((product, json.loads(labels)['themes']), ('moboreels', ['family']))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM post_snapshot').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
