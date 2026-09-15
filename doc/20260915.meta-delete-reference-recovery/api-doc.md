# 接口变更

## POST /api/fb-post-ad-delete/jobs/{job_id}/recheck
登录Cookie、fb_ad_asset_delete模块权限、当前产品权限及任务归属均必需；同源JSON。

请求：`{"preview_id":"...","request_id":"16至80位幂等标识"}`。拒绝其他字段及任意对象ID。

202返回：`{"job_id":"...","operation_id":"...","status":"running","duplicate":false,"read_only":true}`。

详情新增recheck：operation_id、status(running/completed/interrupted)、checked、total、released、step、updated_at、error。原任务主状态保持，核验期间服务端阻止execute/reconcile；重核验完成不会执行DELETE。

可重核验：暂时性读取/共享引用/视频索引阻止。源归属冲突等仍要求重新预览。成功/失败/未知状态不自动改写。

## 对象结果
Graph结果新增credential_user_id、account_diagnostic；detail保留code/error_subcode/type/fbtrace_id/is_transient/error_user_title/error_user_msg/http_status。所有字符串脱敏，Token不进入台账或响应。
