"""Offline behavior tests against the exact independently staged X runtime."""
import hashlib
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, os.environ['X_TAG_TEST_RUNTIME'])
from features.x_tag_publish.policy import Decision, ID_BASE, initialize_ids, label_index, lane_paths, catalog_labels, ranked_dramas
from features.x_tag_publish.selection import ProfileSelector
from features.x_tag_publish.pool import install_reservation_policy, material_candidates
from features.x_tag_publish import gateway
from features.x_tag_revenue.tags import VERSION as TAG_MAPPING_VERSION, localize_audit
from features.x_tag_revenue.taxonomy import VERSION as HIERARCHY_VERSION, classify
from features.x_posts import service as post
from features.x_auto_posts.core import XAutoPostStore
from scripts import test_x_auto_post_selector as fixture
from scripts.test_x_post_material_pool import plan_candidate


def profile(tags=None, revenue=10000, **meta):
    tags = tags or [('romance', 7000), ('revenge', 3000)]
    mapped = [('sweet_romance' if k == 'romance' else k, v) for k,v in tags]
    themes=[dict(key=k,name=k,revenue_cents=v,share_bp=v,matchable=k!='__unknown__') for k,v in mapped]
    audience=dict(key='female',name='女性',revenue_cents=revenue,share_bp=10000,matchable=True,children=themes)
    dtype=dict(key='local',name='本土剧',revenue_cents=revenue,share_bp=10000,matchable=True,children=[audience])
    return ({'accounts': {'999': dict(status='complete', revenue_cents=revenue,
        hierarchy_version=HIERARCHY_VERSION,types=[dtype])}},
        dict(version='frozen-1', end='2026-11-08', tag_mapping_version=TAG_MAPPING_VERSION,
             hierarchy_version=HIERARCHY_VERSION, **meta))


ACCOUNT = dict(id=101, x_user_id='999', username='channel', drama_language='en', long_video_eligible=False)


