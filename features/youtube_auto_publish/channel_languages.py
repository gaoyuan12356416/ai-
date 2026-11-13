"""Saved channel language labels; display only, never a publishing gate."""
LANGUAGE_NAMES = {'en': '英语', 'es': '西班牙语', 'zh-tw': '繁体中文', 'zh-cn': '简体中文',
                  'zh': '中文', 'ja': '日语', 'ko': '韩语', 'th': '泰语', 'id': '印尼语',
                  'pt': '葡萄牙语', 'fr': '法语', 'de': '德语', 'ru': '俄语', 'ar': '阿拉伯语',
                  'tr': '土耳其语', 'hi': '印地语', 'vi': '越南语', 'it': '意大利语', 'ms': '马来语'}


def decorate(rows, templates):
    for row in rows:
        code = templates.get(str(row.get('channel_id') or ''), {}).get('language', '')
        row['language'] = code
        row['language_label'] = LANGUAGE_NAMES.get(code, code) or '未设置语种'
    return rows
