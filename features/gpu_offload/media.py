"""Prepare-only CPU assistance; the existing coordinator owns every claim."""
import copy
import os

from features.pipeline_dispatch import worker
from features.pipeline_dispatch.client import CoordinatorClient


def clamp_capacity(response, maximum):
    result = copy.deepcopy(response)
    capacity = result.get('capacity', {})
    if 'prepare' in capacity:
        capacity['prepare']['target'] = min(maximum, max(0, int(capacity['prepare'].get('target', 0))))
    if 'publish' in capacity:
        capacity['publish']['target'] = 0
        capacity['publish']['ceiling'] = 0
    return result


class PrepareClient(CoordinatorClient):
    def post(self, path, payload):
        if path == 'nodes/register':
            payload = copy.deepcopy(payload)
            payload.update(publish_initial=0, publish_max=0, prepare_initial=0)
            for name in ('inventory', 'metrics'):
                if name in payload:
                    payload[name].update(backend='cpu', gpu_count=0, gpus=[])
            payload['capabilities'] = [x for x in payload.get('capabilities', [])
                                       if not x.startswith('publish:') and not x.endswith('_publish')]
        result = super().post(path, payload)
        return clamp_capacity(result, min(2, int(os.environ.get('PIPELINE_PREPARE_MAX', '1')))) if path == 'nodes/register' else result


def main():
    config = worker.configuration()
    allowed = {'x_preflight', 'x_random', 'x_repair', 'fb_prepare', 'tt_prepare'}
    enabled = set(filter(None, os.environ.get('PIPELINE_EXECUTORS', '').split(',')))
    if config['backend'] != 'cpu' or not enabled or not enabled <= allowed:
        raise ValueError('CPU assistance requires prepare-only CPU executors')
    if config['node_id'] != 'hk-cpu-assist':
        raise ValueError('dedicated assistance identity required')
    config.update(publish_max=0, publish_initial=0, prepare_initial=0,
                  prepare_max=min(2, config['prepare_max']))
    worker.CoordinatorClient = PrepareClient
    worker.run_node(config)


if __name__ == '__main__':
    main()
