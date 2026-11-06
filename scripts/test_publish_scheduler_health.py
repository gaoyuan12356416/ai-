import unittest

from scripts.publish_scheduler_health import evaluate, roles, restore_targets


class SchedulerHealthTests(unittest.TestCase):
    def result(self, kind, state, **fields):
        row = dict(LoadState='loaded', ActiveState=state, SubState='dead', Result='success',
                   ExecMainStatus='0', ExecMainExitTimestampMonotonic='990000000', **fields)
        return evaluate({'fixture': {'kind': kind, 'lane': 'fb', 'max_age': 180}}, {'fixture': row}, 1000)[0]

    def test_successful_inactive_oneshot_is_healthy(self):
        self.assertTrue(self.result('oneshot', 'inactive')['ok'])

    def test_missing_daemon_is_not_mistaken_for_idle_oneshot(self):
        self.assertFalse(self.result('daemon', 'inactive', MainPID='0')['ok'])

    def test_dead_timer_is_an_error_even_after_successful_run(self):
        self.assertFalse(self.result('timer', 'inactive', NextElapseUSecRealtime='tomorrow')['ok'])

    def test_waiting_timer_requires_next_trigger(self):
        self.assertFalse(self.result('timer', 'active')['ok'])
        self.assertTrue(self.result('timer', 'active', NextElapseUSecRealtime='tomorrow')['ok'])

    def test_stale_success_cannot_hide_stopped_scheduling(self):
        spec = {'fixture': {'kind': 'oneshot', 'lane': 'x', 'max_age': 180}}
        state = {'fixture': {'LoadState': 'loaded', 'ActiveState': 'inactive', 'Result': 'success',
                            'ExecMainStatus': '0', 'ExecMainExitTimestampMonotonic': '1000000'}}
        self.assertFalse(evaluate(spec, state, 1000)[0]['ok'])

    def test_in_flight_work_is_preserved(self):
        self.assertEqual(self.result('oneshot', 'activating')['code'], 'in_flight')

    def test_restore_does_not_run_oneshots_or_resume_paused_services(self):
        states = {unit: {'ActiveState': 'active'} for unit in roles(['x'])}
        states['x-video-pool-scheduler.service']['ActiveState'] = 'inactive'
        selected = restore_targets({'lanes': ['x'], 'states': states})
        self.assertIn('x-post-schedule.timer', selected)
        self.assertNotIn('x-post-schedule.service', selected)
        self.assertNotIn('x-video-pool-scheduler.service', selected)
        self.assertNotIn('fb-video-pool-scheduler.service', selected)


if __name__ == '__main__':
    unittest.main()
