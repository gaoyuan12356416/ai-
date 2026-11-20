"""Extend the existing two-stage selector only inside the isolated runtime."""
import os
import threading
from dataclasses import replace

from features.x_auto_posts.selector import MoboUpcomingWait, NoEligibleMaterial, TwoStageSelector
from features.x_auto_posts.x_sidecar import XPostAutoBridgeClient
from .policy import Decision, ranked_dramas, language


def _check_slot_error(exc):
    if getattr(exc, 'code', '') in ('x_mobo_upcoming_daily_full', 'x_mobo_upcoming_paused'):
        raise NoEligibleMaterial({exc.code: 1}) from None


class AuditStore:
    def __init__(self, store, context):
        self.store, self.context = store, context

    def __getattr__(self, key):
        return getattr(self.store, key)

    def reserve_material(self, **kwargs):
        context = getattr(self.context, 'value', None)
        if context:
            key = (str(kwargs['content_id']), context['language'])
            metadata = context['catalog'].get(key, {})
            audit = context['decision'].audit(metadata, fallback=context.get('fallback', ''))
            snapshot = dict(kwargs.get('selection_snapshot') or {})
            snapshot['tag_profile'] = audit
            if context.get('upcoming') is not None:
                snapshot['mobo_upcoming'] = context['upcoming']
            if (context.get('hit') or {}).get('timing_policy'):
                from features.hit_publish import timing
                snapshot['hit_publish_timing'] = timing(context['hit']['release_times'], kwargs['content_id'], context['language'], context['now'])
            kwargs['selection_snapshot'] = snapshot
            context['selected_audit'] = audit
        try:
            return self.store.reserve_material(**kwargs)
        except Exception as exc:
            _check_slot_error(exc)
            raise


