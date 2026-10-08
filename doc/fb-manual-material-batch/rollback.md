# Group 62 rollback

This is an operator procedure, not an automatic failure retry. Drain current
prepare/execute sidecar requests first and record the timer states. Cancel only
the exact run 242 tasks that have never reached Graph. Do not disable or change
the shared automatic template.

On CPU `43.166.187.96`, after draining in-flight requests:

```bash
cd /opt/fb-auto-post/current
umask 0007
python3 - <<'PY'
import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
db = '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
root = Path('/mnt/data-disk/fb-manual-material-batch/operations/group62-round-20261008')
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
with sqlite3.connect(db) as src, sqlite3.connect(str(root / ('rollback-' + stamp + '.sqlite3'))) as dst:
    src.backup(dst)
conn = sqlite3.connect(db)
conn.execute('BEGIN IMMEDIATE')
assert conn.execute("SELECT slot_key FROM fb_auto_run WHERE id=242").fetchone()[0] == 'manual:materials:group62-round-20261008'
assert not conn.execute("SELECT 1 FROM fb_auto_task WHERE run_id=242 AND status IN ('preparing','running')").fetchone()
rows = conn.execute("""SELECT id FROM fb_auto_task t WHERE run_id=242
    AND status IN ('planned','ready') AND unknown_outcome=0 AND graph_post_id=''
    AND NOT EXISTS (SELECT 1 FROM fb_auto_publish_attempt a WHERE a.task_id=t.id)
    AND NOT EXISTS (SELECT 1 FROM fb_auto_publish_ledger l WHERE l.task_id=t.id)
    ORDER BY id""").fetchall()
(root / ('rollback-' + stamp + '.json')).write_text(json.dumps({'run_id':242, 'cancelled_task_ids':[r[0] for r in rows]})+'\n')
conn.executemany("UPDATE fb_auto_task SET status='skipped',skip_reason='fb_manual_operator_cancel',error_code='fb_manual_operator_cancel',completed_at_utc=? WHERE id=?", [(datetime.now(timezone.utc).isoformat(), r[0]) for r in rows])
conn.commit()
conn.close()
print('Cancelled only unsubmitted tasks:', len(rows))
PY
cp -p /mnt/data-disk/fb-manual-material-batch/backups/20261008T104954Z/features/fb_auto_posts/core.py \
  /opt/fb-auto-post/current/features/fb_auto_posts/core.py
cp -p /mnt/data-disk/fb-manual-material-batch/backups/20261008T110459Z/features/fb_auto_posts/gpu.py \
  /opt/fb-auto-post/current/features/fb_auto_posts/gpu.py
python3 -m py_compile features/fb_auto_posts/core.py features/fb_auto_posts/gpu.py
systemctl restart fb-auto-post-service.service
curl --fail --silent http://127.0.0.1:18835/health
```

Restore the timers to their recorded states and refresh the run summary using
the normal sidecar read/refresh path. Leave the unused operator CLI and manual
batch module dormant; no template setting, blacklist or public API needs to be
changed. Keep the current database, accepted/published/unknown tasks, publish
attempts, short-link wrappers, GPU source hashes and recipes. The SQLite backups
are recovery evidence and must not overwrite the live publishing ledger.
