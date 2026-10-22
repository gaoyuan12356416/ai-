"""Narrow patch usable on the Git source and the SHA-guarded live composite."""
import ast

OLD_HINT = '同一产品 + 剧 ID 只允许存在一条任务。失败后请对原任务点击“重新制作”。'
NEW_HINT = '同一产品、同一剧可按不同集数范围分别创建任务；相同范围不可重复创建。失败后请对原任务点击“重新制作”。'


def patch_page(source):
    if source.count(OLD_HINT) != 1:
        raise ValueError('Unexpected drama task hint')
    return source.replace(OLD_HINT, NEW_HINT)


def patch_app(source):
    tree = ast.parse(source)
    nodes = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    lines = source.splitlines(keepends=True)
    replacements = {
        'find_duplicate_job': '''def find_duplicate_job(app_id, content_id, episode_start, episode_end, exclude_job_id=None):
    from features.drama_job_identity import find_duplicate
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            return find_duplicate(conn, app_id, content_id, episode_start, episode_end, exclude_job_id)
        finally:
            conn.close()
''',
        'ensure_no_duplicate_job': '''def ensure_no_duplicate_job(app_id, content_id, episode_start, episode_end, exclude_job_id=None):
    from features.drama_job_identity import reject_duplicate
    reject_duplicate(find_duplicate_job(app_id, content_id, episode_start, episode_end, exclude_job_id))
''',
    }
    submit = ast.get_source_segment(source, nodes['submit_job'])
    old = 'ensure_no_duplicate_job(app_id, content_id)'
    if submit.count(old) != 1:
        raise ValueError('Unexpected submit duplicate guard')
    replacements['submit_job'] = submit.replace(old, 'ensure_no_duplicate_job(app_id, content_id, episode_start, episode_end)') + '\n'
    upsert = ast.get_source_segment(source, nodes['upsert_job_record'])
    # Insert immediately before the existing INSERT, after the lease fence.
    insert = next(n for n in ast.walk(nodes['upsert_job_record'])
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                  and n.value.args and isinstance(n.value.args[0], ast.Constant)
                  and isinstance(n.value.args[0].value, str)
                  and 'INSERT INTO drama_material_job (' in n.value.args[0].value)
    offset = insert.lineno - nodes['upsert_job_record'].lineno
    body = upsert.splitlines(keepends=True)
    body.insert(offset, '            from features.drama_job_identity import guard_new_job\n            guard_new_job(conn, job)\n')
    replacements['upsert_job_record'] = ''.join(body) + '\n'
    for name in sorted(replacements, key=lambda key: nodes[key].lineno, reverse=True):
        node = nodes[name]
        lines[node.lineno-1:node.end_lineno] = [replacements[name]]
    result = ''.join(lines)
    compile(result, 'app.py', 'exec')
    return result
