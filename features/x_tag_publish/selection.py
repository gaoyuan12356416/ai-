"""Extend the existing two-stage selector only inside the isolated runtime."""
import os
import threading
from dataclasses import replace

from features.x_auto_posts.selector import TwoStageSelector
from features.x_auto_posts.x_sidecar import XPostAutoBridgeClient
from .policy import Decision, ranked_dramas, language


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
            kwargs['selection_snapshot'] = snapshot
            context['selected_audit'] = audit
        return self.store.reserve_material(**kwargs)


class ProfileSelector(TwoStageSelector):
    def __init__(self, source, metrics, history, store, **kwargs):
        self._profile_context = threading.local()
        # Claimed selectors wrap the ledger; previews must remain unbound.
        self._run_store = store.store if hasattr(store, 'claim_token') else store
        # A preview must delegate to the preview store, never the live reservation store.
        super().__init__(source, metrics, history, AuditStore(store, self._profile_context), **kwargs)

    def _eligible_dramas(self, request, rows, metrics, rejection_counts):
        from .hit_drama import MARKER, match_reason
        get_run = getattr(self._run_store, 'get_run', None)
        target = get_run(request.run_id).blacklist_snapshot.get(MARKER) if callable(get_run) else None
        if target:
            if str(request.account_id) not in target['account_ids']:
                return []
            from features.hit_drama_target import content_ids
            allowed = set(content_ids(target['target'], request.language))
            if 'account_content_ids' in target:
                allowed.intersection_update(target['account_content_ids'].get(str(request.account_id), []))
            rows = [row for row in rows if row.content_id in allowed]
        candidates = super()._eligible_dramas(request, rows, metrics, rejection_counts)
        bridge = XPostAutoBridgeClient(os.environ['X_POST_AUTOMATION_INTERNAL_URL'],
                                      os.environ['X_POST_AUTO_INTERNAL_TOKEN'])
        account = bridge.account_snapshot(request.account_id)
        decision = Decision(account, ['auto', request.template_id, request.template_version, request.run_id, request.task_id])
        context = dict(decision=decision, catalog={}, language=language(request.language), fallback='')
        self._profile_context.value = context
        if target:
            from .policy import catalog_labels
            context['catalog'] = catalog_labels(self.source, candidates)
            # Exact-drama requests never use ordinary no-profile/rank fallback.
            return [drama for drama in candidates if not match_reason(decision,
                context['catalog'].get((drama.content_id, language(drama.language))))]
        return ranked_dramas(self.source, candidates, context)

    def _select(self, request, *, reserve):
        try:
            result = super()._select(request, reserve=reserve)
            context = getattr(self._profile_context, 'value', None)
            if context and context.get('selected_audit'):
                # Preview and execution responses include the same frozen explanation.
                result = replace(result, reservation=dict(result.reservation, tag_profile=context['selected_audit']))
            return result
        finally:
            self._profile_context.value = None
