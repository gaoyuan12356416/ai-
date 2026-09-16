# 接口

前缀 `/api/youtube-auto-publish/tasks/{task_id}/x-share`。真实Cookie、YouTube/X权限和任务归属；响应no-store。POST还要求同源JSON。

| 方法/后缀 | 契约 |
|---|---|
| GET根路径 | source,macros,default_template,accounts,history,limit |
| POST /preview | description_template → text,valid,errors,weighted_length,limit,appended_url |
| POST根路径 | operation_id(UUID),account_ids(1–20整数),description_template → 202 {run} |
| GET /runs/{32hex} | {run}，不写平台 |

Run有id/operation_id/task_id/video_id/account_ids/description_template/text/status/created_at/items。Item有account_id/username/status/post_url/message/duplicate。状态queued/publishing/published/failed/unknown_outcome。

内部仅backend bearer可POST `/internal/youtube-shares/{query,create,run}`；daily/auto拒绝。actor/scope由服务端提供，源链接和正文中目标链接严格验证。独立SQLite自动建表，无MySQL DDL。

参考：[X字符规则](https://docs.x.com/fundamentals/counting-characters)、[Create Posts](https://docs.x.com/x-api/posts/create-post)。
