import copy
import hashlib
import json
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
import zlib

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_x_hierarchy import portrait, snapshot, ACCOUNT
from features.x_tag_revenue.storage import Store, ProfileWriter, projections, encoded, SnapshotExpired
from features.x_tag_revenue.routes import dispatch
from features.x_tag_revenue.integration import patch_columns
from features.x_tag_publish.policy import Decision
from features.x_tag_revenue.http_server import internal_profiles

VERSION = '20261110T020000000000Z'


class StorageTests(unittest.TestCase):
    def test_internal_auth_precedes_database_query(self):
        handler = Mock(); handler.headers = {}
        with patch('features.x_tag_revenue.http_server.internal_token', return_value='fixture'), patch('features.x_tag_revenue.http_server.read_snapshot') as read:
            internal_profiles(handler)
            read.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[0], 401)

    def test_internal_reader_bounds_ids_and_returns_only_tree(self):
        handler = Mock()
        payload = json.dumps({'x_user_ids': ['123']}).encode()
        handler.headers = {'Authorization': 'Bearer fixture', 'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with patch('features.x_tag_revenue.http_server.internal_token', return_value='fixture'), patch('features.x_tag_revenue.http_server.read_snapshot', return_value=({'accounts': {}}, {'version': VERSION})) as read:
            internal_profiles(handler)
            self.assertEqual(read.call_args.args[1], ['123'])
        self.assertEqual(handler.send_json.call_args.args[0], 200)
        payload = json.dumps({'x_user_ids': ['123'] * 201}).encode()
        handler.headers['Content-Length'] = str(len(payload)); handler.rfile = io.BytesIO(payload)
        with patch('features.x_tag_revenue.http_server.internal_token', return_value='fixture'), patch('features.x_tag_revenue.http_server.read_snapshot') as read:
            internal_profiles(handler); read.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[0], 400)

    def test_publisher_uses_authenticated_loopback_and_reports_failure(self):
        from features.x_tag_publish.policy import portrait_snapshot
        from features.x_tag_publish.transport import Error
        with patch.dict('os.environ', {'X_AUTO_POST_INTERNAL_TOKEN': 'fixture'}), patch('features.x_tag_publish.policy.rpc', return_value={'data': {'accounts': {}}, 'meta': {'version': VERSION}}) as rpc:
            self.assertEqual(portrait_snapshot([123])[1]['version'], VERSION)
            self.assertEqual(rpc.call_args.kwargs['payload'], {'x_user_ids': ['123']})
            self.assertEqual(rpc.call_args.args[0], 'http://127.0.0.1:8896')
            rpc.side_effect = Error('service_unavailable')
            self.assertTrue(portrait_snapshot(['123'])[1]['unavailable'])

    def test_small_projections_preserve_hierarchy_and_decision(self):
        data, meta = snapshot()
        full = data['accounts']['channel']
        summary, tree = projections(full)
        self.assertNotIn('dramas', encoded(tree))
        self.assertNotIn('children', encoded(summary))
        self.assertEqual(summary['focus']['type']['key'], 'local')
        self.assertEqual(summary['focus']['audience']['key'], 'female')
        self.assertEqual(summary['focus']['audiences'][0]['share_bp'], 8000)
        self.assertEqual(summary['focus']['themes'][0]['share_bp'], 5000)
        reduced = {'accounts': {'channel': tree}}
        for seed in range(100):
            before, after = Decision(ACCOUNT, seed, (data, meta)), Decision(ACCOUNT, seed, (reduced, meta))
            self.assertEqual(before.orders, after.orders)
            self.assertEqual(before.node_weights, after.node_weights)

    def test_list_only_reads_summary_for_allowed_ids_in_one_transaction(self):
        reader = Mock()
        reader.query.return_value = [{'meta': {'version': VERSION}}, {'x_user_id': '123', 'profile': {'status': 'complete'}}]
        data, meta = Store(reader=reader).load(['123'], view='summary')
        sql = '\n'.join(reader.query.call_args.args[0])
        self.assertIn('SET @xtr_version=', sql)
        self.assertIn("x_user_id IN (0x313233)", sql)
        self.assertNotIn('detail_json', sql)
        self.assertNotIn('tree_json', sql)
        self.assertEqual(list(data['accounts']), ['123'])

    def test_detail_page_selects_only_requested_drama_positions(self):
        reader = Mock()
        reader.query.return_value = [{'total': 51, 'items': [{'name': 'last'}, None]}]
        result = Store(reader=reader).dramas('123', VERSION, '$.types[0].children[1].children[2].dramas', 2)
        self.assertEqual(result['items'], [{'name': 'last'}])
        sql = reader.query.call_args.args[0][0]
        self.assertIn('.dramas[50]', sql)
        self.assertIn('.dramas[99]', sql)
        self.assertNotIn('.dramas[0]', sql)
        self.assertIn('profile_version=0x', sql)
        reader.query.return_value = [{'total': 51, 'items': None}]
        self.assertEqual(Store(reader=reader).dramas('123', VERSION, '$.types[0].children[1].children[2].dramas', 3)['items'], [])

    def test_expired_version_is_explicit(self):
        reader = Mock(); reader.query.return_value = []
        with self.assertRaises(SnapshotExpired):
            Store(reader=reader).load(['123'], version=VERSION)
        with self.assertRaises(ValueError):
            Store(reader=reader).load(['123'], version="'; DROP TABLE x")

    def test_acl_rejection_precedes_database_access(self):
        handler = Mock(); handler.headers = {'Cookie': 'test'}
        with patch('features.x_tag_revenue.routes.Store') as store:
            dispatch(handler, urlsplit('/api/x-channel-tag-revenue/9'), lambda _: (200, [dict(id=8, x_user_id='123')]))
            store.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[0], 404)

    def test_leaf_route_keeps_version_and_page(self):
        data = portrait(); data['accounts']['123'] = data['accounts'].pop('channel')
        _, tree = projections(data['accounts']['123'])
        store = Mock()
        store.load.return_value = ({'accounts': {'123': tree}}, dict(version=VERSION, source_generated_at='2026-11-10T02:00:00+00:00'))
        store.dramas.return_value = dict(items=[{'name': 'page two'}], total=75, page=2, page_size=50)
        handler = Mock(); handler.headers = {}
        with patch('features.x_tag_revenue.routes.Store', return_value=store):
            dispatch(handler, urlsplit('/api/x-channel-tag-revenue/8?path=local/female/sweet_romance&page=2&version='+VERSION),
                     lambda _: (200, [dict(id=8, x_user_id='123')]))
        status, body = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(body['portrait']['pagination']['total'], 75)
        self.assertEqual(store.dramas.call_args.args[1], VERSION)
        self.assertEqual(store.dramas.call_args.args[-1], 2)

    def writer(self):
        writer = ProfileWriter.__new__(ProfileWriter)
        cursor = Mock(); cursor.fetchone.return_value = None; cursor.fetchall.return_value = []
        writer.db = Mock(); writer.db.cursor.return_value.__enter__ = Mock(return_value=cursor)
        writer.db.cursor.return_value.__exit__ = Mock(return_value=False)
        writer.write_batch = Mock()
        data = portrait(); data['accounts']['123'] = data['accounts'].pop('channel')
        data['meta'] = dict(version=VERSION)
        return writer, data

    def test_failed_replica_verification_never_publishes_head(self):
        writer, data = self.writer()
        reader = Mock(); reader.query.side_effect = RuntimeError('replica unavailable')
        with self.assertRaises(RuntimeError):
            writer.publish(data, reader)
        self.assertTrue(writer.write_batch.called)
        self.assertTrue(all(row[0] != '@current' for call in writer.write_batch.call_args_list for row in call.args[1]))

    def test_complete_bytes_and_income_verified_before_head(self):
        writer, data = self.writer()
        def verification(*args, **kwargs):
            rows = [r for call in writer.write_batch.call_args_list for r in call.args[1]]
            checksum = 0
            for r in rows:
                checksum ^= zlib.crc32((r[1]+':'+hashlib.sha256('\n'.join(r[2:5]).encode()).hexdigest()).encode())
            return [dict(rows=len(rows), revenue=sum(r[5] for r in rows), checksum=checksum)]
        reader = Mock(); reader.query.side_effect = verification
        result = writer.publish(data, reader)
        self.assertEqual(result['rows'], 2)
        self.assertEqual(result['revenue'], 10000)
        self.assertEqual(writer.write_batch.call_args.args[1][0][0], '@current')
        retry, data = self.writer()
        retry.db.cursor.return_value.__enter__.return_value.fetchone.return_value = (json.dumps({'version': VERSION}),)
        reader.query.side_effect = None
        reader.query.return_value = [{k: result[k] for k in ('rows', 'revenue', 'checksum')}]
        self.assertEqual(retry.publish(data, reader), result)
        retry.write_batch.assert_not_called()

    def test_three_columns_preserve_surrounding_shared_page(self):
        text = '<th class="xtr-col">历史三级画像</th>outside<td class="xtr-col" data-x-tag-id="${escapeHtml(item.id)}">old</td>colspan="10"'
        result = patch_columns(text)
        self.assertEqual(result.count('data-x-tag-level='), 3)
        self.assertIn('outside', result)
        self.assertIn('colspan="12"', result)
        self.assertEqual(result, patch_columns(result))


if __name__ == '__main__':
    unittest.main()