class PolicyTests(unittest.TestCase):
    def test_lazy_order_equals_full_sort_across_all_hierarchy_and_unknown_ranks(self):
        decision = Decision(ACCOUNT, 'test', profile([('romance',10000)]))
        metadata = [classify('Female,Sweet','translated'), classify('Female,Revenge','local'),
                    classify('Male,Sweet','local'), classify('Female,Sweet','local'), classify([])]
        candidates = [SimpleNamespace(content_id=str(i), language='en') for i in range(137)]
        catalog = {(row.content_id,'en'):metadata[int(row.content_id)%len(metadata)] for row in candidates}
        context = dict(decision=decision,catalog={},fallback='')
        with patch('features.x_tag_publish.policy.catalog_labels',side_effect=lambda source,batch:{(d.content_id,'en'):catalog[(d.content_id,'en')] for d in batch}) as load:
            actual=list(ranked_dramas(None,candidates,context,batch_size=7))
        self.assertEqual(actual,sorted(candidates,key=lambda d:decision.rank(catalog[(d.content_id,'en')])))
        self.assertEqual(len(context['catalog']),137)
        self.assertEqual(load.call_count,20)

    def test_success_in_best_branch_does_not_load_unused_catalog(self):
        candidates=[SimpleNamespace(content_id=str(i),language='en') for i in range(8281)]
        context=dict(decision=Decision(ACCOUNT,'test',profile([('romance',10000)])),catalog={},fallback='')
        with patch('features.x_tag_publish.policy.catalog_labels',side_effect=lambda source,batch:{(d.content_id,'en'):classify('Female,Sweet','local') for d in batch}) as load:
            iterator=ranked_dramas(None,candidates,context)
            self.assertEqual(next(iterator),candidates[0])
            iterator.close()
        self.assertEqual(load.call_count,1)
        self.assertEqual(len(load.call_args.args[1]),40)

    def test_no_primary_branch_scans_every_candidate_before_ordered_fallback(self):
        candidates=[SimpleNamespace(content_id=str(i),language='en') for i in range(81)]
        context=dict(decision=Decision(ACCOUNT,'test',profile([('romance',10000)])),catalog={},fallback='')
        def load(source,batch):
            return {(d.content_id,'en'):classify('Female,Revenge','local' if d.content_id=='80' else 'translated') for d in batch}
        with patch('features.x_tag_publish.policy.catalog_labels',side_effect=load) as query:
            iterator=ranked_dramas(None,candidates,context)
            self.assertEqual(next(iterator),candidates[-1])
            self.assertEqual(query.call_count,3)
            self.assertEqual(list(iterator),candidates[:-1])

    def test_late_metadata_failure_retains_remaining_old_order_without_duplicates(self):
        candidates=[SimpleNamespace(content_id=str(i),language='en') for i in range(5)]
        context=dict(decision=Decision(ACCOUNT,'test',profile([('romance',10000)])),catalog={},fallback='')
        first={('0','en'):classify('Female,Revenge','local'),('1','en'):classify('Female,Sweet','local')}
        with patch('features.x_tag_publish.policy.catalog_labels',side_effect=[first,OSError('unavailable')]):
            actual=list(ranked_dramas(None,candidates,context,batch_size=2))
        self.assertEqual(actual,[candidates[1],candidates[0],*candidates[2:]])
        self.assertEqual(context['fallback'],'candidate_metadata_unavailable')

    def test_catalog_batches_share_read_only_connection_without_truncation(self):
        cursors = []
        def cursor_factory(connection):
            cursor = Mock()
            def execute(sql, params):
                self.assertTrue(sql.startswith('SELECT DISTINCT '))
                self.assertEqual(params[0], 1479)
                self.assertLessEqual(len(params), 501)
                resource = 'ads_drama_resource' in sql
                self.assertIn('FORCE INDEX(content_id)' if resource else 'FORCE INDEX(ac)', sql)
                rows = [dict(content_id=key, app_id=1479, language='en', series_code='s'+key,
                             **({'labels':'Sweet'} if resource else {'resource_type':2})) for key in params[1:]]
                cursor.fetchmany.side_effect = [rows, []]
            cursor.execute.side_effect = execute
            cursors.append(cursor)
            return cursor
        source = SimpleNamespace(schema='kunlunads_dev', connection_factory=Mock(return_value=Mock()),
                                 _streaming_cursor=cursor_factory)
        dramas = [SimpleNamespace(content_id=str(i)) for i in range(501)]
        result = catalog_labels(source, dramas + dramas[:1])
        self.assertEqual(len(result), 501)
        self.assertTrue(all(row['drama_type']=='translated' and row['audience']=='female' for row in result.values()))
        source.connection_factory.assert_called_once_with()
        source.connection_factory.return_value.close.assert_called_once_with()
        self.assertEqual(len(cursors), 4)
        for cursor in cursors:
            cursor.close.assert_called_once_with()

    def test_catalog_query_failure_releases_cursor_and_connection(self):
        connection, cursor = Mock(), Mock()
        cursor.execute.side_effect = RuntimeError('source temporarily unavailable')
        source = SimpleNamespace(schema='kunlunads_dev', connection_factory=Mock(return_value=connection),
                                 _streaming_cursor=Mock(return_value=cursor))
        with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
            catalog_labels(source, [SimpleNamespace(content_id='1')])
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_legacy_profile_is_not_reinterpreted_using_aggregated_tag_weights(self):
        data,meta=profile();meta.pop('tag_mapping_version')
        self.assertEqual(Decision(ACCOUNT,'task',(data,meta)).reason,'portrait_mapping_outdated')

    def test_frozen_english_audit_only_gets_a_chinese_response_projection(self):
        audit=dict(policy_version='old-policy',profile_version='old-snapshot',selected_tag='Revenge',
                   preferred_tag='Counterattack',candidate_labels=['Revenge','復仇'],
                   weights=[dict(key='Revenge',name='Revenge',revenue_cents=100)])
        before=json.dumps(audit,sort_keys=True)
        rendered=localize_audit(audit)
        self.assertEqual(rendered['selected_tag'],'复仇')
        self.assertEqual(rendered['preferred_tag'],'逆袭')
        self.assertEqual(rendered['weights'][0]['key'],'Revenge')
        self.assertEqual(json.dumps(audit,sort_keys=True),before)
        from features.x_auto_posts.service import _public_selection
        public = _public_selection({'reservation': {'tag_profile': audit}})
        self.assertEqual(public['reservation']['tag_profile']['selected_tag'], '复仇')
        self.assertEqual(json.dumps(audit,sort_keys=True),before)

    def test_weighted_frequency_and_replay(self):
        counts = {'sweet_romance': 0, 'revenge': 0}
        for i in range(4000):
            a = Decision(ACCOUNT, ['task', i], profile())
            b = Decision(ACCOUNT, ['task', i], profile())
            self.assertEqual(a.preferred(), b.preferred())
            counts[a.preferred()[2]] += 1
        self.assertTrue(0.67 < counts['sweet_romance'] / 4000 < 0.73, counts)

    def test_unknown_and_negative_not_preferences(self):
        decision = Decision(ACCOUNT, 'a', profile([('__unknown__', 10000)]))
        self.assertEqual(decision.audit(classify('Female,Sweet','local'))['fallback_reason'], 'no_valid_theme_profile')
        self.assertEqual(Decision(ACCOUNT, 'a', profile([('romance', -100)], -100)).reason, 'no_positive_revenue')

    def test_no_history_missing_source_and_incomplete_are_distinct(self):
        for snapshot, reason in [(({'accounts': {}}, {}), 'no_revenue_history'),
                                 (({'accounts': {}}, {'unavailable': True}), 'portrait_unavailable'),
                                 (profile(missing_dates=['2026-11-01']), 'portrait_incomplete')]:
            self.assertEqual(Decision(ACCOUNT, 1, snapshot).reason, reason)

    def test_multilabel_normalization_and_mapping_conflict(self):
        rows = [dict(content_id='d', app_id=1479, language='jp', series_code='s', labels=' Romance ,romance,REVENGE')]
        value = label_index(rows)[('d', 'ja')]
        self.assertEqual(set(value['tags']), {'romance', 'revenge'})
        rows.append(dict(rows[0], series_code='other'))
        self.assertEqual(label_index(rows)[('d', 'ja')]['tags'], {})

    def test_fallback_and_stale_are_visible(self):
        d = Decision(ACCOUNT, 1, profile(stale=True))
        result = d.audit(classify('Female,unrelated','local'))
        self.assertEqual(result['fallback_reason'], 'no_matching_eligible_theme')
        self.assertTrue(result['profile_stale'])
        self.assertEqual(result['profile_version'], 'frozen-1')

    def test_paths_cannot_point_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                lane_paths(dict(X_TAG_STATE_ROOT=tmp, X_POST_DB_PATH=str(Path(tmp).parent/'old.db'), X_AUTO_POST_DB_PATH=tmp+'/auto.db'))


