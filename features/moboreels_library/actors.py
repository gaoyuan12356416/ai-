"""Resolve only a frozen creator ID against the backend's authoritative directory."""
from pathlib import Path
import sqlite3

from .promotions import PromotionError, identity


def resolve_actor(settings, user_id, tenant_key=''):
    user_id, tenant_key = str(user_id or '').strip(), str(tenant_key or '').strip()
    if not user_id or len(user_id)>128 or len(tenant_key)>128:
        raise PromotionError('任务创建者身份不完整', 'invalid_actor', 409)
    path = Path(settings.job_db_path).resolve()
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
    try:
        rows = conn.execute('SELECT tenant_key,user_id,name,status FROM drama_admin_user WHERE user_id=? LIMIT 2', (user_id,)).fetchall()
    finally:
        conn.close()
    if len(rows)!=1 or rows[0][3]!='active' or (tenant_key and rows[0][0]!=tenant_key):
        raise PromotionError('任务创建者无法唯一对应有效后台用户，请核对原创建者', 'invalid_actor', 409)
    actor = dict(zip(('tenant_key','user_id','name'), rows[0][:3]))
    identity(settings.institution_user_id, actor)
    return actor
