"""Compact Feishu presentation only; never changes frozen reporting facts."""
from __future__ import annotations

import html
import re
from collections import Counter

from .common import BEIJING, Window, parse_time
from .report import total


def number(value):
    return '未知' if value is None else format(value, ',')


def chinese(text):
    text = str(text or '')
    for old, new in [('X Auto', '自动'), ('TikTok', '平台'), ('YouTube', '视频来源'),
                     ('Premium', '会员'), ('Repost', '转发'), ('Meta', '平台'),
                     ('Page', '主页'), ('Post', '帖子'), ('API', '接口'), ('COS', '云存储'),
                     ('GPU', '制作服务'), ('AI 后台', '后台'), ('AI后台', '后台')]:
        text = text.replace(old, new)
    return re.sub(r'[A-Za-z_][A-Za-z_0-9./:-]*',
                  lambda m: m[0] if m[0] in ('FB', 'TT', 'X') else '相关记录', text)


def literal(text):
    return html.escape(chinese(text), quote=False)


def md(text):
    return {'tag': 'markdown', 'content': text}


def note(text):
    return {'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': text}]}


def columns(values, header=False):
    result = {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
        {'tag': 'column', 'width': 'weighted', 'weight': 2 if len(values) == 5 and i == 0 else 1,
         'elements': [md(str(value))]} for i, value in enumerate(values)]}
    if header:
        result['background_style'] = 'grey'
    return result


def panel(title, elements):
    return {'tag': 'collapsible_panel', 'expanded': False,
            'header': {'title': {'tag': 'plain_text', 'content': title},
                       'icon': {'tag': 'standard_icon', 'token': 'down-small-ccm_outlined', 'size': '16px 16px'},
                       'icon_position': 'right', 'icon_expanded_angle': -180},
            'border': {'color': 'grey', 'corner_radius': '6px'}, 'elements': elements}


def source_name(channel, row):
    source = row.get('source', '')
    if (channel, source) in SOURCE_NAMES:
        return SOURCE_NAMES[channel, source]
    names = {'manual_template': '手动触发模板', 'video:pool': '主页素材池',
             'video:group_pool': '分组素材池', 'material_pool': '素材池定时发布',
             'drama_pool': '短剧池定时发布', 'auto_template': '自动模板',
             'manual_immediate': '手动立即发布', 'manual_scheduled': '手动定时发布',
             'legacy_daily': '历史每日排期', 'catchup': '人工补充发布',
             'manual_material_schedule': '人工素材每日排期', 'manual_material_once': '人工素材单次发布'}
    base = source[8:] if source.startswith('profile_') else source
    if base in names:
        return ('画像' if source.startswith('profile_') else '') + names[base]
    label = row.get('label')
    return chinese(label) if label else '其他来源'


def reason_counts(items):
    result = Counter()
    for item in items:
        if not item.get('count'):
            continue
        code = item.get('code', '')
        reason = REASON_NAMES.get(code)
        if reason is None:
            reason = chinese(item.get('reason', ''))
            if not reason or '原错误码' in reason:
                reason = '暂未记录具体原因'
            if item.get('confidence') == 'unknown' and '待核实' not in reason:
                reason += '（待核实）'
        if item.get('confidence') == 'inferred':
            reason += '（推测）'
        result[reason[:100]] += item['count']
    return result


def summary(channel):
    rows = channel.get('rows', [])
    sums = total(rows)
    available = not channel.get('error') and all(r.get('data_available', True) for r in rows)
    success = sums['published'] + sums['late'] if available else None
    return {'channel': channel['channel'], 'expected': sums['expected'] if available else None,
            'expected_lower_bound': sums['expected_lower_bound'] if available else None,
            'unfinished_lower_bound': sums['known_missing'] if available else None,
            'success': success, 'unfinished': sums['missing'] if available else None,
            'skipped': sum(r.get('policy_skipped', 0) for r in rows) if available else None,
            'late': sums['late'] if available else None,
            'prior': channel.get('prior_completed', 0) if available else None,
            'available': available}


def sum_known(items, key):
    values = [item[key] for item in items]
    return sum(values) if all(value is not None for value in values) else None


def metric(item, key):
    value = item[key]
    lower = item.get(key + '_lower_bound')
    return '≥' + number(lower) if value is None and lower is not None else number(value)


def combined_metric(items, key):
    value = sum_known(items, key)
    lower = [item[key] if item[key] is not None else item.get(key + '_lower_bound') for item in items]
    return metric({key: value, key + '_lower_bound': sum(lower) if all(v is not None for v in lower) else None}, key)


