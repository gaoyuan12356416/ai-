"""Attach the isolated tag ledgers to the existing X execution protocol.

Tag IDs already occupy a disjoint, durable numeric namespace. Keep the native
X source keys and node capabilities, and route those IDs to their owning
ledger/bridge. Account resources remain shared; material dedupe stays local.
"""
import os

from features.pipeline_dispatch.adapters import (
    AdapterError, AdapterRegistry, LedgerAdapter, LedgerSpec, RemoteCanonicalDriver,
)

ID_BASE = 9000000000000


def tag_identity(value):
    return str(value).isdigit() and int(value) >= ID_BASE


class TagLedgerAdapter(LedgerAdapter):
    def project(self, row, now=None):
        if not tag_identity(row[self.spec.identity]):
            raise AdapterError('x_tag_source_namespace_invalid')
        offer = super().project(row, now)
        if offer and self.spec.family == 'queue' and offer.get('ancestor_source_keys'):
            offer['details_url'] = '/x-tag-auto-publish-runs.html'
        return offer


class TagRegistry(AdapterRegistry):
    def _adapter(self, job):
        family = job.get('family')
        if job.get('channel') == 'x' and family in ('auto', 'queue') and tag_identity(job.get('source_id')):
            try:
                return self.adapters[('x', 'tag_' + family)]
            except KeyError:
                raise AdapterError('x_tag_execution_not_connected') from None
        return super()._adapter(job)


def extend_registry(original, config, environ=None):
    settings = config.get('x_tag')
    if not settings:
        return original
    env = os.environ if environ is None else environ
    paths = dict(settings['paths'])
    if paths['x'] == paths['x_auto'] or any(paths[k] == config.get('paths', {}).get(k) for k in ('x', 'x_auto')):
        raise ValueError('x_tag_database_collision')
    registry = TagRegistry()
    registry.adapters.update(original.adapters)
    for family, key, port, prefix, token_key, table, page in (
        ('queue', 'x', 8821, '/internal/pipeline-dispatch/x', 'post_token_env',
         'x_post_queue', '/x-tag-post-logs.html'),
        ('auto', 'x_auto', 18843, '/internal/pipeline-dispatch/x-auto', 'auto_token_env',
         'x_auto_task', '/x-tag-auto-publish-runs.html'),
    ):
        token = env.get(settings[token_key], '')
        if len(token) < 32:
            raise ValueError('x_tag_canonical_token_missing')
        spec = LedgerSpec('x', family, paths[key], table, module='x_accounts', details_url=page)
        driver = RemoteCanonicalDriver(port, prefix, token, control_only=family == 'auto')
        registry.adapters[('x', 'tag_' + family)] = TagLedgerAdapter(
            spec, driver, cross_paths=paths, preparation_policy=config.get('preparation_policy', {}))
    return registry


def label_tasks(result):
    """Expose the business source in the existing monitor DTO, not node internals."""
    for item in result.get('items', []):
        if item.get('channel') == 'x' and tag_identity(item.get('source_id')):
            item['source_family'] = ('标签画像自动发布' if 'x-tag-auto-' in item.get('detail_url', '')
                                     else '标签画像素材池')
    return result


def main():
    from features.pipeline_dispatch import service, views
    build_registry, tasks = service.build_registry, views._tasks
    service.build_registry = lambda config: extend_registry(build_registry(config), config)
    views._tasks = lambda *args, **kwargs: label_tasks(tasks(*args, **kwargs))
    service.main()


if __name__ == '__main__':
    main()
