# API behavior

Existing routes unchanged. Native queued publication returns status=queued (already supported by the deployed UI) and pending upload stage. Enqueue errors show safe specific reasons. Explicit retry creates an operator_retry audit event; no schema change.