def cost_text(cost):
    amount = cost.get('display_usd')
    title = '**X 发布费用估算 · %s 美元**' % amount if amount is not None else (
        '**X 发布费用估算 · 未知**\n已核实部分估算 %s 美元。' % cost.get('known_estimated_usd', '0'))
    counts = cost.get('counts', {})
    text = title + '\n含链接原帖 %s · 无链接原帖 %s · 转发 %s。' % tuple(
        number(counts.get(k, 0)) for k in ('post_with_url', 'post_without_url', 'repost'))
    text += '\n按实际发布日及 %s 费率快照估算，含往期计划当天完成；次日补发计入次日。转发暂按互动创建单价估算。' % cost.get('rate_snapshot_date', '原日报')
    text += '\n不含读取、上传等其他调用、失败或结果不明请求、会员、服务器及税费；实际扣费以平台账单为准。'
    if cost.get('unpriced_posts') or cost.get('unconfirmed_attempts'):
        text += '\n缺少正文 %s 条 · 结果不明请求 %s 条，待账单核对。' % (
            number(cost.get('unpriced_posts', 0)), number(cost.get('unconfirmed_attempts', 0)))
    return text


def backlog_elements(backlog):
    if not backlog:
        return []
    stamp = parse_time(backlog.get('as_of'))
    label = stamp.astimezone(BEIJING).strftime('%m-%d %H:%M') if stamp else '未知'
    elements = [md('**历史积压 · 核对时刻 %s**\n与统计日计划分开查看，不可相加。' % label)]
    if not backlog.get('available'):
        return elements + [md('历史积压数量未知：记录读取不完整。')]
    for row in backlog.get('channels', []):
        states = row.get('states', {})
        elements.append(md('**%s** · 未完成 %s（已有任务 %s，未入队计划 %s）\n待执行 %s · 执行中 %s · 受阻 %s · 失败 %s · 待确认 %s · 历史暂停 %s\n另有未来预约 %s · 已关闭 %s · 已取消未入队计划 %s' % (
            row['channel'].upper(), number(row.get('unfinished')), number(row.get('instantiated_unfinished')),
            number(row.get('unqueued')), *(number(states.get(k, 0)) for k in ('waiting', 'executing', 'blocked', 'failed', 'unknown', 'paused')),
            number(row.get('scheduled')), number(row.get('closed')), number(row.get('closed_unqueued')))))
        reasons = reason_counts(row.get('reasons', []) + row.get('plan_reasons', []))
        if reasons:
            elements.append(md('；'.join('%s %s 条' % (literal(reason), number(count)) for reason, count in reasons.most_common(3))))
    warnings = list(dict.fromkeys(backlog.get('warnings', [])))
    if warnings:
        elements.append(md('**历史口径说明**\n' + '\n'.join(literal(w) for w in warnings[:4])))
    return elements


