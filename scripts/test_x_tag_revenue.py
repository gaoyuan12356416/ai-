import json
from pathlib import Path
import sys
import tempfile
import unittest
from fractions import Fraction
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_tag_revenue.core import build, label_index, rounded_parts
from features.x_tag_revenue.routes import response, dispatch, read_snapshot
from features.x_tag_revenue.integration import patch_html, unpatch_html, CHANGES
from features.x_tag_revenue.http_server import authorize


def publication(campaign='1', channel='123', content='drama', **extra):
    return dict(platform='x', site_id='2116', key=[campaign, 'name'], channel='x:'+channel,
                drama=content, drama_label=content, language='en', tenant='t', owner='o',
                date='2026-01-01', link_type='auto_template', link_id='x:'+campaign, excluded=False,
                publications=[dict(id='post:'+campaign, date='2026-01-01')], **extra)


def metric(cents, campaign='1', day='2026-01-02'):
    return dict(platform='x', site_id='2116', campaign_id=campaign, date=day, revenue_cents=cents)


def resource(names='Romance,CEO', content='drama', lang='en', series='s'):
    return dict(app_id=1479, content_id=content, language=lang, series_code=series, labels=names)


class PortraitTests(unittest.TestCase):
    def test_equal_allocation_and_duplicate_labels(self):
        out = build([publication()], [metric(101)], [resource('Romance,CEO,romance,CEO')], '2026-01-31')
        p = out['accounts']['123']
        self.assertEqual([50,51], sorted(r['revenue_cents'] for r in p['tags']))
        self.assertEqual(10000, sum(r['share_bp'] for r in p['tags']))
        self.assertEqual(1, p['published_count'])
        self.assertTrue(all(r['dramas'][0]['label_count'] == 2 for r in p['tags']))
        self.assertEqual(101, out['audit']['included_revenue_cents'])

    def test_all_level_totals_conserve(self):
        cat=[publication(str(n),content='d'+str(n)) for n in range(1,6)]
        out=build(cat,[metric(n, str(n)) for n in range(1,6)],
                  [resource('A,B,C' if n%2 else 'A,D',content='d'+str(n)) for n in range(1,6)],'2026-01-31')
        p=out['accounts']['123']
        self.assertEqual(15,sum(t['revenue_cents'] for t in p['tags']))
        for tag in p['tags']:
            self.assertEqual(tag['revenue_cents'],sum(d['allocated_cents'] for d in tag['dramas']))

    def test_unknown_is_separate_and_zero_not_unknown(self):
        out=build([publication()], [metric(100)], [], '2026-01-31')['accounts']['123']
        self.assertEqual(100,out['unknown_revenue_cents'])
        self.assertEqual('__unknown__',out['tags'][0]['key'])
        zero=build([publication()],[],[resource()],'2026-01-31')['accounts']['123']
        self.assertEqual('complete',zero['status'])
        self.assertIsNone(zero['tags'][0]['share_bp'])

    def test_duplicate_evidence_does_not_multiply_revenue_or_posts(self):
        out=build([publication(),publication()],[metric(100)],[resource(),resource()],'2026-01-31')['accounts']['123']
        self.assertEqual(100,out['revenue_cents']); self.assertEqual(1,out['published_count'])

    def test_conflicting_campaign_is_localized(self):
        out=build([publication(),publication(channel='other'),publication('2',channel='good')],
                  [metric(100),metric(200,'2')],[resource()],'2026-01-31')
        self.assertEqual(200,out['accounts']['good']['revenue_cents'])
        self.assertEqual(100,out['audit']['unassigned_revenue_cents'])
        self.assertEqual('partial',out['accounts']['123']['status'])

    def test_before_publication_and_incomplete_today_excluded(self):
        out=build([publication()], [metric(4,day='2025-12-31'),metric(10),metric(100,day='2026-02-01')], [resource()], '2026-01-31')
        self.assertEqual(10,out['accounts']['123']['revenue_cents'])
        self.assertEqual(4,out['audit']['unassigned_revenue_cents'])
        self.assertEqual(14,out['audit']['source_revenue_cents'])

    def test_mapping_merges_labels_but_keeps_language_product_identity(self):
        rows=[resource('A',lang='jp'),resource('B',lang='ja'),resource('C',lang='en'),resource('D',lang='pt')]
        rows.append(dict(resource('WRONG',lang='ja'),app_id=999))
        idx=label_index(rows)
        self.assertEqual({'a','b'},set(idx[('drama','ja')]['tags']))
        self.assertIn(('drama','pt-br'),idx)
        idx=label_index([resource('A',series='one'),resource('B',series='two')])
        self.assertEqual('ambiguous_drama',idx[('drama','en')]['status'])

    def test_negative_revenue_preserved_without_misleading_ratio(self):
        p=build([publication()],[metric(-1)],[resource()],'2026-01-31')['accounts']['123']
        self.assertEqual(-1,sum(t['revenue_cents'] for t in p['tags']))
        self.assertTrue(all(t['share_bp'] is None for t in p['tags']))

    def test_channel_acl_uses_stable_x_id_not_display_name(self):
        data=build([publication()],[metric(100)],[resource()],'2026-01-31')
        account=dict(id=8,x_user_id='123',display_name='renamed')
        status,body=response(data,{},[account],'8')
        self.assertEqual(200,status); self.assertEqual(100,body['portrait']['revenue_cents'])
        self.assertNotIn('dramas',body['portrait']['tags'][0])
        self.assertEqual(404,response(data,{},[account],'9')[0])
        self.assertEqual({},response(data,{},[])[1]['items'])
        self.assertEqual(1,len(response(data,{},[account],'8','ceo')[1]['portrait']['dramas']))

    def test_cookie_gate_before_any_data_access(self):
        sent=[]
        class Handler:
            headers={}
            def send_json(self,status,payload): sent.append(status)
        dispatch(Handler(),urlsplit('/api/x-channel-tag-revenue'),authorize)
        self.assertEqual([401],sent)

    def test_invalid_tag_query_rejected(self):
        sent=[]
        class Handler:
            headers={}
            def send_json(self,status,payload): sent.append(status)
        dispatch(Handler(),urlsplit('/api/x-channel-tag-revenue?tag=A'),authorize)
        self.assertEqual([400],sent)

    def test_denied_scope_never_reads_snapshot(self):
        sent=[]
        class Handler:
            headers={'Cookie':'fixture'}
            def send_json(self,status,payload): sent.append(status)
        dispatch(Handler(),urlsplit('/api/x-channel-tag-revenue/1'),lambda cookie:(403,[]),'/does-not-exist')
        self.assertEqual([403],sent)

    def test_corrupt_snapshot_is_unavailable_not_zero(self):
        sent=[]
        class Handler:
            headers={'Cookie':'fixture'}
            def send_json(self,status,payload): sent.append((status,payload))
        with tempfile.TemporaryDirectory() as root:
            Path(root,'current.json').write_text('{}')
            dispatch(Handler(),urlsplit('/api/x-channel-tag-revenue'),lambda cookie:(200,[]),root)
        self.assertEqual(503,sent[0][0])
        self.assertNotIn('items',sent[0][1])

    def test_reversible_integration_preserves_surroundings(self):
        html='\n'.join(before for before,after in CHANGES)+'\ncolspan="9"'
        self.assertEqual(html,unpatch_html(patch_html(html)))
        self.assertEqual(patch_html(html),patch_html(patch_html(html)))


if __name__=='__main__': unittest.main()
