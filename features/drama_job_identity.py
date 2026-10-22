"""Exact episode-range identity for new drama composition jobs."""


def find_duplicate(conn, app_id, content_id, episode_start, episode_end, exclude_job_id=None):
    row = conn.execute(
        """SELECT job_id, status, episode_start, episode_end
           FROM drama_material_job
           WHERE app_id = ? AND content_id = ?
             AND episode_start = ? AND episode_end = ?
             AND (? IS NULL OR job_id != ?)
           ORDER BY id DESC LIMIT 1""",
        (str(app_id), str(content_id), int(episode_start), int(episode_end),
         exclude_job_id, exclude_job_id),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(('job_id', 'status', 'episode_start', 'episode_end'), row))


def reject_duplicate(existing):
    if not existing:
        return
    detail = '相同产品、剧 ID 和集数范围（%s–%s 集）已存在任务。' % (
        existing['episode_start'], existing['episode_end'])
    if existing['status'] == 'failed':
        detail += '请对原任务点击“重新制作”继续执行。'
    else:
        detail += '请查看原任务，勿重复创建。'
    raise ValueError('%s job_id=%s status=%s' % (
        detail, existing['job_id'], existing['status']))


def guard_new_job(conn, job):
    """Use the caller's write transaction through its subsequent INSERT/commit.

    SQLite serializes writers across API processes. Existing job updates and
    retries retain their historical identity and are never deduplicated away.
    """
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    if conn.execute('SELECT 1 FROM drama_material_job WHERE job_id = ?',
                    (job['job_id'],)).fetchone():
        return
    reject_duplicate(find_duplicate(
        conn, job['app_id'], job['content_id'], job['episode_start'], job['episode_end']))
