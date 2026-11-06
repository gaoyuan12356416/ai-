from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import deploy_post_monitor_accounting as deploy


class ReadinessTests(unittest.TestCase):
    def test_active_process_waits_for_its_own_listener(self):
        with patch.object(deploy,'run',side_effect=['42','','42','LISTEN pid=99,','42','LISTEN pid=42,']), patch.object(deploy.time,'sleep') as sleep:
            self.assertEqual(deploy.wait_listener(), '42')
            self.assertEqual(sleep.call_count,2)

    def test_success_and_failed_switch_keep_gate_until_listener_ready(self):
        for failure in (False,True):
            events=[];starts=0;stopped=False
            def run(args, **kwargs):
                nonlocal starts,stopped
                if args[:2]==['systemctl','show']:
                    return '0' if stopped else '42'
                if 'stop' in args and deploy.UNIT in args:stopped=True
                if args[:2]==['systemctl','start']:
                    starts+=1;stopped=False
                    if failure and starts==1:raise RuntimeError('startup_fixture')
                if args[:2]==['systemctl','is-active']:return 'active'
                if args[:3]==['iptables','-D','INPUT']:events.append('release')
                return ''
            with tempfile.TemporaryDirectory() as folder, patch.object(deploy,'run',side_effect=run), patch.object(deploy,'handlers',return_value=(2,0,0)), patch.object(deploy,'swap'), patch.object(deploy,'wait_listener',side_effect=lambda:events.append('ready')), patch.object(deploy.time,'sleep'), patch.object(deploy.subprocess,'run'):
                if failure:
                    with self.assertRaisesRegex(RuntimeError,'startup_fixture'):
                        deploy.switch_coordinator('/fixture/new',Path(folder)/'proof.json')
                else:
                    deploy.switch_coordinator('/fixture/new',Path(folder)/'proof.json')
            self.assertEqual(events,['ready','release'])


if __name__=='__main__':unittest.main()
