"""Exercise the actual isolated release layout, not only the full checkout."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import json
from contextlib import closing


class DeploymentTests(unittest.TestCase):
    def module(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('schedule_deploy',root/'ops/meta-page-recovery/deploy.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_four_hour_scan_and_independent_ten_oclock_report(self):
        module=self.module();units=module.unit_files()
        self.assertEqual(set(units),set(module.NAMES))
        self.assertIn('OnCalendar=*-*-* 00,04,08,12,16,20:05:00 Asia/Shanghai',units['meta-page-recovery-scan.timer'])
        self.assertIn('OnCalendar=*-*-* 10:00:00 Asia/Shanghai',units['meta-page-recovery-report.timer'])
        self.assertIn('features.meta_page_recovery report',units['meta-page-recovery-report.service'])
        self.assertIn('Persistent=false',units['meta-page-recovery-report.timer'])

    def test_schedule_switch_retains_baseline_without_running_a_scan(self):
        import sqlite3
        module=self.module()
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);release=folder/'releases'/'verified';release.mkdir(parents=True)
            (folder/'manifest.json').write_text(json.dumps(dict(commit='verified')))
            with closing(sqlite3.connect(folder/'state.sqlite3')) as db, db:
                db.execute('CREATE TABLE metadata(key TEXT,value TEXT)');db.execute("INSERT INTO metadata VALUES('baseline_at','original')")
            with patch.object(module,'CURRENT') as current,patch.object(module,'ROOT',folder),patch.object(module.subprocess,'check_call') as call:
                current.resolve.return_value=release;module.switch(folder)
            self.assertFalse(any('scan' in c.args[0] for c in call.call_args_list))
            self.assertIn((['systemctl','restart','meta-page-recovery-worker.service'],),[c.args for c in call.call_args_list])
            with closing(sqlite3.connect(folder/'state.sqlite3')) as db:self.assertEqual(db.execute('SELECT value FROM metadata').fetchone()[0],'original')

    def test_rollback_disables_worker_boot_restart_and_preserves_business_state(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('recovery_deploy_rollback',root/'ops/meta-page-recovery/deploy.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);release=folder/'releases'/'verified';release.mkdir(parents=True)
            pointer=folder/'current';pointer.mkdir()
            (folder/'manifest.json').write_text(json.dumps(dict(commit='verified',previous_files={},previous_current=str(release))))
            # Avoid requiring Windows symlink privileges in this pure operation test.
            with patch.object(module,'CURRENT') as current,patch.object(module,'ROOT',folder),patch.object(module.subprocess,'check_call') as call:
                current.resolve.return_value=release
                module.rollback(folder)
                self.assertIn((['systemctl','disable','--now','meta-page-recovery-worker.service'],),[c.args for c in call.call_args_list])
            self.assertTrue((folder/'manifest.json').exists())

    def test_schedule_rollback_restores_prior_timers_and_keeps_unknown_receipts(self):
        import sqlite3
        module=self.module()
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);release=folder/'releases'/'verified';release.mkdir(parents=True)
            states={name:dict(enabled='enabled',active='active') for name in module.NAMES[1:3]}
            (folder/'manifest.json').write_text(json.dumps(dict(commit='verified',previous_files={},previous_current=str(folder/'old'),previous_states=states,activation_kind='schedule_update')))
            with closing(sqlite3.connect(folder/'state.sqlite3')) as db, db:
                db.execute('CREATE TABLE notifications(run_id TEXT,status TEXT)')
                db.executemany('INSERT INTO notifications VALUES(?,?)',[('daily-1','pending'),('daily-2','unknown'),('daily-3','sent')])
                db.execute('CREATE TABLE facts(value TEXT)');db.execute("INSERT INTO facts VALUES('keep')")
            with patch.object(module,'CURRENT') as current,patch.object(module,'ROOT',folder),patch.object(module.subprocess,'check_call') as call:
                current.resolve.return_value=release;module.rollback(folder)
            calls=[c.args for c in call.call_args_list]
            self.assertIn((['systemctl','start','meta-page-recovery-worker.service'],),calls)
            self.assertIn((['systemctl','start','meta-page-recovery-scan.timer'],),calls)
            self.assertNotIn((['systemctl','start','meta-page-recovery-report.timer'],),calls)
            with closing(sqlite3.connect(folder/'state.sqlite3')) as db:
                self.assertEqual(dict(db.execute('SELECT run_id,status FROM notifications')),{'daily-1':'recorded','daily-2':'unknown','daily-3':'sent'})
                self.assertEqual(db.execute('SELECT value FROM facts').fetchone()[0],'keep')

    def test_cold_import_in_packaged_release(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('recovery_deploy',root/'ops/meta-page-recovery/deploy.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            release=Path(temporary)/'release'
            module.build_release(root,release,root/'features/fb_page_portrait')
            self.assertEqual((release/'features/fb_page_portrait/storage.py').read_bytes(),(root/'features/fb_page_portrait/storage.py').read_bytes())
            # The local shared fixture contains current owner overlays, while
            # the checkout supplies their unchanged dependency packages.
            code='import sys,features; features.__path__.append(sys.argv[1]); import features.fb_page_portrait.collect; import features.meta_page_recovery.engine'
            result=subprocess.run([sys.executable,'-c',code,str(root/'features')],
                cwd=release,env=dict(os.environ),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
