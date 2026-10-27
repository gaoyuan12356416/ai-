"""Bounded operator summaries from final unique ledger objects, never events."""
from collections import Counter
from datetime import datetime
import json
import math

from .runtime import CST, now


CATEGORIES = (
    ('verified', '已确认删除'), ('accepted', '已接受待核验'),
    ('receipt', '历史已删回执'), ('failed', '平台失败'), ('policy_skip', '按策略跳过'),
    ('skipped', '未提交跳过'), ('unknown', '未知'), ('pending', '待处理'),
)
FAILURES = {
    (200, 1487235): ('共享引用阻塞', '先清理当前合格引用，再重试Creative'),
    (100, 3858800): ('共享引用阻塞', '先清理当前合格引用，再重试Creative'),
}
POLICY_SKIPS = {
    'content_unavailable': '帖子失效/无权限',
    'account_not_writable': '账户不可写入',
}
POLICY_SKIP_ERRORS = {(100, 2446289): 'content_unavailable',
                      (200, 2490592): 'account_not_writable'}


def _date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (parsed.replace(tzinfo=CST) if parsed.tzinfo is None else parsed).astimezone(CST)
    except (TypeError, ValueError):
        return None


def _time(value):
    parsed = _date(value)
    return parsed.strftime('%Y-%m-%d %H:%M:%S') if parsed else '时间未记录'


def _elapsed(start, end):
    start, end = _date(start), _date(end)
    if start is None or end is None or end < start:
        return '未取得'
    seconds = int((end - start).total_seconds())
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return ('%s时' % hours if hours else '') + '%s分%s秒' % (minutes, seconds)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _exact_deleted(row, result):
    """A success label/flag alone is not exact object/account DELETED proof."""
    accounts = row.get('account_ids', [])
    if not isinstance(accounts, list) or len(accounts) != 1:
        return False
    verification = result.get('verification') or {}
    proofs = [result.get('object'), result.get('proof')]
    if isinstance(verification, dict):
        proofs.append(verification.get('object'))
    return any(isinstance(proof, dict) and proof.get('status') == 'DELETED'
               and str(proof.get('id')) == str(row['object_id'])
               and str(proof.get('account_id')) == str(accounts[0]) for proof in proofs)


def classify(row, accepted_writes=None):
    """Exactly one category per final object; do not traverse historical proofs."""
    result = row.get('result') or {}
    if not isinstance(result, dict):
        return 'unknown'
    status = row.get('status')
    # claim_object copies an earlier task's receipt, which may itself contain a
    # verified proof. It must never become a newly verified/current deletion.
    if status == 'already_deleted' and result.get('reason') == 'previous_task_deleted':
        return 'receipt'
    if status in ('deleted', 'already_deleted') and _exact_deleted(row, result):
        if accepted_writes is not None and (row['kind'], str(row['object_id'])) not in accepted_writes:
            return 'receipt'
        return 'verified'
    if status == 'already_deleted':
        return 'receipt'
    if result.get('api_accepted') is True or result.get('delete_status') == 'api_accepted':
        return 'accepted'
    if (status in ('unknown', 'in_progress') or result.get('status') == 'unknown'
            or result.get('reason') == 'unknown_fence'):
        return 'unknown'
    if status in ('failed', 'blocked') and _policy_skip_reason(result, row.get('kind'),
                                                            allow_rejection=status == 'failed'):
        return 'policy_skip'
    if status in ('pending', 'blocked'):
        return 'pending'
    if status == 'failed' and result.get('request_sent') is False:
        return 'skipped'
    if status == 'failed' and result.get('request_sent') is True:
        return 'failed'
    return 'unknown'


def _policy_skip_reason(result, kind, allow_rejection=True):
    """Only current, explicit policy markers or definite exact-code rejections."""
    reason = result.get('policy_skip_reason')
    if (isinstance(reason, str) and reason in POLICY_SKIPS
            and (reason != 'content_unavailable' or kind == 'ad')):
        return reason
    # Historical final rejections can be summarized under the current policy,
    # without changing their saved result, notice body or request_sent truth.
    response = result.get('response')
    if allow_rejection and result.get('request_sent') is True and isinstance(response, dict):
        code, subcode = response.get('code'), response.get('error_subcode')
        if type(code) is int and type(subcode) is int:
            reason = POLICY_SKIP_ERRORS.get((code, subcode))
            if reason != 'content_unavailable' or kind == 'ad':
                return reason
    return None


