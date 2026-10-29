"""Read-only account inventory with bounded retries of the current page."""
import time


def receipt_basis(status, result):
    if result.get('confirmed_absent') is True:
        return 'account_library_absent'
    if status == 'deleted' and result.get('success') is True:
        return 'delete_success_receipt'
    if status in ('deleted', 'already_deleted'):
        return 'recorded_terminal_without_receipt'
    return None


def read_inventory(client, account_id, token, seconds=600, progress=None):
    from features.fb_ad_asset_delete.core import AssetError
    from features.fb_ad_asset_delete.graph import GraphError

    seen, cursors, pages, retries = set(), set(), 0, 0
    params = {'fields': 'id', 'limit': '100'}
    deadline = time.monotonic() + seconds

    def report(**extra):
        if progress:
            progress(dict(pages=pages, items=len(seen), read_retries=retries, **extra))

    try:
        if not str(account_id).isdigit():
            raise GraphError('account_mismatch', '账户 ID 无效，不能核实视频库')
        while True:
            for attempt in range(3):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GraphError('account_video_read_limit', '账户视频完整核实已达到时限')
                try:
                    data = client.request('GET', 'act_'+account_id+'/advideos', token,
                                          dict(params), timeout=min(20, remaining), deadline=deadline)
                    break
                except AssetError as exc:
                    detail = getattr(exc, 'detail', {}) or {}
                    status = detail.get('http_status')
                    retryable = (exc.code == 'read_unavailable' or detail.get('is_transient') is True or
                                 status == 429 or isinstance(status, int) and status >= 500)
                    if not retryable or attempt == 2:
                        raise
                    retries += 1
                    report(retrying_page=pages+1)
                    time.sleep(min(2, max(0, deadline-time.monotonic())))
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise GraphError('account_video_read_incomplete', '账户视频列表未完整返回')
            page_ids = []
            for item in data['data']:
                ident = client._identity_id(item.get('id')) if isinstance(item, dict) else ''
                if not ident or ident in seen or ident in page_ids:
                    raise GraphError('account_video_read_incomplete', '账户视频列表含无法核实或重复的记录')
                page_ids.append(ident)
            paging = data.get('paging', {})
            if not isinstance(paging, dict):
                raise GraphError('account_video_read_incomplete', '账户视频分页信息无效')
            seen.update(page_ids)
            pages += 1
            report()
            if len(seen) > min(client.max_inventory, 50000):
                raise GraphError('account_video_read_limit', '账户视频列表超过完整核实上限')
            if not paging.get('next'):
                return dict(ids=seen, pages=pages, read_retries=retries, complete=True)
            after = (paging.get('cursors') or {}).get('after') if isinstance(paging.get('cursors', {}), dict) else None
            if not isinstance(after, str) or not after or len(after) > 4096 or after in cursors or pages >= min(client.max_inventory, 500):
                raise GraphError('account_video_read_incomplete', '账户视频分页不完整或已达到上限')
            cursors.add(after)
            params['after'] = after
    except AssetError as exc:
        # Partial sightings are positive evidence; missing IDs are inconclusive.
        return dict(ids=seen, pages=pages, read_retries=retries, complete=False,
                    code=exc.code, message=exc.message)


def pair_outcomes(rows, library):
    result = []
    for row in rows:
        found = row['video_id'] in library['ids']
        status = 'still_present' if found else 'confirmed_absent' if library['complete'] else 'unverified'
        result.append(dict(row, status=status))
    return result
