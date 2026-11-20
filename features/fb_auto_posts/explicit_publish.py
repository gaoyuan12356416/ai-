"""New explicit hit publishes do not depend on the automatic schedule switch.

Only server-created run snapshots carry this marker. Existing runs are unchanged.
Template deletion, version checks and all Page/material guards still apply.
"""
MARKER = 'explicit_publish_independent'


def independent_run_sql(run='r'):
    return f"({run}.trigger_type='manual' AND COALESCE(json_extract({run}.config_json,'$.{MARKER}'),0)=1)"


def task_status_sql(template='t', task='x'):
    return (f"({template}.status='enabled' OR ({template}.status='disabled' AND EXISTS "
            f"(SELECT 1 FROM fb_auto_run explicit_run WHERE explicit_run.id={task}.run_id "
            f"AND {independent_run_sql('explicit_run')})))")


def allows_task(conn, template, task):
    if template is None or template['status'] not in {'enabled', 'disabled'}:
        return False
    if template['status'] == 'enabled':
        return True
    return bool(conn.execute(f"SELECT 1 FROM fb_auto_run r WHERE r.id=? AND {independent_run_sql()}",
                             (task['run_id'],)).fetchone())
