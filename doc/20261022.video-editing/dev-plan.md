# Implementation
1. Frontend: shared library, multipart upload, per-drama timeline/model/preview, autosave, tasks/results. Explicit demo mode makes no business writes.
2. Backend: SQLite, trusted owners, upload state, catalog/probe, frozen versions, one-flight coordinator.
3. GPU: deterministic recipe/render/probe, private COS output, mixed-type adapter, existing process recovery.
4. Integration: permissions/navigation, AST patch, systemd, exact provenance, backup/readback/rollback.

Checks: unittest/py_compile, Node syntax/model tests, browser interaction, FFmpeg probe and isolated real COS/GPU media. No social publication. Executed evidence belongs to test-report.md; secrets remain on the server.
