import unittest
from features.pipeline_dispatch.core import DispatchError
from scripts.test_pipeline_dispatch_core import DispatchCoreTests

class PriorityTests(unittest.TestCase):
 setUp=DispatchCoreTests.setUp
 offer=DispatchCoreTests.offer
 claim=DispatchCoreTests.claim
 def priority(self,order):
  return self.store.set_capacity({'expected_version':self.store.config()['version'],'channel_priority':order},{'is_admin':True,'user_id':'test'})
 def enable(self,*channels):
  for channel in channels:self.store.channel_enabled(channel,True)
 def test_youtube_then_x_then_fb_even_when_fb_is_older(self):
  self.enable('youtube')
  for ch in ('fb','x','youtube'):self.offer(ch+':test:1',channel=ch,resource=ch+':1')
  self.priority(['youtube','x','fb'])
  actual=[]
  for i in range(3):
   job=self.claim(worker=str(i));actual.append(job['channel']);self.store.fail(job,'known_test_failure',canonical_no_write=True)
  self.assertEqual(actual,['youtube','x','fb'])
 def test_busy_youtube_does_not_block_available_x(self):
  self.enable('youtube');self.priority(['youtube','x','fb'])
  self.offer('youtube:test:1',channel='youtube',resource='youtube:1');busy=self.claim()
  self.offer('youtube:test:2',channel='youtube',resource='youtube:1');self.offer('fb:test:1',channel='fb',resource='fb:1');self.offer('x:test:2',resource='x:2')
  self.assertEqual(self.claim(worker='1')['channel'],'x')
  self.assertEqual(self.store.get_job(busy['id'])['status'],'running')
 def test_unlisted_channel_keeps_fair_order_in_top_band(self):
  self.enable('tt','youtube');self.priority(['youtube','x','fb'])
  self.offer('tt:test:1',channel='tt',resource='tt:1');self.offer('youtube:test:1',channel='youtube',resource='youtube:1')
  self.assertEqual(self.claim()['channel'],'tt')
 def test_invalid_order_cannot_change_config(self):
  old=self.store.config()
  for order in ('youtube', ['youtube','youtube'], ['bad']):
   with self.assertRaises(DispatchError):self.priority(order)
  self.assertEqual(self.store.config(),old)
 def test_empty_order_restores_existing_fairness(self):
  self.enable('youtube');self.priority([])
  self.offer('fb:test:1',channel='fb',resource='fb:1');self.offer('youtube:test:1',channel='youtube',resource='youtube:1')
  self.assertEqual(self.claim()['channel'],'fb')

if __name__=='__main__':unittest.main()
