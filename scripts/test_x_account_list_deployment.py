"""Exercise deployment and rollback against an ExecStartPost readiness model."""
import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import deploy_x_account_list as deploy


class DeploymentTests(unittest.TestCase):
    def test_main_and_sidecar_readiness_routes(self):
        response = SimpleNamespace(status=200)
        with patch.object(deploy.urllib.request, 'urlopen', side_effect=lambda *a, **k: contextlib.nullcontext(response)) as request:
            deploy.ready(8787)
            deploy.ready(8810)
        self.assertEqual([c.args[0] for c in request.call_args_list],
                         ['http://127.0.0.1:8787/api/ui/topbar', 'http://127.0.0.1:8810/health'])

    def test_apply_and_rollback_allow_systemd_readiness_and_preserve_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, old, new, backup, tokens = [root / n for n in ('source','old','new','backup','tokens')]
            for path in (source,old,new,backup,tokens):path.mkdir()
            target = root / 'live.py'
            target.write_text('previous')
            (source / 'app.py').write_text('candidate')
            (backup / 'external-0').write_text('previous')
            (old / 'runtime.py').write_text('previous')
            (new / 'runtime.py').write_text('candidate')
            (tokens / 'fixture.json').write_text('{"fixture":true}')
            database = root / 'accounts.sqlite3'
            with contextlib.closing(sqlite3.connect(database)) as conn:
                conn.execute('CREATE TABLE retained_history (id INTEGER PRIMARY KEY)')
                conn.execute('INSERT INTO retained_history VALUES (1)')
                conn.commit()
            manifest = {'status':'prepared','commit':'fixture123456','previous_release':str(old),
                        'new_release':str(new),'previous_tree':deploy.tree(old),'new_tree':deploy.tree(new),
                        'external':{str(target):{'relative':'app.py','backup':'external-0',
                                    'before':deploy.digest(target),'after':deploy.digest(source/'app.py')}}}
            deploy.save(backup,manifest)
            current = [old]
            network_blocked = [False]
            original = {u:('active' if u in deploy.UNITS or u=='x-post-manual.timer' else 'inactive') for u in deploy.UNITS+deploy.TRIGGERS}
            states = dict(original)
            channels = {'x','fb','tt','youtube'}
            def channel_enabled(name, enabled, **kwargs):
                channels.add(name) if enabled else channels.discard(name)
            controller = SimpleNamespace(config=lambda:{'enabled_channels':sorted(channels),'preparation_only_channels':[]},channel_enabled=channel_enabled)
            def command(*args):
                if args[0]=='iptables':network_blocked[0] = '-I' in args
                if args[0]=='systemctl':
                    action,unit=args[1:3]
                    if action=='start' and unit=='x-post-automation.service':
                        self.assertFalse(network_blocked[0], 'ExecStartPost cannot reach the sidecar health port')
                    states[unit]='inactive' if action=='stop' else 'active'
                return ''
            with contextlib.ExitStack() as stack:
                for name,value in {'SOURCE':source,'CURRENT':SimpleNamespace(resolve=lambda:current[0]),
                                   'DB':database,'TOKENS':tokens,'pipeline':lambda:controller,
                                   'state':lambda unit:states[unit],'run':command,
                                   'counts':lambda:{'pipeline_jobs':0,'publishing_queues':0,'shares':0,'callers':[],'accepted_ports':0},
                                   'switch':lambda path:current.__setitem__(0,path),
                                   'copy':lambda src,dst:shutil.copyfile(src,dst),'ready':lambda port:None}.items():
                    stack.enter_context(patch.object(deploy,name,value))
                deploy.apply(backup)
                self.assertEqual(current[0],new)
                self.assertEqual(target.read_text(),'candidate')
                self.assertEqual(json.loads((backup/'manifest.json').read_text())['status'],'complete')
                deploy.apply(backup,rollback=True)
                self.assertEqual(current[0],old)
                self.assertEqual(target.read_text(),'previous')
            self.assertFalse(network_blocked[0])
            self.assertEqual(channels,{'x','fb','tt','youtube'})
            self.assertEqual(states,original)
            with contextlib.closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute('SELECT id FROM retained_history').fetchall(),[(1,)])
            self.assertEqual((tokens/'fixture.json').read_text(),'{"fixture":true}')


if __name__ == '__main__':unittest.main()