class IsolationTests(unittest.TestCase):
    def test_new_numeric_namespace_survives_attribution_freeze_and_analytics(self):
        from features.x_posts import attribution
        from features.social_analytics.adapters import read_x
        from features.social_analytics.warehouse_refresh import Reader
        self.assertTrue(callable(Reader))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'posts.db';users=root/'users.db'
            store=post.XPostStore(path);initialize_ids(path)
            with closing(sqlite3.connect(users)) as db, db:
                db.execute('CREATE TABLE drama_admin_user(user_id TEXT,tenant_key TEXT,email TEXT,name TEXT)')
                db.execute("INSERT INTO drama_admin_user VALUES('owner','tenant','test@example.test','Test')")
            item=store.add_pool_materials(['101'],dict(user_id='owner',name='Test'),
                validation_checks=[dict(material_id='101',error_code='',error_message='')])['items'][0]
            queue=store.create_daily_plan('2026-07-23','2026-07-22',[plan_candidate(2,item)],require_pool=True)['queues'][0]
            log=store.reserve_log(queue['id'])
            resolver=attribution.Resolver(path,root/'unused-auto.db',users,lambda email:['248'],lambda uid:['test@example.test'])
            context=resolver.resolve(queue['id'],log['id'])
            attribution.freeze(path,log,context)
            self.assertEqual(context['af_c_id'],str(queue['id']))
            self.assertEqual(context['owner'],'owner')
            self.assertGreater(int(context['af_c_id']),ID_BASE)
            url=attribution.build_url(context,log)
            store.prepare_log(log['id'],url,post._build_short_url('https://gy.g2flow.com/s2l-tag',log['id']),'preview')
            catalog=read_x(path)
            self.assertEqual(len(catalog),1)
            self.assertEqual(catalog[0]['key'][0],str(queue['id']))

    def test_bootstrap_copies_only_active_metrics_and_leaves_every_business_store_empty(self):
        from features.x_tag_publish.bootstrap import initialize
        with tempfile.TemporaryDirectory() as tmp:
            old_path=Path(tmp)/'old.db';old=XAutoPostStore(old_path)
            day=datetime.now(timezone.utc).date().isoformat()
            generation=old.record_metric_generation(platform=1,metric_date=day,product='dramawave',
                rows=[dict(content_id='C1',material_id='101',spend='12',af_revenue0='15')],
                refreshed_at_utc=day+'T00:00:00Z')
            old.activate_metric_generation(generation.id)
            before=old_path.read_bytes()
            new=Path(tmp)/'new'
            with patch.dict(os.environ,X_TAG_STATE_ROOT=str(new),X_POST_DB_PATH=str(new/'post.db'),
                            X_AUTO_POST_DB_PATH=str(new/'auto.db')):
                result=initialize(old_path)
                self.assertEqual(result['metric_days'],1)
            self.assertEqual(old_path.read_bytes(),before)
            with closing(sqlite3.connect(new/'auto.db')) as db:
                for name in ('x_auto_template','x_auto_run','x_auto_task','x_auto_material_ledger'):
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0],0)
                self.assertEqual(db.execute('SELECT generation_id,spend,af_revenue0 FROM x_auto_metric_daily').fetchone(),
                                 (ID_BASE+generation.id,'12','15'))
            with closing(sqlite3.connect(new/'post.db')) as db:
                for name in ('x_post_queue','x_post_publish_log','x_post_material_pool','x_tag_queue_selection'):
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0],0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM x_post_schedule_config WHERE enabled<>0 OR account_ids_json<>'[]' OR publish_times_json<>'[]'").fetchone()[0],0)

    def test_account_storage_is_read_only_and_refresh_delegates_to_existing_owner(self):
        from features.x_tag_publish.runtime import install
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);account=root/'accounts.db'
            with closing(sqlite3.connect(account)) as db, db:db.execute('CREATE TABLE secret(value TEXT)')
            runtime=SimpleNamespace(POST_DB_PATH=root/'posts.db',DB_PATH=account,INTERNAL_TOKEN='new-token',
                create_post_schedule_plan_request=lambda p:{'queues':[]},ServiceError=gateway.Error)
            with patch.dict(os.environ,X_TAG_STATE_ROOT=tmp,X_POST_DB_PATH=str(root/'posts.db'),
                            X_AUTO_POST_DB_PATH=str(root/'auto.db'),X_TAG_ACCOUNT_BROKER_TOKEN='old-broker'), \
                 patch.object(post,'_material_fifo_selection_matches',post._material_fifo_selection_matches), \
                 patch('features.x_posts.attribution.resolve_via_loopback'), \
                 patch('features.x_tag_publish.transport.rpc',return_value={'item':dict(ACCOUNT,publish_approved=True)}) as rpc:
                install(runtime)
                with closing(runtime.db_connect()) as db:
                    with self.assertRaises(sqlite3.OperationalError):db.execute("INSERT INTO secret VALUES('changed')")
                self.assertEqual(runtime.verify_account(101,{'user_id':'u'},require_publish_approved=True)['id'],101)
                self.assertEqual(rpc.call_args.args[:3],('http://127.0.0.1:8810','/internal/accounts/101/verify','old-broker'))

    def test_same_material_account_and_day_can_be_reserved_in_each_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path, new_path = Path(tmp)/'old.db', Path(tmp)/'new.db'
            old, new = post.XPostStore(old_path), post.XPostStore(new_path)
            initialize_ids(new_path)
            actor = dict(user_id='test', name='Test')
            plans = []
            for store in (old, new):
                items = store.add_pool_materials(['101'], actor,
                    validation_checks=[dict(material_id='101', error_code='', error_message='')])['items']
                plan = store.create_daily_plan('2026-07-23', '2026-07-22', [plan_candidate(2, items[0])], require_pool=True)
                plans.append(plan)
                self.assertEqual(store.available_pool_items(), [])
                self.assertEqual(store.query_material_keys(['101']), ['101'])
            self.assertNotEqual(plans[0]['queues'][0]['id'], plans[1]['queues'][0]['id'])
            self.assertGreater(plans[1]['queues'][0]['id'], ID_BASE)
            before = old_path.read_bytes()
            new.add_pool_materials(['102'], actor, validation_checks=[dict(material_id='102', error_code='', error_message='')])
            self.assertEqual(old_path.read_bytes(), before)

    def test_initial_business_state_empty_and_ids_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'auto.db'
            XAutoPostStore(path)
            initialize_ids(path)
            initialize_ids(path)
            with closing(sqlite3.connect(path)) as db:
                for table in ('x_auto_template','x_auto_run','x_auto_task'):
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
                self.assertGreaterEqual(db.execute("SELECT seq FROM sqlite_sequence WHERE name='x_auto_task'").fetchone()[0],ID_BASE)

    def test_new_short_link_namespace_and_legacy_rejection(self):
        self.assertIn('/s2l-tag/', post._build_short_url('https://gy.g2flow.com/s2l-tag', ID_BASE+1))
        with self.assertRaises(post.XPostError):
            post._build_short_url('https://gy.g2flow.com/s2l', 1)

    def test_profile_order_relaxes_only_fifo_not_membership_or_available_set(self):
        original = post._material_fifo_selection_matches
        with patch.object(post, '_material_fifo_selection_matches', original):
            install_reservation_policy(post)
            pools = [dict(id=1,last_error_code='',last_checked_at=''),dict(id=2,last_error_code='',last_checked_at='')]
            candidate = dict(pool_item_id=2, account_id=101, material_language='en', preflight_duration=30)
            self.assertFalse(original(pools,[candidate],[101],[]))
            self.assertTrue(post._material_fifo_selection_matches(pools,[candidate],[101],[]))
            self.assertFalse(post._material_fifo_selection_matches(pools,[dict(candidate,preflight_duration=2000)],[101],[]))
            self.assertFalse(post._material_fifo_selection_matches(pools,[dict(candidate,pool_item_id=3)],[101],[]))


