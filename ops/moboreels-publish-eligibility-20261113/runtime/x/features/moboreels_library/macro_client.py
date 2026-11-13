"""Server-only Moboreels macro adapter; preview rendering must not call resolve."""
import json
import os
import urllib.error
import urllib.request

from .settings import read_env_file


class MoboreelsLinkError(RuntimeError):
    pass


class MoboreelsEligibilityError(MoboreelsLinkError):
    code = 'moboreels_drama_not_publishable'
    status = 409


def contains_macro(*texts):
    return any('{{m_url}}' in str(value or '') for value in texts)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(path, body):
    try:
        config = read_env_file(os.environ.get('MOBOREELS_MACRO_ENV_FILE', '/etc/moboreels-macro.env'))
    except OSError:
        raise MoboreelsLinkError('Moboreels 宏服务配置暂不可读取') from None
    token = os.environ.get('MOBOREELS_MACRO_TOKEN', config.get('MOBOREELS_MACRO_TOKEN', ''))
    if not token:
        raise MoboreelsLinkError('Moboreels 推广服务尚未配置')
    request = urllib.request.Request('http://127.0.0.1:8864'+path,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST',
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=150) as response:
            raw = response.read(1024*1024+1)
            if len(raw) > 1024*1024:
                raise ValueError()
            result = json.loads(raw.decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read(65536).decode('utf-8'))
            message = result.get('message') or 'Moboreels 推广链接暂不可用'
        except (ValueError, UnicodeError):
            message = 'Moboreels 推广链接暂不可用'
        finally:
            exc.close()
        raise MoboreelsLinkError(str(message)) from None
    except (OSError, ValueError, UnicodeError):
        raise MoboreelsLinkError('Moboreels 推广结果暂不可读取，请稍后核对；不会重复创建未知结果') from None
    return result


def lookup_story(story_id):
    story = str(story_id or '').strip()
    if not story or len(story) > 64 or any(ord(c) < 32 for c in story):
        raise MoboreelsEligibilityError('素材缺少有效的 Moboreels 剧 ID，已拦截发布')
    try:
        row = _request('/internal/story-metadata', {'story_id': story}).get('drama') or {}
    except MoboreelsLinkError as exc:
        raise MoboreelsEligibilityError('无法核实 Moboreels 剧目 %s 的当前上架状态，已拦截发布：%s' % (story, exc)) from None
    if str(row.get('story_id') or '') != story:
        raise MoboreelsEligibilityError('Moboreels 剧库未匹配到素材剧目 %s，已拦截发布' % story)
    if str(row.get('is_show')) != '1':
        raise MoboreelsEligibilityError('Moboreels 剧目 %s 当前未上架，已拦截发布，请选择已上架可投放的剧目' % story)
    return dict(row, name=row.get('story_name',''), description=row.get('introduce',''),
                language=row.get('language_name',''))


def assert_publishable(story_id):
    """Read the current institution catalog; never trust a frozen link or UI flag."""
    return lookup_story(story_id)


def resolve_actor(user_id, tenant_key='', name=''):
    # Names come from the backend directory; caller-provided names cannot alter ownership.
    return _request('/internal/actor', {'user_id': str(user_id or ''), 'tenant_key': str(tenant_key or '')})['actor']


def resolve_m_url(actor, *, story_id, platform, account_id, task_id, material_id, purpose='post'):
    assert_publishable(story_id)
    result = _request('/internal/short-links', {
        'actor': {key: str((actor or {}).get(key) or '') for key in ('tenant_key', 'user_id', 'name')},
        'story_id': str(story_id), 'platform': str(platform), 'account_id': str(account_id),
        'task_id': str(task_id), 'material_id': str(material_id), 'purpose': str(purpose)})
    if result.get('status') != 'succeeded' or not result.get('short_url'):
        raise MoboreelsLinkError(result.get('message') or 'Moboreels 推广链接尚未完成，请稍后核对')
    return result