def _failure(result):
    response = result.get('response') or {}
    if not isinstance(response, dict):
        response = {}
    code, subcode = response.get('code'), response.get('error_subcode')
    if type(code) is int and type(subcode) is int and (code, subcode) in FAILURES:
        return FAILURES[(code, subcode)]
    if result.get('code') == 'rate_limited' or response.get('http_status') == 429:
        return ('平台限流', '等待限流恢复后再评估重试')
    return ('其他平台拒绝', '按批次核查平台错误与权限')


def summarize_objects(objects, accepted_writes=None):
    if not isinstance(objects, list):
        raise ValueError('missing_final_objects')
    counts = {'ad': Counter(), 'creative': Counter()}
    failures, seen = Counter(), set()
    for row in objects:
        if not isinstance(row, dict) or row.get('kind') not in counts or not row.get('object_id'):
            raise ValueError('invalid_final_object')
        key = (row['kind'], str(row['object_id']))
        if key in seen:
            raise ValueError('duplicate_final_object')
        seen.add(key)
        category = classify(row, accepted_writes)
        counts[row['kind']][category] += 1
        if category == 'failed':
            failures[_failure(row['result'])] += 1
    return counts, failures


def _accepted_writes(store, job_id, objects):
    """Original submitted, accepted-or-unknown writes from this job's attempts.

    GET reconciliation may rewrite attempts.result.  The append-only
    object_finished audit retains what the write actually returned.  Neither
    current object flags nor nested receipts can create new-write provenance.
    An unknown DELETE may later be confirmed by exact GET evidence; a definite
    rejection cannot. The caller still requires final object/account DELETED.
    """
    by_key = {row['kind'] + ':' + str(row['object_id']): row for row in objects}
    accepted = set()
    with store._transaction(False) as conn:
        attempts = {row['attempt_id']: (row['object_key'], row['run_id']) for row in conn.execute(
            'SELECT attempt_id,object_key,run_id FROM fb_asset_delete_v2_attempts WHERE job_id=?', (job_id,))}
        for audit in conn.execute("SELECT details FROM fb_asset_delete_v2_audit "
                                  "WHERE job_id=? AND action='object_finished'", (job_id,)):
            entry = json.loads(audit['details'])
            if not isinstance(entry, dict):
                raise ValueError('invalid_attempt_audit')
            key = entry.get('key')
            if (key not in by_key or attempts.get(entry.get('attempt_id')) != (key, entry.get('run_id'))):
                continue
            result = entry.get('result')
            if not isinstance(result, dict) or result.get('request_sent') is not True:
                continue
            original_status = result.get('delete_status')
            if (original_status in ('failed', 'blocked')
                    or result.get('status') in ('failed', 'blocked')
                    or entry.get('status') in ('failed', 'blocked')):
                continue
            if original_status is None:
                original_status = 'api_accepted' if result.get('api_accepted') is True else result.get('status')
            if original_status not in ('api_accepted', 'unknown'):
                continue
            row = by_key[key]
            accounts = row.get('account_ids')
            if (not isinstance(accounts, list) or len(accounts) != 1
                    or str(result.get('account_id')) != str(accounts[0])):
                continue
            accepted.add((row['kind'], str(row['object_id'])))
    return accepted