class SelectorTests(unittest.TestCase):
    def make(self):
        source = fixture.FakeSource([fixture.drama('C1'), fixture.drama('C2')],
            {'C1':[fixture.material('101','C1')], 'C2':[fixture.material('201','C2')]})
        source.schema='test'
        source._stream_select=lambda *_: [dict(content_id=c,app_id=1479,language='en',series_code='s'+c,labels='Female,'+t,resource_type=1)
                                         for c,t in [('C1','revenge'),('C2','romance')]]
        store=fixture.FakeStore()
        selector=ProfileSelector(source,fixture.MetricWindowRepository(fixture.FakeMetricStore()),
            fixture.FakePublishedHistory(),store,material_validator=fixture.FakeValidator())
        return selector,store

    def test_selected_profile_is_saved_with_reservation(self):
        selector,store=self.make()
        with patch.dict(os.environ, X_POST_AUTOMATION_INTERNAL_URL='http://127.0.0.1:8821',X_POST_AUTO_INTERNAL_TOKEN='a'*48), \
             patch('features.x_tag_publish.selection.XPostAutoBridgeClient') as bridge, \
             patch('features.x_tag_publish.policy.portrait_snapshot', return_value=profile([('romance',10000)])):
            bridge.return_value.account_snapshot.return_value=ACCOUNT
            result=selector.select_and_reserve(fixture.request())
        self.assertEqual(result.drama.content_id,'C2')
        self.assertEqual(store.reserve_calls[0]['selection_snapshot']['tag_profile']['selected_tag'],'甜宠恋爱')
        self.assertEqual(result.reservation['tag_profile']['profile_version'],'frozen-1')

    def test_executor_claim_phase_preserves_profile_and_freezes_audit(self):
        from features.x_auto_posts.publisher import AutoPostExecutor
        selector,store=self.make()
        executor=object.__new__(AutoPostExecutor)
        executor.selector,executor.store=selector,store
        phase=executor._phase_selector('existing-task-lease')
        with patch.dict(os.environ, X_POST_AUTOMATION_INTERNAL_URL='http://127.0.0.1:8821',X_POST_AUTO_INTERNAL_TOKEN='a'*48), \
             patch('features.x_tag_publish.selection.XPostAutoBridgeClient') as bridge, \
             patch('features.x_tag_publish.policy.portrait_snapshot', return_value=profile([('romance',10000)])):
            bridge.return_value.account_snapshot.return_value=ACCOUNT
            result=phase.select_and_reserve(fixture.request())
        self.assertEqual(result.drama.content_id,'C2')
        saved=store.reserve_calls[0]
        self.assertEqual(saved['claim_token'],'existing-task-lease')
        self.assertEqual(saved['selection_snapshot']['tag_profile']['hierarchy_version'],HIERARCHY_VERSION)
        self.assertEqual(saved['selection_snapshot']['tag_profile']['selected_path_names'],['本土剧','女性','甜宠恋爱'])
        store.existing=result.reservation
        with patch('features.x_tag_publish.selection.XPostAutoBridgeClient',side_effect=AssertionError('must keep frozen reservation')):
            again=executor._phase_selector('next-lease').select_and_reserve(fixture.request())
        self.assertTrue(again.idempotent)
        self.assertEqual(len(store.reserve_calls),1)

    def test_reentry_does_not_rerank_or_query_new_profile(self):
        selector,store=self.make();store.existing={'material_id':'101','task_id':11,'tag_profile':{'profile_version':'old'}}
        with patch('features.x_tag_publish.selection.XPostAutoBridgeClient',side_effect=AssertionError('must not rerank')):
            result=selector.select_and_reserve(fixture.request())
        self.assertTrue(result.idempotent)
        self.assertEqual(result.reservation['tag_profile']['profile_version'],'old')

    def test_label_outage_degrades_without_aborting_valid_material(self):
        selector,store=self.make();selector.source._stream_select=Mock(side_effect=OSError())
        with patch.dict(os.environ, X_POST_AUTOMATION_INTERNAL_URL='http://127.0.0.1:8821',X_POST_AUTO_INTERNAL_TOKEN='a'*48), \
             patch('features.x_tag_publish.selection.XPostAutoBridgeClient') as bridge, \
             patch('features.x_tag_publish.policy.portrait_snapshot', return_value=profile()):
            bridge.return_value.account_snapshot.return_value=ACCOUNT
            result=selector.select_and_reserve(fixture.request())
        self.assertEqual(result.reservation['tag_profile']['fallback_reason'],'candidate_metadata_unavailable')


