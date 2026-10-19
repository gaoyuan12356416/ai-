import json
import unittest
from unittest.mock import Mock

from features.youtube_auto_publish.channel_languages import decorate
from features.youtube_analytics import report, titles
from tests.test_youtube_analytics import catalog_row, metric, params, OWNER


def encoded(**value):
    return [json.dumps(value).encode().hex()]


class DisplayMetadataTests(unittest.TestCase):
    def setUp(self):
        titles._cache.clear()

    def test_saved_channel_language_does_not_change_eligibility(self):
        row = dict(channel_id='UC-a', eligible=False)
        self.assertEqual(decorate([row], {'UC-a': {'language': 'es'}})[0]['language_label'], '西班牙语')
        self.assertFalse(row['eligible'])
        self.assertEqual(decorate([row], {})[0]['language_label'], '未设置语种')

    def test_titles_use_exact_product_series_and_preserve_money_and_csv(self):
        query = Mock(side_effect=[[dict(content_id='drama-a',series_code='1'),dict(content_id='DRAMA-A',series_code='2')],
                                  [dict(series_code='1',name='The Queen'),dict(series_code='1',name='The Queen')]])
        value = report.summarize([catalog_row()], [metric()], OWNER, params(group_by='owner,drama,channel'), '')
        totals = value['totals'].copy()
        titles.enrich(value, query)
        self.assertEqual(value['rows'][0]['drama_english_name'], 'The Queen')
        self.assertEqual(value['totals'], totals)
        self.assertIn('英文剧名', report.csv_export(value).decode('utf-8-sig'))
        self.assertIn('The Queen', report.csv_export(value).decode('utf-8-sig'))
        self.assertIn('app_id=1479', query.call_args.args[0])
        self.assertIn("language='en'", query.call_args.args[0])
        self.assertEqual(query.call_count, 2)
        titles.enrich(value, query)
        self.assertEqual(query.call_count, 2)

    def test_ambiguous_missing_and_unavailable_never_guess(self):
        result = titles.resolve(['a','b','c'], Mock(side_effect=[[
            dict(content_id='a',series_code='1'),dict(content_id='a',series_code='2'),dict(content_id='b',series_code='3')],
            [dict(series_code='3',name='One'),dict(series_code='3',name='Two')]]))
        self.assertEqual([result[x]['drama_english_status'] for x in ['a','b','c']], ['ambiguous','ambiguous','missing'])
        value = report.summarize([catalog_row()], [metric()], OWNER, params(group_by='drama'), '')
        titles.enrich(value, Mock(side_effect=RuntimeError('failure')))
        self.assertEqual(value['rows'][0]['drama_english_status'], 'unavailable')
        self.assertEqual(value['totals']['clicks'], 100)

    def test_non_drama_group_needs_no_query(self):
        value = report.summarize([catalog_row()], [metric()], OWNER, params(group_by='owner'), '')
        query = Mock()
        titles.enrich(value, query)
        query.assert_not_called()

    def test_page_lookup_is_bounded_and_export_covers_all_rows(self):
        rows = [dict(drama='d' + str(i)) for i in range(150)]
        value = dict(group_by=['drama'], rows=rows[:20], all_rows=rows, warnings=[])
        query = Mock(return_value=[])
        titles.enrich(value, query)
        self.assertEqual(query.call_count, 1)
        self.assertNotIn('drama_english_status', rows[20])
        titles.enrich(value, query, export=True)
        self.assertTrue(all(r['drama_english_status'] == 'missing' for r in rows))
        self.assertEqual(query.call_count, 3)

    def test_title_csv_formula_escaped(self):
        value = report.summarize([catalog_row()], [metric()], OWNER, params(group_by='drama'), '')
        value['all_rows'][0]['drama_english_name'] = '=HYPERLINK("x")'
        self.assertIn("'=HYPERLINK", report.csv_export(value).decode('utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