def origin_counts(manifest, objects=None):
    """Reconcile frozen source sets; deferred backlog never inflates admission."""
    candidates = manifest.get('candidates')
    recovery = manifest.get('recovery_creatives', [])
    if not isinstance(candidates, list) or not isinstance(recovery, list):
        raise ValueError('missing_origin_sets')
    products = {'1479': set(), '3302': set()}
    candidate_ads, candidate_creatives, historical = set(), set(), set()
    for row in candidates:
        if (not isinstance(row, dict) or row.get('root_id') not in products
                or not row.get('ad_id') or not row.get('creative_id')):
            raise ValueError('invalid_candidate_origin')
        aid, cid = str(row['ad_id']), str(row['creative_id'])
        if aid in candidate_ads:
            raise ValueError('duplicate_candidate_origin')
        candidate_ads.add(aid); candidate_creatives.add(cid)
        products[row['root_id']].add(aid)
    for entry in recovery:
        basis = entry.get('basis') if isinstance(entry, dict) else None
        if not isinstance(basis, dict) or not basis.get('creative_id'):
            raise ValueError('invalid_recovery_origin')
        cid = str(basis['creative_id'])
        if cid in historical:
            raise ValueError('duplicate_recovery_origin')
        historical.add(cid)
    assessment = manifest.get('recovery_assessment')
    total, deferred = len(historical), 0
    if assessment is not None:
        if not isinstance(assessment, dict):
            raise ValueError('invalid_recovery_assessment')
        eligible = assessment.get('eligible_ids')
        reasons = assessment.get('deferred_by_reason')
        total = _count(assessment.get('total'))
        if (total is None or not isinstance(eligible, list) or not isinstance(reasons, dict)
                or any(_count(value) is None for value in reasons.values())
                or len(eligible) != len(set(map(str, eligible))) or set(map(str, eligible)) != historical):
            raise ValueError('recovery_assessment_mismatch')
        deferred = sum(reasons.values())
        if total != len(historical) + deferred:
            raise ValueError('recovery_assessment_not_partitioned')
    result = {'new_ads': len(candidate_ads), 'new_creatives': len(candidate_creatives),
              'products': {root: len(ids) for root, ids in products.items()},
              'historical_total': total, 'historical_eligible': len(historical),
              'historical_deferred': deferred, 'historical_overlap': len(historical & candidate_creatives),
              'historical_net_new': len(historical - candidate_creatives)}
    if objects is not None:
        # Also validates duplicate/invalid final objects without counting events.
        summarize_objects(objects)
        final_ads = {str(row['object_id']) for row in objects if row['kind'] == 'ad'}
        final_creatives = {str(row['object_id']) for row in objects if row['kind'] == 'creative'}
        if not candidate_ads <= final_ads or candidate_creatives | historical != final_creatives:
            raise ValueError('final_origin_sets_mismatch')
        result.update(shared_ads=len(final_ads-candidate_ads), final_ads=len(final_ads),
                      final_creatives=len(final_creatives))
    return result


