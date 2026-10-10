# API

POST /api/admin/fb-auto-publish/templates/{id}/run-now

既有Cookie登录、fb_page_posts权限与同源JSON保护。服务端根据会话设置_actor，浏览器不得覆盖。旧两字段请求保留。

爆款请求：`{"expected_version":6,"material_id":"7360836","operation_id":"UUID"}`。

成功202：ok、run_id、operation_id、idempotent、queued、skipped、total_pages、skipped_pages[{page_id,reason}]。只表示已创建一轮任务；真实发布以原发布账本为准。全跳过仍返回可追踪的completed运行。

同操作同内容返回原run；不同内容409。未创建时明确区分版本变更、停用、容量、缺素材、无效视频/时长、黑名单和剧集映射问题。模糊失败保持原操作号重试。

跳过代码：fb_auto_page_frequency_limit、fb_page_missing_eligible_token、fb_auto_page_unknown_block、fb_auto_material_active、fb_auto_material_cooldown、fb_auto_page_task_conflict。GET runs/{id} 返回run.skipped_pages及tasks/page_snapshots；前端中文解释。
