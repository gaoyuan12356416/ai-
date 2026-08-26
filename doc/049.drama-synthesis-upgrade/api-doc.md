# API 合同

全部 API 复用登录、模块权限、CSRF 和 operator audit；错误不返回 token、session URI 或内部 SQL。

## 任务合同

既有创建 API 四项输出默认 false，零输出 400。随机模板只接受 `advanced_options.random_template={mode,source,layers}`。成功结果为 `output_random_template_url`、`random_template_recipe`；旧 cover/naming 字段 accept-ignore-default。

## 六个 API

- `GET /api/drama-material/random-template-catalog`：manifest/profile 和四层资产，无 light；未配置/漂移 503。
- `POST /api/drama-material/jobs/{job_id}/short-links`：请求 `material_kind=concat_video|no_bgm_video|random_template`；只允许 done 且 URL 存在，返回 frozen link。
- `GET /api/drama-material/youtube/channels?app_id=<decimal>`：仅 status=1、refreshable、upload+identity-read、非内部测试频道；非法 ID 关闭。
- `POST /api/drama-material/jobs/{job_id}/youtube-publishes`：请求 operation_id、app_id、material_kind、channel_local_id、youtube_account_id、title、description_template、comment_text、duplicate_confirmed；返回 202。
- `GET /api/drama-material/youtube-publishes/{publish_id}`：仅返回安全状态、公开 external ID、时间和操作错误。
- `POST /api/drama-material/youtube-publishes/{publish_id}/retry-comment`：仅 published 且 known-safe comment failure；不重传视频。

标题 required/<=100 chars；描述 required/模板和渲染值 UTF-8 <=5000 bytes；评论可选。`{{url}}` 仅 description replace-all，先确保同 material 短链。冻结 app/job/material URL/channel/title/template/rendered/comment/operator/privacy public。同 operation+同 payload 幂等，不同 payload 409。

主状态 queued/validating/downloading/uploading/submitted/processing/published/failed/unknown。processing/unknown 同 job+material+channel 新请求 409；prior published 未二次确认 409。依赖 503，输入 400，权限 401/403，不可变/并发冲突 409。
