"""Operator-owned mode choice behind the existing cookie/navigation authorization."""
import sqlite3
from contextlib import closing
from pathlib import Path
from .store import channel_details, set_mode

ACCOUNT_DB=Path('/var/lib/x-post-automation/accounts.sqlite3')

def editable(account_id,actor,account_db=None):
    with closing(sqlite3.connect(Path(account_db or ACCOUNT_DB).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT owner_tenant_key,owner_user_id,deleted_at FROM x_authorized_account WHERE id=?',(int(account_id),)).fetchone()
    return bool(row and not row[2] and (actor.get('role')=='admin' or (str(row[0]),str(row[1]))==(str(actor.get('tenant_key')),str(actor.get('user_id')))))

def details(account_id,actor):
    return dict(channel_details(account_id),can_edit=editable(account_id,actor))

def update(account_id,payload,actor):
    from features.x_tag_publish.transport import Error
    if not editable(account_id,actor):
        raise Error('not_found','频道不存在或无权修改',404)
    if set(payload)!={'mode','expected_version'}:
        raise Error('invalid_request','请选择画像类型并提供当前版本',400)
    try:
        result=set_mode(account_id,payload['mode'],payload['expected_version'],str(actor['tenant_key'])+':'+str(actor['user_id']))
    except ValueError as exc:
        conflict=str(exc)=='profile_mode_conflict'
        raise Error(str(exc),'频道画像设置已变化，请刷新后重试' if conflict else '画像类型无效',409 if conflict else 400) from None
    return dict(ok=True,selection_profile=dict(result,can_edit=True))
