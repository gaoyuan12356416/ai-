import json,unittest
from unittest.mock import patch
import service

class ScopeTests(unittest.TestCase):
 def fixture(self):
  return dict(v=2,m={'generated_at':'2026-11-13 23:00:00'},p={'success':3},lp={'0':{'success':3}},
   cf=['platform','campaign_id','copy_date'],c=[[0,'same','2026-11-10'],[3,'same','2026-10-01'],[0,'empty','2026-11-10']],
   df=['platform','campaign_id','dt','spend'],d=[[0,'same','2026-11-10',1],[0,'same','2026-11-11',2],[3,'same','2026-11-10',9]],
   xf=['level','entity_key','copy_date'],x=[[1,'0:1:7','2026-11-10'],[2,'0:2:7','2026-11-10']],
   xdf=['entity_key','dt','spend'],xd=[['0:1:7','2026-11-10',4],['0:2:7','2026-11-10',8]])
 def test_exact_cohort_retains_zero_fact_entities_and_all_effect_dates(self):
  wire=self.fixture();v=service.scoped_wire(wire,0,'2026-11-10','2026-11-10')
  self.assertEqual(len(v['c']),2);self.assertEqual(sum(r[3] for r in v['d']),3)
  self.assertEqual(v['x'],[]);self.assertEqual(v['xd'],[]);self.assertEqual(wire['m'],{'generated_at':'2026-11-13 23:00:00'})
 def test_levels_do_not_mix_identical_entity_ids(self):
  v=service.scoped_wire(self.fixture(),1,'','');self.assertEqual(v['x'],[[1,'0:1:7','2026-11-10']]);self.assertEqual(v['xd'],[['0:1:7','2026-11-10',4]])
 def test_history_is_available_without_date_truncation(self):
  v=service.scoped_wire(self.fixture(),0,'','');self.assertEqual(len(v['c']),3);self.assertEqual(len(v['d']),3)
 def test_view_requests_never_query_sql_and_validate_inputs(self):
  c=service.ReportCache(None);c._install_body(json.dumps(self.fixture()).encode())
  with patch.object(service,'query_raw',side_effect=AssertionError('SQL on GET')):
   b,_,etag,_,_=c.get_view({'level':['0'],'copy_start':['2026-11-10'],'copy_end':['2026-11-10']});self.assertEqual(len(json.loads(b)['c']),2)
   self.assertEqual(c.get_view({'level':['0'],'copy_start':['2026-11-10'],'copy_end':['2026-11-10']})[2],etag)
   with self.assertRaises(ValueError):c.get_view({'level':['9']})
   with self.assertRaises(ValueError):c.get_view({'copy_start':['invalid']})

class CompressedConnectionTests(unittest.TestCase):
 def test_native_connection_preserves_buffered_dictionary_cursor(self):
  from unittest.mock import Mock
  native,guard=Mock(),Mock();wrapped=service.CompressedConnection(native,guard)
  wrapped.cursor();native.cursor.assert_called_once_with(dictionary=True,buffered=True)
  self.assertIs(wrapped._sock,guard)
  wrapped.close();native.close.assert_called_once();guard.close.assert_called_once()
 def test_cancellation_guard_closes_even_when_driver_close_raises(self):
  from unittest.mock import Mock
  native,guard=Mock(),Mock();native.close.side_effect=RuntimeError('driver close failed')
  with self.assertRaises(RuntimeError):service.CompressedConnection(native,guard).close()
  guard.close.assert_called_once()

if __name__=='__main__':unittest.main()

class BoundedRefreshTests(unittest.TestCase):
 def test_resume_pending_dates_without_regressing_watermark(self):
  from datetime import date
  old={'m':{'refreshed_through':'2026-11-14','stat_end':'2026-11-14','history_next_date':'2026-08-28','pending_refresh_dates':['2026-11-13','2026-08-28']}}
  days,cursor,pending=service.bounded_refresh_dates(old,[date(2026,11,14),date(2026,8,28)],'2026-08-29')
  self.assertEqual(days,[date(2026,11,13)]);self.assertEqual(cursor,'2026-08-28');self.assertEqual(pending,['2026-08-28'])
 def test_new_day_preempts_but_preserves_backfill_queue(self):
  from datetime import date
  old={'m':{'refreshed_through':'2026-11-13','stat_end':'2026-11-13','history_next_date':'2026-08-28','pending_refresh_dates':['2026-11-12']}}
  days,cursor,pending=service.bounded_refresh_dates(old,[date(2026,11,14),date(2026,11,13),date(2026,8,28)],'2026-08-29')
  self.assertEqual(days,[date(2026,11,14)]);self.assertIn('2026-11-12',pending);self.assertEqual(cursor,'2026-08-28')
 def test_historical_cursor_advances_only_when_that_day_is_committed(self):
  from datetime import date
  old={'m':{'refreshed_through':'2026-11-14','stat_end':'2026-11-14','history_next_date':'2026-08-28','pending_refresh_dates':['2026-08-28']}}
  days,cursor,pending=service.bounded_refresh_dates(old,[date(2026,11,14),date(2026,8,28)],'2026-08-29')
  self.assertEqual(cursor,'2026-08-29');self.assertEqual(pending,[])