def build_card(report, *, compact=False):
    summaries = [summary(c) for c in report['channels']]
    incomplete = any(s['expected'] is None or not s['available'] for s in summaries)
    cutoff = parse_time(report.get('cutoff_utc')) or Window.for_date(report['date']).cutoff
    cutoff_label = cutoff.astimezone(BEIJING).strftime('%m.%d %H:%M')
    elements = [columns([
        '<font color="grey">%s</font>\n**%s**' % (label, combined_metric(summaries, key))
        if key == 'expected' else '<font color="grey">%s</font>\n<font color="%s">**%s**</font>' % (label, color, combined_metric(summaries, key))
        for label, key, color in [('应发', 'expected', 'grey'), ('确认成功', 'success', 'green'), ('未完成', 'unfinished', 'orange')]])]
    if incomplete:
        elements.append(md('⚠️ **数据不完整：≥ 表示已核实的数量下限，另有计划待核实；读取失败仍显示未知，不计为零。**'))
    elements += [{'tag': 'hr'}, columns(['**平台**', '**应发**', '**成功**', '**未完成**', '**按规则跳过**'], True)]
    for s in summaries:
        elements.append(columns(['**%s**' % s['channel'], metric(s, 'expected'),
                                 '<font color="green">**%s**</font>' % number(s['success']),
                                 '<font color="orange">%s</font>' % metric(s, 'unfinished'), number(s['skipped'])]))
    elements += [note('成功含次日补发 %s 条；另完成往期计划 %s 条。' % (
        number(sum_known(summaries, 'late')), number(sum_known(summaries, 'prior')))), {'tag': 'hr'}]
    sources, details, explanations, main_reasons = [], [], [], {}
    for channel, sums in zip(report['channels'], summaries):
        name, rows = channel['channel'], channel.get('rows', [])
        combined = Counter()
        source_lines = ['**%s**' % name]
        for row in rows:
            active = (row.get('expected') is None or not row.get('data_available', True)
                      or any(row.get(k, 0) for k in ('expected', 'published', 'late', 'pending', 'unknown', 'failed', 'blocked', 'policy_skipped'))
                      or row.get('warnings'))
            if not active:
                continue
            source = literal(source_name(name, row))
            one = summary({'channel': name, 'rows': [row]})
            result = ['成功 **%s** / 应发 %s' % (number(one['success']), metric(one, 'expected'))]
            if one['unfinished'] != 0:
                result.append('未完成 %s' % metric(one, 'unfinished'))
            if one['skipped'] != 0:
                result.append('按规则跳过 %s' % number(one['skipped']))
            source_lines.append(source + '：' + ' · '.join(result))
            counts = reason_counts(row.get('reasons', []) + row.get('policy_reasons', []))
            combined.update(counts)
            if counts:
                limit = 5 if compact else 20
                lines = ['**%s · %s**' % (name, source)]
                lines += ['• %s **%s** 条' % (literal(reason), number(count)) for reason, count in counts.most_common(limit)]
                if len(counts) > limit:
                    lines.append('另有 %s 项原因，详见原始日报归档。' % (len(counts) - limit))
                details.append(md('\n'.join(lines)))
        if channel.get('error'):
            source_lines.append('数据读取失败，数量未知。')
        elif len(source_lines) == 1:
            source_lines.append('当日无发布计划。')
        sources.append(md('\n'.join(source_lines)))
        if combined:
            main_reasons[name] = '；'.join('%s **%s** 条' % (literal(reason), number(count)) for reason, count in combined.most_common(3))
        else:
            main_reasons[name] = '数据不完整，原因暂无法确认' if not sums['available'] else (
                '暂无已记录的具体原因' if sums['unfinished'] is None or sums['unfinished'] > 0 else '无未完成任务')
        if sums['available']:
            statuses = ' · '.join('%s %s' % (label, number(sum(r.get(key, 0) for r in rows))) for key, label in
                                  [('failed', '失败'), ('blocked', '受阻'), ('pending', '处理中'), ('unknown', '待确认')])
            explanations.append(md('**%s · 实际应发 %s · 确认成功 %s**\n%s' % (name, metric(sums, 'expected'), number(sums['success']), statuses)))
        if channel.get('accounting') == 'actual-fb-v2':
            explanations.append(md('FB 按冻结的主页频次与发布时段计算；已排除频次外 %s 个位置、执行前已失效的旧计划 %s 个时隙。' % (
                number(sum(r.get('frequency_excluded', 0) for r in rows)), number(sum(r.get('superseded_slots', 0) for r in rows)))))
        warnings = list(dict.fromkeys(channel.get('warnings', []) + [w for r in rows for w in r.get('warnings', [])]))
        if warnings:
            explanations.append(md('**%s 口径说明**\n' % name + '\n'.join(literal(w)[:220] for w in warnings[:4]) + (
                '\n另有 %s 条说明，详见原始日报归档。' % (len(warnings) - 4) if len(warnings) > 4 else '')))
        if channel.get('cost'):
            explanations.append(md(cost_text(channel['cost'])))
    order = [name for name in ('FB', 'TT', 'X') if name in main_reasons]
    order += [name for name in main_reasons if name not in order]
    elements += [md('**主要未发布原因**\n' + '\n'.join('**%s** · %s' % (name, main_reasons[name]) for name in order)),
                 note('以上含按规则跳过的任务；数字为任务条数。其余原因可展开查看。'),
                 panel('发布来源 · 看每种来源的完成情况', sources)]
    if details:
        elements.append(panel('全部具体原因 · 按平台和来源查看', details))
    explanations.insert(0, md('**统计口径**\n后台全部自动、手动帖子；统计日为 %s，补发截至 %s（北京时间）。\n未完成包含失败、受阻、处理中和结果待确认；结果待确认不计成功或失败。按规则跳过单列。往期计划当天完成单列；全零来源隐藏。' % (report['date'], cutoff_label)))
    explanations.extend(backlog_elements(report.get('backlog')))
    elements.append(panel('费用与统计说明', explanations))
    return {'config': {'wide_screen_mode': True},
            'header': {'template': 'orange' if incomplete else 'blue',
                       'title': {'tag': 'plain_text', 'content': '发布日报  |  ' + report['date'][5:].replace('-', '.')},
                       'subtitle': {'tag': 'plain_text', 'content': '统计至 %s · 北京时间' % cutoff_label}}, 'elements': elements}


