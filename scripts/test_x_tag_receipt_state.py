import unittest
from types import SimpleNamespace as NS

from features.x_auto_posts.execution_body import execution_body
from features.x_auto_posts.run_outcomes import summary
from features.x_tag_publish.receipt_settlement import MEDIA


class RunStates(unittest.TestCase):
    def test_only_future_tasks_wait_for_release(self):
        tasks=[dict(id=1,status="published"),dict(id=2,status="ready",scheduled_at_utc="2026-11-24T04:00:00+00:00")]
        result=summary(NS(status="running"),tasks,{},"2026-11-24T02:00:00+00:00")
        self.assertEqual((result["display_status"],result["published_task_count"],result["unfinished_task_count"]),("waiting_release",1,1))
        self.assertEqual(result["next_task_at_utc"],tasks[1]["scheduled_at_utc"])
        self.assertEqual(summary(NS(status="running"),tasks,{},tasks[1]["scheduled_at_utc"])["display_status"],"ready")

    def test_mixed_pauses_and_unknown_are_disjoint(self):
        tasks=[dict(id=1,status="reserved"),dict(id=2,status="retry_wait",unknown_outcome=1,execution_queue_id=18)]
        result=summary(NS(status="running"),tasks,{1:"historical_pause",2:"account_blocked"},"2026-11-24")
        self.assertEqual(result["display_status"],"mixed_wait")
        self.assertEqual((result["task_counts"]["unknown"],result["task_counts"]["historical_pause"],sum(result["task_counts"].values())),(1,1,2))
        self.assertEqual(summary(NS(status="running"),[dict(id=3,status="retry_wait")],{3:"account_blocked"},"2026-11-24")["display_status"],"account_blocked")

    def test_frozen_product_links_and_media_scope(self):
        self.assertEqual(execution_body("{{url}}",{"source_provider":"moboreels"}),"{{m_url}}")
        self.assertEqual(execution_body("{{url}}",{}),"{{url}}")
        self.assertIsNotNone(MEDIA.fullmatch("x:POST:/2/media/upload/123/finalize"))
        self.assertIsNone(MEDIA.fullmatch("x:POST:/2/tweets"))
        self.assertIsNone(MEDIA.fullmatch("x:POST:/2/users/123/retweets"))


if __name__=="__main__":
    unittest.main()
