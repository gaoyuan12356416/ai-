"""Display the recorded X failure phase without changing execution facts."""


def phases_for(conn, rows):
    ids = [r['id'] for r in rows if r['channel'] == 'x' and r['stage'] == 'publish'
           and r['status'] == 'unknown']
    result = {}
    for offset in range(0, len(ids), 400):
        chunk = ids[offset:offset + 400]
        for row in conn.execute('SELECT p.job_id,p.phase FROM pipeline_checkpoint p '
                                'JOIN pipeline_job j ON j.id=p.job_id AND j.generation=p.generation WHERE p.job_id IN (' +
                                ','.join('?' for _ in chunk) + ')', chunk):
            result.setdefault(row['job_id'], set()).add(row['phase'])
    return result


def display_code(row, phases=()):
    code = row['error_code'] or ('unknown' if row['status'] == 'unknown' else 'failed')
    if row['channel'] != 'x' or row['stage'] != 'publish':
        return code
    if (code == 'x_upstream_error' and 'temporarily locked' in (row['error_message'] or '').lower()):
        return 'x_account_temporarily_locked'
    if row['status'] == 'unknown':
        if 'x_media_uploaded' in phases or 'x_source_published' in phases:
            return 'x_post_outcome_unknown:' + code
        if 'x_media_initialized' in phases:
            # This is a phase label, not authorization to clear unknown or retry.
            return 'x_upload_interrupted:' + code
    return code