class ProfileSelector(TwoStageSelector):
    def upcoming_plan(self, config, now):
        from .material_catalog import upcoming_plan
        return upcoming_plan(self.source, config, now)

    def __init__(self, source, metrics, history, store, **kwargs):
        self._profile_context = threading.local()
        # Claimed selectors wrap the ledger; previews must remain unbound.
        self._run_store = store.store if hasattr(store, 'claim_token') else store
        # A preview must delegate to the preview store, never the live reservation store.
        super().__init__(source, metrics, history, AuditStore(store, self._profile_context), **kwargs)

    def _eligible_dramas(self, request, rows, metrics, rejection_counts):
        from .hit_drama import MARKER, match_reason
        get_run = getattr(self._run_store, 'get_run', None)
        frozen = get_run(request.run_id).blacklist_snapshot if callable(get_run) else {}
        target = frozen.get('hit_material_publish') or frozen.get(MARKER)
        if target:
            if str(request.account_id) not in target['account_ids']:
                return []
            from features.hit_drama_target import content_ids
            allowed = set(content_ids(target['target'], request.language))
            if 'account_content_ids' in target:
                allowed.intersection_update(target['account_content_ids'].get(str(request.account_id), []))
            catalog = target['target'].get('catalog', 'dramawave')
            rows = [row for row in rows if row.content_id in allowed and getattr(row, 'catalog', 'dramawave') == catalog]
        candidates = super()._eligible_dramas(request, rows, metrics, rejection_counts)
        bridge = XPostAutoBridgeClient(os.environ['X_POST_AUTOMATION_INTERNAL_URL'],
                                      os.environ['X_POST_AUTO_INTERNAL_TOKEN'])
        account = dict(bridge.account_snapshot(request.account_id))
        # Real tasks freeze the operator's choice at creation. Previews have no task.
        get_task = getattr(self._run_store, 'get_task', None)
        if callable(get_task):
            task = get_task(request.task_id)
            account['selection_profile'] = task.account_snapshot.get('selection_profile') or {'mode': 'revenue', 'mode_version': 0, 'profile': {}}
        decision = Decision(account, ['auto', request.template_id, request.template_version, request.run_id, request.task_id])
        context = dict(decision=decision, catalog={}, language=language(request.language), fallback='', hit=target, now=request.now)
        if getattr(self.source, 'watching', False):
            context['upcoming'] = self.source.upcoming_summary()
        self._profile_context.value = context
        if target:
            from .policy import catalog_labels
            if target['target'].get('catalog') == 'moboreels':
                from features.moboreels_hit import classifications
                context['catalog'] = classifications(self.source._stream_select, [row.content_id for row in candidates])
            else:
                context['catalog'] = catalog_labels(self.source, candidates)
            # Exact-drama requests never use ordinary no-profile/rank fallback.
            return [drama for drama in candidates if not match_reason(decision,
                context['catalog'].get((drama.content_id, language(drama.language))))]
        return ranked_dramas(self.source, candidates, context)

    def _eligible_materials(self, request, drama, rows, blacklist, metrics, rejection_counts):
        get_run = getattr(self._run_store, 'get_run', None)
        hit = get_run(request.run_id).blacklist_snapshot.get('hit_material_publish') if callable(get_run) else None
        if hit:
            selected = hit.get('assignments', {}).get(str(request.account_id), {}).get('material_id')
            rows = [row for row in rows if str(row.material_id) == selected]
        return super()._eligible_materials(request, drama, rows, blacklist, metrics, rejection_counts)

    def _select(self, request, *, reserve):
        from .material_catalog import for_request
        selector = for_request(self, request)
        if selector is not self:
            return selector._select(request, reserve=reserve)
        try:
            from features.hit_publish import future_scope
            get_run = getattr(self._run_store, 'get_run', None)
            run = get_run(request.run_id) if callable(get_run) else None
            frozen = run.blacklist_snapshot if run is not None else {}
            hit = frozen.get('hit_material_publish') or frozen.get('hit_drama_publish') or {}
            plan = frozen.get('mobo_upcoming') if callable(get_run) else request.preview_upcoming
            read_error = None
            if plan is not None and not hit and hasattr(self.source, 'assess_upcoming'):
                self.source.watching = True
                if reserve and self.store.get_task_reservation(request.task_id) is not None:
                    return super()._select(request, reserve=reserve)
                if getattr(run, 'trigger_type', None) == 'auto' and not self._run_store.get_template(request.template_id).enabled:
                    raise NoEligibleMaterial({'x_mobo_upcoming_paused': 1})
                from features.moboreels_upcoming import discover
                def recover_plan():
                    template = self._run_store.get_template(request.template_id, version=request.template_version) if run else None
                    return discover(self.source._stream_select, self.source.schema,
                                    request.rules.drama.launch_window_days, plan['scanned_at'],
                                    material_config=template.config if template else None)
                try:
                    if callable(get_run):
                        plan = self._run_store.mobo_upcoming_plan(request.run_id, recover_plan)
                    elif 'stories' not in plan:
                        plan = recover_plan()
                    self.source.assess_upcoming(plan, request)
                except Exception as exc:
                    if not self.source.has_dramawave:
                        raise
                    read_error = exc
            if frozen.get('hit_drama_publish'):
                # Override only this request, before source SQL and hard gates.
                request = replace(request, rules=replace(request.rules,
                    drama=replace(request.rules.drama, launch_window_days=0)))
            try:
                with future_scope(hit.get('release_times', {})):
                    result = super()._select(request, reserve=reserve)
            except NoEligibleMaterial as exc:
                if 'x_mobo_upcoming_daily_full' in exc.rejection_counts or 'x_mobo_upcoming_paused' in exc.rejection_counts:
                    raise
                if read_error is not None:
                    raise read_error
                state = getattr(self.source, 'upcoming', None)
                from features.x_auto_posts.repositories import MetricTotals
                empty = MetricTotals()
                rules = (request.rules.drama, request.rules.material)
                if (state is not None and state.retry_at is not None
                        and all(rule.spend.contains(empty.spend) and rule.d0_roas.contains(empty.d0_roas)
                                for rule in rules)):
                    if state.ready and getattr(run, 'trigger_type', None) == 'auto':
                        try:
                            self._run_store.check_mobo_wait_capacity(request.task_id, request.now)
                        except Exception as slot_error:
                            _check_slot_error(slot_error)
                            raise
                    raise MoboUpcomingWait(self.source.upcoming_summary()) from None
                raise
            context = getattr(self._profile_context, 'value', None)
            if context and context.get('upcoming') is not None:
                result = replace(result, reservation=dict(result.reservation, upcoming=context['upcoming']))
            if context and context.get('selected_audit'):
                # Preview and execution responses include the same frozen explanation.
                result = replace(result, reservation=dict(result.reservation, tag_profile=context['selected_audit']))
            return result
        finally:
            self._profile_context.value = None