class GatewayTests(unittest.TestCase):
    def test_account_refresh_preserves_empty_internal_body_contract(self):
        h=object.__new__(gateway.Handler);h.path=gateway.AUTO+'/accounts/101/verify';h.command='POST'
        h.headers={'Cookie':'valid','Origin':'https://ai.yingliangads.com'};h.body=lambda:{}
        with patch.object(gateway,'authorize',return_value=dict(user_id='owner',tenant_key='t',name='Owner')), \
             patch.object(gateway,'rpc',return_value={'item':ACCOUNT}) as rpc, \
             patch.dict(os.environ,X_AUTO_POST_INTERNAL_TOKEN='a'*48):h.dispatch()
        self.assertEqual(rpc.call_args.kwargs['payload'],{})

    def test_schedule_put_uses_new_service_and_trusted_actor(self):
        h=object.__new__(gateway.Handler);h.path=gateway.POST+'/material-pool/schedule';h.command='PUT'
        h.headers={'Cookie':'valid','Origin':'https://ai.yingliangads.com'}
        h.body=lambda:dict(enabled=False,account_ids=[],publish_times=[])
        with patch.object(gateway,'authorize',return_value=dict(user_id='owner',tenant_key='t',name='Owner')), \
             patch.object(gateway,'post_rpc',return_value={'item':{}}) as rpc:
            h.dispatch()
        self.assertEqual(rpc.call_args.args[0],'material-pool/schedule/save')
        self.assertEqual(rpc.call_args.args[1]['actor']['user_id'],'owner')
        self.assertFalse(rpc.call_args.args[1]['settings']['enabled'])

    def test_cookie_required_and_acl_checked_every_time(self):
        with self.assertRaises(gateway.Error):gateway.authorize('', '/probe')
        user=dict(user_id='u',tenant_key='t',name='User',role='user')
        with patch.object(gateway,'rpc',side_effect=[dict(authenticated=True,user=user),{},dict(authenticated=True,user=user),gateway.Error('denied','',403)]) as call:
            self.assertEqual(gateway.authorize('cookie','/probe')['user_id'],'u')
            with self.assertRaises(gateway.Error):gateway.authorize('cookie','/probe')
            self.assertEqual(call.call_count,4)
            self.assertEqual(call.call_args.kwargs['cookie'],'cookie')

    def test_gateway_overwrites_untrusted_actor(self):
        h=object.__new__(gateway.Handler);h.path=gateway.AUTO+'/templates';h.command='POST'
        h.headers={'Cookie':'valid','Origin':'https://ai.yingliangads.com'}
        h.body=lambda:dict(name='x',_actor=dict(user_id='attacker'))
        with patch.object(gateway,'authorize',return_value=dict(user_id='owner',tenant_key='tenant',name='Owner')), \
             patch.object(gateway,'rpc',return_value={'ok':True}) as call, \
             patch.dict(os.environ,X_AUTO_POST_INTERNAL_TOKEN='a'*48):
            h.dispatch()
        self.assertEqual(call.call_args.kwargs['payload']['_actor']['user_id'],'owner')
        self.assertEqual(call.call_args.args[0],'http://127.0.0.1:18843')

    def test_cross_origin_write_rejected(self):
        h=object.__new__(gateway.Handler);h.path=gateway.AUTO+'/templates';h.command='POST'
        h.headers={'Cookie':'valid','Origin':'https://example.test'}
        with patch.object(gateway,'authorize',return_value=dict(user_id='owner',tenant_key='tenant',name='Owner')):
            with self.assertRaises(gateway.Error) as exc:h.dispatch()
        self.assertEqual(exc.exception.status,403)


