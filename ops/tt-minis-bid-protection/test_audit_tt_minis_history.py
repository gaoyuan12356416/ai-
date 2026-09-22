import datetime
import json
import unittest
from unittest import mock
from decimal import Decimal

import audit_tt_minis_history as audit


class AuditTests(unittest.TestCase):
    def test_literal_percent_query_has_no_empty_bind_tuple(self):
        c = mock.MagicMock()
        cursor = c.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        sql = "SELECT id FROM t WHERE value LIKE '%minis_id%'"
        with mock.patch.object(audit, 'read_connection', return_value=c):
            self.assertEqual([], audit.query(sql))
        cursor.execute.assert_called_once_with(sql, None)
        c.close.assert_called_once()

    def test_repeated_ids_pack_days_and_cover_every_pair_once(self):
        ids = {str(i) for i in range(1, 51)}
        days = {'2026-08-%02d' % i: set(ids) for i in range(1, 9)}
        tasks = audit.plan_windows(days)
        self.assertEqual(2, len(tasks))
        seen = set()
        for start, end, query_ids in tasks:
            span = (audit.sync.parse_day(end) - audit.sync.parse_day(start)).days + 1
            self.assertLessEqual(span * len(query_ids), 200)
            for day in days:
                if start <= day <= end:
                    for qid in set(query_ids) & days[day]:
                        self.assertNotIn((day, qid), seen)
                        seen.add((day, qid))
        self.assertEqual(400, len(seen))

    def test_sparse_dates_use_calendar_span_not_number_of_active_days(self):
        days = {'2026-07-24': {'1','2','3','4'}, '2026-09-21': {'1','2','3','4'}}
        for start, end, ids in audit.plan_windows(days):
            span = (audit.sync.parse_day(end)-audit.sync.parse_day(start)).days+1
            self.assertLessEqual(span * len(ids), 200)

    def test_single_day_splits_more_than_200_ids(self):
        tasks = audit.plan_windows({'2026-08-18': {str(i) for i in range(1, 402)}})
        self.assertEqual([200,200,1], [len(x[2]) for x in tasks])

    def fixture(self):
        params = dict(advertiser_id='900', data_level='CAMPAIGN', query_ids='["100"]',
                      start_date='2026-08-18',end_date='2026-08-19')
        c = audit.candidate('2026-08-18','900','100',dict(product_id=3346,product_name='DramaWaveMinis',minis_id='mn1yi38ikcrqhitt'))
        row = dict(query_id='100',record_date='2026-08-18',data_level='CAMPAIGN',
                   bid_protection_daily_status='PAYMENT_COMPLETE',credit_amount='8950000',currency='USD')
        return params, {audit.key(c):c}, dict(code=0,data=dict(bid_protection_records=[row]))

    def test_normalizes_scaled_money_exactly(self):
        p, expected, response = self.fixture()
        rows, extras = audit.normalize_response(p,response,expected)
        self.assertEqual(Decimal('89.50000'),rows[('2026-08-18','900','100')]['credit_amount'])
        self.assertEqual([],extras)

    def test_duplicate_response_is_rejected(self):
        p,e,r = self.fixture()
        r['data']['bid_protection_records'] *= 2
        with self.assertRaises(RuntimeError): audit.normalize_response(p,r,e)

    def test_positive_extra_day_is_not_silently_discarded(self):
        p,e,r = self.fixture()
        r['data']['bid_protection_records'][0]['record_date']='2026-08-19'
        with self.assertRaisesRegex(RuntimeError,'positive compensation'): audit.normalize_response(p,r,e)

    def test_zero_extra_day_is_counted_as_extra(self):
        p,e,r = self.fixture()
        r['data']['bid_protection_records'][0].update(record_date='2026-08-19',credit_amount='0')
        rows, extras = audit.normalize_response(p,r,e)
        self.assertEqual({},rows)
        self.assertEqual(1,len(extras))

    def test_wrong_level_or_unrequested_id_is_rejected(self):
        for field,value in [('data_level','ADGROUP'),('query_id','101'),('record_date','2026-08-20')]:
            p,e,r = self.fixture()
            r['data']['bid_protection_records'][0][field]=value
            with self.assertRaises(RuntimeError): audit.normalize_response(p,r,e)

    def test_signature_ignores_sync_timestamp_but_not_amount_status_or_currency(self):
        p,e,r=self.fixture()
        rows,_=audit.normalize_response(p,r,e)
        old=next(iter(rows.values()))
        new=dict(old,sync_at='later')
        self.assertEqual(audit.signature(old),audit.signature(new))
        for field,value in [('credit_amount','89.6'),('credit_amount_scaled','8960000'),
                            ('currency','EUR'),('protection_status','CONFIRMING')]:
            self.assertNotEqual(audit.signature(old),audit.signature(dict(old,**{field:value})))


if __name__=='__main__': unittest.main()
