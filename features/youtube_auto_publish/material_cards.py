"""Read-only card listing, independently deployable from publishing workers.

The existing bootstrap route authenticates each request with the same YouTube
navigation permission. Full metadata and submission remain in the main API.
"""
import json
import os
import re
import sqlite3
from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from .runtime import readonly_runner
from .source import DEFAULT_HOSTS, MaterialSource
from .templates import WorkflowError

ROUTE = '/api/youtube-auto-publish/material-cards'


def logged_in_uploader_ids(app, reader):
    # Match the deployed main API's uploader visibility (login history + email).
    def resolve():
        with closing(sqlite3.connect('file:' + str(app.JOB_DB_PATH) + '?mode=ro', uri=True, timeout=3)) as db:
            rows = db.execute("SELECT email FROM drama_admin_user WHERE login_count>0 "
                              "AND TRIM(COALESCE(email,''))<>''").fetchall()
        emails = sorted({str(row[0]).strip().lower() for row in rows})
        if not emails:
            return set()
        database = app.ADMIN_MAPPING_MYSQL_DATABASE
        if not re.fullmatch(r'[A-Za-z0-9_]+', database):
            raise WorkflowError('material_query_failed', '上传人映射配置无效', 503)
        literals = ','.join('BINARY CONVERT(0x' + email.encode().hex() + ' USING utf8mb4)' for email in emails)
        sql = ('SELECT DISTINCT CAST(sub_user_id AS CHAR) FROM `' + database + '`.admin_user_group '
               'WHERE BINARY LOWER(TRIM(email)) IN (' + literals + ') AND sub_user_id IS NOT NULL')
        return {str(row[0]) for row in reader(sql) if row and re.fullmatch(r'[1-9][0-9]{0,18}', str(row[0]))}
    return resolve


def authorize(headers):
    conn = HTTPConnection('127.0.0.1', 8787, timeout=5)
    try:
        conn.request('GET', '/api/youtube-auto-publish/bootstrap?include_channels=0',
                     headers={'Host': 'ai.yingliangads.com', 'Cookie': headers.get('Cookie', ''),
                              'Accept': 'application/json'})
        response = conn.getresponse()
        body = response.read(1024 * 1024)
        if response.status in (401, 403):
            raise WorkflowError('authentication_required' if response.status == 401 else 'permission_denied',
                                '请登录后重试' if response.status == 401 else '没有 YouTube 发布权限', response.status)
        data = json.loads(body)
        if response.status != 200 or not isinstance(data, dict) or not isinstance(data.get('source'), dict) or data.get('ok') is False:
            raise ValueError('authorization unavailable')
    except WorkflowError:
        raise
    except Exception:
        raise WorkflowError('authorization_unavailable', '登录校验暂不可用，请稍后重试', 503) from None
    finally:
        conn.close()


def make_handler(source, auth=authorize):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.path != ROUTE:
                    raise WorkflowError('not_found', '页面不存在', 404)
                auth(self.headers)
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
                result = source.list(query.get('search', [''])[0][:200],
                                     uploader_id=query.get('uploader_id', [''])[0],
                                     refresh=query.get('refresh', ['0'])[0] == '1', compact=True)
                self.respond(200, dict(result, ok=True))
            except WorkflowError as exc:
                self.respond(exc.status, dict(ok=False, error=exc.code, message=exc.message))
            except ValueError:
                self.respond(400, dict(ok=False, error='invalid_query', message='查询参数无效'))
            except Exception:
                self.respond(503, dict(ok=False, error='material_query_failed', message='素材查询暂不可用，请稍后重试'))

        def respond(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            # No query text, cookies or headers in the service journal.
            pass

    return Handler


def main():
    app = SimpleNamespace(
        MYSQL_USER=os.environ.get('DRAMA_DB_USER') or os.environ.get('ADMIN_MAPPING_MYSQL_USER'),
        MYSQL_PASSWORD=os.environ.get('DRAMA_DB_PASSWORD') or os.environ.get('ADMIN_MAPPING_MYSQL_PASSWORD'),
        JOB_DB_PATH=os.environ.get('DRAMA_JOB_DB_PATH', '/root/drama_material_service/data/drama_material_jobs.sqlite3'),
        ADMIN_MAPPING_MYSQL_DATABASE=os.environ.get('ADMIN_MAPPING_MYSQL_DATABASE', 'kunlunads_dev'))
    reader = readonly_runner(app)
    hosts = tuple(x.strip().lower() for x in os.environ.get('DRAMA_YOUTUBE_SOURCE_HOSTS', ','.join(DEFAULT_HOSTS)).split(',') if x.strip())
    source = MaterialSource(os.environ.get('YOUTUBE_AUTO_MATERIAL_SQL_FILE', ''), reader,
                            allowed_hosts=hosts, async_cache=True,
                            uploader_ids_provider=logged_in_uploader_ids(app, reader))
    ThreadingHTTPServer(('127.0.0.1', 8851), make_handler(source)).serve_forever()


if __name__ == '__main__':
    main()
