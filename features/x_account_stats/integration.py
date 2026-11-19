"""Scoped HTML edits so unrelated account-list changes survive deployment."""
import re


def patch_html(html, asset_version='20261119-unified'):
    html = html.replace(
        '下表累计 / 昨日 Post、Repost 和收入为定时统计，每天北京时间 10:00、11:00、12:00 更新；刷新列表读取最近一次结果。',
        '运营统计与画像每小时同步更新；收入按 UTC 日期统计至前一个完整日，Post / Repost 昨日计数按北京时间。刷新列表读取同一批完整结果。')
    html = html.replace(
        '["累计收入", formatUsd(stats.revenue_total_usd)], [`${yesterday} 收入`, formatUsd(stats.revenue_yesterday_usd)],',
        '["累计收入", formatUsd(stats.revenue_total_usd)], [`${state.statsMeta?.revenue_yesterday_date || state.statsMeta?.yesterday_date || "昨日"} 收入（UTC）`, formatUsd(stats.revenue_yesterday_usd)],')
    html = html.replace(
        'detail.textContent = `${status} · 昨日统计日期：${meta.yesterday_date || "—"}`;',
        'detail.textContent = `${meta.refresh_failed ? "本轮更新失败，保留上次完整结果" : status} · 收入范围：${meta.start || "—"} 至 ${meta.end || "—"}（UTC）· 昨日发布计数：${meta.yesterday_date || "—"}（北京时间）`;')
    html = html.replace('window.XTagRevenue?.paint();\n          window.XTagRevenue?.refresh();',
                        'window.XTagRevenue?.refresh(state.statsMeta?.version || "");')
    html = re.sub(r'(/x-tag-revenue\.js)(?:\?[^"\s]*)?', r'\1?v=' + asset_version, html)
    required = ('收入（UTC）', 'refresh(state.statsMeta?.version', '运营统计与画像每小时同步更新', 'meta.refresh_failed')
    if not all(value in html for value in required):
        raise ValueError('account_list_integration_anchor_changed')
    return html
