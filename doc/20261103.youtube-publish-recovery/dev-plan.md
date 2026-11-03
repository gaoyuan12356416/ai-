# Implementation

Patch youtube_native, native retry audit, adapter retry revision, central re-admission and workflow DTO/error wording. Add real-SQLite tests for preflight failure, no duplicate write permit, explicit retry and unknown fencing. Deploy exact GitHub source to isolated canonical bridge, coordinator and two main-runtime modules only. Restart only affected API/control services with no active YouTube work; do not restart CPU/GPU execution nodes.
