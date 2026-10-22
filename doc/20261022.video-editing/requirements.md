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


## 2026-10-22 交互响应与等待反馈修正

用户反馈“交互响应慢、点击后无响应、无即时反馈”。打开弹窗、查询、选剧、添加、保存、制作、预览及刷新等异步操作，应在等待网络前显示状态并防止同一动作重复提交。媒体校验是实际后台工作，不承诺瞬间完成；必须持续展示本次所选集数的已就绪进度和可采取的操作。

点击添加后固定本次剧目、剧集和项目身份，所选剧集全部通过校验后自动加入时间线一次，无需再次点击。关闭弹窗、取消等待、切换产品/短剧或切换项目应取消自动添加，已入队的原校验任务继续按既有规则运行。失败须在弹窗内提示具体剧集和原因；不得只在弹窗背后的toast显示。

轮询仅处理本次所选未就绪来源和已知恢复任务，避免查询无关未校验剧集；限定并发且轮询不重叠。晚到响应不能覆盖新搜索、产品、剧目、项目或已关闭弹窗。同一写请求不自动重发，保留原operation ID和未知结果核实语义。保持上一轮后台标准样式、共享登录导航和GPU单槽位。
