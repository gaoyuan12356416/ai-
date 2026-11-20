"""Operator termination of one tag-template run, preserving publication facts."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


TERMINAL = {'published', 'failed', 'canceled', 'skipped', 'no_candidate'}
CODE = 'operator_terminated'
MESSAGE = '操作人手动终止本次执行'


class RunControlError(Exception):
    def __init__(self, code, message, status):
        self.code, self.status = code, status
        super().__init__(message)


def _event(conn, run_id, task_id, previous, stamp, actor, details=None):
    conn.execute(
        "INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,"
        "message,details_json,created_at) VALUES(?,?,'operator_terminated',?,'canceled',?,?,?)",
        (run_id, task_id, previous, MESSAGE,
         json.dumps(dict(actor_user_id=actor['user_id'], actor_name=actor.get('name', ''),
                         **(details or {})), ensure_ascii=False), stamp))


def _sidecar_owners(paths):
    # The gateway already runs as root and owns cross-ledger administration.
    # SQLite WAL/SHM files must remain accessible to each ledger's service user.
    if not hasattr(os, 'chown') or os.geteuid() != 0:
        return
    for path in paths:
        stat = Path(path).stat()
        for suffix in ('-wal', '-shm'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                os.chown(sidecar, stat.st_uid, stat.st_gid)
                os.chmod(sidecar, stat.st_mode & 0o777)


def terminate_run(auto_path, post_path, run_id, actor):
    if type(run_id) is not int or not 0 < run_id < 2**63:
        raise RunControlError('invalid_request', '运行编号无效', 400)
    if not actor.get('user_id'):
        raise RunControlError('permission_denied', '请先登录', 403)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with closing(sqlite3.connect(str(auto_path), timeout=15)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('ATTACH DATABASE ? AS posts', (str(post_path),))
        try:
            conn.execute('BEGIN IMMEDIATE')
            _sidecar_owners((auto_path, post_path))
            run = conn.execute('SELECT * FROM x_auto_run WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise RunControlError('x_auto_run_not_found', '运行记录不存在', 404)
            if run['status'] not in {'queued', 'running', 'canceled'}:
                raise RunControlError('x_auto_run_finished', '本次运行已经结束', 409)
            # A selector/preparer may still return after termination. Fence its
            # stale transition in the same database transaction as cancellation.
            conn.execute("""CREATE TRIGGER IF NOT EXISTS x_tag_terminated_task_fence
                BEFORE UPDATE OF status ON x_auto_task
                WHEN OLD.status='canceled' AND OLD.error_code='operator_terminated'
                  AND NEW.status NOT IN ('canceled','published','unknown','reconciling')
                BEGIN SELECT RAISE(ABORT,'x_auto_run_terminated'); END""")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS x_tag_terminated_run_fence
                BEFORE UPDATE OF status,claim_phase ON x_auto_task
                WHEN (SELECT status FROM x_auto_run WHERE id=NEW.run_id)='canceled'
                  AND (NEW.status IN ('pending','selecting','reserved','preparing','ready','publishing')
                       OR (NEW.status='retry_wait' AND NEW.claim_phase<>'reconcile'))
                BEGIN SELECT RAISE(ABORT,'x_auto_run_terminated'); END""")
            tasks = conn.execute('SELECT * FROM x_auto_task WHERE run_id=?', (run_id,)).fetchall()
            canceled = waiting = 0
            for task in tasks:
                if task['status'] in TERMINAL:
                    continue
                executions = conn.execute(
                    "SELECT * FROM posts.x_post_manual_run WHERE trigger_source='auto_template' "
                    "AND external_task_key=?", ('x-auto-task-' + str(task['id']),)).fetchall()
                queues = []
                for execution in executions:
                    queues.extend(conn.execute('SELECT * FROM posts.x_post_queue WHERE manual_run_id=?',
                                               (execution['id'],)).fetchall())
                logs = [conn.execute('SELECT * FROM posts.x_post_publish_log WHERE queue_id=?',
                                     (queue['id'],)).fetchone() for queue in queues]
                # An upstream request already sent cannot be withdrawn. Keep its
                # exact receipt/unknown status for normal read-only reconciliation.
                in_flight = (task['unknown_outcome'] or task['publish_id'] or task['publish_attempt_count']
                             or task['status'] in {'publishing', 'unknown'}
                             or any(log and (log['attempt_count'] or log['x_post_id'] or log['unknown_outcome']
                                             or log['status'] in {'post_creating','published'}) for log in logs))
                if in_flight:
                    waiting += 1
                    continue
                for queue, log in zip(queues, logs):
                    if queue['status'] == 'published':
                        raise RunControlError('x_auto_receipt_changed', '发布回执已变化，请刷新后重试', 409)
                    conn.execute("INSERT OR IGNORE INTO posts.pipeline_canonical_hold VALUES(?,?,'operator_terminated')",
                                 ('x:queue:' + str(queue['id']), stamp))
                    conn.execute("UPDATE posts.x_post_queue SET status='failed',updated_at=? WHERE id=?",
                                 (stamp, queue['id']))
                    if log:
                        conn.execute("UPDATE posts.x_post_publish_log SET status='failed',error_code=?,"
                                     "error_message=?,updated_at=? WHERE id=?", (CODE,MESSAGE,stamp,log['id']))
                    else:
                        conn.execute("INSERT INTO posts.x_post_publish_log(queue_id,account_id,status,error_code,"
                                     "error_message,unknown_outcome,created_at,updated_at) VALUES(?,?,'failed',?,?,0,?,?)",
                                     (queue['id'],queue['account_id'],CODE,MESSAGE,stamp,stamp))
                for execution in executions:
                    conn.execute("UPDATE posts.x_post_manual_run SET status='stopped',error_code=?,error_message=?,"
                                 "failed_count=queued_count,finished_at=?,updated_at=? WHERE id=?",
                                 (CODE,MESSAGE,stamp,stamp,execution['id']))
                conn.execute("UPDATE x_auto_task SET status='canceled',claim_phase='',claim_worker='',claim_token='',"
                             "lease_expires_at_utc='',next_attempt_at_utc='',error_code=?,error_message=?,"
                             "finished_at_utc=?,updated_at=? WHERE id=?", (CODE,MESSAGE,stamp,stamp,task['id']))
                conn.execute("UPDATE x_auto_material_ledger SET last_task_status='canceled',updated_at=? WHERE task_id=?",
                             (stamp,task['id']))
                _event(conn,run_id,task['id'],task['status'],stamp,actor)
                canceled += 1
            if run['status'] != 'canceled':
                conn.execute("UPDATE x_auto_run SET status='canceled',error_code=?,error_message=?,"
                             "finished_at_utc=?,updated_at=? WHERE id=?", (CODE,MESSAGE,stamp,stamp,run_id))
                _event(conn,run_id,None,run['status'],stamp,actor,dict(canceled_count=canceled,waiting_count=waiting))
            conn.commit()
            return dict(ok=True,run_id=run_id,status='canceled',canceled_count=canceled,
                        waiting_count=waiting,already_terminated=run['status']=='canceled',
                        message=('已终止后续执行；%d 个在途任务继续核对结果' % waiting if waiting else '本次队列执行已终止'))
        except BaseException:
            conn.rollback()
            raise
        finally:
            _sidecar_owners((auto_path, post_path))
