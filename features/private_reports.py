"""Fail-closed, cookie-only owner authorization for private report assets."""
import json
import os
import time
from pathlib import Path


def authorize(handler, app):
    status = 503
    try:
        path = os.environ.get('PRIVATE_REPORT_OWNERS_FILE', '/root/drama_material_service/config/private_report_owners.json')
        owner = json.loads(Path(path).read_text())['yuesiying']
        if not owner.get('user_id') or not owner.get('tenant_key'):
            raise ValueError('Owner is not configured')
        if app['feishu_auth_enabled']():
            # Deliberately bypass _session(): API tokens are not human login.
            session = app['load_session'](handler._cookies().get(app['SESSION_COOKIE_NAME'], ''))
            status = 401
            if session and int(session.get('expires_at', 0)) > time.time():
                status = 403
                if (session.get('user_id') == owner['user_id']
                        and session.get('tenant_key') == owner['tenant_key']
                        and session.get('source') in ('feishu_web', 'feishu_internal')):
                    with app['JOB_DB_LOCK']:
                        conn = app['get_job_db_connection']()
                        try:
                            row = conn.execute('SELECT status, tenant_key FROM drama_admin_user WHERE user_id = ?', (owner['user_id'],)).fetchone()
                        finally:
                            conn.close()
                    if row and row[0] == 'active' and row[1] == owner['tenant_key']:
                        status = 204
    except Exception:
        # Do not log cookies, user records, or configuration contents.
        status = 503
    handler.send_response(status)
    handler.send_header('Content-Length', '0')
    handler.send_header('Cache-Control', 'private, no-store')
    handler.end_headers()