class PoolMatchingTests(unittest.TestCase):
    def test_live_scheduler_and_template_service_bind_profile_selectors(self):
        import inspect
        from scripts import x_post_schedule_runner as runner
        from features.x_auto_posts import service as auto
        self.assertIs(inspect.signature(runner.execute_schedule_tick).parameters['material_candidate_loader'].default,
                      material_candidates)
        self.assertIs(auto.TwoStageSelector, ProfileSelector)

    def select(self, accounts, candidates, *, fail=None, snapshot=None, identity=None):
        from scripts import x_post_schedule_runner as runner
        with tempfile.TemporaryDirectory() as tmp:
            config=SimpleNamespace(material_pool_path='/pool',scan_limit=1000,candidate_pool_limit=50,
                                   mysql_database='test',work_dir=tmp,duration_policy_version=2)
            sidecar=Mock();sidecar.available_pool_items.return_value=candidates
            def preflight(cfg,candidate,account,*args,**kwargs):
                if fail and fail(candidate,account):
                    raise runner.CandidatePreflightError('media rejected','media_invalid')
                return dict(candidate,account_id=account['id'])
            with patch.object(runner,'_open_source_connection',return_value=Mock()), \
                 patch.object(runner,'select_pool_candidates',return_value=(candidates,[])), \
                 (nullcontext() if identity else patch.object(runner,'_stable_shuffled',side_effect=lambda rows,_:list(rows))), \
                 patch.object(runner,'_preflight_candidate',side_effect=preflight), \
                 patch.object(runner,'_record_pool_checks_best_effort') as checks, \
                 patch('features.x_tag_publish.pool.portrait_snapshot',return_value=snapshot or profile([('romance',10000)])), \
                 patch('features.x_tag_publish.pool.collect_classifications',return_value={(r['content_id'],r['material_language']):classify(['Female']+r['drama_labels'],'local') for r in candidates}), \
                 patch.dict(os.environ,PIPELINE_X_DISPATCH_ENABLED='0'):
                result=material_candidates(config,sidecar,accounts,source_date='2026-11-08',
                    connection_factory=None,downloader=None,prober=None,repair_client=None,timestamp=1,
                    assignment_identity=identity)
                recorded=[row for call in checks.call_args_list for row in call.args[2]]
            return result,recorded

    def candidate(self,mid,tag='romance',**extra):
        return dict(material_id=str(mid),content_id='C'+str(mid),pool_item_id=mid,material_language='en',source_duration=30,
                    drama_labels=[tag],media_kind='video',**extra)

    def test_premium_first_draw_preserves_short_video_for_ordinary_account(self):
        from features.x_tag_publish.policy import canonical
        from scripts import x_post_schedule_runner as runner
        accounts = [dict(ACCOUNT, long_video_eligible=True), dict(ACCOUNT, id=102)]
        identity = dict(source_type='material', run_date='2026-11-09', publish_time='03:00',
                        version=1, account_ids=[101, 102])
        self.assertEqual(runner._stable_shuffled(accounts, [canonical(identity), 'tag-profile-accounts'])[0]['id'], 101)
        short, long = self.candidate(1), dict(self.candidate(2), source_duration=2000)
        for snapshot in (profile([('romance', 10000)]), ({'accounts': {}}, {})):
            for rows in ([short, long], [long, short]):
                with self.subTest(snapshot=snapshot, order=[r['material_id'] for r in rows]):
                    result, _ = self.select(accounts, rows, snapshot=snapshot, identity=identity)
                    self.assertEqual([(r['account_id'], r['material_id']) for r in result], [(101, '2'), (102, '1')])
                    self.assertEqual(result.shortage_snapshot['languages'], [])
                    replay, _ = self.select(accounts, rows, snapshot=snapshot, identity=identity)
                    self.assertEqual(result, replay)

    def test_same_capability_contention_keeps_stable_rotation_and_input_order(self):
        from features.x_tag_publish.policy import canonical
        from scripts import x_post_schedule_runner as runner
        winners = set()
        for premium in (False, True):
            accounts = [dict(ACCOUNT, id=i, long_video_eligible=premium) for i in (101, 102)]
            for hour in range(12):
                identity = dict(source_type='material', run_date='2026-11-09', publish_time=f'{hour:02}:00',
                                version=1, account_ids=[101, 102])
                expected = runner._stable_shuffled(accounts, [canonical(identity), 'tag-profile-accounts'])[0]['id']
                result, _ = self.select(accounts, [self.candidate(1), self.candidate(2)], identity=identity)
                self.assertEqual([r['account_id'] for r in result], [101, 102])
                self.assertEqual(next(r['account_id'] for r in result if r['material_id'] == '1'), expected)
                winners.add(expected)
        self.assertEqual(winners, {101, 102})

    def test_only_long_stock_skips_ordinary_without_blocking_premium(self):
        accounts = [ACCOUNT, dict(ACCOUNT, id=102, long_video_eligible=True)]
        result, _ = self.select(accounts, [dict(self.candidate(1), source_duration=2000)])
        self.assertEqual([(r['account_id'], r['material_id']) for r in result], [(102, '1')])
        self.assertEqual(result.shortage_snapshot['languages'][0]['missing'], 1)

    def test_profile_rank_language_duration_and_lane_dedupe(self):
        rows=[self.candidate(1,'revenge'),self.candidate(2)]
        rows.insert(0,dict(self.candidate(3),material_language='ja'))
        rows.insert(0,dict(self.candidate(4),source_duration=3000))
        result,_=self.select([ACCOUNT,dict(ACCOUNT,id=102)],rows)
        self.assertEqual([r['material_id'] for r in result],['2','1'])
        self.assertEqual(result[0]['tag_profile']['selected_tag'],'甜宠恋爱')
        self.assertEqual(result[1]['tag_profile']['fallback_reason'],'no_matching_eligible_theme')

    def test_failed_candidate_does_not_block_other_account_or_poison_success(self):
        rows=[self.candidate(1),self.candidate(2,'revenge')]
        result,checks=self.select([ACCOUNT,dict(ACCOUNT,id=102)],rows,
            fail=lambda c,a:c['material_id']=='1' and a['id']==101)
        self.assertEqual([r['material_id'] for r in result],['2','1'])
        self.assertFalse(any(r['material_id']=='1' for r in checks))

    def test_no_history_falls_back_and_missing_material_is_not_success(self):
        result,_=self.select([ACCOUNT],[self.candidate(1)],snapshot=({'accounts':{}},{}))
        self.assertEqual(result[0]['tag_profile']['fallback_reason'],'no_revenue_history')
        from scripts.x_post_schedule_runner import ScheduleRunError
        with self.assertRaises(ScheduleRunError) as exc:self.select([ACCOUNT],[])
        self.assertTrue(hasattr(exc.exception,'shortage_snapshot'))


if __name__=='__main__':
    unittest.main()
