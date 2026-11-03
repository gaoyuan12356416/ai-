# API contract
No new endpoints. Existing administrator PUT /api/pipeline-monitor/capacity remains authoritative with expected_version, host_id, stage and max_workers. Worker supervisor reads desired pools periodically; shrink stops future claims while running work finishes. No renderer-local concurrency override.
