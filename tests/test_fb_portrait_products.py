import copy
import unittest
from unittest.mock import patch

from features.fb_page_portrait.collect import merge_paid, series_ranges
from features.fb_page_portrait.build import build, cents


def raw():
    return dict(page_ids=['11','22'], start='2026-11-06', end='2026-11-12', social_version='fixture',
        resources=[dict(app_id=1479,content_id='same',language='en',series_code='dw',labels=['ceo']),
                   dict(app_id=979,content_id='same',language='en',series_code='fr',labels=['revenge'])],
        infos=[dict(app_id=1479,content_id='same',language='en',series_code='dw',resource_type=1,name='DW'),
               dict(app_id=979,content_id='same',language='en',series_code='fr',resource_type=2,name='FR')],
        paid=[dict(app_id=1479,page_id='11',date='2026-11-06',content_id='same',language='en',spend='1',revenue='.05'),
              dict(app_id=979,page_id='11',date='2026-11-06',content_id='same',language='en',spend='2',revenue='.07')],
        social=[dict(app_id=1479,page_id='11',date='2026-11-06',content_id='same',language='en',revenue_cents=6,posts=['post'])])


def audiences(ids):
    return {'same':dict(status='matched',audience='female',languages=['en'],audience_labels=['女性'],fetched_at='fixture')} if 'same' in ids else {}


class ProductTests(unittest.TestCase):
    def test_same_content_id_stays_product_scoped(self):
        with patch('features.x_tag_revenue.taxonomy.audience_records',side_effect=audiences):
            data=build(raw())
        p=data['pages']['11']
        self.assertEqual((p['paid_cents'],p['social_cents'],p['revenue_cents'],p['drama_count']),(12,6,18,2))
        rows={d['app_id']:d for a in p['audiences'] for t in a['children'] for d in t['dramas']}
        self.assertEqual(rows[1479]['name'],'DW')
        self.assertEqual(rows[979]['name'],'FR')
        self.assertEqual(rows[1479]['classification']['audience'],'female')
        self.assertEqual(rows[979]['classification']['audience'],'__unknown__')
        self.assertNotEqual(rows[1479]['classification']['themes'],rows[979]['classification']['themes'])
        self.assertEqual(sum(a['revenue_cents'] for a in p['audiences']),18)
        self.assertEqual(data['pages']['22']['status'],'no_income')
        self.assertEqual(data['meta']['paid_products'],['Dramawave','FreeReels'])

    def test_missing_metadata_does_not_use_other_product_audience(self):
        value=raw();value['resources']=[];value['infos']=[];value['social']=[]
        with patch('features.fb_page_portrait.build.audience_records',side_effect=audiences):
            p=build(value)['pages']['11']
        by={d['app_id']:d for a in p['audiences'] for t in a['children'] for d in t['dramas']}
        self.assertEqual(by[1479]['classification']['audience'],'female')
        self.assertEqual(by[979]['classification']['audience'],'__unknown__')

    def test_ambiguous_freereels_metadata_is_not_borrowed(self):
        value=raw();other=copy.deepcopy(value['resources'][1]);other['series_code']='different';value['resources'].append(other)
        p=build(value)['pages']['11']
        row=next(d for a in p['audiences'] for t in a['children'] for d in t['dramas'] if d['app_id']==979)
        self.assertEqual(row['classification']['themes'],['__unknown__'])
        self.assertEqual(row['classification']['label_status'],'ambiguous_drama')

    def test_range_coverage_has_no_gap_and_includes_empty_and_new_series(self):
        ranges=series_ranges([{'series_code':'','rows':1},{'series_code':'a','rows':3},{'series_code':'b','rows':8},{'series_code':'c','rows':1}],4)
        for code in ['', 'a', 'aa', 'b', 'bb', 'c', 'z']:
            self.assertEqual(sum((lo is None or lo<=code) and (hi is None or code<hi) for lo,hi in ranges),1)
        self.assertEqual(series_ranges([]),[(None,None)])

    def test_partition_sums_merge_before_rounding_and_never_cross_products(self):
        value=raw()['paid'][0]
        rows=[dict(value,revenue='.004'),dict(value,revenue='.004'),dict(value,app_id=979,revenue='.004')]
        result=merge_paid(rows)
        self.assertEqual(len(result),2)
        by={r['app_id']:r for r in result}
        self.assertEqual(cents(by[1479]['revenue']),1)
        self.assertEqual(cents(by[979]['revenue']),0)
        self.assertEqual(by[1479]['spend'],'2')


if __name__=='__main__':unittest.main()
