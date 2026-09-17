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

## 2026-09-17 列表标记

现有 GET `/api/youtube-auto-publish/tasks`（含 compact=1）及 GET `/tasks/{id}` 增加 `x_player_card`，非已公开视频为 null。对象字段为 `state,eligible,message,checked_at,expires_at` 及可选 `card_type`；state 为 ready/restricted/not_player/not_public/not_embeddable/unavailable/checking/pending。时间为 UTC ISO8601，页面显示北京时间。

只有 fresh ready 且 eligible=true 显示绿色；过期、检查中、队列等待、错误都不是可用判定。地区/年龄限制为 restricted，不承诺对任意地区观众可播。字段进入既有 compact revision，因此异步检测完成后下一轮正常轮询得到更新；未变化仍返回 unchanged。接口不新增网络参数、不增加单行详情请求，不暴露授权凭据。

此快照与转发弹窗的 `player_card` 不同：前者含缓存时间和公开视频/播放限制检查，后者仍是提交前实时检查的一部分。列表快照不作为任何平台写入的授权依据。
