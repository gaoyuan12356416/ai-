"""The approved UI's single-brace templates and existing YouTube URL contract."""
import re
from features.drama_synthesis.core import build_long_url


class WorkflowError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


DEFAULT_DESCRIPTION = 'Watch more exciting short dramas on DramaWave. Subscribe for more stories!'
TOKENS = re.compile(r'\{+[^{}\r\n]*\}+')
FIELDS = {'url': 'macro_url', 'desc': 'macro_desc', 'name': 'macro_name'}


def long_url(material):
    try:
        return build_long_url(str(material.get('source_job_id') or ''), str(material.get('content_id') or ''))
    except Exception:
        return ''


def render(template, material, field, *, allow_unresolved=False):
    if not isinstance(template, str):
        raise WorkflowError('invalid_template', '发布文案必须为文本')
    if len(template.encode('utf-8')) > 16000:
        raise WorkflowError('template_too_long', '文案模板过长')
    def replace(match):
        token = match.group(0)
        key = token[1:-1]
        if key not in FIELDS:
            raise WorkflowError('unknown_macro', '仅支持 {url}、{desc}、{name} 宏参数')
        if allow_unresolved:
            return token
        value = str(material.get(FIELDS[key]) or '').strip()
        if not value or (key == 'url' and not long_url(material)):
            raise WorkflowError('macro_source_missing', '素材缺少 %s 的值或有效剧集/来源合成任务关联' % token, 409)
        return value
    value = TOKENS.sub(replace, template).strip()
    if field != 'comment' and not value:
        raise WorkflowError('required_text', '请填写视频标题和描述')
    limit = {'title': 100, 'description': 5000, 'comment': 1000}[field]
    size = len(value.encode('utf-8')) if field == 'description' else len(value)
    if size > limit:
        raise WorkflowError('text_too_long', '替换后的%s超过 %d %s' % ({'title':'标题','description':'描述','comment':'首评'}[field], limit, '字节' if field == 'description' else '字符'))
    return value
