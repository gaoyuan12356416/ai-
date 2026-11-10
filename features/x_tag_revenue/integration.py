"""Small reversible insertions preserve the independently deployed shared files."""
import re
ASSETS = '''  <link rel="stylesheet" href="/x-tag-revenue.css?v=20261109v1" />
  <script src="/x-tag-revenue.js?v=20261109v1" defer></script>
'''
CHANGES = [
    ('</head>', ASSETS + '</head>'),
    ('<th>运营统计</th>', '<th>运营统计</th><th class="xtr-col">历史标签收入</th>'),
    ('<td>${operatingMetricsHtml(item)}</td>', '<td>${operatingMetricsHtml(item)}</td>\n            <td class="xtr-col" data-x-tag-id="${escapeHtml(item.id)}">${window.XTagRevenue ? window.XTagRevenue.cell(item.id) : "正在读取画像…"}</td>'),
    ('renderStats(); renderOperatingSummary(); renderLanguageFilter(); renderRows();', 'renderStats(); renderOperatingSummary(); renderLanguageFilter(); renderRows();\n          window.XTagRevenue?.refresh();'),
]


def patch_html(text):
    if ASSETS in text:
        return text
    for before, after in CHANGES:
        if text.count(before) != 1:
            raise ValueError('channel_list_anchor_changed:' + before[:60])
        text = text.replace(before, after)
    return text.replace('colspan="9"', 'colspan="10"')


def unpatch_html(text):
    for before, after in reversed(CHANGES):
        if text.count(after) != 1:
            raise ValueError('channel_list_patch_changed')
        text = text.replace(after, before)
    return text.replace('colspan="10"', 'colspan="9"')


def patch_columns(text):
    """Upgrade the existing portrait column without regenerating shared HTML."""
    if 'data-x-tag-level="type"' in text:
        return text
    headers = '<th class="xtr-col">剧类型<small>占频道收入</small></th><th class="xtr-col">受众方向<small>主要类型内</small></th><th class="xtr-col xtr-col-theme">题材<small>主要类型与受众内</small></th>'
    text, count = re.subn(r'<th class="xtr-col">历史(?:三级画像|标签收入)</th>', headers, text)
    if count != 1:
        raise ValueError('portrait_header_anchor_changed')
    cells = '\n            '.join('<td class="xtr-col' + (' xtr-col-theme' if key == 'theme' else '') +
              '" data-x-tag-id="${escapeHtml(item.id)}" data-x-tag-level="' + key +
              '">${window.XTagRevenue ? window.XTagRevenue.cell(item.id, "' + key + '") : "读取中…"}</td>'
              for key in ('type', 'audience', 'theme'))
    text, count = re.subn(r'<td class="xtr-col" data-x-tag-id="\$\{escapeHtml\(item.id\)\}">.*?</td>', lambda _: cells, text)
    if count != 1:
        raise ValueError('portrait_cell_anchor_changed')
    return text.replace('colspan="10"', 'colspan="12"')
