# 视频剪辑工具第一期

Confirmed by the operator on 2026-10-22 (Asia/Shanghai).

## Outcome
Browser direct COS upload -> shared video library -> select one asset and episodes from multiple dramas -> independently edit and render one output per drama -> COS preview/download. External processing and Yingliang ingestion are disabled reserved interfaces.

Reuse Feishu, QuickNav and UiTopbar. Assignable video_editing permission defaults false for ordinary users, true for admins. Authorized users share ready assets; uploader/admin can rename or soft-delete. Projects/tasks belong to creator; admins inspect all. Ownership comes exclusively from the trusted session.

## Editing
Each project selects one uploaded video and up to 20 dramas. Each drama has independent episodes and ordered clips. Provide preview, split, delete multiple intervals/whole episodes, reorder, repeat asset insertion, trim handles/time inputs, undo, autosave. Shared insertion/transition settings preserve episode trims. Submitted versions freeze; edits cannot mutate executing or successful jobs.

Clips use half-open [start_ms,end_ms), contain with black padding or centered cover, mute, and outgoing transition. Default cut; fade crossfades audio/video for 300 ms. Last clip is cut. Reject segments too short for incoming plus outgoing overlap. Frame boundaries round to nearest 30 fps frame, half upward. Preview and renderer share recipe. Output: 720x1280, 30 fps, H.264/AAC MP4, continuous 48 kHz audio; normalize timestamps, mixed rates/dimensions and missing audio.

## Storage and tasks
Default upload max 8 GiB; 16 MiB multipart chunks. CPU accepts JSON/signatures only. Sign key/upload ID/part number/exact bytes/content digest. Do not trust client completion, full-file MD5 inferred from ETag, arbitrary URL or client media duration. Verify COS then authoritative GPU probe. Private ACL and expiring links; no public local fallback.

Independent SQLite on CPU data disk. Explicit video_editing_v1 and ve_ IDs enter existing single GPU runtime/slot, independent recipe identity, media paths and result contract. Coordinator occupies at most one GPU job; remaining work queues on CPU. Reconcile submission ambiguity by durable ID. Never automatically regenerate successful output. Freeze source identity/version; URL signature is transport rather than identity.

GPU scratch is allowed. Clean success only after COS verification. Failed scratch eligible after 24h and positive stopped-process evidence; preserve receipts/checkpoints. Soft deletion retains COS objects referenced by frozen jobs.

## Release
Use clean branch, preserve dirty original checkout and composite CPU runtime. Scoped AST CPU patch; exact live GPU source-overlay baseline with reviewed overlays. Preserve legacy fingerprints/results/prefetch and publishing services. Rollback after new records exist retains mixed-type reader/resume.
