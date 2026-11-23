"""People-first cards following the 11:00 social report's visual structure."""
from decimal import Decimal, ROUND_HALF_UP
import html
import json

from .collector import CHANNELS

CHAT_ID = 'oc_57b890fed0b872cf15ef3f5b6899f53e'
LIMIT = 29000


def money(value):
    return '$' + format(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), ',.2f')


def safe(value):
    return str(value).replace('\n', ' ').replace('\r', ' ').translate(str.maketrans('<>*[]', '＜＞＊［］'))[:100]


def md(value):
    return {'tag': 'div', 'text': {'tag': 'lark_md', 'content': value}}


def income(row, available):
    if not available:
        return '待同步'
    return money(row['total_revenue_usd'])


def line(row, available):
    if not available:
        return '充值／退款／广告收入及点击：待同步'
    return '充值 %s · 退款 %s · 广告 %s\n充值订单 %s · 退款订单 %s · 点击 %s' % (
        money(row['order_recharge_usd']), money(row['order_refund_usd']), money(row['advertising_revenue_usd']),
        format(row['order_recharge_count'], ','), format(row['order_refund_count'], ','), format(row['clicks'], ','))


def elements(report):
    quality, totals = report['quality'], report['totals']
    available = quality['financial_available']
    partial = '' if quality['complete'] else '（部分数据待核对，下列为已知部分）'
    result = [md('**%s · Mobo 社媒分销收入**\n按人分组，展示全部历史推广内容的当日收益。%s\n数据读取：%s' % (
        report['report_date'], partial, report['generated_at'][:19].replace('T', ' ')))]
    result.append({'tag': 'div', 'fields': [
        {'is_short': True, 'text': {'tag': 'lark_md', 'content': '💰 总收入\n**%s**' % income(totals, available)}},
        {'is_short': True, 'text': {'tag': 'lark_md', 'content': '🎬 后台确认发布\n**%s 条 Post**' % format(totals['published_count'], ',')}}]})
    result.append(md(line(totals, available) + '\n%s 位发布人' % len(report['rows'])))
    for index, person in enumerate(report['rows'], 1):
        result.append({'tag': 'hr'})
        text = '**%02d · %s**\n收入 **%s** ｜ 确认发布 **%s 条 Post**\n%s' % (
            index, safe(person['name']), income(person, available), format(person['published_count'], ','), line(person, available))
        for platform, row in sorted(person['platforms'].items()):
            text += '\n\n**%s**　%s · Post %s\n%s' % (
                CHANNELS.get(platform, safe(platform)), income(row, available), format(row['published_count'], ','), line(row, available))
        result.append(md(text))
    result.append({'tag': 'hr'})
    result.append(md('**渠道汇总**\n' + '\n'.join('%s　**%s** · Post %s' % (
        CHANNELS.get(p, safe(p)), income(row, available), format(row['published_count'], ','))
        for p, row in sorted(report['platforms'].items()))))
    for warning in report['warnings']:
        result.append(md('⚠ ' + safe(warning)))
    result.append({'tag': 'note', 'elements': [{'tag': 'plain_text', 'content':
        '金额单位 USD。收入＝充值－退款＋广告收入，沿用 Mobo 收益报表口径，并非另行计算的分佣。'
        '\n收入按供应方来源日期（供应方未注明时区）；Post 按 UTC 自然日，与 11 点日报一致；点击按北京时间自然日。'
        '\nPost 仅计后台确认成功的 Mobo 发布回执；短链创建、排队、失败及结果未知不计成功。外部手动发布量暂不可核实。'
        '\n多渠道共用／渠道待归属收入单列，不重复分摊；点击已排除识别到的预览与机器人访问。收益可能随后续回传更新。'}]})
    result.append({'tag': 'action', 'actions': [{'tag': 'button', 'type': 'primary',
        'text': {'tag': 'plain_text', 'content': '打开 Mobo 收益报表'},
        'url': 'https://ai.yingliangads.com/moboreels-report.html'}]})
    return result


def card(report, content, index, count):
    suffix = '（%s/%s）' % (index, count) if count > 1 else ''
    return dict(config={'wide_screen_mode': True, 'enable_forward': True},
        header={'template': 'blue' if report['quality']['complete'] else 'orange',
                'title': {'tag': 'plain_text', 'content': 'Mobo 社媒分销收入日报 · ' + report['report_date'] + suffix}},
        elements=content)


def size(value):
    return len(json.dumps(dict(receive_id=CHAT_ID, msg_type='interactive',
        content=json.dumps(value, ensure_ascii=False, separators=(',', ':')), uuid='0' * 36),
        ensure_ascii=False).encode())


def build_cards(report):
    groups, group = [], []
    for element in elements(report):
        if size(card(report, group + [element], 9999, 9999)) > LIMIT:
            if not group:
                raise ValueError('card_element_too_large')
            groups.append(group)
            group = []
        group.append(element)
        if size(card(report, group, 9999, 9999)) > LIMIT:
            raise ValueError('card_element_too_large')
    if group:
        groups.append(group)
    result = [card(report, group, i, len(groups)) for i, group in enumerate(groups, 1)]
    if any(size(c) > LIMIT for c in result):
        raise ValueError('card_too_large')
    return result


def preview_html(cards):
    content = []
    for c in cards:
        content.append('<h1>' + html.escape(c['header']['title']['content']) + '</h1>')
        for e in c['elements']:
            texts = [e['text']['content']] if 'text' in e else [r['text']['content'] for r in e.get('fields', [])]
            texts += [r['content'] for r in e.get('elements', []) if 'content' in r]
            content.extend('<pre>' + html.escape(t) + '</pre>' for t in texts)
    return '<!doctype html><html lang="zh"><meta charset="utf-8"><title>Mobo 日报样例</title><style>body{max-width:850px;margin:32px auto;background:#f1f5fa;font:16px system-ui;color:#172033}pre{white-space:pre-wrap;background:white;padding:18px;border-radius:10px;line-height:1.7}h1{font-size:24px}</style>' + ''.join(content) + '</html>'