def preview_html(report, card):
    def text(value):
        escaped = html.escape(value).replace('\n', '<br>')
        escaped = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', escaped)
        escaped = re.sub(r'&lt;font color=&quot;(grey|green|orange)&quot;&gt;', r'<span class="\1">', escaped)
        return escaped.replace('&lt;/font&gt;', '</span>')

    def render(element):
        tag = element['tag']
        if tag == 'hr':
            return '<hr>'
        if tag in ('markdown', 'div'):
            return '<p>' + text(element.get('text', element)['content']) + '</p>'
        if tag == 'note':
            return '<small>' + html.escape(element['elements'][0]['content']) + '</small>'
        if tag == 'column_set':
            weights = ' '.join(str(c.get('weight', 1)) + 'fr' for c in element['columns'])
            return '<div class="row %s" style="grid-template-columns:%s">%s</div>' % (
                'head' if element.get('background_style') == 'grey' else '', weights,
                ''.join('<div>' + ''.join(render(e) for e in c['elements']) + '</div>' for c in element['columns']))
        if tag == 'collapsible_panel':
            return '<details><summary>%s</summary>%s</details>' % (
                html.escape(element['header']['title']['content']), ''.join(render(e) for e in element['elements']))
        return ''
    return ('<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>发布日报预览</title><style>body{margin:24px auto;max-width:740px;padding:0 16px;font:15px/1.6 system-ui;color:#20252e;background:#f5f6f8}'
            'article{background:white;border:1px solid #e0e4eb;border-radius:10px;overflow:hidden}header{background:#eaf0ff;padding:16px 22px;color:#1456f0}'
            'h1{font-size:22px;margin:0}main{padding:18px 22px}p{margin:7px 0}.row{display:grid;gap:12px;padding:7px 4px}.head{background:#f5f6f8;border-radius:6px}'
            'small,.grey{color:#747b89}small{display:block;font-size:12px}hr{border:0;border-top:1px solid #e4e6eb;margin:16px 0}.green{color:#168327}.orange{color:#e87b00}'
            'details{border:1px solid #dde1e8;border-radius:6px;margin-top:10px;padding:8px 12px}summary{cursor:pointer;color:#626b79}'
            '</style><article><header><h1>' + html.escape(card['header']['title']['content']) + '</h1>'
            + html.escape(card['header'].get('subtitle', {}).get('content', '')) + '</header><main>'
            + ''.join(render(e) for e in card['elements']) + '</main></article></html>')

SOURCE_NAMES = {('TT', 'auto_template:random_overlay'): '随机叠加模板',
 ('TT', 'auto_template:direct_outro'): '引导片尾模板',
 ('FB', 'auto_template'): '自动模板',
 ('FB', 'video:manual'): '手动视频',
 ('X', 'manual_immediate'): '手动立即发布',
 ('X', 'manual_scheduled'): '手动定时发布',
 ('X', 'profile_material_pool'): '画像素材池定时发布',
 ('X', 'profile_auto_template'): '画像自动模板'}

REASON_NAMES = {'canonical_execution_failed': '执行失败，具体原因未记录',
 'source_url_not_allowed': '源视频地址未通过校验',
 'tt_upstream_rejected': '平台拒绝发布请求',
 'fb_page_missing_eligible_token': '主页发布授权不可用',
 'fb_auto_page_language_missing': '主页未设置语言',
 'fb_auto_no_video_after_drama_cooldown': '剧目冷却期内无可用视频',
 'fb_auto_no_eligible_video': '没有符合筛选条件的视频',
 'fb_graph_response_outcome_unknown': '平台返回异常，发布结果待确认',
 'pipeline_execution_failed': '执行失败，具体原因未记录',
 'fb_graph_network_outcome_unknown': '连接中断，发布结果待确认',
 'pipeline_lease_lost': '任务执行权丢失',
 'fb_auto_no_video_after_material_cooldown': '素材冷却期内无可用视频',
 'cos_output_duration_invalid': '成片时长不符合要求',
 'fb_auto_worker_interrupted': '发布中断，结果待确认',
 'fb_gpu_cos_upload_failed': '成片上传失败',
 'artifact_head_failed': '成片文件检查失败',
 'fb_gpu_transcode_failed': '视频转码失败',
 'cos_provider_failed': '云端处理失败，具体原因未记录',
 'fb_auto_drama_cooldown_at_publish': '同主页同剧发布间隔未到，按规则跳过',
 'cutover_overdue': '历史任务已暂停',
 'x_upstream_error': '发布前检查失败，未进入发布队列',
 'youtube_source_download_failed': '源视频下载失败',
 'x_video_account_unavailable_cancelled': '账号不可用，任务已取消',
 'operator_stopped': '人工停止',
 'x_post_schedule_partial_capacity': '部分账号未进入发布队列，具体原因待核实',
 'x_post_account_locked': '账号被临时锁定',
 'operator_terminated': '人工终止',
 'x_account_suspended': '账号被平台停用',
 'x_account_disabled': '账号已在后台停用',
 'x_auto_no_eligible_material': '没有符合筛选条件的素材，按规则跳过',
 'publish_failed': '发布未完成，具体原因未记录',
 'x_account_verification_rejected': '账号验证未通过',
 'unknown_outcome': '发布结果待确认',
 'media_download_failed': '素材下载失败'}
