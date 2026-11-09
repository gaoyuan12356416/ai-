"""Cohort boundaries and actual-delivery comparison; no production writes."""
import copy
import datetime as dt
import unittest

from collector import collect_days, resolve_scope
from human_comparison import classify_inventory, attach_comparison
from test_three_products import DAY, app_rows, material, fact, snapshot, report


def inventory_row(key, **extra):
    return dict(id=str(key), task_id=42, created_at=DAY + ' 12:00:00', remark='', content_sign='', **extra)


def coverage():
    return [dict(kind='human_coverage', dt=(dt.date.fromisoformat(DAY) - dt.timedelta(days=n)).isoformat(), ok=1)
            for n in range(7)]


class HumanComparisonTests(unittest.TestCase):
    def test_provenance_excludes_ai_ambiguous_imports_and_copies(self):
        mapped = {'1': material('1', content_sign='same-sign')}
        rows = [inventory_row(str(n)) for n in range(1, 10)]
        rows[1]['remark'] = '{"origin":"ai-cover"}'
        rows[2]['task_id'] = -2
        rows[3]['content_sign'] = 'same-sign'
        rows[4]['task_id'] = 0
        rows[5]['task_id'] = -1
        rows[7]['task_id'] = None
        result = classify_inventory(rows, mapped, [{'id': '7'}])
        self.assertEqual(set(result['members']), {'5', '6', '9'})
        self.assertEqual(result['excluded']['1'], 'verified_ai')
        self.assertEqual(result['excluded']['4'], 'ai_signature_unverified')
        self.assertEqual(result['catalog_count'], 9)

    def test_comparison_actual_delivery_weighted_ratios_shared_cells_and_week_dedupe(self):
        mapped = {'1': material('1', source_product='2373', created='2026-10-01')}
        previous = (dt.date.fromisoformat(DAY) - dt.timedelta(days=1)).isoformat()
        ai = [fact('1', spend=10, revenue=5), fact('1', app='[W2A]Dramawave', spend=20, revenue=5),
              fact('1', app='Moboshort', spend=1000), fact('1', day=previous, spend=5, revenue=3)]
        human = [fact('2', kind='dramawave_image', spend=30, revenue=30),
                 fact('2', kind='dramawave_image', day=previous, spend=10, revenue=2),
                 fact('3', kind='dramawave_image', platform=9, spend=70, revenue=10)]
        unknown = fact('4', kind='dramawave_image', spend=4, revenue=1)
        facts = snapshot(ai, extra_rows=human + [unknown] + coverage())
        result = report(mapped, facts)
        inv = classify_inventory([inventory_row('2'), inventory_row('3')], mapped, [])
        before = copy.deepcopy(result['summary'])
        attach_comparison(result, inv, mapped, facts)
        self.assertEqual(result['summary'], before)
        comp = result['human_comparison']
        self.assertEqual(comp['modes']['all']['day']['ai']['spend'], 30)
        self.assertEqual(comp['modes']['all']['day']['human']['spend'], 100)
        self.assertEqual(comp['modes']['all']['week']['human']['spending_materials'], 2)
        self.assertAlmostEqual(comp['modes']['all']['week']['human']['roas'], 42 / 110 * 100)
        self.assertEqual(comp['modes']['matched']['day']['human']['spend'], 30)
        self.assertEqual(comp['modes']['matched']['day']['ai']['spend'], 10)
        self.assertEqual(comp['unknown']['day']['spend'], 4)

    def test_zero_spend_is_not_missing_coverage(self):
        mapped = {}
        result = report(mapped, coverage())
        inv = classify_inventory([], mapped, [])
        attach_comparison(result, inv, mapped, coverage())
        self.assertIsNone(result['human_comparison']['modes']['all']['day']['human']['roas'])
        with self.assertRaisesRegex(ValueError, 'coverage_incomplete'):
            attach_comparison(result, inv, mapped, coverage()[:-1])

    def test_same_id_cannot_be_both_human_and_ai(self):
        mapped = {'1': material('1')}
        facts = snapshot([fact('1')], extra_rows=[fact('1', kind='dramawave_image')] + coverage())
        inv = classify_inventory([inventory_row('1')], {}, [])
        with self.assertRaisesRegex(ValueError, 'human_ai_overlap'):
            attach_comparison(report(mapped, facts), inv, mapped, facts)

    def test_human_query_uses_formal_ids_and_same_snapshot(self):
        class Catalog:
            queries = []

            def query(self, sql):
                self.queries.append(sql)
                return [dict(kind='coverage', dt=DAY, ok=1), dict(kind='human_coverage', dt=DAY, ok=1)]

        catalog = Catalog()
        collect_days(catalog, {}, resolve_scope(app_rows()), [DAY], human_comparison=True)
        self.assertEqual(len(catalog.queries), 1)
        query = catalog.queries[0]
        self.assertIn('s.id=i.resource_id', query)
        self.assertIn('BINARY i.resource_id=CAST(s.id AS CHAR)', query)
        self.assertIn('i.source_type=3 AND i.resource_type=1', query)
        self.assertIn('s.type=1', query)
        self.assertIn("'kind','market'", query)
        self.assertNotIn('is_delete=0', query)


if __name__ == '__main__':
    unittest.main()