def _duration(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return '未取得'
    hours, seconds = divmod(int(seconds), 3600)
    minutes, seconds = divmod(seconds, 60)
    return ('%s时' % hours if hours else '') + '%s分%s秒' % (minutes, seconds)


def _v3_lines(runner, run_id, run, manifest):
    """New contract, with provenance-backed execution counts and source sets."""
    data = run.get('data', {})
    lines = ['规则：最近3天，同素材≥5个不同违规广告＋Ad/Ad Set/Campaign任一级暂停；活跃不删。']
    try:
        origin = origin_counts(manifest)
        lines.append('新候选：DramaWave %s；HotDrama %s；广告合计%s，关联唯一Creative %s。' % (
            origin['products']['1479'], origin['products']['3302'], origin['new_ads'], origin['new_creatives']))
        lines.append('历史待清理Creative：总数%s；本批合格%s；暂缓%s；与新候选重复%s；历史净新增%s。' % (
            origin['historical_total'], origin['historical_eligible'], origin['historical_deferred'],
            origin['historical_overlap'], origin['historical_net_new']))
    except Exception:
        lines.append('来源统计：冻结清单读取或校验失败，暂不可用。')
        return lines
    execution_started = (data.get('execution_mode') == 'run'
                         or (_count(data.get('execution_attempt')) or 0) > 0
                         or data.get('shared_job_id') or data.get('shared_run_id'))
    preview = (run.get('mode') == 'preview' or run.get('status') == 'previewed') and not execution_started
    preview_line = '预演：未执行删除；无最终执行统计。'
    if runner.store is None:
        return lines + [preview_line if preview else '最终对象统计：未取得执行账本，不能判断删除结果。']
    try:
        job_id = 'policy-cleanup-' + run_id
        job = runner.store.get_job(job_id)
        objects = job.get('objects')
        accepted = _accepted_writes(runner.store, job_id, objects)
        counts, failures = summarize_objects(objects, accepted)
        origin = origin_counts(manifest, objects)
    except Exception as exc:
        if getattr(exc, 'code', None) == 'not_found':
            return lines + [preview_line if preview else '最终对象统计：未建立执行账本，尚无执行结果。']
        return lines + ['最终对象统计：账本、删除凭据或来源校验失败，暂不可用。']
    lines.append('共享补删：新增纳入广告%s；本批纳入总数：广告%s、Creative %s。' % (
        origin['shared_ads'], origin['final_ads'], origin['final_creatives']))
    lines.append('本批实际确认删除：广告%s；Creative %s。' % (counts['ad']['verified'], counts['creative']['verified']))
    other = (('accepted', '已接受待核验'), ('receipt', '历史/非本批已删'), ('failed', '平台失败'),
             ('policy_skip', '策略跳过'), ('skipped', '未提交跳过'), ('unknown', '未知'), ('pending', '待处理'))
    for kind, label in (('ad', '广告'), ('creative', 'Creative')):
        lines.append(label + '其余：' + '；'.join('%s%s' % (title, counts[kind][key]) for key, title in other) + '。')
    policy_skips = Counter(_policy_skip_reason(row['result'], row['kind']) for row in objects
                           if classify(row, accepted) == 'policy_skip')
    if policy_skips:
        lines.append('策略跳过原因：' + '；'.join('%s%s' % (label, policy_skips[reason])
                     for reason, label in POLICY_SKIPS.items()) + '。')
    ranked = sorted(failures.items(), key=lambda item: (-item[1], item[0][0]))[:3]
    if ranked:
        lines.append('失败主因：' + '；'.join('%s%s个（%s）' % (label, count, action)
                     for (label, action), count in ranked) + '。')
    return lines


def start_text(run_id, start, end):
    return '\n'.join([
        'Meta违规素材清理｜开始｜北京时间 ' + _time(now()),
        '批次：' + run_id,
        'DramaWave、HotDrama（含W2A）｜窗口：%s 至 %s' % (_time(start), _time(end)),
    ])


def _summary_lines(runner, run_id, run):
    data = run.get('data', {})
    execution_started = (data.get('execution_mode') == 'run'
                         or (_count(data.get('execution_attempt')) or 0) > 0
                         or data.get('shared_job_id') or data.get('shared_run_id'))
    preview = (run.get('mode') == 'preview' or run.get('status') == 'previewed') and not execution_started
    preview_line = '预演：未执行删除；无最终执行统计。'
    if runner.store is None:
        return [preview_line if preview else '最终对象统计：未取得执行账本，不能判断删除结果。']
    try:
        job = runner.store.get_job('policy-cleanup-' + run_id)
        counts, failures = summarize_objects(job.get('objects'))
    except Exception as exc:
        if getattr(exc, 'code', None) == 'not_found':
            return [preview_line if preview else '最终对象统计：未建立执行账本，尚无执行结果。']
        return ['最终对象统计：账本读取或校验失败，暂不可用。']
    lines = []
    for kind, label in (('ad', '广告'), ('creative', 'Creative')):
        total = sum(counts[kind].values())
        lines.append('%s（最终唯一%s）：' % (label, total) + '；'.join(
            '%s%s' % (title, counts[kind][key]) for key, title in CATEGORIES))
    policy_skips = Counter(_policy_skip_reason(row['result'], row['kind']) for row in job['objects']
                           if classify(row) == 'policy_skip')
    if policy_skips:
        lines.append('策略跳过：' + '；'.join('%s%s' % (label, policy_skips[reason])
                     for reason, label in POLICY_SKIPS.items()) + '。')
    # Failure event retries are deliberately excluded: a later success removes
    # the object from this ranking, and multiple failures still count once.
    ranked = sorted(failures.items(), key=lambda item: (-item[1], item[0][0]))[:3]
    for index, ((label, action), count) in enumerate(ranked, 1):
        lines.append('失败主因%s：%s %s个；%s。' % (index, label, count, action))
    return lines


def _candidates(manifest, data):
    candidates = manifest.get('candidates')
    if isinstance(candidates, list):
        ids = {'1479': set(), '3302': set()}
        for row in candidates:
            root = row.get('root_id')
            if root in ids and row.get('ad_id'):
                ids[root].add(str(row['ad_id']))
            else:
                return '候选广告：明细不完整，未取得产品统计。'
        counts = {root: len(values) for root, values in ids.items()}
    else:
        counts = data.get('product_candidate_ads', {})
        if not isinstance(counts, dict) or any(_count(counts.get(root)) is None for root in ('1479', '3302')):
            return '候选广告：未取得产品统计。'
    return '候选广告（去重）：DramaWave %s；HotDrama %s。' % (counts['1479'], counts['3302'])


def end_text(runner, run_id):
    """Reporting must not turn a completed/fenced execution into an exception."""
    try:
        run = runner.runtime.get(run_id)
        data = run['data']
        try:
            manifest = runner.runtime.load_artifact(run_id, 'manifest.json')
            if not isinstance(manifest, dict):
                manifest = {}
        except Exception:
            manifest = {}
        labels = {'completed': '完成', 'partial': '部分完成', 'interrupted': '中断',
                  'failed': '失败', 'previewed': '预演完成', 'running': '执行中'}
        start = data.get('execution_started_at') or run.get('started')
        end = run.get('ended') or now()
        v3 = manifest.get('policy_version') == '20261027-v3'
        if v3:
            metrics = data.get('reference_scan_metrics')
            reference_seconds = metrics.get('wall_seconds') if isinstance(metrics, dict) else None
            lines = ['Meta违规素材清理｜%s｜批次：%s' % (labels.get(run.get('status'), '状态待核对'), run_id),
                     '北京时间：%s → %s｜总耗时：%s｜共享引用耗时：%s' % (
                         _time(start), _time(end), _elapsed(start, end), _duration(reference_seconds))]
        else:
            lines = ['Meta违规素材清理｜' + labels.get(run.get('status'), '状态待核对'),
                     '北京时间：%s → %s｜耗时：%s' % (_time(start), _time(end), _elapsed(start, end)),
                     '批次：' + run_id, _candidates(manifest, data)]
        if run.get('status') == 'failed' and data.get('error_type') == 'SqlSourceError':
            codes = data.get('mysql_error_codes')
            codes = sorted({v for v in codes if type(v) is int and 0 < v < 65536}) if isinstance(codes, list) else []
            label = '数据库读取失败'
            if 3024 in codes:
                label = '数据库查询超时'
            elif set(codes) & {2006, 2013}:
                label = '数据库连接中断'
            suffix = '（MySQL %s）' % ','.join(map(str, codes[:5])) if codes else ''
            lines.append('运行错误：' + label + suffix + '；脱敏原始错误见本批 mysql-errors.jsonl。')
        lines.extend(_v3_lines(runner, run_id, run, manifest) if v3 else _summary_lines(runner, run_id, run))
        notes = []
        coverage = _count(data.get('coverage_errors'))
        errors = manifest.get('coverage', {}).get('errors')
        if isinstance(errors, list):
            coverage = max(coverage or 0, len(errors))
        if coverage:
            notes.append('扫描覆盖异常%s项' % coverage)
        elif coverage is None:
            notes.append('扫描覆盖情况未取得')
        reference_coverage = manifest.get('shared_reference_coverage')
        if reference_coverage not in ('complete', 'checked_on_dependency_failure', 'checked_before_creative_delete', None):
            notes.append('共享引用覆盖未完成')
        # The saved deferred count predates later GET reconciliation. Final
        # pending/failed/unknown objects above are authoritative for this notice;
        # do not repeat the stale backlog snapshot as current outstanding work.
        impacts = manifest.get('shared_creative_impacts', {})
        if isinstance(impacts, dict) and impacts:
            notes.append('共享Creative %s个' % len(impacts))
        if notes:
            lines.append('关注：' + '；'.join(notes) + '。')
        if v3:
            lines.append('口径：按对象去重、结果互斥；实际删除须本批提交且回读确认，历史已删不计入。明细保留在本批清单和账本。')
        else:
            lines.append('口径：本批最终唯一对象（含共享补删），各类互斥；历史回执不算本批新删。')
            lines.append('完整明细保留：按批次查 manifest、deferred-cleanup 和共享账本。')
        return '\n'.join(lines)
    except Exception:
        # Do not expose arbitrary exceptions, IDs other than the run, or paths.
        return ('Meta违规素材清理｜结束（统计暂不可用）\n北京时间：%s\n批次：%s\n'
                '未取得最终统计，请按批次核对运行记录和共享账本；不能据此判断删除结果。') % (_time(now()), run_id)
