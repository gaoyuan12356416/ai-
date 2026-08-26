# API 合同

全部 API 复用登录、模块权限、CSRF 和 operator audit；错误不返回 token、session URI 或内部 SQL。

## 任务合同

创建 API 的 outputs 精确使用 `random_template_video`，与其他三项均默认 false，零输出 400；历史错误 `random_template` 仅可被服务端归一输入，存储/响应不得继续使用它作为新合同。随机模板参数只接受 `advanced_options.random_template={mode,source,layers}`。成功结果为 `output_random_template_url`、`random_template_recipe`；旧 cover/naming 字段 accept-ignore-default。

## 六个 API

- `GET /api/drama-material/random-template-catalog`：manifest/profile 和四层资产，无 light；未配置/漂移 503。
- `POST /api/drama-material/jobs/{job_id}/short-links`：请求 `material_kind=concat_video|no_bgm_video|random_template`；只允许 done 且 URL 存在，返回 frozen link。
- `GET /api/drama-material/youtube/channels?app_id=<decimal>`：仅 status=1、refreshable、upload+identity-read、非内部测试频道；每个候选必须实际 refresh 并以 `channels.list(mine=true)` 验证冻结 channel ID，失败/空/多/错身份隐藏且不执行外部 mutation；非法 ID 关闭。
- `POST /api/drama-material/jobs/{job_id}/youtube-publishes`：请求 operation_id、app_id、material_kind、channel_local_id、youtube_account_id、title、description_template、comment_text、duplicate_confirmed；返回 202。
- `GET /api/drama-material/youtube-publishes/{publish_id}`：仅返回安全状态、公开 external ID、时间和操作错误。
- `POST /api/drama-material/youtube-publishes/{publish_id}/retry-comment`：仅 published 且 known-safe comment failure；不重传视频。

标题 required/<=100 chars；描述 required/模板和渲染值 UTF-8 <=5000 bytes；评论可选。`{{url}}` 仅 description replace-all，先确保同 material 短链。冻结 app/job/material URL/channel/title/template/rendered/comment/operator/privacy public。同 operation+同 payload 幂等，不同 payload 409。

主状态 queued/validating/downloading/uploading/submitted/processing/published/failed/unknown。processing/unknown 同 job+material+channel 新请求 409；prior published 未二次确认 409。依赖 503，输入 400，权限 401/403，不可变/并发冲突 409。

统一同步 RPC 不接受通用 JSON。video/publish_log 精确字段为 `publish_id,video_id,app_id,channel_local_id,operator_user_id,job_id,content_id,source_kind,source_url,title,description_rendered,privacy_status,published_at_utc`；comment 精确字段为 `publish_id,video_id,comment_id,channel_local_id,operator_user_id,comment_text,published_at_utc`。video/comment external ID 分别等于对应 ID；publish_log external ID 是无前导零的十进制 `publish_id` 字符串。任何 extra/missing/错类型/identity mismatch 均 409 并由 outbox 安全记 failed。RPC 内部只映射固定 legacy 列，调用方不能传 SQL、列名或数据库凭据。

内部 RPC 固定为 `127.0.0.1:18837`（18836 为既有 FB 隧道）：Bearer `GET /health` 只返回 `ok/schema/grant_fingerprint`；Bearer `POST /v1/youtube-sync` 只接收 exact `action,table,external_id,payload` envelope。同一至少 32 字符 token 使用两份同值文件：root 客户端 copy 为 root:root 0600，服务端 copy 为 `drama-youtube:drama-youtube` 0600；两者都非 symlink。无 token、慢 body、redirect、额外字段、schema/grant/index 漂移全部 fail closed。
