"""Deploy only the manual link attribution update, using the existing backup flow."""
import deploy_youtube_manual_links as deployment
import sqlite3
from contextlib import closing

EXPECTED = {
    'features/youtube_auto_publish/manual_links.py':
        '538f5a62679ae494dfa65905e01c1803223ebed83c820f258f40ed4fad15b556',
}

restore_code = deployment.rollback


def rollback(backup):
    # V1 cannot resume a V2 operation. Stop new requests before checking this.
    try:
        deployment.run('systemctl', 'stop', deployment.UNIT)
        db = deployment.ROOT/'data/drama_material_jobs.sqlite3'
        with closing(sqlite3.connect('file:'+str(db)+'?mode=ro', uri=True)) as conn:
            pending = conn.execute("SELECT COUNT(*) FROM youtube_manual_short_link m "
                                   "JOIN drama_material_short_link s ON s.id=m.link_id "
                                   "WHERE json_extract(m.context_json,'$.version')='youtube-manual-link-v2' "
                                   "AND s.publish_state<>'published'").fetchone()[0]
        if pending:
            raise RuntimeError('Rollback refused: finish pending V2 operations first; records are retained')
        restore_code(backup)
    finally:
        deployment.run('systemctl', 'start', deployment.UNIT)


if __name__ == '__main__':
    deployment.EXPECTED = EXPECTED
    deployment.rollback = rollback
    deployment.main()
