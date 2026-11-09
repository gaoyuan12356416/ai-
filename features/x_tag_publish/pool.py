"""Profile-aware pool matching while retaining existing media admission/reservation."""
from .policy import Decision, portrait_snapshot, canonical, language


def material_candidates(config, sidecar, accounts, *, source_date, connection_factory,
                        downloader, prober, repair_client, timestamp,
                        assignment_identity=None, stable_shuffler=None, heartbeat=None,
                        allow_images=True):
    from scripts import x_post_schedule_runner as runner
    identity = dict(assignment_identity or {})
    options = {}
    if identity:
        options['schedule_identity'] = {k: identity[k] for k in
                                       ('source_type', 'run_date', 'publish_time', 'version', 'account_ids')}
    pool = sidecar.available_pool_items(config.material_pool_path, config.scan_limit, **options)
    if not pool:
        error = runner.ScheduleRunError('画像素材池暂无可用素材', 'x_post_schedule_material_shortage')
        error.shortage_snapshot = runner.material_shortage.snapshot(accounts, [], scan_limit=config.scan_limit)
        raise error
    connection = runner._open_source_connection(config, connection_factory)
    candidates, shortage_rejections = [], {}
    try:
        for start in range(0, len(pool), config.candidate_pool_limit):
            if callable(heartbeat):
                heartbeat()
            batch = pool[start:start + config.candidate_pool_limit]
            try:
                selected, rejected = runner.select_pool_candidates(connection, batch, source_date,
                    limit=config.candidate_pool_limit, schema=config.mysql_database, allow_images=allow_images)
            except runner.CandidateQueryError:
                runner._close_source_connection(connection)
                connection = runner._open_source_connection(config, connection_factory)
                selected, rejected = runner.select_pool_candidates(connection, batch, source_date,
                    limit=config.candidate_pool_limit, schema=config.mysql_database, allow_images=allow_images)
            candidates.extend(selected)
            runner._record_pool_checks_best_effort(sidecar, config, rejected)
    finally:
        runner._close_source_connection(connection)
    snapshot = portrait_snapshot()
    assigned, used, failures = {}, set(), []
    repair_state = {'attempted': 0}
    # Rotate contention deterministically between channels. Never redraw a frozen queue.
    ordered_accounts = runner._stable_shuffled(accounts, [canonical(identity), 'tag-profile-accounts'])
    for account in ordered_accounts:
        decision = Decision(account, ['pool', identity], snapshot)
        eligible = [item for item in candidates if str(item['material_id']) not in used
                    and language(item.get('material_language')) == language(account.get('drama_language'))
                    and (account.get('long_video_eligible') or float(item.get('source_duration') or 0) <= 1200)]
        ranked = sorted(eligible, key=lambda item: decision.rank(item.get('drama_labels', [])))
        for candidate in ranked:
            planned, rejected = runner._preflight_material_candidates(config, sidecar, [candidate], [account],
                source_date=source_date, timestamp=timestamp, downloader=downloader, prober=prober,
                repair_client=repair_client, assignment_identity=identity, repair_state=repair_state,
                heartbeat=heartbeat)
            failures.extend(rejected)
            runner.material_shortage.observe_rejections(shortage_rejections, [candidate], rejected)
            if planned:
                item = planned[0]
                item['tag_profile'] = decision.audit(candidate.get('drama_labels', []))
                assigned[int(account['id'])] = item
                used.add(str(candidate['material_id']))
                break
    # A failure on one target must not poison a material successfully assigned elsewhere.
    runner._record_pool_checks_best_effort(sidecar, config,
        [row for row in failures if str(row.get('material_id')) not in used])
    planned = []
    for account in accounts:
        if int(account['id']) in assigned:
            item = assigned[int(account['id'])]
            item['candidate_rank'] = len(planned) + 1
            planned.append(item)
    shortage = runner.material_shortage.snapshot(accounts, planned, scanned=len(pool), scan_limit=config.scan_limit,
                                                 rejections=shortage_rejections)
    if not planned:
        error = runner.ScheduleRunError('画像素材池中没有符合频道语言和素材条件的候选', 'x_post_schedule_material_shortage')
        error.shortage_snapshot = shortage
        raise error
    return runner.MaterialScheduleCandidates(planned, shortage_snapshot=shortage)


def install_reservation_policy(service):
    original = service._material_fifo_selection_matches

    def profile_pool_selection(pool_rows, prepared, account_ids, premium_account_ids, **kwargs):
        # Profile ranking replaces FIFO only. Run the existing identity, membership,
        # duration and current deploy-time checks on the actual selected subset.
        chosen = {int(item['pool_item_id']) for item in prepared}
        rows = [row for row in pool_rows if int(row['id']) in chosen]
        if len(rows) != len(prepared):
            return False
        kwargs['capacity_skips'] = []
        return original(rows, prepared, account_ids, premium_account_ids, **kwargs)
    service._material_fifo_selection_matches = profile_pool_selection
