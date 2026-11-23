"""Scoped account metadata reads in the account owner's filesystem namespace."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

FIELDS = ('id', 'x_user_id', 'username', 'display_name', 'profile_image_url',
          'scopes_json', 'status', 'access_expires_at', 'deleted_at', 'publish_approved',
          'subscription_type', 'drama_language', 'owner_tenant_key', 'owner_user_id',
          'owner_name', 'owner_email', 'profile_synced_at', 'verified', 'protected', 'last_error')


def read_snapshot(path, account_id, actor, scope):
    from features.x_accounts.oauth_service import normalize_account_scope, ServiceError
    actor, scope = normalize_account_scope(actor, scope)
    if type(account_id) is not int or account_id <= 0:
        raise ServiceError('invalid_request', '账号编号无效', 400)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=8)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        clause, values = 'id=?', [account_id]
        if scope == 'mine':
            clause += ' AND owner_tenant_key=? AND owner_user_id=?'
            values += [actor['tenant_key'], actor['user_id']]
        row = db.execute('SELECT '+','.join(FIELDS)+' FROM x_authorized_account WHERE '+clause, values).fetchone()
    if not row:
        raise ServiceError('x_account_not_found', 'X账号记录不存在', 404)
    return dict(row)


def install_reader(runtime):
    from .transport import rpc, Error
    def scoped(account_id, actor, scope='mine'):
        actor, scope = runtime.normalize_account_scope(actor, scope)
        try:
            result = rpc('http://127.0.0.1:8898', '/internal/x-tag/account-snapshot',
                         runtime.INTERNAL_TOKEN, payload={'account_id': int(account_id),
                         'actor': actor, 'scope': scope}, timeout=15)
        except Error as exc:
            raise runtime.ServiceError(exc.code, str(exc), exc.status) from None
        row = result.get('record')
        if not isinstance(row, dict) or row.get('id') != int(account_id) or not set(FIELDS).issubset(row):
            raise runtime.ServiceError('x_tag_account_snapshot_invalid', '账号读取结果与发布任务不一致', 503)
        return row
    runtime.find_scoped_account_row = scoped


def main():
    from features.x_accounts.oauth_service import ServiceError
    try:
        body = json.loads(sys.stdin.read(16385))
        if set(body) != {'account_id', 'actor', 'scope'}:
            raise ValueError()
        record = read_snapshot('/var/lib/x-post-automation/accounts.sqlite3', **body)
        result = {'ok': True, 'record': record}
    except ServiceError as exc:
        result = {'ok': False, 'code': exc.code, 'message': str(exc), 'status': exc.status}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result = {'ok': False, 'code': 'x_tag_account_snapshot_unavailable',
                  'message': '发布账号记录暂时无法读取，保留原任务等待重试', 'status': 503}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
