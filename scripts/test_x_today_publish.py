import contextlib
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from features.x_accounts import oauth_service as oauth
from features.x_tag_publish import account_snapshot, dispatch_native
from features.x_tag_publish.transport import Error
from features.x_auto_posts.selector import DecimalRange, DramaFilterRule, SortRule
from features.x_material_effects import selection

ACTOR = {'user_id': 'u', 'tenant_key': 't', 'role': 'admin'}


class AccountSnapshotTests(unittest.TestCase):
    def test_gateway_reads_owner_api_and_returns_no_tokens(self):
        item={'id':7,'scopes':['tweet.write'],'access_token':'private','publish_approved':False}
        rpc=Mock(return_value={'items':[item]})
        row=account_snapshot.read_owner_snapshot({'account_id':7,'actor':ACTOR,'scope':'mine'},rpc,'test-only',Error)
        self.assertNotIn('access_token',row)
        self.assertFalse(row['publish_approved'])
        self.assertEqual(json.loads(row['scopes_json']),['tweet.write'])
        self.assertEqual(rpc.call_args.kwargs['payload'],{'actor':ACTOR,'scope':'mine'})
    def test_scope_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'accounts.sqlite3'
            row = dict.fromkeys(account_snapshot.FIELDS, '')
            row.update(id=7, owner_tenant_key='t', owner_user_id='u', publish_approved=1, status='active')
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE x_authorized_account ('+','.join(k+(' INTEGER' if k in ('id','publish_approved') else ' TEXT') for k in row)+', token_store_key TEXT)')
                db.execute('INSERT INTO x_authorized_account VALUES ('+','.join('?' for _ in range(len(row)+1))+')', [*row.values(), 'private-path'])
            result = account_snapshot.read_snapshot(path, 7, ACTOR, 'mine')
            self.assertEqual(result['id'], 7)
            self.assertNotIn('token_store_key', result)
            self.assertEqual(account_snapshot.read_snapshot(path,None,ACTOR,'all'),[result])
            self.assertEqual(account_snapshot.read_snapshot(path,None,dict(ACTOR,tenant_key='other'),'mine'),[])
            with self.assertRaises(oauth.ServiceError) as caught:
                account_snapshot.read_snapshot(path, 7, dict(ACTOR, tenant_key='other'), 'mine')
            self.assertEqual(caught.exception.code, 'x_account_not_found')
            with self.assertRaises(oauth.ServiceError):
                account_snapshot.read_snapshot(path, 7, dict(ACTOR, role='user'), 'all')

    def test_publisher_reads_broker_and_preserves_credential_guards(self):
        row = dict.fromkeys(account_snapshot.FIELDS, '')
        row.update(id=7, x_user_id='77', status='active', publish_approved=True)
        old = oauth.find_scoped_account_row
        old_accounts=oauth.auto_template_accounts_request
        old_find=oauth.find_account
        try:
            with patch.object(oauth, 'db_connect', side_effect=sqlite3.OperationalError('unable to open database file')), \
                 patch.object(oauth, 'row_to_item', side_effect=lambda value: dict(value)), \
                 patch.object(oauth, 'account_lock', return_value=contextlib.nullcontext()), \
                 patch.object(oauth, 'read_token_file', return_value={'access_token':'test-only'}), \
                 patch('features.x_tag_publish.transport.rpc', return_value={'record':row}):
                account_snapshot.install_reader(oauth)
                self.assertEqual(oauth.find_account(7)['id'],7)
                with oauth.publish_credentials(7, ACTOR, 'all') as (item, token):
                    self.assertEqual((item['id'], token), (7, 'test-only'))
                row['publish_approved'] = False
                with self.assertRaises(oauth.ServiceError) as caught:
                    with oauth.publish_credentials(7, ACTOR, 'all'): pass
                self.assertEqual(caught.exception.code, 'x_account_publish_not_approved')
                row['id'] = 8
                with self.assertRaises(oauth.ServiceError) as caught:
                    oauth.find_scoped_account_row(7, ACTOR, 'all')
                self.assertEqual(caught.exception.code, 'x_tag_account_snapshot_invalid')
        finally:
            oauth.find_scoped_account_row = old
            oauth.auto_template_accounts_request=old_accounts
            oauth.find_account=old_find

    def test_account_list_uses_owner_broker_and_existing_projection(self):
        row=dict.fromkeys(account_snapshot.FIELDS,'');row.update(id=7)
        runtime=SimpleNamespace(normalize_account_scope=oauth.normalize_account_scope,
            INTERNAL_TOKEN='test-only',AUTO_TEMPLATE_ACTOR=ACTOR,ServiceError=oauth.ServiceError,
            row_to_item=lambda r:dict(r,publish_eligible=False),_safe_post_account=lambda r:r,iso_utc=lambda:'now')
        with patch('features.x_tag_publish.transport.rpc',return_value={'record':[row]}):
            account_snapshot.install_reader(runtime)
            result=runtime.auto_template_accounts_request({})
        self.assertEqual(result['total'],1)
        self.assertFalse(result['items'][0]['publish_eligible'])

    def test_lost_response_does_not_become_business_failure(self):
        handler = SimpleNamespace(headers={'Authorization':'Bearer '+('x'*40)},
            read_json=lambda _: {'job':{'source_id':9000000000100}}, send_json=Mock(side_effect=BrokenPipeError))
        bridge = SimpleNamespace(dispatch=Mock(return_value={'accepted':True}))
        with patch.dict('os.environ', {'PIPELINE_X_CANONICAL_BRIDGE_ENABLED':'1','PIPELINE_X_CANONICAL_TOKEN':'x'*40}), \
             patch.object(dispatch_native, '_bridge', bridge), \
             patch('features.pipeline_dispatch.x_native.XNativeBridge.identity'):
            self.assertIsNone(dispatch_native.handle(handler, None, '/internal/pipeline-dispatch/x/checkpoint'))
        handler.send_json.assert_called_once_with(200, {'accepted':True})


class EffectCoverageTests(unittest.TestCase):
    def test_known_subtotal_proves_minimum_but_not_maximum(self):
        partial = {'views':None, 'known':{'views':1300}}
        self.assertTrue(selection.matches(partial,'views',DecimalRange(Decimal(1000))))
        self.assertFalse(selection.matches(partial,'views',DecimalRange(Decimal(1400))))
        self.assertFalse(selection.matches(partial,'views',DecimalRange(None,Decimal(2000))))
        self.assertFalse(selection.matches({},'views',DecimalRange(Decimal(1000))))
        self.assertIsNone(selection.value(partial,'views'))

    def test_sort_uses_measured_counts_and_leaves_missing_last(self):
        items = [SimpleNamespace(catalog='dramawave', content_id=k, language='en') for k in ('partial','complete','missing')]
        data = {'partial':{'views':None,'known':{'views':1300}}, 'complete':{'views':500}, 'missing':{}}
        token=selection.ACTIVE.set({'drama':{('dramawave',k,'en'):v for k,v in data.items()}})
        try:
            for direction, expected in [('desc',['partial','complete','missing']),('asc',['complete','partial','missing'])]:
                result=selection.apply(items, DramaFilterRule(sort=SortRule('x_views',direction)), 'drama', {})
                self.assertEqual([i.content_id for i in result],expected)
        finally:selection.ACTIVE.reset(token)


if __name__ == '__main__':unittest.main()
