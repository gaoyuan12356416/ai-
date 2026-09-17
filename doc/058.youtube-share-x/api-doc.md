# 接口

前缀 `/api/youtube-auto-publish/tasks/{task_id}/x-share`。真实Cookie、YouTube/X权限和任务归属；响应no-store。POST还要求同源JSON。

| 方法/后缀 | 契约 |
|---|---|
| GET根路径 | source,player_card,macros,default_template,accounts,history,limit |
| POST /preview | description_template → text,valid,errors,weighted_length,limit,appended_url |
| POST根路径 | operation_id(UUID),account_ids(1–20整数),description_template → 202 {run} |
| GET /runs/{32hex} | {run}，不写平台 |

宏列表新增 `{key:"short_url",label:"推广短链",value:"<任务已保存的短链>"}`，值来自任务冻结的 `material.macro_url`。预览与提交共用同一取值和渲染方法；空值按现有缺值规则报错。`{youtube_url}` 仍是原视频直链，未填写时自动追加。

`player_card={eligible,state,card_type,message,checked_at}`。`eligible=true` 仅表示刚读取的 YouTube 页面提供与当前视频匹配的播放器元数据。`state` 为 ready/not_player/invalid_player/unavailable。前端未收到明确 true 不允许新提交；创建接口再次执行 YouTube API 公开/嵌入权限与公共页面播放器检查。不满足时返回 409 `youtube_player_unavailable` 或 `youtube_not_embeddable`，不会进入分享队列。视频 URL 必须是正文第一个链接；省略该宏时，在有其他链接的文案前补上视频链接。

Run有id/operation_id/task_id/video_id/account_ids/description_template/text/status/created_at/items。Item有account_id/username/status/post_url/message/duplicate。状态queued/publishing/published/failed/unknown_outcome。

内部仅backend bearer可POST `/internal/youtube-shares/{query,create,run}`；daily/auto拒绝。actor/scope由服务端提供，源链接和正文中目标链接严格验证。独立SQLite自动建表，无MySQL DDL。

参考：[X字符规则](https://docs.x.com/fundamentals/counting-characters)、[Create Posts](https://docs.x.com/x-api/posts/create-post)。
