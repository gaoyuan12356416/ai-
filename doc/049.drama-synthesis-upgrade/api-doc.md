# API 合同

## 最新覆盖范围：现有账号 v3（2026-08-27 16:35）

按用户新决定与 [现行合同](ads-ai-new-tables-20260827.md)，不再创建专用数据库账号。CPU 使用现有 ads_aius 与已有频道授权，应用 SQL 仅限 ads_ai 新三表；原 MySQL 表只读。健康合同为 drama-youtube-writer-preflight-v3，shared-existing-account / application-table-allowlist / db_least_privilege=false；仅核验必要能力，不宣称全量 grant 审计。每次写前验证 TRIGGER 可见性和无 trigger/FK，旧健康合同拒绝。既有 DDL/v2 payload 与 UI 合同不变；下文专用账号/旧 v2 health 是历史。本轮专项 108/108，独立唯一完整回归及实机发布验收另记，不叠加历史批次。

2026-08-27 最新增量不改变 HTTP/前端合同：仅将结果 ledger 改为 ads_ai 专用新表，原库继续只读频道/OAuth。受控 RPC health 升为 v2/ads_ai，旧版拒绝；见 [新表合同](ads-ai-new-tables-20260827.md)。

全部 API 复用登录、模块权限、CSRF 和 operator audit；错误不返回 token、session URI 或内部 SQL。

## 任务合同

创建 API 的 outputs 精确使用 `random_template_video`，与其他三项均默认 false，零输出 400；历史错误 `random_template` 仅可被服务端归一输入，存储/响应不得继续使用它作为新合同。随机模板参数只接受 `advanced_options.random_template={mode,source,layers}`。成功结果为 `output_random_template_url`、`random_template_recipe`；旧 cover/naming 字段 accept-ignore-default。

## 六个 API

- `GET /api/drama-material/random-template-catalog`：由 CPU 本地只读原始 manifest 返回 manifest/profile 和四层资产，无 light；不访问 GPU、数据库或媒体文件。`DRAMA_RANDOM_OVERLAY_MANIFEST_FILE` 必须是绝对路径 regular 非 symlink 文件，并匹配固定 SHA；未配置/漂移/格式无效 503，错误不含内部路径，不回退 GPU。
- `POST /api/drama-material/jobs/{job_id}/short-links`：请求 `material_kind=concat_video|no_bgm_video|random_template`；只允许 done 且 URL 存在，返回 frozen link。
- `GET /api/drama-material/youtube/channels?app_id=<decimal>`：仅 status=1、refreshable、upload+identity-read、非内部测试频道；每个候选必须实际 refresh 并以 `channels.list(mine=true)` 验证冻结 channel ID，失败/空/多/错身份隐藏且不执行外部 mutation；非法 ID 关闭。
- `POST /api/drama-material/jobs/{job_id}/youtube-publishes`：请求 operation_id、app_id、material_kind、channel_local_id、youtube_account_id、title、description_template、comment_text、duplicate_confirmed；返回 202。
- `GET /api/drama-material/youtube-publishes/{publish_id}`：仅返回安全状态、公开 external ID、时间和操作错误。
- `POST /api/drama-material/youtube-publishes/{publish_id}/retry-comment`：仅 published 且 known-safe comment failure；不重传视频。

标题 required/<=100 chars；描述 required/模板和渲染值 UTF-8 <=5000 bytes；评论可选。`{{url}}` 仅 description replace-all，先确保同 material 短链。冻结 app/job/material URL/channel/title/template/rendered/comment/operator/privacy public。同 operation+同 payload 幂等，不同 payload 409。

主状态 queued/validating/downloading/uploading/submitted/processing/published/failed/unknown。processing/unknown 同 job+material+channel 新请求 409；prior published 未二次确认 409。依赖 503，输入 400，权限 401/403，不可变/并发冲突 409。

统一同步 RPC 不接受通用 JSON。video/publish_log 精确字段为 `publish_id,video_id,app_id,channel_local_id,operator_user_id,job_id,content_id,source_kind,source_url,title,description_rendered,privacy_status,published_at_utc`；comment 精确字段为 `publish_id,video_id,comment_id,channel_local_id,operator_user_id,comment_text,published_at_utc`。video/comment external ID 分别等于对应 ID；publish_log external ID 是无前导零的十进制 `publish_id` 字符串。任何 extra/missing/错类型/identity mismatch 均 409 并由 outbox 安全记 failed。RPC 内部只映射固定 legacy 列，调用方不能传 SQL、列名或数据库凭据。

内部 RPC 固定为 `127.0.0.1:18837`（18836 为既有 FB 隧道）：Bearer `GET /health` 只返回 `ok/schema/grant_fingerprint`；Bearer `POST /v1/youtube-sync` 只接收 exact `action,table,external_id,payload` envelope。同一至少 32 字符 token 使用两份同值文件：root 客户端 copy 为 root:root 0600，服务端 copy 为 `drama-youtube:drama-youtube` 0600；两者都非 symlink。无 token、慢 body、redirect、额外字段、schema/grant/index 漂移全部 fail closed。

## CPU 与制作端边界

所有业务查询、状态存取、短链和 YouTube 操作都在 CPU。CPU 先解析剧集/分集地址和封面等制作参数，并冻结随机配方，再向 HK 下发完整媒体请求；HK 不凭 content/job ID 反查业务数据。HK 只下载请求中的素材、制作、上传 COS、校验制作缓存并返回产物；CPU 校验回传配方并持久化 COS 地址。服务间 Bearer 仅用于制作接口鉴权，不传业务数据库或 OAuth 凭据。接口职责详见 [边界记录](cpu-gpu-boundary-20260827.md)。
