import hashlib
import json
from contextlib import closing, contextmanager, ExitStack
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import deploy_video_editing_resource_mirror as deploy


class ResourceMirrorDeployTests(unittest.TestCase):
    @contextmanager
    def gpu_fixture(self,root,*,installed=False):
        base=root/'gpu';rev='b'*40;feature=root/'feature';candidate=base/'releases'/rev
        for name in deploy.GPU_FILES:
            for directory in (candidate,feature):
                path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'candidate = True\n')
        current=base/'current';live=candidate if installed else base/'releases'/deploy.GPU_BASELINE
        marker=root/'intake.disabled'
        guard=SimpleNamespace(health=Mock(return_value={'ok':True,'release_sha':live.name,'render_concurrency':1,
                                                       'video_editing_profile':'video-editing-720-v1','video_editing_probe_concurrency':3,
                                                       'video_editing_enabled':False}),
            check_health=Mock(),effective_env=Mock(return_value={'DRAMA_WORK_ROOT':str(root/'work')}),
            ledger_snapshot=Mock(return_value={'hashes':{'old.json':'original'},'busy':0}),
            SAFE_HEALTH=('render_concurrency',),run=Mock(),property_value=Mock(return_value='active'),
            TUNNEL='gpu-tunnel.service',WORKER='gpu-worker.service')
        original_resolve=Path.resolve;original_link=Path.is_symlink
        with ExitStack() as stack:
            stack.enter_context(patch.multiple(deploy,GPU_BASE=base,ROOT=feature,MARKER=marker))
            stack.enter_context(patch.object(deploy,'published',return_value=rev))
            stack.enter_context(patch.object(deploy,'run',side_effect=lambda *args,**kwargs:'\n'.join(deploy.GPU_FILES) if 'diff' in args else ''))
            stack.enter_context(patch.object(deploy,'gpu_guard',return_value=guard))
            stack.enter_context(patch.object(Path,'is_symlink',autospec=True,side_effect=lambda p:p==current or original_link(p)))
            stack.enter_context(patch.object(Path,'resolve',autospec=True,side_effect=lambda p,*args,**kwargs:live if p==current else original_resolve(p,*args,**kwargs)))
            yield candidate,base/'deploy-backups'/('resource-mirror-'+rev[:12]),marker,guard

    def test_cpu_plan_only_changes_owned_files_and_both_static_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);candidate=root/'candidate';live=root/'live';public=root/'public'
            public.mkdir();old=b'old = True\n';new=b'new = True\n'
            for name in (*deploy.CPU_FILES, *('static/'+name for name in deploy.STATIC_FILES)):
                source=candidate/name;source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(new)
                if name.endswith('projects_v2.py'):continue
                target=live/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(old)
            for name in deploy.STATIC_FILES:(public/name).write_bytes(old)
            baseline=lambda name:None if name.endswith('projects_v2.py') else hashlib.sha256(old).hexdigest()
            with patch.multiple(deploy,ROOT=candidate,CPU_ROOT=live,PUBLIC=public),patch.object(deploy,'baseline_hash',side_effect=baseline):
                rows=deploy.cpu_plan()
                self.assertEqual(len(rows),len(deploy.CPU_FILES)+2*len(deploy.STATIC_FILES))
                self.assertFalse(any(row['path'].endswith('worker.py') for row in rows))
                self.assertTrue(all(str(live) in row['path'] or str(public) in row['path'] for row in rows))
                (live/'features/video_editing/catalog.py').write_text('unreviewed = True')
                with self.assertRaisesRegex(RuntimeError,'live_file_drift'):deploy.cpu_plan()

    def test_cpu_default_check_writes_nothing_and_does_not_close_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)/'data';marker=Path(tmp)/'intake.disabled';material=Path(tmp)/'material.disabled'
            with patch.multiple(deploy,CPU_DATA=data,MARKER=marker,MATERIAL_MARKER=material),\
                 patch.object(deploy,'cpu_storage'),patch.object(deploy,'cpu_plan',return_value=[]),\
                 patch.object(deploy,'cpu_counts',return_value={'editing':0,'material_writes':0}),\
                 patch.object(deploy,'save') as save,patch.object(deploy,'run') as run:
                result=deploy.cpu(SimpleNamespace(phase='check'),'a'*40)
            self.assertEqual(result['phase'],'check');save.assert_not_called();run.assert_not_called()
            self.assertFalse(data.exists());self.assertFalse(marker.exists());self.assertFalse(material.exists())

    def test_marker_restore_preserves_preexisting_pause_and_allows_lost_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path(tmp)/'old';new=Path(tmp)/'new';old.write_bytes(b'operator paused\n')
            control={'markers':deploy.marker_state((old,new))}
            deploy.close_markers(control);self.assertEqual(new.read_bytes(),deploy.MARKER_TEXT)
            deploy.restore_markers(control);deploy.restore_markers(control)
            self.assertEqual(old.read_bytes(),b'operator paused\n');self.assertFalse(new.exists())
            new.write_bytes(b'another rollout')
            with self.assertRaisesRegex(RuntimeError,'rollout_marker_changed'):deploy.restore_markers(control)
            self.assertEqual(new.read_bytes(),b'another rollout')

    def test_cpu_abort_recovers_partial_prepare_without_prepare_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)/'data';rev='a'*40;backup=data/'backups'/('resource-mirror-'+rev[:12]);backup.mkdir(parents=True)
            marker=Path(tmp)/'marker';other=Path(tmp)/'not-yet-created'
            control={'release':rev,'markers':deploy.marker_state((marker,other)),'units':{},'observed':{}}
            deploy.save(backup/'control.json',control);marker.write_bytes(deploy.MARKER_TEXT)
            with patch.object(deploy,'CPU_DATA',data),patch.object(deploy,'cpu_storage'),\
                 patch.object(deploy,'resume_units') as resume:
                result=deploy.cpu(SimpleNamespace(phase='abort'),rev)
            self.assertEqual(result['phase'],'abort');resume.assert_called_once_with(control)
            self.assertFalse(marker.exists());self.assertFalse(other.exists())

    def test_cpu_abort_never_rolls_code_back_after_install_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);rev='a'*40;backup=data/'backups'/('resource-mirror-'+rev[:12]);backup.mkdir(parents=True)
            deploy.save(backup/'control.json',{'release':rev,'markers':{},'units':{},'observed':{}})
            deploy.save(backup/'install.started',{'release':rev})
            with patch.object(deploy,'CPU_DATA',data),patch.object(deploy,'cpu_storage'),patch.object(deploy,'resume_units') as resume:
                with self.assertRaisesRegex(RuntimeError,'retain_v2_readers'):deploy.cpu(SimpleNamespace(phase='abort'),rev)
            resume.assert_not_called()

    def test_counts_ignore_unrelated_and_read_only_reconciliation_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('CREATE TABLE ve_task(status TEXT);CREATE TABLE ve_material_upload(status TEXT);')
                db.executemany('INSERT INTO ve_task VALUES(?)',[(state,) for state in ('unknown','recovery_required','completed','queued')])
                db.executemany('INSERT INTO ve_material_upload VALUES(?)',[(state,) for state in ('unknown_outcome','verifying','succeeded','copying')])
                db.commit()
            self.assertEqual(deploy.cpu_counts(path),{'editing':1,'material_writes':1})

    def test_unrelated_pid_changes_are_warnings(self):
        with patch.object(deploy,'prop',return_value='200'):
            self.assertEqual(deploy.observed_warnings({'observed':{'unrelated.service':'100'}}),
                             ['unrelated_pid_changed:unrelated.service'])

    def test_gpu_historical_records_must_remain_but_new_jobs_are_allowed(self):
        before={'hashes':{'old.json':'original'}}
        self.assertTrue(deploy.preserved_gpu_ledger(before,{'hashes':{'old.json':'original','new.json':'new'}}))
        self.assertFalse(deploy.preserved_gpu_ledger(before,{'hashes':{'old.json':'changed'}}))
        self.assertFalse(deploy.preserved_gpu_ledger(before,{'hashes':{}}))

    def test_gpu_check_has_no_marker_backup_or_service_mutation(self):
        with tempfile.TemporaryDirectory() as tmp,self.gpu_fixture(Path(tmp)) as (candidate,backup,marker,guard):
            result=deploy.gpu(SimpleNamespace(phase='check',gpu_release=str(candidate)),'a'*40)
            self.assertEqual(result['phase'],'check');self.assertFalse(backup.exists());self.assertFalse(marker.exists())
            guard.run.assert_not_called()

    def test_gpu_abort_recovers_failed_prepare_without_prepare_proof(self):
        with tempfile.TemporaryDirectory() as tmp,self.gpu_fixture(Path(tmp)) as (candidate,backup,marker,guard):
            backup.mkdir(parents=True)
            control={'release':candidate.name,'feature_commit':'a'*40,'markers':deploy.marker_state((marker,))}
            deploy.save(backup/'control.json',control);marker.write_bytes(deploy.MARKER_TEXT)
            result=deploy.gpu(SimpleNamespace(phase='abort',gpu_release=str(candidate)),'a'*40)
            self.assertEqual(result['phase'],'abort');self.assertFalse(marker.exists());guard.run.assert_not_called()

    def test_gpu_lost_apply_ack_reconciles_running_candidate_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp,self.gpu_fixture(Path(tmp),installed=True) as (candidate,backup,marker,guard):
            backup.mkdir(parents=True)
            health=dict(guard.health.return_value);health['video_editing_enabled']=True
            control={'release':candidate.name,'feature_commit':'a'*40,'markers':deploy.marker_state((marker,)),'health':health}
            deploy.save(backup/'control.json',control);marker.write_bytes(deploy.MARKER_TEXT)
            for name,value in {'prepare.json':{},'install.started':{},
                               'before.json':{'ledger':{'hashes':{'old.json':'original'}},'work_root':str(Path(tmp)/'work'),'preflight_ok':True}}.items():
                deploy.save(backup/name,value)
            result=deploy.gpu(SimpleNamespace(phase='apply',gpu_release=str(candidate),drain_timeout=0),'a'*40)
            self.assertEqual(result['phase'],'apply');self.assertTrue((backup/'apply.json').is_file())
            guard.run.assert_not_called();guard.effective_env.assert_not_called()

    def test_gpu_apply_recovery_starts_missing_tunnel_without_restarting_healthy_worker(self):
        for proof_exists in (False,True):
            with self.subTest(apply_proof=proof_exists),tempfile.TemporaryDirectory() as tmp,\
                 self.gpu_fixture(Path(tmp),installed=True) as (candidate,backup,marker,guard):
                backup.mkdir(parents=True)
                health=dict(guard.health.return_value);health['video_editing_enabled']=True
                control={'release':candidate.name,'feature_commit':'a'*40,'markers':deploy.marker_state((marker,)),'health':health}
                deploy.save(backup/'control.json',control);marker.write_bytes(deploy.MARKER_TEXT)
                for name,value in {'prepare.json':{},'install.started':{},
                                   'before.json':{'ledger':{'hashes':{'old.json':'original'}},'work_root':str(Path(tmp)/'work'),'preflight_ok':True}}.items():
                    deploy.save(backup/name,value)
                if proof_exists:deploy.save(backup/'apply.json',{'ok':True,'phase':'apply'})
                state={'tunnel':'inactive'}
                guard.property_value.side_effect=lambda unit,key:state['tunnel']
                def start(*args,**kwargs):
                    self.assertEqual(args,('systemctl','start',guard.TUNNEL))
                    state['tunnel']='active'
                guard.run.side_effect=start
                args=SimpleNamespace(phase='apply',gpu_release=str(candidate),drain_timeout=0)
                self.assertEqual(deploy.gpu(args,'a'*40)['phase'],'apply')
                guard.run.assert_called_once_with('systemctl','start',guard.TUNNEL)
                guard.effective_env.assert_not_called()
                deploy.gpu(args,'a'*40)
                self.assertEqual(guard.run.call_count,1)

    def test_cpu_apply_reconciliation_does_not_restart_already_applied_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);rev='a'*40;backup=data/'backups'/('resource-mirror-'+rev[:12]);backup.mkdir(parents=True)
            proof={'ok':True,'host':'cpu','phase':'apply','release':rev}
            for name,value in {'control.json':{'release':rev,'markers':{},'units':{},'api_state':'active'},
                               'prepare.json':{},'before.json':{},'files.json':[],'apply.json':proof}.items():
                deploy.save(backup/name,value)
            with patch.object(deploy,'CPU_DATA',data),patch.object(deploy,'cpu_storage'),\
                 patch.object(deploy,'prop',return_value='0'),patch.object(deploy,'cpu_idle'),\
                 patch.object(deploy,'evidence',return_value={}),patch.object(deploy,'api_ready'),patch.object(deploy,'run') as run:
                self.assertEqual(deploy.cpu(SimpleNamespace(phase='apply'),rev),proof)
            run.assert_not_called()

    def test_additive_migration_rehearsal_preserves_original_business_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''
                CREATE TABLE ve_source(id TEXT PRIMARY KEY,kind TEXT NOT NULL,owner TEXT NOT NULL,name TEXT NOT NULL,
                  status TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
                CREATE TABLE ve_task(id TEXT PRIMARY KEY,owner TEXT NOT NULL,project_id TEXT NOT NULL DEFAULT '',operation_id TEXT NOT NULL,
                  kind TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,UNIQUE(owner,operation_id));
                CREATE TABLE ve_output(id TEXT PRIMARY KEY,owner TEXT NOT NULL,task_id TEXT NOT NULL UNIQUE,batch_id TEXT NOT NULL,
                  status TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
                CREATE TABLE ve_material_upload(id TEXT PRIMARY KEY,output_id TEXT NOT NULL UNIQUE,task_id TEXT NOT NULL UNIQUE,
                  owner TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
                INSERT INTO ve_source VALUES('old-source','asset','owner','old.mp4','ready','{}',1,1);
                INSERT INTO ve_task VALUES('old-task','owner','project','old-operation','render','completed','{"recipe":"historical"}',1,1);
                INSERT INTO ve_output VALUES('old-output','owner','old-task','batch','ready','{"sha":"immutable"}',1,1);
                INSERT INTO ve_material_upload VALUES('old-upload','old-output','old-task','owner','unknown_outcome','{"attempts":1}',1,1);
                ''')
            before=deploy.evidence(path);deploy.migrate(path);deploy.migrate(path)
            self.assertEqual(deploy.evidence(path),before)
            with closing(sqlite3.connect(path)) as db:
                columns={row[1] for row in db.execute('PRAGMA table_info(ve_source)')}
                self.assertTrue({'series_code','library_kind'} <= columns)
                self.assertEqual(db.execute('SELECT id,name,data,library_kind,series_code FROM ve_source').fetchone(),
                                 ('old-source','old.mp4','{}','material',''))
                self.assertEqual(db.execute('SELECT count(*) FROM ve_unit_render').fetchone()[0],0)


if __name__=='__main__':unittest.main()
