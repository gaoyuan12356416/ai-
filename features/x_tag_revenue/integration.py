"""Small reversible insertions preserve the independently deployed shared files."""
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
